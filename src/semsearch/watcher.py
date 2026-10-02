"""ReadDirectoryChangesW-based watcher for immediate change notifications.

One thread per root. Events are coalesced for a short settle window so a file that is
written in several steps is enqueued once. Rename pairs are delivered as (new, old).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

FILE_LIST_DIRECTORY = 0x0001
FILE_NOTIFY_CHANGE_FILE_NAME = 0x1
FILE_NOTIFY_CHANGE_DIR_NAME = 0x2
FILE_NOTIFY_CHANGE_SIZE = 0x8
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
FILE_NOTIFY_CHANGE_CREATION = 0x40
ACTIONS = {1: "added", 2: "removed", 3: "modified", 4: "renamed_old", 5: "renamed_new"}


class DirectoryWatcher:
    def __init__(self, roots: list[str], callback: Callable[[str, str, str | None], None], settle_s: float = 1.5):
        self.roots = roots
        self.callback = callback
        self.settle_s = settle_s
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pending: dict[str, tuple[str, float, str | None]] = {}
        self._lock = threading.Lock()
        self._flusher: threading.Thread | None = None

    def start(self) -> None:
        import win32file  # noqa: F401  (fail early if pywin32 missing)
        for r in self.roots:
            t = threading.Thread(target=self._watch, args=(r,), name=f"semsearch-watch:{r}", daemon=True)
            t.start()
            self._threads.append(t)
        self._flusher = threading.Thread(target=self._flush_loop, name="semsearch-watch-flush", daemon=True)
        self._flusher.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _watch(self, root: str) -> None:
        import pywintypes
        import win32con
        import win32file
        try:
            h = win32file.CreateFile(root, FILE_LIST_DIRECTORY,
                                     win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                                     None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OVERLAPPED, None)
        except pywintypes.error as e:
            log.warning("cannot watch %s: %s", root, e)
            return
        import win32event
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, False, False, None)
        buf = win32file.AllocateReadBuffer(1 << 16)
        flags = FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_CREATION
        log.info("watching %s", root)
        while not self._stop.is_set():
            try:
                win32file.ReadDirectoryChangesW(h, buf, True, flags, overlapped)
                while not self._stop.is_set():
                    rc = win32event.WaitForSingleObject(overlapped.hEvent, 1000)
                    if rc == win32event.WAIT_OBJECT_0:
                        break
                if self._stop.is_set():
                    break
                n = win32file.GetOverlappedResult(h, overlapped, True)
                if n == 0:
                    # buffer overflow: caller should reconcile; signal with a root-level modified
                    self._queue("overflow", root, None)
                    continue
                results = win32file.FILE_NOTIFY_INFORMATION(buf, n)
                old = None
                for action, name in results:
                    p = os.path.join(root, name)
                    a = ACTIONS.get(action, "modified")
                    if a == "renamed_old":
                        old = p
                        continue
                    if a == "renamed_new":
                        self._queue("renamed", p, old)
                        old = None
                    else:
                        self._queue(a, p, None)
            except Exception as e:
                log.warning("watcher %s error: %s", root, e)
                time.sleep(2.0)
        try:
            h.Close()
        except Exception:
            pass

    def _queue(self, action: str, path: str, old: str | None) -> None:
        with self._lock:
            prev = self._pending.get(path)
            if prev and prev[0] == "removed" and action in ("modified", "added"):
                action = "added"
            self._pending[path] = (action, time.time(), old or (prev[2] if prev else None))

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.5)
            now = time.time()
            ready = []
            with self._lock:
                for p, (a, ts, old) in list(self._pending.items()):
                    if now - ts >= self.settle_s:
                        ready.append((a, p, old))
                        del self._pending[p]
            for a, p, old in ready:
                if a == "overflow":
                    log.warning("watch buffer overflow under %s; relying on next incremental/reconcile", p)
                    continue
                try:
                    self.callback("removed" if a == "removed" else "changed", p, old)
                except Exception as e:
                    log.debug("watch callback failed: %s", e)
