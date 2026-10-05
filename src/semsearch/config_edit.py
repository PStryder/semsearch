"""Edit list-valued top-level keys of semsearch.yaml in place, textually, so the comments and
layout the installer wrote survive (PyYAML round-trips lose comments). Only `roots:` and
`excludes:` are edited this way; everything else stays byte-identical.
"""
from __future__ import annotations

import os
import re
import tempfile

_TOP_KEY = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)$")


def _yaml_str(s: str) -> str:
    # double-quoted YAML scalar; backslashes and quotes escaped, forward slashes for paths
    return '"' + s.replace("\\", "/").replace('"', '\\"') + '"'


def render_block(key: str, values: list[str], comment: str | None = None, nl: str = "\n") -> str:
    lines = [f"{key}:" + (f"   # {comment}" if comment else "")]
    if not values:
        lines.append("  []")  # an explicit empty list (for excludes this means "no exclusions"; for roots "nothing indexed")
    for v in values:
        lines.append(f"  - {_yaml_str(v)}")
    return nl.join(lines) + nl


def set_list_block(text: str, key: str, values: list[str], comment: str | None = None) -> str:
    """Replace the top-level `key:` block (the key line and every following line that is
    indented, blank or a comment up to the next top-level key) with a fresh list. If the key
    is absent, the block is appended at the end."""
    nl = "\r\n" if "\r\n" in text else "\n"  # keep the file's line endings (PowerShell writes CRLF)
    lines = text.splitlines(keepends=True)
    start = end = None
    for i, ln in enumerate(lines):
        m = _TOP_KEY.match(ln.rstrip("\r\n"))
        if start is None:
            if m and m.group("key") == key:
                start = i
            continue
        # inside the block: ends at the next top-level key
        if m:
            end = i
            break
    if start is None:
        body = "".join(lines)
        if body and not body.endswith("\n"):
            body += nl
        return body + render_block(key, values, comment, nl)
    if end is None:
        end = len(lines)
    # keep trailing blank lines and column-0 comments (they introduce the NEXT block) out of the replaced span
    while end - 1 > start:
        tail = lines[end - 1]
        if tail.strip() == "" or (tail.startswith("#")):
            end -= 1
        else:
            break
    new = render_block(key, values, comment, nl)
    return "".join(lines[:start]) + new + "".join(lines[end:])


def list_block_values(text: str, key: str) -> list[str] | None:
    """Current values of a top-level list key (None if the key is absent). Minimal parser:
    `- "value"` / `- value` items; comments stripped."""
    import yaml
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    v = data.get(key) if isinstance(data, dict) else None
    return [str(x) for x in v] if isinstance(v, list) else None


def _copy_dacl(src: str, dst: str) -> None:
    """Carry the existing file's explicit ACL (e.g. the operator's modify grant the installer
    placed on semsearch.yaml) over to the replacement file."""
    if not os.path.exists(src) or os.name != "nt":
        return
    try:
        import win32security
        sd = win32security.GetFileSecurity(src, win32security.DACL_SECURITY_INFORMATION)
        win32security.SetFileSecurity(dst, win32security.DACL_SECURITY_INFORMATION, sd)
    except Exception:  # noqa: BLE001 - best effort; the inherited ACL is the safe fallback
        pass


def write_atomic(path: str | os.PathLike, text: str) -> None:
    path = str(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".semsearch-", suffix=".yaml", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        _copy_dacl(path, tmp)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                import time
                time.sleep(0.05)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_config_lists(path: str | os.PathLike, roots: list[str] | None = None, excludes: list[str] | None = None) -> str:
    """Rewrite roots/excludes in the YAML file (whichever are given), atomically. Returns the new
    text. Refuses values with control characters, and refuses to write unless the new text parses
    back to exactly the requested lists with every other top-level key unchanged."""
    import yaml
    for v in (roots or []) + (excludes or []):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError(f"control character in configuration value: {v!r}")
    with open(path, "r", encoding="utf-8-sig", newline="") as f:  # -sig: BOM; newline="": keep CRLF as written
        text = f.read()
    original = text
    if roots is not None:
        text = set_list_block(text, "roots", roots, "directories to index (edited by the SemSearch tray / API)")
    if excludes is not None:
        text = set_list_block(text, "excludes", excludes, "setting this REPLACES the default exclusion list")
    before = yaml.safe_load(original) or {}
    after = yaml.safe_load(text) or {}
    want = dict(before)
    if roots is not None:
        want["roots"] = [r.replace("\\", "/") for r in roots]
    if excludes is not None:
        want["excludes"] = [e.replace("\\", "/") for e in excludes]
    if after != want:
        raise ValueError("refusing to write the configuration: the edited file would not parse back to the requested values")
    write_atomic(path, text)
    return text
