"""Regression tests for the code-review findings. Each test fails on the pre-fix code."""
import os
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app
from semsearch.models import SearchHit, ScoreComponents, iso_timestamp
from semsearch.security import file_extension, normalize_path, suspected_secret
from semsearch.store.db import VectorCache


# ---- security boundary ----

def test_host_header_guard_blocks_dns_rebinding(built, cfg):
    with TestClient(create_app(cfg, state=built), base_url="http://127.0.0.1") as c:
        assert c.get("/health").status_code == 200                                   # testclient uses host 'testserver'? no: base_url host
        r = c.get("/health", headers={"host": "evil.example:8765"})
        assert r.status_code == 421 and "not allowed" in r.json()["detail"]
        assert c.get("/health", headers={"host": "127.0.0.1:8765"}).status_code == 200
        assert c.get("/health", headers={"host": "localhost"}).status_code == 200
        assert c.get("/health", headers={"host": "[::1]:8765"}).status_code == 200
        assert c.post("/search", json={"query": "x"}, headers={"host": "attacker.test"}).status_code == 421


def test_suspected_secret_patterns():
    assert suspected_secret("-----BEGIN RSA PRIVATE KEY-----\nMIIE...") == "private_key_block"
    assert suspected_secret("aws_access_key_id = AKIAIOSFODNN7EXAMPLE") == "aws_access_key"
    assert suspected_secret('{"client_id": "x", "client_secret": "GOCSPX-abcdefghijklmnopqrstuvwxyz"}') == "google_oauth_client_secret"
    assert suspected_secret("OPENAI_API_KEY=sk-proj-" + "a" * 40) is not None
    assert suspected_secret("token = os.environ['TOKEN']") is None
    assert suspected_secret("The password policy requires rotation every 90 days.") is None
    assert suspected_secret("api_key: ${API_KEY}") is None


def test_secret_files_are_not_indexed_but_are_visible_in_diagnostics(built, root):
    p = root / "sub" / "deploy_notes.md"
    write(str(p), "# Deploy\n\nUse this: -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n")
    built.indexer.index_path(str(p))
    res = drain(built)
    assert res[0][2] == "secret_suspected"
    row = built.store.get_document(normalize_path(str(p)))
    assert row["extract_status"] == "secret_suspected" and row["n_chunks"] == 0 and row["text_chars"] == 0
    assert all(h["filename"] != "deploy_notes.md" for h in built.retriever.search("openssh private key", "hybrid", 10)["results"])
    assert built.store.recent_errors(1)[0]["stage"] == "policy"
    assert built.store.stats()["by_extract_status"]["secret_suspected"] == 1
    # unchanged bytes are not re-scanned on the next pass
    built.indexer.index_path(str(p))
    assert drain(built)[0][2] == "unchanged"


def test_credential_named_files_are_excluded_by_default(cfg, root):
    from semsearch.security import is_excluded
    for name in ("client_secret_1234.json", ".env", ".env.local", "id_rsa", "server.pem", "my_api_key.txt", ".ssh/known_hosts"):
        assert is_excluded(str(root / name), cfg.excludes), name
    assert not is_excluded(str(root / "notes.md"), cfg.excludes)
    # bare name patterns never match directory names: a folder about credentials is still indexed
    assert not is_excluded(str(root / "credential-docs" / "policy.md"), cfg.excludes)
    assert not is_excluded(str(root / "secrets"), cfg.excludes, is_dir=True)
    assert is_excluded(str(root / "credential-docs" / "client_secret.json"), cfg.excludes)


def test_dotfile_extension():
    assert file_extension(r"F:\x\.gitignore") == ".gitignore"
    assert file_extension(r"F:\x\.env.local") == ".local"
    assert file_extension(r"F:\x\README.MD") == ".md"
    assert file_extension(r"F:\x\Makefile") == ""


@pytest.mark.skipif(os.name != "nt", reason="IFilter registry is Windows-only")
def test_isolated_extractor_keeps_native_chains_out_of_process(cfg):
    from semsearch.extract.isolated import IsolatedExtractor
    from semsearch.extract.registry import build_default_registry
    reg = build_default_registry(cfg)
    iso = IsolatedExtractor(cfg, reg)
    try:
        assert ".md" in iso._inproc_exts and ".py" in iso._inproc_exts
        assert ".pdf" not in iso._inproc_exts and ".docx" not in iso._inproc_exts
        if reg.extractors_for(".html") and reg.extractors_for(".html")[0].name == "ifilter":
            assert ".html" not in iso._inproc_exts
    finally:
        iso.close()


# ---- correctness ----

def test_filename_like_handles_underscores_and_percent(built, root):
    write(str(root / "test_indexer.py"), "x = 1\n")
    write(str(root / "100%done.md"), "# done\n")
    built.indexer.index_path(str(root))
    drain(built)
    assert [f for _, f in built.store.filename_like("test_indexer")] == ["test_indexer.py"]
    assert [f for _, f in built.store.filename_like("testXindexer")] == []      # '_' is not a wildcard
    assert [f for _, f in built.store.filename_like("100%done")] == ["100%done.md"]
    hit = built.retriever.search("test_indexer", "hybrid", 3)["results"][0]
    assert hit["filename"] == "test_indexer.py" and hit["scores"]["filename"] == 1.0


def test_throughput_survives_concurrent_appends(built):
    st = built.indexer.state
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            st.recent.append((time.time(), 1, 1))

    t = threading.Thread(target=hammer, daemon=True)
    t.start()
    try:
        for _ in range(300):
            st.throughput()
    finally:
        stop.set()
        t.join(2)


def test_bad_mtime_does_not_break_results():
    assert iso_timestamp(-11644473600.0) is None
    assert iso_timestamp(None) is None
    assert iso_timestamp(1700000000.0).startswith("2023-")
    h = SearchHit(path="x", filename="x", score=1.0, match_type="lexical", excerpt="", chunk_ordinal=0, modified=-11644473600.0,
                  file_type="md", size=1, components=ScoreComponents(), why=[], doc_id=1)
    assert h.to_dict()["modified"] is None


def test_tombstone_timestamp_is_cleared_on_reindex_and_set_on_vanish(built, root):
    p = root / "gpu.txt"
    data = p.read_bytes()
    p.unlink()
    built.indexer.reconcile()
    row = built.store.get_document(normalize_path(str(p)))
    assert row["extract_status"] == "missing" and row["missing_since"] is not None
    p.write_bytes(data + b"\nmore\n")  # comes back modified -> full re-index
    built.indexer.index_path(str(p))
    assert drain(built)[0][2] == "indexed"
    row = built.store.get_document(normalize_path(str(p)))
    assert row["extract_status"] == "ok" and row["missing_since"] is None
    # vanish again: a fresh timestamp, so the grace period restarts instead of purging at once
    p.unlink()
    t0 = time.time()
    built.indexer.reconcile()
    row = built.store.get_document(normalize_path(str(p)))
    assert row["missing_since"] >= t0 - 1
    assert built.store.purge_missing(3600) == 0


def test_file_deleted_between_stat_and_extract_is_tombstoned_not_zombied(built, root, monkeypatch):
    p = root / "gpu.txt"
    real = built.indexer.extractor.extract

    def vanish_then_extract(path, ext):
        os.unlink(path)
        return real(path, ext)

    monkeypatch.setattr(built.indexer.extractor, "extract", vanish_then_extract)
    p.write_text(p.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    built.indexer.index_path(str(p))
    assert drain(built)[0][2] == "missing"
    row = built.store.get_document(normalize_path(str(p)))
    assert row["extract_status"] == "missing" and row["missing_since"] is not None
    assert built.store.documents_needing_embedding(built.indexer.fingerprint) == []


def test_change_during_indexing_is_not_lost(store_factory):
    s = store_factory()
    s.enqueue("c:\\r\\a.md", "index", 1)
    j = s.next_job()
    s.enqueue("c:\\r\\a.md", "index", 2)  # a second save lands while the first is being processed
    s.complete_job(int(j["id"]))
    assert s.queue_stats()["pending"] == 1
    j2 = s.next_job()
    assert j2["path"] == "c:\\r\\a.md" and j2["dirty"] == 0
    s.complete_job(int(j2["id"]))
    assert s.queue_stats() == {"pending": 0, "running": 0, "failed": 0}


def test_watcher_added_directory_enqueues_its_files(built, root):
    newdir = root / "dropped"
    write(str(newdir / "a.md"), "# dropped a\n\ncontent alpha\n")
    write(str(newdir / "b" / "c.txt"), "content charlie\n")
    built.indexer._on_watch_event("changed", str(newdir), None)
    assert built.store.queue_stats()["pending"] == 2
    drain(built)
    assert built.retriever.search("content charlie", "literal", 1)["results"][0]["filename"] == "c.txt"


def test_prefix_removal_uses_range_scan(built, root):
    ids = built.store.paths_with_prefix(normalize_path(str(root / "sub")))
    assert len(ids) == 2
    assert built.store.paths_with_prefix(normalize_path(str(root / "su"))) == []  # not a sibling-prefix match
    r = built.indexer.remove_path(str(root / "sub"))
    assert r["removed"] == 2


def test_reconcile_refuses_mass_deletion_on_truncated_enumeration(built, root, cfg):
    cfg.indexing.reconcile_min_fraction = 0.5
    for i in range(120):
        write(str(root / "many" / f"f{i}.md"), f"doc {i}\n")
    built.indexer.index_path(str(root / "many"))
    drain(built)
    known = built.store.count_documents(normalize_path(str(root)))
    assert known >= 120
    # simulate an enumeration that saw almost nothing
    removed = built.indexer._reconcile_root(normalize_path(str(root)), {normalize_path(str(root / "gpu.txt"))})
    assert removed == 0
    assert built.store.stats()["documents_missing"] == 0
    assert built.store.recent_errors(1)[0]["stage"] == "reconcile"


def test_reembed_stale_queues_whole_backlog(built):
    from semsearch.embed.hashing import HashingProvider
    new = HashingProvider(dim=64)
    built.store.ensure_vectors(new.fingerprint, new.dim)
    built.indexer.embedder = new
    built.indexer.fingerprint = new.fingerprint
    n = built.indexer.reembed_stale(batch_docs=2)  # page size smaller than the backlog
    assert n == 5
    assert built.store.queue_stats()["pending"] == 5


def test_move_with_new_suffix_updates_extension_and_title(built, root):
    src = root / "gpu.txt"
    dst = root / "gpu_notes.md"
    os.rename(src, dst)
    built.indexer.index_path(str(dst))
    assert drain(built)[0][2] == "moved"
    row = built.store.get_document(normalize_path(str(dst)))
    assert row["extension"] == ".md" and row["title"] == "gpu notes"


def test_fs_poll_is_rate_limited_for_scheduler_but_not_manual(built, cfg, root):
    cfg.indexing.fs_poll_interval_s = 3600
    write(str(root / "later.md"), "# later\n\nnew content here\n")
    time.sleep(0.05)
    assert built.indexer.incremental(force=False)["enqueued"] >= 1   # first scheduler pass scans
    drain(built)
    write(str(root / "later2.md"), "# later2\n\nmore new content\n")
    assert built.indexer.incremental(force=False)["enqueued"] == 0   # within the interval: skipped
    assert built.indexer.incremental(force=True)["enqueued"] >= 1    # manual/API: scans


# ---- performance ----

def test_vector_cache_grows_amortized_and_stays_correct():
    c = VectorCache(4, capacity=2)
    rng = np.random.default_rng(0)
    allv = {}
    for i in range(0, 1000, 7):
        ids = list(range(i, i + 7))
        v = rng.normal(size=(7, 4)).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        c.add(ids, v)
        for cid, row in zip(ids, v):
            allv[cid] = row
    assert len(c) == 1001 - 1001 % 7 + 7 - 7 or len(c) == len(allv)
    c.remove(range(0, 500))
    for cid in range(0, 500):
        allv.pop(cid, None)
    assert len(c) == len(allv)
    q = allv[700]
    top = c.search(q, 3)
    assert top[0][0] == 700 and abs(top[0][1] - 1.0) < 1e-5
    assert all(cid >= 500 for cid, _ in top)
    # buffer grew from capacity 2 by doubling (bounded headroom), not by re-copying per add
    assert len(c.ids) >= c.n and len(c.ids) <= max(4096, 2 * c.n)


def test_health_is_cheap_and_reads_count(built, cfg, monkeypatch):
    calls = {"stats": 0}
    orig = built.store.stats

    def counting():
        calls["stats"] += 1
        return orig()

    monkeypatch.setattr(built.store, "stats", counting)
    with TestClient(create_app(cfg, state=built), base_url="http://127.0.0.1") as c:
        assert c.get("/health").json()["documents"] == 6
    assert calls["stats"] == 0


def test_set_vectors_on_existing_rowids(store_factory):
    from semsearch.models import Chunk
    s = store_factory()
    now = time.time()
    d = s.upsert_document(path="c:\\r\\a.txt", display_path="A", root="c:\\r", filename="a.txt", extension=".txt", size=1, mtime=now, ctime=now,
                          file_id="1", volume_serial="1", content_hash="h", extract_status="ok", extract_method="text", text_chars=1, indexed_at=now, last_seen=now, source="fs")
    v = np.eye(4, dtype=np.float32)[:1]
    ids = s.replace_chunks(d, [Chunk(0, 0, 1, "t")], v, s.fingerprint)
    s.set_vectors(ids, np.eye(4, dtype=np.float32)[1:2], d, s.fingerprint)  # same rowid again must not raise
    assert s.knn(np.eye(4, dtype=np.float32)[1], 1)[0][0] == ids[0]


def test_query_terms_are_capped():
    from semsearch.retrieval import Retriever
    assert len(Retriever.terms(" ".join(f"word{i}" for i in range(500)))) == Retriever.MAX_TERMS
