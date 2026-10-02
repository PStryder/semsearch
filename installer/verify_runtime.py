"""Verify a packaged runtime: every native dependency imports and works. Run with the
venv's python; exits non-zero on any problem."""
import sqlite3
import sys

import comtypes  # noqa: F401
import docx  # noqa: F401
import fastapi  # noqa: F401
import numpy  # noqa: F401
import onnxruntime
import openpyxl  # noqa: F401
import pptx  # noqa: F401
import pypdf  # noqa: F401
import servicemanager  # noqa: F401
import sqlite_vec
import tokenizers  # noqa: F401
import uvicorn  # noqa: F401
import win32service  # noqa: F401

import semsearch
import semsearch.devices  # noqa: F401
import semsearch.service  # noqa: F401

c = sqlite3.connect(":memory:")
c.enable_load_extension(True)
sqlite_vec.load(c)
vec = c.execute("select vec_version()").fetchone()[0]
c.execute("create virtual table t using fts5(x)")
c.execute("create virtual table v using vec0(e float[4])")
providers = onnxruntime.get_available_providers()
if "CPUExecutionProvider" not in providers:
    print("onnxruntime has no CPU provider", file=sys.stderr)
    sys.exit(1)
adapters = semsearch.devices.enumerate_adapters()
print(f"runtime ok: semsearch {semsearch.__version__}, onnxruntime {onnxruntime.__version__} {providers}, "
      f"sqlite {sqlite3.sqlite_version}, sqlite-vec {vec}, fts5 ok, adapters {[a.name for a in adapters]}")
