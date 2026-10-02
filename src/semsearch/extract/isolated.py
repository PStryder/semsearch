"""Run document-format extractors in a long-lived child process with a per-file timeout.

Why: IFilters and PDF parsers are the components most likely to hang or crash on a corrupt
file. Keeping them out of the indexer process means a bad file costs one child restart,
not the service. Plain-text extraction stays in-process (fast, no native code).
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time

from ..config import Config
from ..models import ExtractResult

log = logging.getLogger(__name__)


def _child_main(conn, cfg_json: str) -> None:  # pragma: no cover - runs in child
    import logging as _l
    _l.basicConfig(level=_l.WARNING)
    from ..config import Config as _C
    from .registry import build_default_registry
    cfg = _C.model_validate_json(cfg_json)
    reg = build_default_registry(cfg)
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        path, ext = msg
        try:
            r = reg.extract(path, ext)
            conn.send((r.text, r.status, r.method, r.error, r.meta))
        except BaseException as e:  # noqa: BLE001
            try:
                conn.send(("", "error", "isolated", f"{type(e).__name__}: {e}"[:500], {}))
            except Exception:
                return


class IsolatedExtractor:
    """Wraps a registry: text-like extensions run in-process; everything else in the child."""

    def __init__(self, cfg: Config, registry, timeout_s: float = 120.0):
        self.cfg = cfg
        self.registry = registry
        self.timeout_s = timeout_s
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._conn = None
        self._lock = threading.Lock()
        # Only chains made purely of pure-Python text extraction stay in-process. Anything that
        # can reach native code (IFilter, pypdf, Office parsers) goes to the child, including
        # text-like extensions whose chain starts with an IFilter (.html/.htm).
        from .text import TextExtractor
        self._inproc_exts = {ext for ext, chain in registry.chains.items() if chain and all(isinstance(e, TextExtractor) for e in chain)}
        self.restarts = 0

    def _start(self) -> None:
        parent, child = self._ctx.Pipe()
        p = self._ctx.Process(target=_child_main, args=(child, self.cfg.model_dump_json(exclude={"source_path"})), daemon=True, name="semsearch-extract")
        p.start()
        child.close()
        self._proc, self._conn = p, parent

    def _kill(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        try:
            if self._proc is not None and self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(5)
        except Exception:
            pass
        self._proc, self._conn = None, None

    def kill_now(self) -> None:
        """Terminate the child WITHOUT taking the lock: called from another thread while
        extract() may be blocked waiting on the child. The waiting extract() sees the broken
        pipe, returns an error result, and the next call starts a fresh child."""
        p = self._proc
        try:
            if p is not None and p.is_alive():
                p.terminate()
                self.restarts += 1
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.send(None)
            except Exception:
                pass
            self._kill()

    def extract(self, path: str, extension: str) -> ExtractResult:
        ext = extension.lower()
        if ext in self._inproc_exts:
            return self.registry.extract(path, ext)
        with self._lock:
            if self._proc is None or not self._proc.is_alive():
                if self._proc is not None:
                    self.restarts += 1
                self._start()
            try:
                self._conn.send((path, ext))
                if not self._conn.poll(self.timeout_s):
                    self._kill()
                    self.restarts += 1
                    return ExtractResult("", "error", "isolated", error=f"extraction timed out after {self.timeout_s:.0f}s; extractor process restarted")
                text, status, method, error, meta = self._conn.recv()
                return ExtractResult(text, status, method, error, meta or {})
            except (EOFError, OSError, BrokenPipeError) as e:
                self._kill()
                self.restarts += 1
                return ExtractResult("", "error", "isolated", error=f"extractor process died: {e}")
