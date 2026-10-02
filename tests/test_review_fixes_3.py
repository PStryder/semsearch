"""Regression tests for the third review round: cache consistency under rollback, stale
inventory metadata, read authorization by roots, policy rescans, filtered candidate pools,
fingerprint completeness, rename re-embedding, config path plumbing, shutdown deadline."""
import os
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app
from semsearch.models import Chunk
from semsearch.security import normalize_path


# ---------------------------------------------------------------- cache consistency

def test_rollback_leaves_vector_cache_and_version_untouched(store_factory, monkeypatch):
    s = store_factory()
    now = time.time()
    fields = dict(path="c:\\r\\a.txt", display_path="A", root="c:\\r", filename="a.txt", extension=".txt", size=1, mtime=now, ctime=now,
                  file_id="1", volume_serial="1", content_hash="h1", extract_status="ok", extract_method="text", text_chars=1,
                  indexed_at=now, last_seen=now, source="fs", missing_since=None, title="t")
    v1 = np.eye(4, dtype=np.float32)[:1]
    s.write_document(fields, [Chunk(0, 0, 1, "first")], v1, s.fingerprint)
    ids_before = [cid for cid, _ in s.knn(v1[0], 5)]
    version_before = s.version
    # second write fails after the old vectors were deleted inside the transaction
    bad = dict(fields, content_hash="h2")
    with pytest.raises(AssertionError):
        s.write_document(bad, [Chunk(0, 0, 1, "second")], np.eye(4, dtype=np.float32)[:2], s.fingerprint)
    assert s.version == version_before                      # no invalidation for a write that did not happen
    assert [cid for cid, _ in s.knn(v1[0], 5)] == ids_before  # the cache still serves the committed vector
    assert len(s.cache) == 1 and s.stats()["vectors"] == 1


def test_response_cache_does_not_keep_pre_commit_results(built, root):
    """A search that runs while a write is in flight must not be served after the commit."""
    r1 = built.retriever.search("GPU memory architecture", "literal", 3)
    assert r1["results"][0]["filename"] == "gpu.txt"
    v_before = built.store.version
    (root / "gpu.txt").write_text("Totally different text about bananas now.\n", encoding="utf-8")
    built.indexer.index_path(str(root / "gpu.txt"))
    drain(built)
    assert built.store.version > v_before
    r2 = built.retriever.search("GPU memory architecture", "literal", 3)
    assert all(h["filename"] != "gpu.txt" for h in r2["results"])
    assert built.retriever.search("bananas", "literal", 1)["results"][0]["filename"] == "gpu.txt"


# ---------------------------------------------------------------- reconcile uses the filesystem, not inventory metadata

def test_reconcile_detects_edit_when_inventory_metadata_is_stale(built, root, monkeypatch):
    p = root / "gpu.txt"
    row = built.store.get_document(normalize_path(str(p)))
    stale_size, stale_mtime = row["size"], row["mtime"]
    time.sleep(0.05)
    p.write_text("Edited on disk; the inventory has not noticed yet.\n", encoding="utf-8")
    real_enum = built.fs.enumerate

    def stale_enumerate(directory):
        for fe in real_enum(directory):
            if fe.path.lower().endswith("gpu.txt"):
                fe.size, fe.mtime = stale_size, stale_mtime  # what a lagging Windows Search would report
            yield fe

    monkeypatch.setattr(built.fs, "enumerate", stale_enumerate)
    r = built.indexer.reconcile()
    assert r["enqueued"] == 1
    assert drain(built)[0][2] == "indexed"
    assert built.retriever.search("inventory has not noticed", "literal", 1)["results"][0]["filename"] == "gpu.txt"


# ---------------------------------------------------------------- read authorization by roots

def test_document_endpoint_refuses_paths_outside_roots(built, cfg, root, tmp_path):
    other = tmp_path / "other"
    write(str(other / "secret.md"), "# outside\n\nshould never be served\n")
    # smuggle a row for a path outside the roots into the store (as a removed root would leave)
    now = time.time()
    built.store.write_document(dict(path=normalize_path(str(other / "secret.md")), display_path=str(other / "secret.md"), root=normalize_path(str(other)),
                                    filename="secret.md", extension=".md", size=10, mtime=now, ctime=now, file_id="9", volume_serial="9", content_hash="x",
                                    extract_status="ok", extract_method="text", text_chars=10, indexed_at=now, last_seen=now, source="fs", missing_since=None, title="t"),
                               [Chunk(0, 0, 10, "should never be served")], None, None)
    with TestClient(create_app(cfg, state=built), base_url="http://127.0.0.1") as c:
        assert c.get("/document", params={"path": str(other / "secret.md"), "chunks": "true"}).status_code == 404
        assert c.get("/document", params={"path": str(root / "gpu.txt")}).status_code == 200
        assert all(h["filename"] != "secret.md" for h in c.post("/search", json={"query": "never be served", "mode": "literal"}).json()["results"])


def test_empty_roots_means_nothing_is_searchable_and_nothing_is_deleted(built, cfg):
    n_before = built.store.count_documents()
    cfg.roots.clear()
    built.indexer.roots = []
    built.retriever.roots = []
    assert built.retriever.search("gpu", "literal", 5)["results"] == []
    assert built.indexer.enforce_scope() == {"outside_roots": 0, "policy": 0}
    assert built.store.count_documents() == n_before  # a config mistake must not wipe the index


# ---------------------------------------------------------------- secret screening policy rescan

def test_enabling_secret_screening_rescans_unchanged_documents(cfg, root):
    from semsearch.app_state import AppState
    cfg.indexing.skip_suspected_secrets = False
    write(str(root / "sub" / "keys.md"), "# keys\n\nAWS: AKIAIOSFODNN7EXAMPLE is the access key id.\n")
    st = AppState(cfg, start_indexer=False, isolate_extractors=False)
    st.indexer.full_build()
    drain(st)
    assert st.store.get_document(normalize_path(str(root / "sub" / "keys.md")))["extract_status"] == "ok"
    assert st.retriever.search("AKIAIOSFODNN7EXAMPLE", "literal", 1)["results"][0]["filename"] == "keys.md"
    st.indexer.enforce_secret_policy()  # policy recorded as off
    # operator turns screening on and restarts: the file has not changed
    cfg.indexing.skip_suspected_secrets = True
    n = st.indexer.enforce_secret_policy()
    assert n == 1
    row = st.store.get_document(normalize_path(str(root / "sub" / "keys.md")))
    assert row["extract_status"] == "secret_suspected" and row["n_chunks"] == 0
    assert st.retriever.search("AKIAIOSFODNN7EXAMPLE", "literal", 1)["results"] == []
    assert st.store.stats()["vectors"] == st.store.stats()["chunks"]
    assert st.indexer.enforce_secret_policy() == 0  # idempotent
    st.close()


# ---------------------------------------------------------------- filtered searches get a deeper pool

def test_filter_is_not_starved_by_global_candidate_limit(built, root, cfg):
    cfg.retrieval.candidate_chunks = 5
    cfg.retrieval.candidate_docs = 5
    for i in range(30):
        write(str(root / "many" / f"note{i}.md"), f"# note {i}\n\nkeyword zebrafish appears here {i}\n")
    write(str(root / "rare" / "the_one.txt"), "keyword zebrafish appears in the rare folder too\n")
    built.indexer.index_path(str(root))
    drain(built)
    r = built.retriever.search("zebrafish", "literal", 5, roots=[str(root / "rare")])
    assert [h["filename"] for h in r["results"]] == ["the_one.txt"]
    r = built.retriever.search("zebrafish", "hybrid", 5, extensions=["txt"])
    names = [h["filename"] for h in r["results"]]
    assert names[0] == "the_one.txt" and all(n.endswith(".txt") for n in names)


# ---------------------------------------------------------------- fingerprint and rename re-embedding

def test_fingerprint_covers_sequence_length_prefix_and_normalization():
    from semsearch.config import EmbeddingConfig
    from semsearch.embed.factory import create_provider
    try:
        base = create_provider(EmbeddingConfig(allow_download=False))
    except Exception:
        pytest.skip("bge-small not cached")
    a = create_provider(EmbeddingConfig(allow_download=False, max_seq_length=256))
    b = create_provider(EmbeddingConfig(allow_download=False, query_prefix=""))
    c = create_provider(EmbeddingConfig(allow_download=False, normalize=False))
    fps = {base.fingerprint, a.fingerprint, b.fingerprint, c.fingerprint}
    assert len(fps) == 4


def test_rename_that_changes_title_refreshes_vectors(built, root):
    src = root / "gpu.txt"
    dst = root / "hbm_bandwidth_notes.txt"
    old_hashes = [c["text_hash"] for c in built.store.chunks_for_doc(int(built.store.get_document(normalize_path(str(src)))["id"]))]
    os.rename(src, dst)
    built.indexer.index_path(str(dst))
    res = drain(built)
    assert [r for _, _, r in res] == ["moved", "reembedded"]
    row = built.store.get_document(normalize_path(str(dst)))
    assert row["title"] == "hbm bandwidth notes"
    # the filename-only query now reaches the file through its refreshed title embedding
    assert built.retriever.search("hbm bandwidth", "semantic", 1)["results"][0]["filename"] == "hbm_bandwidth_notes.txt"


# ---------------------------------------------------------------- config path plumbing

def test_service_config_path_argument():
    from semsearch.service import config_path_from_argv
    assert config_path_from_argv(["--config", r"D:\x\semsearch.yaml"]) == r"D:\x\semsearch.yaml"
    assert config_path_from_argv([]) is None
    assert config_path_from_argv(["install", "--account", "x", "--config", "c.yaml"]) == "c.yaml"


# ---------------------------------------------------------------- shutdown deadline with a hung extractor

class HangingExtractor:
    """Blocks in extract() until killed, like a native filter stuck on a bad file."""

    def __init__(self):
        self.release = threading.Event()
        self.killed = False
        self.restarts = 0

    def extract(self, path, ext):
        self.release.wait(60)
        from semsearch.models import ExtractResult
        return ExtractResult("", "error", "hung", error="killed at shutdown")

    def kill_now(self):
        self.killed = True
        self.release.set()

    def close(self):
        self.release.set()


def test_stop_honours_deadline_with_hung_extractor(app, root):
    hang = HangingExtractor()
    app.indexer.extractor = hang
    app.indexer.start()
    app.indexer.index_path(str(root / "gpu.txt"))
    deadline = time.time() + 5
    while time.time() < deadline and app.indexer.state.current_path is None:
        time.sleep(0.05)
    assert app.indexer.state.current_path is not None  # worker is inside the hung extractor
    t0 = time.time()
    app.indexer.stop(timeout=4.0)
    assert time.time() - t0 < 6.0
    assert hang.killed is True
    assert not app.indexer._worker.is_alive()
