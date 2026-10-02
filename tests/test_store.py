import time

import numpy as np
import pytest

from semsearch.models import Chunk
from semsearch.store.db import Store, text_hash


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db", vector_cache=True)
    s.ensure_vectors("test:model:1:4:cls", 4)
    yield s
    s.close()


def doc(store, path="c:\\r\\a.txt", **kw):
    now = time.time()
    f = dict(path=path, display_path=path.upper(), root="c:\\r", filename="a.txt", extension=".txt", size=3, mtime=now, ctime=now,
             file_id="1", volume_serial="7", content_hash="h", extract_status="ok", extract_method="text", text_chars=3,
             indexed_at=now, last_seen=now, source="fs")
    f.update(kw)
    return store.upsert_document(**f)


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def test_upsert_is_idempotent_on_path(store):
    a = doc(store)
    b = doc(store, size=99)
    assert a == b
    assert store.get_document("c:\\r\\a.txt")["size"] == 99


def test_chunks_fts_and_vectors_roundtrip(store):
    d = doc(store)
    chunks = [Chunk(0, 0, 10, "alpha bravo charlie"), Chunk(1, 10, 20, "delta echo foxtrot")]
    vecs = np.vstack([unit(1, 0, 0, 0), unit(0, 1, 0, 0)])
    ids = store.replace_chunks(d, chunks, vecs, store.fingerprint)
    assert len(ids) == 2
    hits = store.fts('"bravo"', 10)
    assert hits and hits[0][1] == d
    knn = store.knn(unit(0, 1, 0, 0), 2)
    assert knn[0][0] == ids[1] and knn[0][1] > 0.99
    # sqlite-vec path agrees with the cache path
    store.cache = None
    knn2 = store.knn(unit(0, 1, 0, 0), 2)
    assert knn2[0][0] == ids[1] and abs(knn2[0][1] - 1.0) < 1e-5
    assert store.get_document_by_id(d)["n_chunks"] == 2


def test_replace_chunks_removes_old_fts_and_vectors(store):
    d = doc(store)
    store.replace_chunks(d, [Chunk(0, 0, 5, "oldword here")], unit(1, 0, 0, 0)[None, :], store.fingerprint)
    store.replace_chunks(d, [Chunk(0, 0, 5, "newword here")], unit(0, 0, 1, 0)[None, :], store.fingerprint)
    assert store.fts('"oldword"', 10) == []
    assert store.fts('"newword"', 10)
    assert len(store.knn(unit(1, 0, 0, 0), 5)) == 1
    assert store.stats()["vectors"] == 1


def test_remove_document_cascades(store):
    d = doc(store)
    store.replace_chunks(d, [Chunk(0, 0, 5, "gone soon")], unit(1, 0, 0, 0)[None, :], store.fingerprint)
    assert store.remove_document("c:\\r\\a.txt")
    assert store.fts('"gone"', 10) == []
    assert store.knn(unit(1, 0, 0, 0), 5) == []
    assert store.stats()["chunks"] == 0 and store.stats()["vectors"] == 0
    assert not store.remove_document("c:\\r\\a.txt")


def test_vectors_for_hashes_reuses_existing(store):
    d = doc(store)
    store.replace_chunks(d, [Chunk(0, 0, 5, "shared text")], unit(0, 1, 1, 0)[None, :], store.fingerprint)
    got = store.vectors_for_hashes([text_hash("shared text"), text_hash("other")])
    assert set(got) == {text_hash("shared text")}
    assert np.allclose(got[text_hash("shared text")], unit(0, 1, 1, 0), atol=1e-6)


def test_model_change_resets_vectors_and_marks_docs_stale(store):
    d = doc(store)
    store.replace_chunks(d, [Chunk(0, 0, 5, "keep my chunks")], unit(1, 0, 0, 0)[None, :], store.fingerprint)
    assert store.get_document_by_id(d)["embedding_fingerprint"] == "test:model:1:4:cls"
    action = store.ensure_vectors("test:model:2:3:mean", 3)
    assert action == "reset"
    assert store.dim == 3 and store.fingerprint == "test:model:2:3:mean"
    assert store.stats()["vectors"] == 0
    assert store.stats()["chunks"] == 1  # chunk text retained for re-embedding
    assert store.get_document_by_id(d)["embedding_fingerprint"] is None
    assert [r["id"] for r in store.documents_needing_embedding("test:model:2:3:mean")] == [d]


def test_model_change_refuse_policy(store):
    with pytest.raises(RuntimeError):
        store.ensure_vectors("other:model:1:4:cls", 4, on_change="refuse")
    assert store.ensure_vectors("test:model:1:4:cls", 4, on_change="refuse") == "ok"


def test_job_queue_priority_and_recovery(store):
    store.enqueue("c:\\r\\low.txt", "index", 5)
    store.enqueue("c:\\r\\high.txt", "index", 1)
    store.enqueue("c:\\r\\low.txt", "index", 9)  # keeps the better (lower) priority
    j = store.next_job()
    assert j["path"] == "c:\\r\\high.txt" and j["state"] == "running"
    assert store.queue_stats() == {"pending": 1, "running": 1, "failed": 0}
    assert store.requeue_running() == 1
    assert store.queue_stats()["running"] == 0
    j = store.next_job()  # attempts is now 2: a crash mid-job counts as an attempt
    assert j["attempts"] == 2
    store.fail_job(int(j["id"]), "boom", max_attempts=3)
    assert store.queue_stats()["pending"] == 2  # attempts=2 < 3 -> pending again
    j = store.next_job()
    store.fail_job(int(j["id"]), "boom", max_attempts=3)
    assert store.queue_stats()["failed"] == 1
    assert store.retry_failed() == 1
    assert store.queue_stats()["failed"] == 0


def test_enqueue_running_job_keeps_running_state(store):
    store.enqueue("c:\\r\\x.txt", "index", 5)
    j = store.next_job()
    store.enqueue("c:\\r\\x.txt", "remove", 1)
    row = store.conn.execute("SELECT state, op FROM jobs WHERE path=?", ("c:\\r\\x.txt",)).fetchone()
    assert row["state"] == "running" and row["op"] == "remove"
    store.complete_job(int(j["id"]))


def test_filename_search(store):
    doc(store, path="c:\\r\\design_notes.md", filename="design_notes.md", extension=".md")
    doc(store, path="c:\\r\\paper.pdf", filename="paper.pdf", extension=".pdf")
    assert [f for _, f in store.filename_like("design")] == ["design_notes.md"]
    assert [f for _, f in store.filename_glob("*.pdf")] == ["paper.pdf"]
    assert [f for _, f in store.filename_glob("*.PDF")] == ["paper.pdf"]


def test_errors_and_wipe(store):
    d = doc(store)
    store.replace_chunks(d, [Chunk(0, 0, 5, "x y z")], unit(1, 0, 0, 0)[None, :], store.fingerprint)
    store.record_error("p", "extract", "bad")
    assert store.recent_errors(5)[0]["stage"] == "extract"
    store.set_meta("checkpoint:c:\\r", "1.0")
    store.wipe()
    s = store.stats()
    assert s["documents"] == 0 and s["chunks"] == 0 and s["vectors"] == 0 and s["errors"] == 0
    assert store.get_meta("checkpoint:c:\\r") is None
    assert store.fingerprint == "test:model:1:4:cls"
