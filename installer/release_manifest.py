"""Write release-manifest.json (SHA-256 of every file in the stage) or verify a tree against
one. Lets the installer prove the runtime it copied into %ProgramFiles% is byte-identical to
what was built, and lets anyone verify a downloaded release.

    python -s release_manifest.py write  <stage_dir>
    python -s release_manifest.py verify <dir> <manifest.json> [--subdir python]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

SKIP = {"release-manifest.json"}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def walk(base: str):
    for dp, dn, fn in os.walk(base):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f in SKIP or f.endswith(".pyc"):
                continue
            p = os.path.join(dp, f)
            yield os.path.relpath(p, base).replace("\\", "/"), p


def write(stage: str) -> int:
    files = {rel: {"sha256": sha256(p), "bytes": os.path.getsize(p)} for rel, p in walk(stage)}
    ver = open(os.path.join(stage, "VERSION"), encoding="ascii").read().strip() if os.path.isfile(os.path.join(stage, "VERSION")) else None
    with open(os.path.join(stage, "release-manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"version": ver, "files": files}, f, indent=0)
    print(f"release manifest: {len(files)} files")
    return 0


def verify(target: str, manifest: str, subdir: str | None) -> int:
    m = json.load(open(manifest, encoding="utf-8"))["files"]
    want = {k: v for k, v in m.items() if not subdir or k.startswith(subdir.rstrip("/") + "/")}
    bad = []
    seen = 0
    for rel, p in walk(target):
        key = (subdir.rstrip("/") + "/" + rel) if subdir else rel
        if key not in want:
            continue
        seen += 1
        if os.path.getsize(p) != want[key]["bytes"] or sha256(p) != want[key]["sha256"]:
            bad.append(key)
    missing = len(want) - seen
    if bad or missing:
        print(f"verification FAILED: {len(bad)} changed, {missing} missing (first: {bad[:5]})", file=sys.stderr)
        return 1
    print(f"verified {seen} files against the release manifest")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "write":
        sys.exit(write(sys.argv[2]))
    if len(sys.argv) >= 4 and sys.argv[1] == "verify":
        sub = sys.argv[sys.argv.index("--subdir") + 1] if "--subdir" in sys.argv else None
        sys.exit(verify(sys.argv[2], sys.argv[3], sub))
    print(__doc__)
    sys.exit(2)
