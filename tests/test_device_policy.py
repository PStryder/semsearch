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


def _switching(app):
    prov = SwitchingProvider()
    app.indexer.embedder = prov
    app.indexer.fingerprint = prov.fingerprint
    app.retriever.embedder = prov
    app.store.ensure_vectors(prov.fingerprint, prov.dim)
    return prov


def _long_doc(n_paragraphs, edit=""):
    return "\n\n".join(f"Paragraph {i} about subject {i * 7919 % 1000}: " + ("distinct words %d " % i) * 60 + (edit if i == 0 else "")
                       for i in range(n_paragraphs))


def test_long_document_goes_to_bulk_on_a_quiet_queue(app, cfg, root):
    from conftest import drain, write
    prov = _switching(app)
    cfg.embedding.bulk_doc_chunks = 5
    write(str(root / "short.md"), "# Short\n\none small note\n")
    app.indexer.index_path(str(root / "short.md"))
    drain(app)
    assert prov.calls and all(m == "steady" for m, _ in prov.calls)  # below the threshold: steady
    prov.calls.clear()
    write(str(root / "long.md"), _long_doc(12))
    app.indexer.index_path(str(root / "long.md"))
    drain(app)
    assert sum(n for _, n in prov.calls) >= 5 and all(m == "bulk" for m, _ in prov.calls)


def test_reused_vectors_do_not_count_and_zero_disables(app, cfg, root):
    from conftest import drain, write
    prov = _switching(app)
    cfg.embedding.bulk_doc_chunks = 5
    write(str(root / "long.md"), _long_doc(12))
    app.indexer.index_path(str(root / "long.md"))
    drain(app)
    # one paragraph edited: the other chunks reuse their vectors, so the edit is steady work
    prov.set_bulk_mode(False)
    prov.calls.clear()
    write(str(root / "long.md"), _long_doc(12, edit=" an edit"))
    app.indexer.index_path(str(root / "long.md"))
    drain(app)
    assert prov.calls and sum(n for _, n in prov.calls) < 5 and all(m == "steady" for m, _ in prov.calls)
    # 0 turns the per-document rule off
    cfg.embedding.bulk_doc_chunks = 0
    prov.calls.clear()
    write(str(root / "other.md"), _long_doc(12).replace("Paragraph", "Section"))
    app.indexer.index_path(str(root / "other.md"))
    drain(app)
    assert sum(n for _, n in prov.calls) >= 5 and all(m == "steady" for m, _ in prov.calls)


def test_bulk_doc_chunks_default_and_validation():
    import pytest
    from semsearch.config import Config
    assert Config().embedding.bulk_doc_chunks == 200
    with pytest.raises(Exception):
        Config.model_validate({"embedding": {"bulk_doc_chunks": -1}})
