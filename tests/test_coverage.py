"""Option 2 coverage: every chunk is full-text indexed; prose and code are embedded in full,
data formats (JSON, CSV, logs, HTML, XML) only for their first `embed_chunks_data` chunks;
truncation is recorded and reported; upgrades re-extract only affected documents."""
import json
import time

import pytest

from conftest import drain, write
from semsearch.security import normalize_path


def _small_chunks(cfg, limit=5):
    cfg.chunking.target_chars = 200
    cfg.chunking.max_chars = 300
    cfg.chunking.overlap_chars = 20
    cfg.chunking.embed_chunks_data = limit


def _vectors_for(st, path):
    row = st.store.get_document(normalize_path(str(path)))
    ids = [int(c["id"]) for c in st.store.chunks_for_doc(int(row["id"]))]
    with_vec = {int(r[0]) for r in st.store.conn.execute(
        f"SELECT rowid FROM vec_chunks WHERE rowid IN ({','.join('?' * len(ids))})", ids)}
    return row, ids, with_vec


def test_data_file_is_full_text_everywhere_but_embedded_only_at_the_start(built, root, cfg):
    _small_chunks(cfg, limit=5)
    records = [{"id": i, "note": f"routine telemetry record number {i} with nothing special"} for i in range(400)]
    records[-1]["note"] = "the deepest record mentions xylophone maintenance"
    write(str(root / "dump.json"), json.dumps(records, indent=1))
    built.indexer.index_path(str(root / "dump.json"))
    assert [r for _, _, r in drain(built)] == ["indexed"]
    row, ids, with_vec = _vectors_for(built, root / "dump.json")
    assert row["n_chunks"] > 20 and row["n_embedded"] == 5
    assert with_vec == set(ids[:5])                                       # exactly the first five
    hit = built.retriever.search("xylophone maintenance", "literal", 3)["results"][0]
    assert hit["filename"] == "dump.json"                                 # the tail is searchable literally
    assert hit["coverage"]["embedded_chunks"] == 5 and "first 5" in hit["coverage"]["note"]
    assert built.store.stats()["documents_partially_embedded"] >= 1


def test_prose_is_embedded_past_the_old_400_chunk_cap(built, root, cfg):
    _small_chunks(cfg, limit=5)
    paras = [f"Paragraph {i} discusses topic number {i} in ordinary prose sentences that go on." for i in range(1500)]
    write(str(root / "book.md"), "# Book\n\n" + "\n\n".join(paras))
    built.indexer.index_path(str(root / "book.md"))
    drain(built)
    row, ids, with_vec = _vectors_for(built, root / "book.md")
    assert row["n_chunks"] > 400 and row["n_embedded"] == row["n_chunks"] and with_vec == set(ids)
    hit = built.retriever.search("Paragraph 1499", "literal", 1)["results"][0]
    assert hit["filename"] == "book.md" and "coverage" not in hit       # complete: no coverage note


def test_text_beyond_the_cap_is_flagged_and_reported(built, root, cfg):
    cfg.indexing.max_text_chars = 3000
    built.registry.chains[".txt"][0].max_chars = 3000   # the in-process text extractor was built with the old cap
    write(str(root / "long.txt"), ("lorem ipsum dolor sit amet " * 400) + " unreachable-tail-word")
    built.indexer.index_path(str(root / "long.txt"))
    drain(built)
    row = built.store.get_document(normalize_path(str(root / "long.txt")))
    assert row["text_truncated"] == 1
    assert any(e["stage"] == "limit" and e["path"].endswith("long.txt") for e in built.store.recent_errors(20))
    assert built.store.stats()["documents_text_truncated"] >= 1
    hit = built.retriever.search("lorem ipsum", "literal", 1)["results"][0]
    assert hit["coverage"]["text_truncated"] is True
    assert built.retriever.search("unreachable-tail-word", "literal", 1)["results"] == []


def test_reembed_keeps_only_the_embedded_prefix(built, root, cfg):
    _small_chunks(cfg, limit=4)
    write(str(root / "events.csv"), "\n".join(f"{i},event,{'x' * 120}" for i in range(300)))
    built.indexer.index_path(str(root / "events.csv"))
    drain(built)
    assert built.indexer._reembed(normalize_path(str(root / "events.csv"))) == "reembedded"
    row, ids, with_vec = _vectors_for(built, root / "events.csv")
    assert with_vec == set(ids[:4])


def test_secret_deep_in_a_large_text_is_screened(built, root):
    write(str(root / "big_notes.txt"), ("ordinary words " * 40_000) + "\nkey AKIAIOSFODNN7EXAMPLE\n")
    built.indexer.index_path(str(root / "big_notes.txt"))
    assert [r for _, _, r in drain(built)] == ["secret_suspected"]


def test_upgrade_reextracts_only_documents_the_old_caps_affected(built, root, cfg):
    _small_chunks(cfg, limit=5)
    write(str(root / "short.md"), "# short\n\nnothing long here\n")
    built.indexer.index_path(str(root / "short.md"))
    drain(built)
    # simulate an index written by 0.4.0: legacy preprocess identity (with the chunk cap), no
    # coverage key, and one document that had been cut at the old 400-chunk cap
    built.store.set_meta("policy:preprocess", built.indexer.preprocess_id() + ":400")
    built.store.set_meta("policy:coverage", None)
    long_n = normalize_path(str(root / "gpu.txt"))
    built.store.conn.execute("UPDATE documents SET n_chunks=400 WHERE path=?", (long_n,))
    built.store.clear_jobs()
    queued = built.indexer._check_preprocess_version()
    paths = {r[0] for r in built.store.conn.execute("SELECT path FROM jobs")}
    assert queued == 1 and paths == {long_n}                     # not the whole index
    assert built.store.get_meta("policy:preprocess") == built.indexer.preprocess_id()
    assert built.store.get_meta("policy:coverage") == built.indexer.coverage_id()
    assert built.indexer._check_preprocess_version() == 0        # idempotent


def test_lowering_the_data_embedding_limit_reextracts_long_data_files_only(built, root, cfg):
    _small_chunks(cfg, limit=50)
    write(str(root / "big.json"), json.dumps([{"i": i, "pad": "y" * 150} for i in range(200)], indent=1))
    write(str(root / "small.json"), json.dumps({"a": 1}))
    built.indexer.index_path(str(root))
    drain(built)
    built.indexer._check_preprocess_version()
    built.store.clear_jobs()
    cfg.chunking.embed_chunks_data = 3
    built.indexer._check_preprocess_version()
    paths = {r[0] for r in built.store.conn.execute("SELECT path FROM jobs")}
    assert normalize_path(str(root / "big.json")) in paths and normalize_path(str(root / "small.json")) not in paths


def test_schema_v2_database_gains_the_coverage_columns(tmp_path):
    import sqlite3
    from semsearch.store.db import SCHEMA_VERSION, Store
    p = tmp_path / "old.db"
    s = Store(p, vector_cache=False)
    s.conn.execute("ALTER TABLE documents DROP COLUMN n_embedded")
    s.conn.execute("ALTER TABLE documents DROP COLUMN text_truncated")
    s.set_meta("schema_version", "2")
    s.close()
    s = Store(p, vector_cache=False)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(documents)")}
    assert {"n_embedded", "text_truncated"} <= cols and s.get_meta("schema_version") == str(SCHEMA_VERSION)
    s.close()


def test_chunking_a_large_unbroken_text_is_linear(cfg):
    """A JSON dump is one "sentence" to the splitter; slicing the remainder after every cut was
    quadratic (50 MB: 78 s). Doubling the input must not much more than double the time."""
    from semsearch.chunking import chunk_text
    line = '{"id": 12345, "name": "record", "value": "abcdefghij", "flag": true},\n'

    def t(n):
        start = time.perf_counter()
        chunks = chunk_text(line * n, cfg.chunking, ".json")
        return time.perf_counter() - start, len(chunks)
    t1, c1 = t(60_000)
    t2, c2 = t(120_000)
    assert c2 >= 2 * c1 - 2
    assert t2 < 3.0 * t1 + 0.05, (t1, t2)      # quadratic would be ~4x
