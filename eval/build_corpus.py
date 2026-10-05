"""Copy the evaluation corpus described by eval/private/corpus_manifest.yaml into eval/corpus.

Read-only with respect to the sources (shutil.copy2, which preserves mtimes). Secrets are
never copied: files whose names look like credentials are skipped by name pattern.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import shutil
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
# Labelled queries, the corpus manifest and results live in eval/private/ (gitignored: the author's
# set names private documents). Without it, the *.example.yaml files here show the format.
DATA = os.environ.get("SEMSEARCH_EVAL_DATA") or (os.path.join(HERE, "private") if os.path.isdir(os.path.join(HERE, "private")) else HERE)


def data_file(name: str) -> str:
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        return p
    ex = os.path.join(HERE, name.replace(".yaml", ".example.yaml"))
    print(f"note: {p} not found; using the example {ex}", file=sys.stderr)
    return ex
SECRET_NAMES = ["*secret*", "*client_secret*", "*key*.txt", "*token*", "*.pem", "*.env", ".env*", "*credential*"]
EXCLUDE_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", "site-packages", "dist", "build", ".pytest_cache", ".mypy_cache"}


def looks_secret(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, p) for p in SECRET_NAMES)


def main() -> int:
    man = yaml.safe_load(open(data_file("corpus_manifest.yaml"), encoding="utf-8"))
    base = man["base"]
    cap = int(man.get("max_per_pattern", 150))
    if os.path.isdir(CORPUS):
        shutil.rmtree(CORPUS)
    os.makedirs(CORPUS)
    total = 0
    skipped = 0
    for pat in man["patterns"]:
        hits = sorted(glob.glob(os.path.join(base, pat["src"]), recursive=True))
        n = 0
        for src in hits:
            if not os.path.isfile(src):
                continue
            rel_parts = os.path.relpath(src, base).split(os.sep)
            if any(p in EXCLUDE_DIRS for p in rel_parts):
                continue
            if looks_secret(os.path.basename(src)):
                skipped += 1
                continue
            if os.path.getsize(src) > 20 * 1024 * 1024:
                continue
            # keep the path relative to the pattern's anchor so names stay meaningful
            anchor = os.path.join(base, pat["src"].split("*")[0].rstrip("/\\"))
            rel = os.path.relpath(src, anchor) if os.path.isdir(anchor) else os.path.basename(src)
            dest = os.path.join(CORPUS, pat["dest"], rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                continue
            shutil.copy2(src, dest)
            n += 1
            total += 1
            if n >= cap:
                break
        print(f"{pat['src']:<40} -> {pat['dest']:<24} {n:4d} files")
    print(f"\ncorpus: {total} files at {CORPUS} ({skipped} secret-looking files skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
