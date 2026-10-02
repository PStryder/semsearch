"""Path containment and filesystem safety.

Rules enforced here:
  * every path the sidecar touches must resolve inside a configured root
  * reparse points (symlinks, junctions, mount points) are not followed unless configured
  * hard-linked files are reported (st_nlink > 1) because resolve() cannot see them
  * device paths, UNC and long-path prefixes are normalized or rejected
"""
from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_SYSTEM = 0x0004
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


class PathRejected(Exception):
    pass


def normalize_path(p: str) -> str:
    """Canonical comparison form: absolute, backslashes, lower-case, no trailing slash, no \\?\\ prefix."""
    s = str(p)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    if s.startswith("file:"):
        s = s[5:]
        s = s.lstrip("/")
        s = s.replace("/", "\\")
    s = os.path.abspath(s)
    s = os.path.normcase(s)
    if len(s) > 3 and s.endswith("\\"):
        s = s.rstrip("\\")
    return s


def display_path(p: str) -> str:
    s = str(p)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.abspath(s)


def true_case_path(p: str) -> str:
    """The path as it is spelled on disk. Jobs are keyed by the case-folded form, so this is
    what users should see in results. Falls back to display_path for missing files."""
    d = display_path(p)
    try:
        r = os.path.realpath(d)  # on Windows this resolves to the on-disk casing
    except OSError:
        return d
    if r.startswith("\\\\?\\"):
        r = r[4:]
    # only accept the resolved spelling when it is the same path (not a symlink target)
    return r if normalize_path(r) == normalize_path(d) else d


def is_within(path: str, roots: list[str]) -> bool:
    n = normalize_path(path)
    for r in roots:
        rn = normalize_path(r)
        if n == rn or n.startswith(rn.rstrip("\\") + "\\"):
            return True
    return False


def root_for(path: str, roots: list[str]) -> str | None:
    n = normalize_path(path)
    best = None
    for r in roots:
        rn = normalize_path(r)
        if n == rn or n.startswith(rn.rstrip("\\") + "\\"):
            if best is None or len(rn) > len(best):
                best = rn
    return best


def to_glob_form(path: str) -> str:
    return str(path).replace("\\", "/")


def is_excluded(path: str, patterns: list[str]) -> bool:
    g = to_glob_form(path)
    low = g.lower()
    for pat in patterns:
        p = pat.lower()
        if fnmatch.fnmatchcase(low, p):
            return True
        # "**/name/**" should also match when name is the last segment (a dir itself)
        if p.endswith("/**") and fnmatch.fnmatchcase(low, p[:-3]):
            return True
    return False


@dataclass(slots=True)
class StatInfo:
    size: int
    mtime: float
    ctime: float
    file_id: int
    volume_serial: int
    nlink: int
    is_reparse: bool
    is_dir: bool
    attributes: int


def lstat_info(path: str) -> StatInfo:
    st = os.lstat(path)
    attrs = getattr(st, "st_file_attributes", 0)
    return StatInfo(
        size=st.st_size,
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        file_id=st.st_ino,
        volume_serial=st.st_dev,
        nlink=st.st_nlink,
        is_reparse=bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT),
        is_dir=stat.S_ISDIR(st.st_mode),
        attributes=attrs,
    )


_reparse_dir_cache: dict[str, bool] = {}


def has_reparse_ancestor(path: str, roots: list[str]) -> bool:
    """True if any directory between the containing root and the file is a reparse point
    (junction, symlinked directory, mount point). Results are cached per directory."""
    root = root_for(path, roots)
    if root is None:
        return True
    n = normalize_path(path)
    rel = n[len(root):].strip("\\")
    parts = rel.split("\\")[:-1]
    cur = root
    for part in parts:
        cur = cur + "\\" + part
        hit = _reparse_dir_cache.get(cur)
        if hit is None:
            try:
                attrs = getattr(os.lstat(cur), "st_file_attributes", 0)
                hit = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
            except OSError:
                hit = False
            if len(_reparse_dir_cache) > 50000:
                _reparse_dir_cache.clear()
            _reparse_dir_cache[cur] = hit
        if hit:
            return True
    return False


def check_indexable(path: str, roots: list[str], follow_reparse: bool = False) -> StatInfo:
    """Raise PathRejected unless the path is a regular file safely inside a root."""
    if not is_within(path, roots):
        raise PathRejected(f"outside configured roots: {path}")
    try:
        info = lstat_info(path)
    except FileNotFoundError:
        raise
    except PermissionError as e:
        raise PathRejected(f"permission denied: {e}")
    if info.is_reparse and not follow_reparse:
        raise PathRejected("reparse point (symlink/junction) not followed")
    if not follow_reparse and has_reparse_ancestor(path, roots):
        raise PathRejected("path passes through a reparse point (junction/symlinked directory)")
    if info.is_reparse and follow_reparse:
        real = os.path.realpath(path)
        if not is_within(real, roots):
            raise PathRejected(f"reparse target outside roots: {real}")
    if info.is_dir:
        raise PathRejected("is a directory")
    if info.attributes & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
        raise PathRejected("offline / cloud placeholder file; not recalled")
    return info


def walk_safe(root: str, roots: list[str], excludes: list[str], follow_reparse: bool = False):
    """os.scandir-based walk that never descends into reparse points (unless allowed) and
    never leaves the configured roots. Yields (path, os.DirEntry)."""
    stack = [root]
    seen_dirs: set[tuple[int, int]] = set()
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue
        with it:
            for entry in it:
                p = entry.path
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT and not follow_reparse:
                    continue
                if is_excluded(p, excludes):
                    continue
                if entry.is_dir(follow_symlinks=follow_reparse):
                    if follow_reparse:
                        real = os.path.realpath(p)
                        if not is_within(real, roots):
                            continue
                        key = (st.st_dev, st.st_ino)
                        if key in seen_dirs:
                            continue
                        seen_dirs.add(key)
                    stack.append(p)
                elif entry.is_file(follow_symlinks=follow_reparse):
                    yield p, entry
