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
import re
import stat
import sys
import time
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


def file_extension(path: str) -> str:
    """Lower-case extension; a dot-name with no other extension (.gitignore, .env) is its own
    extension, which os.path.splitext would report as empty."""
    name = os.path.basename(str(path))
    ext = os.path.splitext(name)[1]
    if not ext and name.startswith("."):
        return name.lower()
    return ext.lower()


_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}\b")),  # before openai_key: sk-ant- also matches the broader sk- shape
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("google_oauth_client_secret", re.compile(r"\"client_secret\"\s*:\s*\"[A-Za-z0-9_-]{20,}\"")),
    ("service_account_key", re.compile(r"\"private_key\"\s*:\s*\"-----BEGIN")),
    ("generic_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)\b\s*[:=]\s*[\"']?[A-Za-z0-9+/_\-]{24,}[\"']?")),
]


# anchored at the START of a token run (lookbehind): without it, finditer retries at every
# position inside a long run, which is O(n^2) again
_DOTTED_RUN = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]{20,}+(?:\.[A-Za-z0-9_-]{20,}+){2,}+")


def _has_jwt(text: str) -> bool:
    """A JWT: three or more dot-separated base64url segments, the first two starting with
    "eyJ" (a JSON object). Found by one linear pass over maximal dotted runs: a regex anchored
    on "eyJ" restarts at every occurrence inside a run, which is O(n^2) on "eyJ-eyJ-..."
    (34 s for 400 KB, measured)."""
    for m in _DOTTED_RUN.finditer(text):
        segs = m.group().split(".")
        for i in range(len(segs) - 2):
            a, b, c = segs[i], segs[i + 1], segs[i + 2]
            k = a.rfind("eyJ")
            if k >= 0 and len(a) - k >= 23 and b.startswith("eyJ") and len(b) >= 23 and len(c) >= 20:
                return True
    return False


def suspected_secret(text: str, max_scan_chars: int = 400_000) -> str | None:
    """Name of the first credential pattern found in text, or None. Deliberately conservative:
    the generic rule needs an assignment to a 24+ character opaque literal, so prose and
    ordinary code do not trip it. Every rule is linear in the input (tested on adversarial
    payloads): this runs in the service process, outside the extractor's timeout."""
    sample = text[:max_scan_chars]
    for name, pat in _SECRET_PATTERNS:
        if name == "generic_assignment" and _has_jwt(sample):
            return "jwt"
        if pat.search(sample):
            return name
    return "jwt" if _has_jwt(sample) else None


def compile_excludes(patterns: list[str]):
    """The same decision as is_excluded(), compiled to two regular expressions so a sweep over
    hundreds of thousands of paths costs one match each instead of len(patterns) fnmatch calls.
    Returns matcher(path, is_dir=False) -> bool."""
    full: list[str] = []
    bare: list[str] = []
    for pat in patterns:
        p = pat.lower()
        if "/" not in p:
            bare.append(fnmatch.translate(p))
            continue
        full.append(fnmatch.translate(p))
        if p.endswith("/**"):
            full.append(fnmatch.translate(p[:-3]))
    full_re = re.compile("|".join(f"(?:{x})" for x in full)) if full else None
    bare_re = re.compile("|".join(f"(?:{x})" for x in bare)) if bare else None

    def match(path: str, is_dir: bool = False) -> bool:
        low = to_glob_form(path).lower()
        if full_re is not None and full_re.match(low):
            return True
        if not is_dir and bare_re is not None and bare_re.match(low.rsplit("/", 1)[-1]):
            return True
        return False

    return match


def is_excluded(path: str, patterns: list[str], is_dir: bool = False) -> bool:
    """Exclusion globs. A pattern containing '/' is matched against the full path (forward
    slashes); a bare pattern such as '*.pem' or '*secret*' is matched against the FILE name
    only, never against directory names, so a folder called 'credential-docs' is not skipped
    while 'client_secret.json' inside it is."""
    g = to_glob_form(path)
    low = g.lower()
    base = low.rsplit("/", 1)[-1]
    for pat in patterns:
        p = pat.lower()
        if "/" not in p:
            if not is_dir and fnmatch.fnmatchcase(base, p):
                return True
            continue
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


_reparse_dir_cache: dict[str, tuple[bool, float]] = {}
# No caching: a path-keyed cache with any lifetime lets a directory that was checked once be
# swapped for a junction and trusted until the entry expires (reproduced by probe). An lstat per
# ancestor is cheap next to hashing and extracting the file.
REPARSE_CACHE_TTL_S = 0.0


def reset_reparse_cache() -> None:
    _reparse_dir_cache.clear()


def is_reparse_dir(path: str) -> bool:
    """Cached (time-bounded) lstat: is this directory a reparse point?"""
    now = time.time()
    ent = _reparse_dir_cache.get(path)
    if ent is not None and REPARSE_CACHE_TTL_S > 0 and now - ent[1] < REPARSE_CACHE_TTL_S:
        return ent[0]
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        hit = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        hit = False
    if len(_reparse_dir_cache) > 50000:
        _reparse_dir_cache.clear()
    _reparse_dir_cache[path] = (hit, now)
    return hit


def has_reparse_ancestor(path: str, roots: list[str]) -> bool:
    """True if the containing root or any directory between it and the file is a reparse
    point (junction, symlinked directory, mount point). The root itself counts: a root that
    is a junction would otherwise let everything under its target in under the root's name."""
    root = root_for(path, roots)
    if root is None:
        return True
    n = normalize_path(path)
    rel = n[len(root):].strip("\\")
    parts = rel.split("\\")[:-1] if rel else []
    if is_reparse_dir(root):
        return True
    cur = root
    for part in parts:
        cur = cur + "\\" + part
        if is_reparse_dir(cur):
            return True
    return False


def reparse_roots(roots: list[str]) -> list[str]:
    """Configured roots that are themselves reparse points (to warn about at startup)."""
    out = []
    for r in roots:
        try:
            attrs = getattr(os.lstat(r), "st_file_attributes", 0)
        except OSError:
            continue
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            out.append(r)
    return out


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
    if follow_reparse:
        # following is allowed, but only to targets that are themselves inside a root: this
        # covers a symlinked file and a junction anywhere in the ancestor chain alike
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
    if not follow_reparse and is_reparse_dir(normalize_path(root)):
        return  # a junction root is not walked: its contents live outside the configured boundary
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
                is_dir = entry.is_dir(follow_symlinks=follow_reparse)
                if is_excluded(p, excludes, is_dir=is_dir):
                    continue
                if is_dir:
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


def resolves_inside(path: str, roots: list[str], follow_reparse: bool = False) -> bool:
    """Containment of the path as the filesystem resolves it NOW (junctions and symlinks
    anywhere in the chain followed): the resolved path must lie inside a configured root, and
    unless reparse points may be followed, it must be the same path as the one given."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    if real.startswith("\\\\?\\"):
        real = real[4:]
    if not is_within(real, roots):
        return False
    if not follow_reparse and normalize_path(real) != normalize_path(path):
        return False
    return True


FILE_READ_DATA = 0x0001


def _process_sid():
    import win32api
    import win32security
    tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    return win32security.GetTokenInformation(tok, win32security.TokenUser)[0]


def explicit_grant_problem(path: str) -> str | None:
    """None when <path> is an existing local directory whose DACL carries an EXPLICIT (not
    inherited) allow entry granting this process's own account read access; otherwise the
    reason. UNC and device paths are refused: roots are local folders."""
    p = display_path(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return f"network and device paths cannot be roots: {p}"
    if not os.path.isabs(p) or not os.path.isdir(p):
        return f"not an existing local directory: {p}"
    if sys.platform != "win32":
        return None
    import win32security
    try:
        sd = win32security.GetFileSecurity(p, win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        me = _process_sid()
    except Exception as e:  # noqa: BLE001
        return f"cannot read the permissions of {p}: {e}"
    if dacl is None:
        return f"{p} has no DACL (open to everyone); grant read explicitly before indexing it"
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        (ace_type, ace_flags), mask, sid = ace[0], ace[1], ace[2]
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE or ace_flags & win32security.INHERITED_ACE:
            continue
        if sid == me and mask & FILE_READ_DATA:
            return None
    return (f"{p} has no explicit read grant for this service's account; add the folder with `semsearch roots add` "
            f"or the tray (they grant it as the folder's owner)")
