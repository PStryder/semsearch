"""Indexer: inventory -> jobs -> extract -> chunk -> embed -> store, incrementally and resumably.

Pipeline for one file (``_index_file``):
  1. containment + reparse-point + size checks (security.check_indexable)
  2. cheap skip: same size+mtime+fingerprint as stored -> touch and return
  3. content hash; if unchanged -> metadata touch only (no re-extraction, no re-embedding)
  4. rename/move detection by NTFS (volume, file_id) with the same content hash -> re-point
  5. extract (document formats in an isolated child process), chunk
  6. reuse vectors for identical chunk text; embed only the rest
  7. one transaction: document row + chunks + vectors

Scheduling:
  * full build: enumerate each root through its inventory source, enqueue all, reconcile
  * incremental: ``changed_since(checkpoint)`` per root (Windows GatherTime or fs mtime)
  * reconcile: enumerate again and remove documents that no longer exist
  * filesystem watcher (ReadDirectoryChangesW) for immediate add/modify/delete/rename
The job queue lives in SQLite so a restart resumes where it stopped.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .chunking import chunk_text
from .config import Config
from .embed.base import EmbeddingProvider
from .extract.registry import ExtractorRegistry
from .models import FileEntry
from .security import (PathRejected, check_indexable, display_path, file_extension, is_excluded, is_within, normalize_path,
                       root_for, suspected_secret, true_case_path)
from .store.db import Store, file_hash, text_hash

log = logging.getLogger(__name__)

_GENERIC_STEMS = {"readme", "index", "claude", "spec", "changelog", "todo", "notes", "main", "__init__", "setup", "config", "license", "doc", "docs"}
_HEADING_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*#*\s*$", re.M)


def derive_title(text: str, path: str) -> str:
    """A short header that is prepended to every chunk before embedding: the first Markdown
    heading when there is one near the top, otherwise a humanized filename (with the parent
    folder for generic names such as README). This gives the embedding the document's subject
    even for chunks deep inside it."""
    stem = os.path.splitext(os.path.basename(path))[0]
    human = re.sub(r"[_\-.]+", " ", stem).strip()
    if stem.lower() in _GENERIC_STEMS:
        parent = os.path.basename(os.path.dirname(path))
        if parent:
            human = f"{parent} {human}"
    m = _HEADING_RE.search(text[:1500]) if text else None
    if m:
        heading = re.sub(r"[*_`]+", "", m.group(1)).strip()
        if heading and heading.lower() != human.lower():
            return f"{heading} ({human})"[:200]
    return human[:200]


def apply_title(chunks, title: str) -> None:
    for ch in chunks:
        ch.embed_text = f"{title}\n\n{ch.text}" if title else ch.text


PRIO_USER = 1
PRIO_WATCH = 2
PRIO_INCREMENTAL = 3
PRIO_REEMBED = 4
PRIO_FULL = 5


@dataclass
class IndexerState:
    running: bool = False
    paused: bool = False
    current_path: str | None = None
    started_at: float | None = None
    last_incremental_at: float | None = None
    last_reconcile_at: float | None = None
    last_full_build_at: float | None = None
    full_build_in_progress: bool = False
    docs_indexed: int = 0
    docs_skipped: int = 0
    docs_touched: int = 0
    docs_moved: int = 0
    docs_removed: int = 0
    docs_failed: int = 0
    chunks_embedded: int = 0
    chunks_reused: int = 0
    bytes_hashed: int = 0
    extract_ms: float = 0.0
    embed_ms: float = 0.0
    recent: deque = field(default_factory=lambda: deque(maxlen=500))  # (ts, docs, chunks)
    sources: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None
    gpu_yielding: bool = False

    def throughput(self, window_s: float = 300.0) -> dict[str, float]:
        now = time.time()
        docs = chunks = 0
        oldest = now
        for ts, d, c in list(self.recent):  # copy: the worker appends concurrently
            if now - ts <= window_s:
                docs += d
                chunks += c
                oldest = min(oldest, ts)
        span = max(now - oldest, 1.0) if docs else window_s
        return {"window_s": window_s, "docs_per_s": round(docs / span, 3), "chunks_per_s": round(chunks / span, 3)}


class Indexer:
    def __init__(self, cfg: Config, store: Store, extractor, embedder: EmbeddingProvider,
                 windows_inventory=None, fs_inventory=None):
        self.cfg = cfg
        self.store = store
        self.extractor = extractor  # ExtractorRegistry or IsolatedExtractor (same .extract signature)
        self.embedder = embedder
        self.win = windows_inventory
        self.fs = fs_inventory
        self.roots = cfg.normalized_roots()
        self.allowed_ext = cfg.all_extensions()
        self.state = IndexerState()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None
        self._watcher = None
        self._lock = threading.Lock()
        self.fingerprint = embedder.fingerprint
        self.bulk_luid: str | None = None  # set by AppState from the device resolution (bulk adapter)

    # ---------- lifecycle ----------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        n = self.store.requeue_running()
        if n:
            log.info("requeued %d jobs left running by a previous process", n)
        try:
            self.enforce_scope()
        except Exception as e:
            log.warning("scope enforcement failed: %s", e)
        self._policy_rescan_pending = True  # done by the scheduler thread (can be a long scan)
        self._stop.clear()
        self.state.running = True
        self.state.started_at = time.time()
        self._worker = threading.Thread(target=self._work_loop, name="semsearch-indexer", daemon=True)
        self._worker.start()
        self._scheduler = threading.Thread(target=self._schedule_loop, name="semsearch-scheduler", daemon=True)
        self._scheduler.start()
        if self.cfg.indexing.watch_filesystem:
            try:
                from .watcher import DirectoryWatcher
                self._watcher = DirectoryWatcher([display_path(r) for r in self.cfg.roots if os.path.isdir(r)], self._on_watch_event)
                self._watcher.start()
            except Exception as e:
                log.warning("filesystem watcher not started: %s", e)

    def stop(self, timeout: float = 30.0) -> None:
        """Stop within `timeout` seconds end to end. A job in progress gets a share of the budget;
        if it is still stuck (typically a native extractor on a bad file) the extractor child is
        killed, which fails that job so it is retried at the next start."""
        deadline = time.time() + timeout
        self._stop.set()
        self._wake.set()
        if self._watcher:
            self._watcher.stop()
        if self._worker and self._worker.is_alive():
            self._worker.join(max(0.5, timeout * 0.5))
            if self._worker.is_alive():
                killer = getattr(self.extractor, "kill_now", None)
                if killer is not None:
                    log.warning("worker still busy on %s at shutdown; killing the extractor child", self.state.current_path)
                    killer()
                self._worker.join(max(0.5, deadline - time.time()))
                if self._worker.is_alive():
                    log.error("indexer worker did not stop within %.0fs; the running job will be requeued at next start", timeout)
        if self._scheduler and self._scheduler.is_alive():
            self._scheduler.join(max(0.5, deadline - time.time()))
        self.state.running = False

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self._wake.set()

    # ---------- public operations ----------
    def index_path(self, path: str, priority: int = PRIO_USER) -> dict[str, Any]:
        """Enqueue a file, or every file under a directory, for (re)indexing."""
        p = display_path(path)
        if not is_within(p, self.roots):
            raise PathRejected(f"outside configured roots: {p}")
        if os.path.isdir(p):
            if is_excluded(p, self.cfg.excludes, is_dir=True):
                raise PathRejected("directory matches an exclusion pattern")
            n = self._enqueue_tree(p, priority)
            self._wake.set()
            return {"enqueued": n, "kind": "directory"}
        self._check_policy(p)
        self.store.enqueue(normalize_path(p), "index", priority)
        self._wake.set()
        return {"enqueued": 1, "kind": "file"}

    def remove_path(self, path: str) -> dict[str, Any]:
        p = display_path(path)
        n = normalize_path(p)
        removed = 0
        if self.store.remove_document(n):
            removed = 1
        else:
            for doc_id, _ in self.store.paths_with_prefix(n):
                self.store.remove_document_id(doc_id)
                removed += 1
        self.state.docs_removed += removed
        return {"removed": removed}

    def request_full_build(self, wipe: bool = False) -> None:
        if wipe:
            self.store.wipe()
        self._full_requested = True
        self._wake.set()

    def retry_failed(self) -> int:
        n = self.store.retry_failed()
        self._wake.set()
        return n

    # ---------- inventory ----------
    def _source_for(self, root_norm: str):
        if self.cfg.indexing.use_windows_search and self.win is not None:
            try:
                if self.win.covers(root_norm):
                    if not self.state.sources.get(root_norm, "").startswith("windows_search"):
                        self.state.sources[root_norm] = "windows_search"
                    return self.win
            except Exception as e:
                log.warning("Windows Search coverage check failed for %s: %s", root_norm, e)
        self.state.sources[root_norm] = "fs"
        return self.fs

    def _wanted(self, fe: FileEntry) -> bool:
        ext = (fe.extension or file_extension(fe.path)).lower()
        if ext not in self.allowed_ext:
            return False
        if is_excluded(fe.path, self.cfg.excludes):
            return False
        if fe.size is not None and fe.size > self.cfg.indexing.max_file_bytes:
            return False
        return True

    def _enumerate(self, directory: str, root: str | None = None):
        """Yield every file under directory. Windows Search is used first when it covers the
        containing root (fast, metadata included), then a direct filesystem walk fills in
        anything the indexer has not gathered yet (new folders, lag) or excludes by its own rules."""
        root = root or root_for(directory, self.roots) or normalize_path(directory)
        src = self._source_for(root)
        seen: set[str] = set()
        if src is self.win:
            try:
                for fe in src.enumerate(directory):
                    seen.add(normalize_path(fe.path))
                    yield fe
                self.state.sources[root] = "windows_search+fs"
            except Exception as e:
                log.warning("Windows Search enumeration of %s failed (%s); falling back to filesystem walk", directory, e)
                self.state.sources[root] = "fs"
        for fe in self.fs.enumerate(directory):
            if normalize_path(fe.path) in seen:
                continue
            yield fe

    def _enumerate_root(self, root: str):
        return self._enumerate(root, root)

    def _enqueue_tree(self, directory: str, priority: int) -> int:
        items = [(normalize_path(fe.path), "index", priority) for fe in self._enumerate(directory) if self._wanted(fe)]
        return self.store.enqueue_many(items)

    def full_build(self) -> dict[str, Any]:
        """Enumerate all roots, enqueue everything wanted, then reconcile. Blocking; usually
        called from the scheduler thread."""
        t0 = time.time()
        self.state.full_build_in_progress = True
        total = 0
        try:
            for root in self.roots:
                if not os.path.isdir(root):
                    log.warning("root does not exist: %s", root)
                    continue
                seen: set[str] = set()
                batch: list[tuple[str, str, int]] = []
                max_gather = 0.0
                for fe in self._enumerate_root(root):
                    if self._stop.is_set():
                        break
                    if not self._wanted(fe):
                        continue
                    n = normalize_path(fe.path)
                    seen.add(n)
                    batch.append((n, "index", PRIO_FULL))
                    if fe.gather_time:
                        max_gather = max(max_gather, fe.gather_time)
                    if len(batch) >= 2000:
                        total += self.store.enqueue_many(batch)
                        batch = []
                        self._wake.set()
                if batch:
                    total += self.store.enqueue_many(batch)
                self._reconcile_root(root, seen)
                cp = max_gather if max_gather else time.time()
                self.store.set_meta(f"checkpoint:{root}", str(cp))
                log.info("full build: root %s via %s -> %d files enqueued", root, self.state.sources.get(root, "fs"), len(seen))
            self.state.last_full_build_at = time.time()
            self.store.set_meta("last_full_build_at", str(self.state.last_full_build_at))
        finally:
            self.state.full_build_in_progress = False
            self._wake.set()
        return {"enqueued": total, "seconds": round(time.time() - t0, 1)}

    _last_fs_scan: dict[str, float] = {}

    def incremental(self, force: bool = True) -> dict[str, Any]:
        """Enqueue files changed since each root's checkpoint. Windows-indexed roots are a cheap
        GatherTime query; roots without Windows coverage need a full mtime walk, which the
        scheduler (force=False) runs at most every fs_poll_interval_s, relying on the watcher
        in between."""
        total = 0
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            src = self._source_for(root)
            cp_raw = self.store.get_meta(f"checkpoint:{root}")
            if cp_raw is None:
                continue  # no full build yet for this root
            if src is self.fs and not force:
                if time.time() - self._last_fs_scan.get(root, 0.0) < self.cfg.indexing.fs_poll_interval_s:
                    continue
            if src is self.fs:
                self._last_fs_scan[root] = time.time()
            since = float(cp_raw) - 5.0
            max_seen = float(cp_raw)
            batch: list[tuple[str, str, int]] = []
            for fe in src.changed_since(root, since):
                if self._stop.is_set():
                    break
                if not self._wanted(fe):
                    continue
                batch.append((normalize_path(fe.path), "index", PRIO_INCREMENTAL))
                ts = fe.gather_time or fe.mtime or 0.0
                max_seen = max(max_seen, ts)
            if batch:
                # skip jobs for docs whose stat is unchanged (cheap pre-filter)
                filtered = []
                for n, op, pr in batch:
                    row = self.store.get_document(n)
                    if row is not None:
                        try:
                            st = os.stat(n)
                            if self._stat_unchanged(row, st.st_size, st.st_mtime):
                                continue
                        except OSError:
                            pass
                    filtered.append((n, op, pr))
                total += self.store.enqueue_many(filtered)
            self.store.set_meta(f"checkpoint:{root}", str(max_seen))
        self.state.last_incremental_at = time.time()
        if total:
            self._wake.set()
        return {"enqueued": total}

    def reconcile(self) -> dict[str, Any]:
        """Full enumeration diff in both directions: tombstone what vanished, and enqueue files
        the store lacks or holds with a stale size/mtime (recovers lost watcher events and
        Windows Search gaps)."""
        removed = 0
        added = 0
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            seen: set[str] = set()
            batch: list[tuple[str, str, int]] = []
            for fe in self._enumerate_root(root):
                if not self._wanted(fe):
                    continue
                n = normalize_path(fe.path)
                seen.add(n)
                row = self.store.get_document(n)
                if row is None:
                    batch.append((n, "index", PRIO_INCREMENTAL))
                    continue
                # compare against the FILESYSTEM, not the inventory's metadata: Windows Search can
                # report a stale size/mtime for a file it has not re-gathered yet
                try:
                    st = os.stat(fe.path)
                except OSError:
                    continue  # vanished between enumeration and stat; tombstoning handles it
                if not self._stat_unchanged(row, st.st_size, st.st_mtime):
                    batch.append((n, "index", PRIO_INCREMENTAL))
                    if len(batch) >= 2000:
                        added += self.store.enqueue_many(batch)
                        batch = []
            if batch:
                added += self.store.enqueue_many(batch)
            removed += self._reconcile_root(root, seen)
        self.state.last_reconcile_at = time.time()
        if added:
            self._wake.set()
        return {"removed": removed, "enqueued": added}

    def _reconcile_root(self, root: str, seen: set[str]) -> int:
        """Tombstone documents that are no longer present, then purge tombstones older than the
        grace period. Tombstoning (instead of deleting) lets a rename that is indexed later reclaim
        the document's chunks and vectors through the NTFS file id."""
        removed = 0
        known = self.store.count_documents(root)
        if known > 100 and len(seen) < self.cfg.indexing.reconcile_min_fraction * known:
            # an enumeration that lost most of the tree (permission error, unmounted volume,
            # indexer outage) must not look like mass deletion
            log.warning("reconcile %s: enumeration returned %d files but %d are indexed; skipping tombstoning", root, len(seen), known)
            self.store.record_error(root, "reconcile", f"enumeration returned {len(seen)} of {known} known files; tombstoning skipped")
            return 0
        for doc_id, p, _ in list(self.store.iter_paths(root)):
            if p in seen:
                continue
            if os.path.exists(p) and not is_excluded(p, self.cfg.excludes):
                # Windows Search may lag or have excluded it; the file is real, keep it
                continue
            self.store.tombstone_document(doc_id)
            removed += 1
        purged = self.store.purge_missing(self.tombstone_grace_s())
        if removed or purged:
            log.info("reconcile %s: tombstoned %d vanished documents, purged %d", root, removed, purged)
            self.state.docs_removed += purged
        return removed

    def tombstone_grace_s(self) -> float:
        return max(600.0, self.cfg.indexing.reconcile_interval_s)

    def reembed_stale(self, batch_docs: int = 1000) -> int:
        """Enqueue every document whose vectors belong to a different model (re-embedded from
        stored chunk text). Pages by id so the whole backlog is queued in one pass."""
        total = 0
        after = 0
        while True:
            rows = self.store.documents_needing_embedding(self.fingerprint, batch_docs, after_id=after)
            if not rows:
                break
            total += self.store.enqueue_many((r["path"], "reembed", PRIO_REEMBED) for r in rows)
            after = int(rows[-1]["id"])
            if len(rows) < batch_docs:
                break
        if total:
            self._wake.set()
        return total

    # ---------- watcher ----------
    def _on_watch_event(self, action: str, path: str, old_path: str | None = None) -> None:
        try:
            if old_path:
                # rename: index the new path first (move detection re-points the row), then
                # tombstone whatever is left at the old path
                n_old = normalize_path(old_path)
                if self.store.get_document(n_old) is not None:
                    self.store.enqueue(n_old, "vanish", PRIO_WATCH + 1)
                else:
                    for doc_id, dp in self.store.paths_with_prefix(n_old):
                        self.store.enqueue(dp, "vanish", PRIO_WATCH + 1)
                if os.path.isdir(path):
                    self._enqueue_tree(display_path(path), PRIO_WATCH)
                    self._wake.set()
                    return
            n = normalize_path(path)
            if action == "removed":
                if self.store.get_document(n) is not None:
                    self.store.enqueue(n, "vanish", PRIO_WATCH)
                else:
                    # directory removed: tombstone everything under it (indexed range scan)
                    for doc_id, dp in self.store.paths_with_prefix(n):
                        self.store.enqueue(dp, "vanish", PRIO_WATCH)
            elif os.path.isdir(path):
                # a folder copied or moved into a root arrives as one 'added' event
                if not is_excluded(path, self.cfg.excludes, is_dir=True):
                    self._enqueue_tree(display_path(path), PRIO_WATCH)
            else:
                ext = file_extension(path)
                if ext in self.allowed_ext and not is_excluded(path, self.cfg.excludes):
                    self.store.enqueue(n, "index", PRIO_WATCH)
            self._wake.set()
        except Exception as e:
            log.debug("watch event error %s %s: %s", action, path, e)

    # ---------- loops ----------
    _full_requested = False
    _policy_rescan_pending = False

    def _schedule_loop(self) -> None:
        if self._policy_rescan_pending:
            self._policy_rescan_pending = False
            try:
                self.enforce_secret_policy()
            except Exception as e:
                log.exception("secret policy rescan failed: %s", e)
        last_inc = 0.0
        # the incremental (GatherTime delta) pass runs on the first loop; the first full
        # reconcile is deferred so a boot does not start with a complete enumeration
        last_rec = time.time() - self.cfg.indexing.reconcile_interval_s + self.cfg.indexing.startup_reconcile_delay_s
        while not self._stop.is_set():
            try:
                if self._full_requested:
                    self._full_requested = False
                    self.full_build()
                    last_inc = time.time()
                    last_rec = time.time()
                elif self.cfg.indexing.auto_start and any(os.path.isdir(r) and self.store.get_meta(f"checkpoint:{r}") is None for r in self.roots):
                    # first start, or a root added to the configuration since the last build:
                    # enumerate now (roots already built are a cheap stat pass) instead of
                    # waiting for the deferred reconcile
                    self.full_build()
                    last_inc = last_rec = time.time()
                now = time.time()
                if now - last_inc >= self.cfg.indexing.poll_interval_s and not self.state.paused:
                    self.incremental(force=False)
                    self.reembed_stale()
                    last_inc = now
                if now - last_rec >= self.cfg.indexing.reconcile_interval_s and not self.state.paused:
                    self.reconcile()
                    last_rec = now
                self._maybe_vacuum(now)
            except Exception as e:
                log.exception("scheduler error: %s", e)
                self.state.last_error = f"scheduler: {e}"
            self._stop.wait(min(5.0, self.cfg.indexing.poll_interval_s))

    def _maybe_vacuum(self, now: float) -> None:
        """Idle-time housekeeping: reclaim free pages once per vacuum_interval_s when the queue is empty."""
        interval = self.cfg.indexing.vacuum_interval_s
        if interval <= 0 or self.store.queue_stats()["pending"] > 0 or self.state.current_path is not None:
            return
        last = float(self.store.get_meta("last_vacuum_at") or 0.0)
        if now - last < interval:
            return
        try:
            r = self.store.vacuum()
            log.info("index vacuum (%s): free pages %d -> %d in %.1fs", r["kind"], r["free_pages_before"], r["free_pages_after"], r["seconds"])
        except Exception as e:
            log.warning("vacuum failed: %s", e)
            self.store.set_meta("last_vacuum_at", str(now))

    def _work_loop(self) -> None:
        idle_wait = 1.0
        while not self._stop.is_set():
            if self.state.paused:
                self._wake.wait(2.0)
                self._wake.clear()
                continue
            job = self.store.next_job()
            if job is None:
                self._set_bulk(False)
                self._wake.wait(idle_wait)
                self._wake.clear()
                continue
            try:
                want_bulk = self.state.full_build_in_progress or self.store.queue_stats()["pending"] > self.cfg.embedding.bulk_threshold
                self._set_bulk(want_bulk and not self._bulk_gpu_busy_elsewhere())
                self.process_job(job)
            except Exception as e:
                log.exception("job %s failed: %s", job["path"], e)
                self.store.fail_job(int(job["id"]), str(e), self.cfg.indexing.max_attempts)
                self.store.record_error(job["path"], "job", str(e))
                self.state.docs_failed += 1
                self.state.last_error = f"{job['path']}: {e}"

    # ---------- job processing ----------
    _gpu_monitor = None
    _gpu_yielding = False

    def _bulk_gpu_busy_elsewhere(self) -> bool:
        """GPU courtesy: stay off the bulk GPU while other processes keep it busier than
        `indexing.bulk_yield_gpu_percent` (games, training runs). The bulk device's LUID comes
        from the resolution report; the check is sampled at most every few seconds."""
        thr = self.cfg.indexing.bulk_yield_gpu_percent
        luid = self.bulk_luid
        if thr <= 0 or not luid:
            return False
        if self._gpu_monitor is None:
            from .gpu_monitor import GpuMonitor
            self._gpu_monitor = GpuMonitor()
        busy = self._gpu_monitor.others_busy_percent(luid)
        yielding = busy >= thr
        if yielding != self._gpu_yielding:
            self._gpu_yielding = yielding
            log.info("bulk GPU %s by other processes (%.0f%%): %s", "busy" if yielding else "free again", busy, "using the steady-state device" if yielding else "bulk device available")
        self.state.gpu_yielding = yielding
        return yielding

    def _set_bulk(self, on: bool) -> None:
        """Switch the embedder between its steady-state and bulk devices (no-op for providers
        without the capability)."""
        setter = getattr(self.embedder, "set_bulk_mode", None)
        if setter is not None:
            setter(on)

    def process_job(self, job) -> str:
        path, op, jid = job["path"], job["op"], int(job["id"])
        self.state.current_path = path
        try:
            if op == "remove":
                if self.store.remove_document(path):
                    self.state.docs_removed += 1
                self.store.complete_job(jid)
                return "removed"
            if op == "vanish":
                row = self.store.get_document(path)
                if row is not None and not os.path.exists(row["display_path"]):
                    self.store.tombstone_document(int(row["id"]))
                self.store.complete_job(jid)
                return "tombstoned"
            if op == "reembed":
                res = self._reembed(path)
                self.store.complete_job(jid)
                return res
            res = self._index_file(path)
            self.store.complete_job(jid)
            return res
        finally:
            self.state.current_path = None

    def _check_policy(self, path: str) -> None:
        """Exclusion globs and the extension allow-list, enforced for every job regardless of
        how it was enqueued (watcher, full build, explicit API/CLI request)."""
        if is_excluded(path, self.cfg.excludes):
            raise PathRejected("matches an exclusion pattern")
        if file_extension(path) not in self.allowed_ext:
            raise PathRejected(f"extension {file_extension(path) or '(none)'!r} is not in the configured extension lists")

    def enforce_scope(self) -> dict[str, int]:
        """Remove documents that the current configuration no longer covers: a root that was
        removed from the config, or paths that now match an exclusion / lost their extension.
        Runs once at startup so 'stop indexing this folder' also means 'stop showing it'."""
        removed_root = removed_policy = 0
        if not self.roots:
            # an empty roots list is a configuration error, not an instruction to delete the index
            log.warning("no roots configured: scope enforcement skipped (nothing is searchable until roots are set)")
            return {"outside_roots": 0, "policy": 0}
        for doc_id, p, _ in list(self.store.iter_paths()):
            if not is_within(p, self.roots):
                self.store.remove_document_id(doc_id)
                removed_root += 1
            elif is_excluded(p, self.cfg.excludes) or file_extension(p) not in self.allowed_ext:
                self.store.remove_document_id(doc_id)
                removed_policy += 1
        if removed_root or removed_policy:
            log.info("scope enforcement: removed %d documents outside roots, %d by exclusion/extension policy", removed_root, removed_policy)
            self.state.docs_removed += removed_root + removed_policy
        return {"outside_roots": removed_root, "policy": removed_policy}

    def enforce_secret_policy(self) -> int:
        """When secret screening is (re)enabled, already-indexed, unchanged documents must be
        screened too: scan stored chunk text and quarantine hits. Runs once per policy change
        (tracked in meta 'policy:secret_scan'); returns the number of documents quarantined."""
        want = "on" if self.cfg.indexing.skip_suspected_secrets else "off"
        have = self.store.get_meta("policy:secret_scan")
        if have == want:
            return 0
        n = 0
        if want == "on":
            log.info("secret screening enabled: scanning stored text of indexed documents")
            for doc_id, disp, text in self.store.iter_documents_with_text():
                if is_excluded(disp, self.cfg.indexing.secret_scan_allow):
                    continue
                hit = suspected_secret(text)
                if hit:
                    self.store.quarantine_document(doc_id, "secret_suspected", f"credential pattern: {hit}")
                    self.store.record_error(disp, "policy", f"credential pattern: {hit} (found by policy rescan)")
                    n += 1
            log.info("secret screening rescan: %d documents quarantined", n)
        else:
            # screening turned off: previously quarantined documents are re-indexed on their next change;
            # force it now so the index matches the policy
            rows = self.store._r().execute("SELECT path FROM documents WHERE extract_status='secret_suspected'").fetchall()
            n = self.store.enqueue_many((r[0], "index", PRIO_REEMBED) for r in rows)
            log.info("secret screening disabled: %d previously quarantined documents re-queued", n)
        self.store.set_meta("policy:secret_scan", want)
        return n

    def _stat_unchanged(self, row, size: int, mtime: float) -> bool:
        """Same size and mtime as the stored row, and nothing left to do for it: an 'ok' document
        must carry current-model vectors; non-text outcomes (empty/binary/unsupported/too_large)
        are final until the bytes change."""
        if row["size"] != size or row["mtime"] is None or abs(row["mtime"] - mtime) >= 1e-6:
            return False
        st = row["extract_status"]
        if st == "ok":
            return row["embedding_fingerprint"] == self.fingerprint and row["n_chunks"] > 0
        return st in ("empty", "binary", "unsupported", "too_large", "secret_suspected")

    def _index_file(self, path_norm: str) -> str:
        disp = true_case_path(path_norm)
        try:
            info = check_indexable(disp, self.roots, self.cfg.indexing.follow_reparse_points)
            self._check_policy(disp)
        except FileNotFoundError:
            row = self.store.get_document(path_norm)
            if row is not None:
                self.store.tombstone_document(int(row["id"]))
            return "missing"
        except PathRejected as e:
            # policy applies to explicit requests too: an excluded or unsupported file is never
            # indexed, and if it was indexed under an older policy it is removed now
            self.store.record_error(disp, "policy", str(e))
            if self.store.remove_document(path_norm):
                self.state.docs_removed += 1
            self.state.docs_skipped += 1
            return "rejected"
        if info.size > self.cfg.indexing.max_file_bytes:
            self._write_status(path_norm, disp, info, "too_large", "none", f"{info.size} bytes > limit")
            self.state.docs_skipped += 1
            return "too_large"
        now = time.time()
        existing = self.store.get_document(path_norm)
        if existing is not None and self._stat_unchanged(existing, info.size, info.mtime):
            self.store.update_document(int(existing["id"]), last_seen=now)
            self.state.docs_skipped += 1
            return "unchanged"
        if existing is not None and existing["extract_status"] == "missing" and existing["size"] == info.size \
                and existing["mtime"] is not None and abs(existing["mtime"] - info.mtime) < 1e-6 and existing["embedding_fingerprint"] == self.fingerprint \
                and existing["n_chunks"] > 0:
            # the file came back at the same path (restore from recycle bin, undo) before purge
            self.store.restore_document(int(existing["id"]), last_seen=now)
            self.state.docs_touched += 1
            return "restored"

        t0 = time.perf_counter()
        try:
            chash = file_hash(disp)
        except PermissionError as e:
            self._write_status(path_norm, disp, info, "denied", "none", str(e))
            self.state.docs_failed += 1
            return "denied"
        self.state.bytes_hashed += info.size

        if existing is not None and existing["content_hash"] == chash and existing["embedding_fingerprint"] == self.fingerprint and existing["extract_status"] == "ok":
            self.store.update_document(int(existing["id"]), size=info.size, mtime=info.mtime, ctime=info.ctime, last_seen=now,
                                       file_id=str(info.file_id), volume_serial=str(info.volume_serial))
            self.state.docs_touched += 1
            return "touched"

        if existing is None:
            # rename/move detection: same NTFS identity + same bytes, old path gone (or tombstoned)
            for row in self.store.find_by_file_id(info.volume_serial, info.file_id):
                if row["content_hash"] == chash and row["embedding_fingerprint"] == self.fingerprint \
                        and row["extract_status"] in ("ok", "missing") and row["n_chunks"] > 0 and not os.path.exists(row["display_path"]):
                    new_ext = file_extension(disp)
                    fields = dict(path=path_norm, display_path=disp, filename=os.path.basename(disp), extension=new_ext,
                                  root=root_for(disp, self.roots), size=info.size, mtime=info.mtime, ctime=info.ctime, last_seen=now)
                    # a filename-derived title follows the file; a heading-derived one does not change
                    retitle = bool(row["title"]) and row["title"] == derive_title("", row["display_path"]) and derive_title("", disp) != row["title"]
                    if retitle:
                        fields["title"] = derive_title("", disp)
                    self.store.restore_document(int(row["id"]), **fields)
                    self.state.docs_moved += 1
                    if retitle:
                        # the title is part of what gets embedded: refresh the vectors from stored chunk text
                        self.store.enqueue(path_norm, "reembed", PRIO_REEMBED)
                    log.info("moved: %s -> %s (vectors kept%s)", row["display_path"], disp, ", re-embed queued for new title" if retitle else "")
                    return "moved"

        ext = file_extension(disp)
        res = self.extractor.extract(disp, ext)
        extract_ms = (time.perf_counter() - t0) * 1000
        self.state.extract_ms += extract_ms
        if res.status == "missing":
            # deleted between stat and extract (temp/autosave files do this): tombstone, do not record a zombie
            if existing is not None:
                self.store.tombstone_document(int(existing["id"]))
            return "missing"
        if res.ok and self.cfg.indexing.skip_suspected_secrets and not is_excluded(disp, self.cfg.indexing.secret_scan_allow):
            hit = suspected_secret(res.text)
            if hit:
                res.status, res.error, res.text = "secret_suspected", f"credential pattern: {hit}", ""
        fields = dict(
            path=path_norm, display_path=disp, root=root_for(disp, self.roots), filename=os.path.basename(disp), extension=ext,
            size=info.size, mtime=info.mtime, ctime=info.ctime, file_id=str(info.file_id), volume_serial=str(info.volume_serial),
            content_hash=chash, extract_status=res.status, extract_method=res.method, extract_error=res.error,
            text_chars=len(res.text), indexed_at=now, last_seen=now, source="fs", missing_since=None, title=None)
        if not res.ok:
            self.store.write_document(fields, [], None, None)
            if res.status in ("error", "denied"):
                self.store.record_error(disp, "extract", res.error or res.status)
                self.state.docs_failed += 1
            elif res.status == "secret_suspected":
                self.store.record_error(disp, "policy", res.error or res.status)
                self.state.docs_skipped += 1
            else:
                self.state.docs_skipped += 1
            return res.status

        chunks = chunk_text(res.text, self.cfg.chunking, ext)
        if not chunks:
            fields["extract_status"] = "empty"
            self.store.write_document(fields, [], None, None)
            self.state.docs_skipped += 1
            return "empty"
        title = derive_title(res.text, disp)
        fields["title"] = title
        apply_title(chunks, title)
        # embed BEFORE touching the document row: if this raises, the previous version stays
        # intact and searchable, and the retry sees the old stat/hash so it re-extracts
        vectors, n_new, n_reused, embed_ms = self._embed_chunks(chunks)
        self.state.embed_ms += embed_ms
        doc_id = self.store.write_document(fields, chunks, vectors, self.fingerprint)
        self.state.docs_indexed += 1
        self.state.chunks_embedded += n_new
        self.state.chunks_reused += n_reused
        self.state.recent.append((time.time(), 1, len(chunks)))
        log.debug("indexed %s: %d chunks (%d embedded, %d reused) extract %.0fms embed %.0fms", disp, len(chunks), n_new, n_reused, extract_ms, embed_ms)
        return "indexed"

    def _embed_chunks(self, chunks) -> tuple[np.ndarray, int, int, float]:
        t0 = time.perf_counter()
        hashes = [text_hash(c.for_embedding) for c in chunks]
        reuse = self.store.vectors_for_hashes(hashes)
        todo_idx = [i for i, h in enumerate(hashes) if h not in reuse]
        vectors = np.zeros((len(chunks), self.embedder.dim), dtype=np.float32)
        for i, h in enumerate(hashes):
            if h in reuse:
                vectors[i] = reuse[h]
        if todo_idx:
            # de-duplicate within the document too
            uniq: dict[str, int] = {}
            texts: list[str] = []
            for i in todo_idx:
                if hashes[i] not in uniq:
                    uniq[hashes[i]] = len(texts)
                    texts.append(chunks[i].for_embedding)
            emb = self.embedder.embed(texts, "document")
            for i in todo_idx:
                vectors[i] = emb[uniq[hashes[i]]]
        return vectors, len(todo_idx), len(chunks) - len(todo_idx), (time.perf_counter() - t0) * 1000

    def _reembed(self, path_norm: str) -> str:
        row = self.store.get_document(path_norm)
        if row is None:
            return "missing"
        chunks_rows = self.store.chunks_for_doc(int(row["id"]))
        if not chunks_rows:
            return "no_chunks"
        from .models import Chunk
        chunks = [Chunk(int(c["ordinal"]), int(c["start"]), int(c["end"]), c["text"]) for c in chunks_rows]
        apply_title(chunks, row["title"] or derive_title("", row["display_path"]))
        vectors, n_new, n_reused, embed_ms = self._embed_chunks(chunks)
        self.state.embed_ms += embed_ms
        self.store.set_vectors([int(c["id"]) for c in chunks_rows], vectors, int(row["id"]), self.fingerprint)
        self.state.chunks_embedded += n_new
        self.state.chunks_reused += n_reused
        self.state.recent.append((time.time(), 1, len(chunks)))
        return "reembedded"

    def _write_status(self, path_norm: str, disp: str, info, status: str, method: str, error: str | None) -> None:
        now = time.time()
        self.store.write_document(dict(
            path=path_norm, display_path=disp, root=root_for(disp, self.roots), filename=os.path.basename(disp),
            extension=file_extension(disp), size=info.size, mtime=info.mtime, ctime=info.ctime,
            file_id=str(info.file_id), volume_serial=str(info.volume_serial), extract_status=status, extract_method=method,
            extract_error=error, text_chars=0, indexed_at=now, last_seen=now, source="fs", missing_since=None, title=None), [], None, None)

    # ---------- reporting ----------
    def status(self) -> dict[str, Any]:
        s = self.state
        q = self.store.queue_stats()
        return {
            "running": s.running, "paused": s.paused, "full_build_in_progress": s.full_build_in_progress,
            "current_path": s.current_path, "queue": q,
            "started_at": s.started_at, "last_incremental_at": s.last_incremental_at,
            "last_reconcile_at": s.last_reconcile_at, "last_full_build_at": s.last_full_build_at or (float(self.store.get_meta("last_full_build_at")) if self.store.get_meta("last_full_build_at") else None),
            "counters": {"indexed": s.docs_indexed, "skipped_unchanged": s.docs_skipped, "touched": s.docs_touched, "moved": s.docs_moved,
                         "removed": s.docs_removed, "failed": s.docs_failed, "chunks_embedded": s.chunks_embedded, "chunks_reused": s.chunks_reused,
                         "bytes_hashed": s.bytes_hashed, "extract_ms_total": round(s.extract_ms), "embed_ms_total": round(s.embed_ms)},
            "throughput": s.throughput(), "sources": s.sources, "roots": [display_path(r) for r in self.cfg.roots],
            "watcher": bool(self._watcher and self._watcher.is_alive()),
            "extractor_restarts": getattr(self.extractor, "restarts", 0),
            "last_error": s.last_error,
            "failed_jobs": self.store.failed_jobs(20),
            "gpu_yielding": s.gpu_yielding,
            "embedding": {"fingerprint": self.fingerprint, "dim": self.embedder.dim, "device": self.embedder.device,
                          "devices": getattr(self.embedder, "device_summary", lambda: {"steady": self.embedder.device})(),
                          "bulk_mode": getattr(self.embedder, "bulk_mode", False)},
        }
