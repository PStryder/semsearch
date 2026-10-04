"""The Windows Search crawl scope, read-only: which folders Windows indexes for CONTENT and
which patterns it excludes. Used to seed semsearch's roots/excludes with what the user has
already curated in "Indexing Options".

Source: the crawl scope manager's registry mirror (the same rules ISearchCrawlScopeManager
enumerates; readable by every account):
  HKLM\\SOFTWARE\\Microsoft\\Windows Search\\CrawlScopeManager\\Windows\\SystemIndex\\DefaultRules
  HKLM\\SOFTWARE\\Microsoft\\Windows Search\\CrawlScopeManager\\Windows\\SystemIndex\\WorkingSetRules
Rule URLs look like  file:///C:\\[<volume guid>]\\Users\\*\\AppData\\  and carry flags:
  Include   1 = include, 0 = exclude
  NoContent 1 = properties only (file names/metadata), 0 = full content
  Default   1 = Windows' built-in rule, 0 = added by the user (or an app)

Whole volumes are typically included "NoContent" (filename search works everywhere) while a
handful of folders (Documents, Desktop, Downloads, OneDrive, Users\\) are content-indexed. The
content rules are what "search my files by meaning" should default to.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

SCOPE_KEY = r"SOFTWARE\Microsoft\Windows Search\CrawlScopeManager\Windows\SystemIndex"
_URL_RE = re.compile(r"^file:///(?P<drive>[A-Za-z]):\\\[(?P<guid>[0-9a-fA-F-]{36})\]\\(?P<rest>.*)$")


@dataclass(slots=True)
class ScopeRule:
    url: str                       # raw rule
    include: bool
    content: bool                  # False = properties only
    default: bool                  # Windows' own rule, not user-added
    drive: str | None = None       # letter in the rule (may be stale: volumes are identified by guid)
    volume_guid: str | None = None
    rest: str = ""                 # path below the volume, backslashes, trailing backslash stripped
    mounted_drive: str | None = None   # letter the volume has now, or None if not mounted

    @property
    def is_file_rule(self) -> bool:
        return self.volume_guid is not None

    @property
    def has_wildcard(self) -> bool:
        return "*" in self.rest or "?" in self.rest

    def path(self) -> str | None:
        """Windows path under the CURRENT drive letter of the volume (None when unmounted)."""
        d = self.mounted_drive
        if not d:
            return None
        return f"{d}:\\{self.rest}" if self.rest else f"{d}:\\"

    def exclude_glob(self) -> str | None:
        """semsearch exclusion pattern (full-path glob, forward slashes, lower case)."""
        p = self.path()
        if p is None:
            return None
        g = p.replace("\\", "/").rstrip("/").lower()
        return g + "/**"


def parse_rule(url: str, values: dict) -> ScopeRule:
    r = ScopeRule(url=url, include=bool(values.get("Include", 0)), content=not bool(values.get("NoContent", 0)),
                  default=bool(values.get("Default", 0)))
    m = _URL_RE.match(url)
    if m:
        r.drive = m.group("drive").upper()
        r.volume_guid = m.group("guid").lower()
        r.rest = m.group("rest").rstrip("\\")
    return r


def _read_registry_rules(sub: str) -> list[tuple[str, dict]]:
    import winreg
    out: list[tuple[str, dict]] = []
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SCOPE_KEY + "\\" + sub)
    except OSError:
        return out
    with k:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            vals: dict = {}
            with winreg.OpenKey(k, name) as sk:
                j = 0
                while True:
                    try:
                        vn, vv, _ = winreg.EnumValue(sk, j)
                    except OSError:
                        break
                    j += 1
                    vals[vn] = vv
            url = vals.get("URL")
            if isinstance(url, str):
                out.append((url, vals))
    return out


def mounted_drive_letters() -> set[str]:
    if sys.platform != "win32":
        return set()
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return {chr(ord("A") + i) for i in range(26) if mask & (1 << i)}


def resolve_rules(raw: list[tuple[str, dict]], mounted: set[str]) -> list[ScopeRule]:
    """Parse rules and decide which drive each applies to now.

    The bracketed id in a rule URL is Windows Search's own volume identity; it matches
    neither the volume GUID, the NTFS serial nor the object id (measured 2026-10-04), so it
    cannot be mapped to a current drive letter from outside the indexer. The drive letter in
    the rule is used instead, with two guards: the letter must be mounted now, and when
    several different volume ids share one letter (a removable-drive slot such as D:) the
    letter is ambiguous and only rules whose path exists are accepted.
    """
    rules = [parse_rule(url, vals) for url, vals in raw]
    ids_per_letter: dict[str, set[str]] = {}
    for r in rules:
        if r.volume_guid and r.drive:
            ids_per_letter.setdefault(r.drive, set()).add(r.volume_guid)
    for r in rules:
        if not r.volume_guid or not r.drive or r.drive not in mounted:
            continue
        ambiguous = len(ids_per_letter.get(r.drive, ())) > 1
        if ambiguous and r.rest and (r.has_wildcard or not os.path.exists(f"{r.drive}:\\{r.rest}")):
            continue  # a volume-root rule applies to whatever is mounted there; a deeper one must exist
        r.mounted_drive = r.drive
    return rules


def windows_scope_rules() -> list[ScopeRule]:
    """All rules (working set first, then defaults), with the applicable drive resolved."""
    if sys.platform != "win32":
        return []
    raw: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for sub in ("WorkingSetRules", "DefaultRules"):
        for url, vals in _read_registry_rules(sub):
            if url not in seen:
                seen.add(url)
                raw.append((url, vals))
    return resolve_rules(raw, mounted_drive_letters())


@dataclass(slots=True)
class ScopeSuggestion:
    roots: list[str] = field(default_factory=list)        # content-indexed folders, this user's profile only
    excludes: list[str] = field(default_factory=list)     # Windows' exclusion rules as semsearch globs
    skipped: list[str] = field(default_factory=list)      # rules not translated, with the reason
    unmounted: list[str] = field(default_factory=list)    # rules for volumes not present right now


def suggest(rules: list[ScopeRule], profile_dir: str | None = None, users_dir: str | None = None) -> ScopeSuggestion:
    """Turn the rule list into semsearch roots and excludes.

    Roots: include rules that are CONTENT-indexed file rules without wildcards. A rule for the
    whole Users\\ directory (Windows' default) is narrowed to this user's profile: the service
    account is granted read on exactly the chosen roots, and other accounts' profiles are not
    this user's to index. Property-only rules (whole volumes) are not roots: that is file-name
    search, which the Windows index already provides.
    Excludes: every exclusion rule on a mounted volume, as a full-path glob; the wildcard
    syntax is the same (* and ?).
    """
    s = ScopeSuggestion()
    profile = os.path.normpath(profile_dir or os.environ.get("USERPROFILE", "")) if (profile_dir or os.environ.get("USERPROFILE")) else ""
    users = os.path.normpath(users_dir or (os.path.dirname(profile) if profile else "")) if (users_dir or profile) else ""
    # Windows' own content rules for system locations (Start Menu, Office sample content) are
    # not "my files"; anything under these never becomes a root
    system_dirs = [os.path.normcase(os.path.normpath(os.environ[k])) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "SystemRoot") if os.environ.get(k)]
    roots: list[str] = []
    for r in rules:
        if not r.is_file_rule:
            continue
        if r.mounted_drive is None:
            s.unmounted.append(r.url)
            continue
        p = r.path()
        if r.include:
            if not r.content:
                continue  # whole-volume, properties-only
            if r.has_wildcard:
                s.skipped.append(f"{r.url}: wildcard include rules are not supported as roots")
                continue
            pn = os.path.normcase(os.path.normpath(p))
            if any(pn == d or pn.startswith(d + "\\") for d in system_dirs):
                s.skipped.append(f"{r.url}: system location")
                continue
            if users and os.path.normcase(os.path.normpath(p)) == os.path.normcase(users):
                if profile and os.path.isdir(profile):
                    p = profile  # all profiles -> this user's
                else:
                    s.skipped.append(f"{r.url}: covers every user profile; add your own profile folder explicitly")
                    continue
            if not os.path.isdir(p):
                s.skipped.append(f"{r.url}: folder does not exist")
                continue
            roots.append(os.path.normpath(p))
        else:
            g = r.exclude_glob()
            if g:
                s.excludes.append(g)
    # drop roots nested inside another suggested root (the parent already covers them)
    norm = sorted({os.path.normcase(x): x for x in roots}.items())
    kept: list[str] = []
    for n, x in norm:
        if any(n.startswith(os.path.normcase(k).rstrip("\\") + "\\") for k in kept):
            continue
        kept.append(x)
    s.roots = kept
    s.excludes = sorted(set(s.excludes))
    return s


def windows_scope_suggestion(profile_dir: str | None = None) -> ScopeSuggestion:
    return suggest(windows_scope_rules(), profile_dir)
