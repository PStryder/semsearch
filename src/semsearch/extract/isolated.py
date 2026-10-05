"""Run document-format extractors in a long-lived child process with a per-file timeout.

Why: IFilters and PDF parsers are the components most likely to hang or crash on a corrupt
file. Keeping them out of the indexer process means a bad file costs one child restart,
not the service. Plain-text extraction stays in-process (fast, no native code).
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import threading
import time

from ..config import Config
from ..models import ExtractResult

log = logging.getLogger(__name__)

# The child parses hostile documents, so it is treated as untrusted: its replies cross the pipe
# as JSON bytes (never pickle, which would let a compromised parser run code in the service),
# with a size cap, and are validated field by field before use.
_STATUSES = {"ok", "empty", "binary", "unsupported", "too_large", "error", "missing", "denied"}


def _encode(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8", "surrogatepass")


def _decode(b: bytes):
    return json.loads(b.decode("utf-8", "surrogatepass"))


def _valid_reply(obj) -> ExtractResult | None:
    if not (isinstance(obj, list) and len(obj) == 5):
        return None
    text, status, method, error, meta = obj
    if not isinstance(text, str) or status not in _STATUSES or not isinstance(method, str):
        return None
    if error is not None and not isinstance(error, str):
        return None
    if not isinstance(meta, dict):
        meta = {}
    return ExtractResult(text, status, method[:64], (error or None) and error[:1000], meta)


def _child_main(conn, cfg_json: str) -> None:  # pragma: no cover - runs in child
    import logging as _l
    _l.basicConfig(level=_l.WARNING)
    from ..config import Config as _C
    from .registry import build_default_registry, clean_text
    cfg = _C.model_validate_json(cfg_json)
    reg = build_default_registry(cfg)
    while True:
        try:
            msg = _decode(conn.recv_bytes())
        except (EOFError, OSError, ValueError):
            return
        if msg is None:
            return
        path, ext = msg
        try:
            r = reg.extract(path, ext)
            # cap and clean BEFORE encoding: the parent refuses oversized replies, and an
            # extractor that overran its own cap must cost a truncated result, not a dead child
            text = clean_text(r.text or "")[: cfg.indexing.max_text_chars]
            conn.send_bytes(_encode([text, r.status, r.method, r.error, r.meta]))
        except BaseException as e:  # noqa: BLE001
            try:
                conn.send_bytes(_encode(["", "error", "isolated", f"{type(e).__name__}: {e}"[:500], {}]))
            except Exception:
                return


def _confine(pid: int, memory_mb: int):
    """Put the extractor child in a Windows job object: a commit limit so a pathological
    document cannot take the machine down, and kill-on-close so the child never outlives the
    service. Returns the job handle (keep it alive) or None when unavailable."""
    if memory_mb <= 0:
        return None
    try:
        import win32api
        import win32con
        import win32job
    except ImportError:
        return None
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = (win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                                                       | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                                       | win32job.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
        info["ProcessMemoryLimit"] = int(memory_mb) * 1024 * 1024
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, h)
        finally:
            h.Close()
        return job
    except Exception as e:  # noqa: BLE001 - confinement is best effort (nested jobs on old Windows etc.)
        log.debug("extractor child not confined to a job object: %s", e)
        return None


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
        # JSON escapes a character to at most 6 bytes (\uXXXX); the child sends at most max_text_chars
        self._max_reply = 6 * int(cfg.indexing.max_text_chars) + (1 << 20)

    def _start(self) -> None:
        parent, child = self._ctx.Pipe()
        p = self._ctx.Process(target=_child_main, args=(child, self.cfg.model_dump_json(exclude={"source_path"})), daemon=True, name="semsearch-extract")
        p.start()
        child.close()
        self._proc, self._conn = p, parent
        self._job = _confine(p.pid, self.cfg.indexing.extractor_memory_mb)

    _job = None

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

    def close(self, timeout_s: float = 3.0) -> None:
        """Shut the child down. If a worker thread still holds the lock (blocked in extract()
        at shutdown), do not wait on it: terminate the child outright."""
        if not self._lock.acquire(timeout=timeout_s):
            self.kill_now()
            return
        try:
            try:
                if self._conn is not None:
                    self._conn.send_bytes(_encode(None))
            except Exception:
                pass
            self._kill()
        finally:
            self._lock.release()

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
                self._conn.send_bytes(_encode([path, ext]))
                if not self._conn.poll(self.timeout_s):
                    self._kill()
                    self.restarts += 1
                    return ExtractResult("", "error", "isolated", error=f"extraction timed out after {self.timeout_s:.0f}s; extractor process restarted")
                # bounded read: text is capped at max_text_chars by the extractors (4 bytes/char worst case)
                raw = self._conn.recv_bytes(self._max_reply)
                res = _valid_reply(_decode(raw))
                if res is None:
                    self._kill()
                    self.restarts += 1
                    return ExtractResult("", "error", "isolated", error="extractor process sent a malformed reply; restarted")
                res.text = res.text[: self.cfg.indexing.max_text_chars]
                return res
            except (ValueError, UnicodeDecodeError) as e:
                self._kill()
                self.restarts += 1
                return ExtractResult("", "error", "isolated", error=f"extractor process sent an unreadable reply: {type(e).__name__}")
            except (EOFError, OSError, BrokenPipeError) as e:
                self._kill()
                self.restarts += 1
                return ExtractResult("", "error", "isolated", error=f"extractor process died: {e}")
