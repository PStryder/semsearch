"""Regression tests for the second round of review findings (atomic writes, reconcile
recovery, exclusion enforcement, root scope)."""
import os
import time

import pytest

from conftest import drain, write
from semsearch.security import PathRejected, normalize_path


def test_failed_embedding_leaves_previous_version_intact_and_retry_recovers(built, root, monkeypatch):
    p = root / "gpu.txt"
    old = built.store.get_document(normalize_path(str(p)))
    old_hash, old_chunks = old["content_hash"], [c["text"] for c in built.store.chunks_for_doc(int(old["id"]))]
    p.write_text("Completely new text about tensor cores and HBM3 bandwidth.\n", encoding="utf-8")

    real = built.indexer.embedder.embed
    calls = {"n": 0}

    def flaky(texts, kind="document"):
        if kind == "document" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("simulated embedding failure")
        return real(texts, kind)

    monkeypatch.setattr(built.indexer.embedder, "embed", flaky)
    built.indexer.index_path(str(p))
    job = built.store.next_job()
    with pytest.raises(RuntimeError):
        built.indexer.process_job(job)
    built.store.fail_job(int(job["id"]), "boom", 3)
    # nothing about the document changed: old hash, old chunks still searchable
    row = built.store.get_document(normalize_path(str(p)))
    assert row["content_hash"] == old_hash
    assert [c["text"] for c in built.store.chunks_for_doc(int(row["id"]))] == old_chunks
    assert built.retriever.search("GPU memory architecture", "literal", 1)["results"][0]["filename"] == "gpu.txt"
    # the retry must NOT conclude 'unchanged'
    res = drain(built)
    assert res and res[0][2] == "indexed"
    row = built.store.get_document(normalize_path(str(p)))
    assert row["content_hash"] != old_hash
    assert built.retriever.search("HBM3 bandwidth", "literal", 1)["results"][0]["filename"] == "gpu.txt"
    assert built.retriever.search("memory hierarchy accelerators", "literal", 3)["results"] == [] or \
        all(h["filename"] != "gpu.txt" for h in built.retriever.search("memory hierarchy accelerators", "literal", 3)["results"])


def test_reconcile_enqueues_missed_additions_and_modifications(built, root):
    # a file created without any watcher/incremental noticing it
    write(str(root / "sub" / "orphan.md"), "# Orphan\n\nnobody told the indexer about this file\n")
    # a file modified behind the indexer's back
    time.sleep(0.05)
    (root / "gpu.txt").write_text("GPU text rewritten silently.\n", encoding="utf-8")
    r = built.indexer.reconcile()
    assert r["enqueued"] == 2
    res = {os.path.basename(p): out for _, p, out in drain(built)}
    assert res["orphan.md"] == "indexed" and res["gpu.txt"] == "indexed"
    # a clean second pass enqueues nothing
    assert built.indexer.reconcile()["enqueued"] == 0


def test_explicit_index_of_excluded_or_unsupported_file_is_refused(built, root, cfg):
    cfg.excludes.append("**/private/**")
    write(str(root / "private" / "diary.md"), "# diary\n\nvery private\n")
    with pytest.raises(PathRejected):
        built.indexer.index_path(str(root / "private" / "diary.md"))
    with pytest.raises(PathRejected):
        built.indexer.index_path(str(root / "private"))
    with pytest.raises(PathRejected):
        built.indexer.index_path(str(root / "bin.dat2"))  # unsupported extension (file need not exist for the policy check)
    # a job that sneaks in through the queue is rejected at processing time too
    built.store.enqueue(normalize_path(str(root / "private" / "diary.md")), "index", 1)
    assert drain(built)[0][2] == "rejected"
    assert built.store.get_document(normalize_path(str(root / "private" / "diary.md"))) is None
    assert built.retriever.search("very private", "literal", 3)["results"] == []


def test_newly_excluded_content_is_dropped_when_policy_changes(built, root, cfg):
    assert built.retriever.search("blackboard", "literal", 1)["results"][0]["filename"] == "blackboard.py"
    cfg.excludes.append("**/sub/**")
    r = built.indexer.enforce_scope()
    assert r["policy"] == 2
    assert built.retriever.search("blackboard", "literal", 1)["results"] == []


def test_removed_root_content_is_dropped_and_filtered(built, root, cfg, tmp_path):
    from semsearch.indexer import Indexer
    from semsearch.retrieval import Retriever
    other = tmp_path / "other_root"
    write(str(other / "elsewhere.md"), "# Elsewhere\n\ncontent about zebras\n")
    # index it under the current config by pretending it is a root
    cfg.roots.append(other)
    idx = Indexer(cfg, built.store, built.extractor, built.embedder, fs_inventory=built.fs)
    idx.fs.roots.append(str(other))
    idx.index_path(str(other / "elsewhere.md"))
    for _ in range(5):
        j = built.store.next_job()
        if not j:
            break
        idx.process_job(j)
    assert built.store.get_document(normalize_path(str(other / "elsewhere.md"))) is not None
    # now the user removes that root from the config and restarts
    cfg.roots.pop()
    idx2 = Indexer(cfg, built.store, built.extractor, built.embedder, fs_inventory=built.fs)
    retr = Retriever(cfg, built.store, built.embedder)
    # even before enforcement runs, retrieval never surfaces it
    assert all(h["filename"] != "elsewhere.md" for h in retr.search("zebras", "literal", 5)["results"])
    assert idx2.enforce_scope()["outside_roots"] == 1
    assert built.store.get_document(normalize_path(str(other / "elsewhere.md"))) is None


def test_write_document_is_atomic(store_factory):
    import numpy as np
    from semsearch.models import Chunk
    s = store_factory()
    now = time.time()
    fields = dict(path="c:\\r\\a.txt", display_path="A", root="c:\\r", filename="a.txt", extension=".txt", size=1, mtime=now, ctime=now,
                  file_id="1", volume_serial="1", content_hash="h1", extract_status="ok", extract_method="text", text_chars=1,
                  indexed_at=now, last_seen=now, source="fs", missing_since=None, title="t")
    s.write_document(fields, [Chunk(0, 0, 1, "first")], np.eye(4, dtype=np.float32)[:1], s.fingerprint)
    bad = dict(fields, content_hash="h2")
    with pytest.raises(AssertionError):
        s.write_document(bad, [Chunk(0, 0, 1, "second")], np.eye(4, dtype=np.float32)[:2], s.fingerprint)  # vector/chunk mismatch
    row = s.get_document("c:\\r\\a.txt")
    assert row["content_hash"] == "h1"
    assert [c["text"] for c in s.chunks_for_doc(int(row["id"]))] == ["first"]
    assert s.stats()["vectors"] == 1
