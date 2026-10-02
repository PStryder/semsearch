from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


def setup_logging(log_dir: Path | None, level: str = "INFO", to_stderr: bool = True) -> None:
    root = logging.getLogger()
    if getattr(root, "_semsearch_configured", False):
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    if to_stderr:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
    if log_dir is not None:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "semsearch.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._semsearch_configured = True  # type: ignore[attr-defined]
