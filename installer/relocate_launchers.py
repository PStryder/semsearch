"""Regenerate the console launchers (Scripts\\semsearch.exe etc.) for the interpreter that runs
this script, and copy the main one next to the runtime.

Why: pip writes a launcher as <launcher stub> + "#!<absolute python path>" + <zip of __main__>.
The stub reads that shebang at run time, so a launcher built in dist\\SemSearch-<ver>\\... keeps
pointing at the build tree after the runtime is copied to %ProgramFiles%. The installer runs
this with the INSTALLED python so the shebang is the installed path. No pip needed: the stub
is taken from the existing launcher (everything before its "#!" line).

    python -s relocate_launchers.py [--copy-to <dir>]
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile

ENTRY_POINTS = {
    "semsearch": ("semsearch.cli", "main"),
    "semsearch-serve": ("semsearch.api", "serve"),
}

MAIN_TEMPLATE = """# -*- coding: utf-8 -*-
import re
import sys
from {module} import {func}
if __name__ == "__main__":
    sys.argv[0] = re.sub(r"(-script\\.pyw|\\.exe)?$", "", sys.argv[0])
    sys.exit({func}())
"""


def split_launcher(data: bytes) -> bytes:
    """The PE stub: everything before the shebang that precedes the trailing zip archive."""
    i = data.rfind(b"#!")
    if i < 0 or b"PK\x03\x04" not in data[i:]:
        raise SystemExit("not a pip console launcher (no '#!' + zip trailer)")
    return data[:i]


def build_launcher(stub: bytes, python: str, module: str, func: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", MAIN_TEMPLATE.format(module=module, func=func))
    shebang = ("#!" + python + "\n").encode("utf-8")
    return stub + shebang + buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy-to", help="directory that receives a copy of semsearch.exe")
    ap.add_argument("--check", action="store_true", help="only verify the shebangs point at this interpreter")
    a = ap.parse_args()
    python = os.path.abspath(sys.executable)
    here = os.path.dirname(python)
    # standalone runtime: python.exe at the root with Scripts\ beside it; a venv: python.exe inside Scripts\
    scripts = here if os.path.basename(here).lower() == "scripts" else os.path.join(here, "Scripts")
    src = os.path.join(scripts, "semsearch.exe")
    if not os.path.isfile(src):
        print(f"no launcher at {src}", file=sys.stderr)
        return 1
    with open(src, "rb") as f:
        stub = split_launcher(f.read())
    bad = 0
    for name, (module, func) in ENTRY_POINTS.items():
        dest = os.path.join(scripts, name + ".exe")
        data = build_launcher(stub, python, module, func)
        if a.check:
            with open(dest, "rb") as f:
                cur = f.read()
            if ("#!" + python + "\n").encode("utf-8") not in cur:
                print(f"{dest}: shebang does not point at {python}", file=sys.stderr)
                bad += 1
            continue
        with open(dest, "wb") as f:
            f.write(data)
        print(f"launcher {dest} -> {python}")
    if a.copy_to and not a.check:
        out = os.path.join(a.copy_to, "semsearch.exe")
        with open(os.path.join(scripts, "semsearch.exe"), "rb") as f, open(out, "wb") as g:
            g.write(f.read())
        print(f"copied to {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
