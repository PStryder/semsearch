"""The indexer switches the embedder between steady-state and bulk devices by queue depth."""
import numpy as np

from conftest import drain
from semsearch.embed.hashing import HashingProvider


class SwitchingProvider(HashingProvider):
    """Hashing provider that records which 'device' each document batch used."""

    def __init__(self):
        super().__init__()
        self.bulk_mode = False
        self.calls: list[tuple[str, int]] = []

    def set_bulk_mode(self, on):
        self.bulk_mode = on

    def embed(self, texts, kind="document"):
        if kind == "document":
            self.calls.append(("bulk" if self.bulk_mode else "steady", len(texts)))
        return super().embed(texts, kind)


def test_parse_and_resolve_device():
    from semsearch.embed.onnx_provider import parse_device, resolve_device
    assert parse_device("dml:1") == ("dml", 1)
    assert parse_device("cpu") == ("cpu", 0)
    assert resolve_device("dml:1", ["DmlExecutionProvider", "CPUExecutionProvider"]) == ("dml", 1)
    assert resolve_device("dml:1", ["CPUExecutionProvider"]) == ("cpu", 0)  # graceful fallback
    assert resolve_device("auto", ["DmlExecutionProvider", "CPUExecutionProvider"]) == ("dml", 0)
    assert resolve_device("auto", ["CPUExecutionProvider"]) == ("cpu", 0)


def test_bulk_mode_follows_full_build_and_queue_depth(app, cfg):
    prov = SwitchingProvider()
    app.indexer.embedder = prov
    app.indexer.fingerprint = prov.fingerprint
    app.retriever.embedder = prov
    app.store.ensure_vectors(prov.fingerprint, prov.dim)
    cfg.embedding.bulk_threshold = 2
    # a full build enqueues 6 jobs: the worker sees a deep queue -> bulk; the tail drains in steady mode
    app.indexer.start()
    try:
        app.indexer.request_full_build()
        import time
        deadline = time.time() + 20
        while time.time() < deadline and (app.store.queue_stats()["pending"] or app.store.queue_stats()["running"] or app.indexer.state.full_build_in_progress or app.store.stats()["documents"] < 6):
            time.sleep(0.1)
    finally:
        app.indexer.stop()
    modes = [m for m, _ in prov.calls]
    assert "bulk" in modes
    assert prov.bulk_mode is False  # idle -> back to steady state
    # a single user-triggered re-index on a quiet queue runs on the steady device
    prov.calls.clear()
    (cfg.roots[0] / "gpu.txt").write_text("GPU memory architecture, updated with HBM notes.\n", encoding="utf-8")
    app.indexer.index_path(str(cfg.roots[0] / "gpu.txt"))
    app.indexer.start()   # the WORKER decides the mode, as in production
    try:
        import time
        deadline = time.time() + 15
        while time.time() < deadline and (app.store.queue_stats()["pending"] or app.store.queue_stats()["running"] or not prov.calls):
            time.sleep(0.05)
    finally:
        app.indexer.stop()
    assert prov.calls and all(m == "steady" for m, _ in prov.calls)
