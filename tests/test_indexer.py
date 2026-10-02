import os
import shutil
import time

import pytest

from conftest import drain, write
from semsearch.security import normalize_path


def counters(app):
    return dict(app.indexer.status()["counters"])


def test_full_build_indexes_wanted_files_only(built):
    s = built.store.stats()
    assert s["by_extract_status"]["ok"] == 5
    assert s["by_extract_status"]["binary"] == 1
    assert s["documents"] == 6
    assert s["vectors"] == s["chunks"] > 0
    assert built.store.queue_stats() == {"pending": 0, "running": 0, "failed": 0}
    assert built.indexer.status()["sources"]  # root -> source recorded


def test_unchanged_files_are_not_re_embedded(built):
    before = counters(built)
    built.indexer.full_build()
    results = drain(built)
    assert results and all(r == "unchanged" for _, _, r in results)
    after = counters(built)
    assert after["chunks_embedded"] == before["chunks_embedded"]
    assert after["indexed"] == before["indexed"]


def test_touch_only_when_mtime_changes_but_content_same(built, root):
    p = root / "gpu.txt"
    content = p.read_text(encoding="utf-8")
    time.sleep(0.05)
    p.write_text(content, encoding="utf-8")  # new mtime, same bytes
    before = counters(built)
    built.indexer.index_path(str(p))
    res = drain(built)
    assert res[0][2] == "touched"
    assert counters(built)["chunks_embedded"] == before["chunks_embedded"]


def test_modified_file_re_embeds_only_new_chunks(built, root):
    p = root / "agents.md"
    before = counters(built)
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n\nA brand new paragraph about rollback plans that did not exist before.\n")
    built.indexer.index_path(str(p))
    res = drain(built)
    assert res[0][2] == "indexed"
    after = counters(built)
    assert after["chunks_embedded"] > before["chunks_embedded"]
    assert after["chunks_reused"] >= before["chunks_reused"]
    hit = built.retriever.search("rollback plans", "literal", 3)["results"][0]
    assert hit["filename"] == "agents.md"


def test_rename_is_detected_as_move_without_re_embedding(built, root):
    # a move that keeps the file name keeps the title header, so nothing needs re-embedding
    # (a rename that changes the name queues a re-embed: see test_review_fixes_3)
    src = root / "gpu.txt"
    dst = root / "sub" / "gpu.txt"
    before = counters(built)
    os.rename(src, dst)
    built.indexer.reconcile()  # tombstones the old path; the move below must still reclaim its vectors
    built.indexer.index_path(str(dst))
    res = drain(built)
    assert any(r == "moved" for _, _, r in res), res
    after = counters(built)
    assert after["moved"] == before["moved"] + 1
    assert after["chunks_embedded"] == before["chunks_embedded"]
    assert built.store.get_document(normalize_path(str(src))) is None  # row was re-pointed, not duplicated
    d = built.store.get_document(normalize_path(str(dst)))
    assert d is not None and d["n_chunks"] > 0
    assert built.retriever.search("GPU memory architecture", "semantic", 1)["results"][0]["path"].lower() == str(dst).lower()


def test_deleted_files_are_tombstoned_then_purged(built, root):
    (root / "notes.json").unlink()
    assert built.indexer.reconcile()["removed"] == 1
    row = built.store.get_document(normalize_path(str(root / "notes.json")))
    assert row is not None and row["extract_status"] == "missing"
    # tombstoned documents never surface in search results
    assert "notes.json" not in [h["filename"] for h in built.retriever.search("quarterly budget", "hybrid", 10)["results"]]
    (root / "gpu.txt").unlink()
    built.indexer.index_path(str(root / "gpu.txt"))
    res = drain(built)
    assert res[0][2] == "missing"
    assert built.store.get_document(normalize_path(str(root / "gpu.txt")))["extract_status"] == "missing"
    assert built.store.stats()["documents_missing"] == 2
    # after the grace period the next reconcile purges them for real
    assert built.store.purge_missing(0.0) == 2
    assert built.store.get_document(normalize_path(str(root / "gpu.txt"))) is None
    assert built.store.stats()["vectors"] == built.store.stats()["chunks"]


def test_file_restored_at_same_path_is_revived_without_re_embedding(built, root):
    p = root / "gpu.txt"
    data = p.read_bytes()
    st = p.stat()
    p.unlink()
    built.indexer.reconcile()
    assert built.store.get_document(normalize_path(str(p)))["extract_status"] == "missing"
    p.write_bytes(data)
    os.utime(p, (st.st_atime, st.st_mtime))
    before = counters(built)
    built.indexer.index_path(str(p))
    res = drain(built)
    assert res[0][2] == "restored"
    assert counters(built)["chunks_embedded"] == before["chunks_embedded"]
    assert built.retriever.search("GPU memory architecture", "semantic", 1)["results"][0]["filename"] == "gpu.txt"


def test_incremental_picks_up_new_and_changed_files(built, root):
    time.sleep(0.05)
    write(str(root / "sub" / "fresh.md"), "# Fresh\n\nA freshly created note about capacity planning.\n")
    r = built.indexer.incremental()
    assert r["enqueued"] >= 1
    res = drain(built)
    assert any(p.endswith("fresh.md") and r == "indexed" for _, p, r in res)
    # second incremental pass has nothing new to do
    r2 = built.indexer.incremental()
    drain(built)
    assert r2["enqueued"] == 0


def test_paths_outside_roots_are_rejected(built, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    from semsearch.security import PathRejected
    with pytest.raises(PathRejected):
        built.indexer.index_path(str(outside))
    built.store.enqueue(normalize_path(str(outside)), "index", 1)
    res = drain(built)
    assert res[0][2] == "rejected"
    assert built.store.get_document(normalize_path(str(outside))) is None
    assert built.store.recent_errors(1)[0]["stage"] == "policy"


def test_too_large_file_is_recorded_not_indexed(built, root, cfg):
    cfg.indexing.max_file_bytes = 100
    big = root / "big.txt"
    big.write_text("x" * 1000)
    built.indexer.index_path(str(big))
    res = drain(built)
    assert res[0][2] == "too_large"
    d = built.store.get_document(normalize_path(str(big)))
    assert d["extract_status"] == "too_large" and d["n_chunks"] == 0


def test_remove_path_directory(built, root):
    r = built.indexer.remove_path(str(root / "sub"))
    assert r["removed"] == 2
    assert built.store.get_document(normalize_path(str(root / "sub" / "blackboard.py"))) is None


def test_model_change_triggers_reembed_from_stored_chunks(built, cfg):
    from semsearch.embed.hashing import HashingProvider
    new = HashingProvider(dim=64)
    assert built.store.ensure_vectors(new.fingerprint, new.dim) == "reset"
    built.indexer.embedder = new
    built.indexer.fingerprint = new.fingerprint
    built.retriever.embedder = new
    assert built.store.stats()["vectors"] == 0
    assert built.retriever.search("GPU memory", "semantic", 3)["results"] == []
    n = built.indexer.reembed_stale()
    assert n == 5
    res = drain(built)
    assert all(r == "reembedded" for _, _, r in res)
    s = built.store.stats()
    assert s["vectors"] == s["chunks"] and s["documents_awaiting_embedding"] == 0
    assert built.retriever.search("GPU memory architecture", "semantic", 1)["results"][0]["filename"] == "gpu.txt"


def test_resume_after_crash_requeues_running_jobs(built, root):
    built.store.enqueue(normalize_path(str(root / "gpu.txt")), "index", 1)
    j = built.store.next_job()
    assert j["state"] == "running"
    # simulate restart
    built.indexer.start()
    time.sleep(1.5)
    built.indexer.stop()
    assert built.store.queue_stats()["running"] == 0
    assert built.store.queue_stats()["pending"] == 0


def test_root_added_to_config_is_built_at_next_start(built, cfg, tmp_path):
    from semsearch.indexer import Indexer
    extra = tmp_path / "extra_root"
    write(str(extra / "late.md"), "# Late root\n\ncontent from a root added after the first build\n")
    cfg.roots.append(extra)
    cfg.indexing.poll_interval_s = 0.2
    idx = Indexer(cfg, built.store, built.extractor, built.embedder, fs_inventory=built.fs)
    idx.fs.roots.append(str(extra))
    assert built.store.get_meta("last_full_build_at") is not None  # a previous build exists
    idx.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline and built.store.get_document(normalize_path(str(extra / "late.md"))) is None:
            time.sleep(0.2)
    finally:
        idx.stop()
    assert built.store.get_document(normalize_path(str(extra / "late.md"))) is not None
    assert built.store.get_meta(f"checkpoint:{normalize_path(str(extra))}") is not None


def test_background_worker_processes_jobs(app, root):
    app.indexer.start()
    try:
        app.indexer.request_full_build()
        deadline = time.time() + 20
        while time.time() < deadline:
            s = app.store.stats()
            if s["documents"] >= 6 and s["queue"]["pending"] == 0 and s["queue"]["running"] == 0 and not app.indexer.state.full_build_in_progress:
                break
            time.sleep(0.2)
        assert app.store.stats()["documents"] == 6
    finally:
        app.indexer.stop()
