"""Graceful shutdown: a normal stop must finish inside its budget with every thread exited,
whatever the indexer was doing. Each test here fails when the corresponding cancellation
check is removed (the slow operation then runs to completion and the budget is overrun)."""
import os
import sys
import threading
import time

import numpy as np
import pytest

from conftest import drain, write
from semsearch.embed.hashing import HashingProvider

WIN = sys.platform == "win32"


class SlowHashing(HashingProvider):
    """An embedder that takes `delay` seconds per call (a GPU busy with a long document)."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay
        self.calls = 0

    def embed(self, texts, kind="document"):
        self.calls += 1
        time.sleep(self.delay)
        return super().embed(texts, kind)


# ---------------------------------------------------------------- scheduler sweeps

def test_stop_interrupts_a_long_scope_sweep(built, root, cfg, monkeypatch):
    for i in range(300):
        write(str(root / "many" / f"n{i}.md"), f"# n{i}\n\nnote {i}\n")
    built.indexer.index_path(str(root / "many"))
    drain(built)
    assert built.store.count_documents() > 300
    real = built.store.remove_document_id
    removed = []

    def slow_remove(doc_id):
        time.sleep(0.05)  # 300 docs -> 15 s if the sweep cannot be interrupted
        removed.append(doc_id)
        real(doc_id)
    monkeypatch.setattr(built.store, "remove_document_id", slow_remove)
    cfg.excludes = list(cfg.excludes) + ["**/many/**"]
    built.indexer._scope_pending = True
    built.indexer.start()
    deadline = time.time() + 5
    while not removed and time.time() < deadline:
        time.sleep(0.02)
    assert removed, "the sweep did not start"
    t0 = time.time()
    assert built.indexer.stop(timeout=6) is True
    assert time.time() - t0 < 3.0
    assert not built.indexer._scheduler.is_alive() and 0 < len(removed) < 300
    assert built.indexer._scope_pending is True  # the interrupted sweep is repeated at the next start
    assert built.indexer.status()["phase"] == "idle"


def test_stop_interrupts_a_long_embedding_job(cfg, root):
    from semsearch.app_state import AppState
    cfg.chunking.target_chars = 120
    cfg.chunking.max_chars = 180
    cfg.chunking.overlap_chars = 10
    cfg.embedding.batch_size = 2
    write(str(root / "long.md"), "# Long\n\n" + "\n\n".join(f"paragraph {i} " + ("word " * 25) for i in range(60)) + "\n")
    st = AppState(cfg, start_indexer=False, isolate_extractors=False)
    slow = SlowHashing(0.25)  # 30 chunks / batch 2 -> 15 calls -> ~4 s per document
    st.embedder = slow
    st.indexer.embedder = slow
    try:
        st.indexer.index_path(str(root / "long.md"))
        st.indexer.start()
        deadline = time.time() + 5
        while slow.calls < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert slow.calls >= 2, "embedding did not start"
        t0 = time.time()
        assert st.indexer.stop(timeout=6) is True
        assert time.time() - t0 < 2.0
        assert slow.calls < 15  # interrupted between batches, not run to the end
        # nothing half-written: the document is absent, the job is still queued (running -> requeued at start)
        assert st.store.get_document(os.path.normcase(str(root / "long.md"))) is None
        assert st.store.requeue_running() == 1
    finally:
        st.close()


def test_prune_stops_between_batches(store_factory):
    s = store_factory()
    s.enqueue_many((f"c:\\x\\{i}.txt", "index", 5) for i in range(12000))
    calls = []

    def should_stop():
        calls.append(1)
        return len(calls) > 1  # first batch runs, then stop
    n = s.prune_pending_jobs(lambda p: True, should_stop=should_stop)
    assert 0 < n < 12000 and s.queue_stats()["pending"] == 12000 - n
    assert s.prune_pending_jobs(lambda p: True) == 12000 - n


# ---------------------------------------------------------------- service runtime

def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_service_stop_is_not_held_by_an_in_flight_request(cfg, root, monkeypatch):
    import httpx
    from semsearch.service import ServiceRuntime
    cfg.api.port = _free_port()
    cfg.service.shutdown_timeout_s = 12
    cfg.indexing.auto_start = False
    rt = ServiceRuntime(cfg, progress=lambda m: None)
    rt.start()
    try:
        stop_evt = rt.state.indexer._stop

        def long_sweep():
            # a maintenance call that honours the stop flag, like the real sweeps
            for _ in range(600):
                if stop_evt.is_set():
                    return {"interrupted": True}
                time.sleep(0.05)
            return {"interrupted": False}
        monkeypatch.setattr(rt.state.indexer, "prune_queue", lambda: 0)
        monkeypatch.setattr(rt.state.indexer, "enforce_scope", long_sweep)
        tok = rt.state.admin_token
        result = {}

        def call():
            try:
                result["r"] = httpx.post(f"http://127.0.0.1:{cfg.api.port}/indexer/prune", headers={"x-semsearch-token": tok}, timeout=60)
            except Exception as e:  # noqa: BLE001
                result["e"] = e
        th = threading.Thread(target=call, daemon=True)
        th.start()
        time.sleep(0.8)  # the request is in flight
        t0 = time.time()
        assert rt.stop() is True
        assert time.time() - t0 < cfg.service.shutdown_timeout_s
        th.join(5)
    finally:
        try:
            rt.stop(2)
        except Exception:  # noqa: BLE001
            pass


def test_forced_stop_kills_the_extractor_child(cfg, monkeypatch):
    from semsearch.service import ServiceRuntime
    cfg.api.port = _free_port()
    cfg.service.shutdown_timeout_s = 6
    cfg.indexing.auto_start = False
    rt = ServiceRuntime(cfg, progress=lambda m: None)
    rt.start()
    killed = []
    try:
        monkeypatch.setattr(rt.state.indexer, "stop", lambda timeout=30.0: False)  # simulate an overrun
        monkeypatch.setattr(rt.state.extractor, "kill_now", lambda: killed.append(1))
        assert rt.stop() is False
        assert killed == [1]
    finally:
        rt.state.indexer.stop(timeout=5)
        rt.state.close_without_indexer()


@pytest.mark.skipif(not WIN, reason="job objects are Windows-only")
def test_extractor_child_is_confined_and_dies_with_close(cfg, tmp_path):
    import win32api
    import win32con
    import win32job
    from semsearch.extract.isolated import IsolatedExtractor
    from semsearch.extract.registry import build_default_registry
    ex = IsolatedExtractor(cfg, build_default_registry(cfg), timeout_s=20)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"not a pdf")
    ex.extract(str(f), ".pdf")  # a document extension goes to the child
    assert ex._proc is not None and ex._proc.is_alive()
    h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, ex._proc.pid)
    assert ex._job is not None and win32job.IsProcessInJob(h, ex._job)
    proc = ex._proc
    ex.kill_now()
    deadline = time.time() + 5
    while proc.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert not proc.is_alive()
    ex.close()
