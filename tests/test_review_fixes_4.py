"""Regression tests for the fourth (distribution) review: rename refreshes the vector-reuse key
and heading titles; screening policy identity covers the exemption list and disabling it
restores documents; filters are applied inside candidate collection; junction roots and
stale reparse caches; files that change while being indexed; DirectML run serialization;
backup confinement; opaque 500s; /document paging; Office container bounds; preprocessing
identity; local model fingerprints; HF_HOME authority; watcher/extractor shutdown; read token."""
import os
import subprocess
import sys
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app
from semsearch.indexer import derive_title
from semsearch.security import PathRejected, check_indexable, normalize_path, reset_reparse_cache, walk_safe
from semsearch.store.db import text_hash

WIN = sys.platform == "win32"


def _mklink_j(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"mklink failed: {r.stderr or r.stdout}")


# ---------------------------------------------------------------- rename: reuse key and heading titles

def test_rename_rewrites_chunk_reuse_hashes(built, root):
    src, dst = root / "gpu.txt", root / "renamed_gpu.txt"
    os.rename(src, dst)
    built.indexer.index_path(str(dst))
    assert [r for _, _, r in drain(built)] == ["moved", "reembedded"]
    row = built.store.get_document(normalize_path(str(dst)))
    for c in built.store.chunks_for_doc(int(row["id"])):
        assert c["text_hash"] == text_hash(f"{row['title']}\n\n{c['text']}")  # key == what the vector represents


def test_heading_title_takes_the_new_file_name_on_rename(built, root):
    src, dst = root / "agents.md", root / "new_agents.md"
    os.rename(src, dst)
    built.indexer.index_path(str(dst))
    assert [r for _, _, r in drain(built)] == ["moved", "reembedded"]
    row = built.store.get_document(normalize_path(str(dst)))
    assert row["title"] == derive_title(dst.read_text(encoding="utf-8"), str(dst)) == "Agent safety (new agents)"


def test_rename_without_title_change_does_not_reembed(built, root):
    # README-style names derive from the parent folder; the heading wins anyway, so same title
    write(str(root / "proj" / "README.md"), "# Project Zeta\n\nabout zeta\n")
    built.indexer.index_path(str(root / "proj"))
    drain(built)
    os.rename(root / "proj" / "README.md", root / "proj" / "readme.md")  # case-only rename: same normalized path
    os.rename(root / "proj" / "readme.md", root / "proj" / "README.md")
    os.rename(root / "proj", root / "proj2")
    built.indexer.index_path(str(root / "proj2"))
    res = [r for _, _, r in drain(built)]
    assert res == ["moved", "reembedded"]  # parent folder is part of a generic-name title: changed => refreshed
    row = built.store.get_document(normalize_path(str(root / "proj2" / "README.md")))
    assert row["title"] == "Project Zeta (proj2 README)"


# ---------------------------------------------------------------- secret screening policy identity

AWS_EXAMPLE = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\nnothing else here\n"


def test_disabling_screening_restores_quarantined_documents(built, root, cfg):
    p = root / "example.txt"
    write(str(p), AWS_EXAMPLE)
    built.indexer.index_path(str(p))
    assert [r for _, _, r in drain(built)] == ["secret_suspected"]
    built.indexer.enforce_secret_policy()  # records the current policy
    cfg.indexing.skip_suspected_secrets = False
    assert built.indexer.enforce_secret_policy() == 1
    assert [r for _, _, r in drain(built)] == ["indexed"]
    assert built.store.get_document(normalize_path(str(p)))["extract_status"] == "ok"
    assert built.retriever.search("AKIAIOSFODNN7EXAMPLE", "literal", 1)["results"][0]["filename"] == "example.txt"


def test_removing_an_exemption_rescans_and_adding_one_restores(built, root, cfg):
    p = root / "example.txt"
    write(str(p), AWS_EXAMPLE)
    cfg.indexing.secret_scan_allow = ["example.txt"]
    built.indexer.enforce_secret_policy()
    built.indexer.index_path(str(p))
    assert [r for _, _, r in drain(built)] == ["indexed"]
    # exemption removed: the unchanged file must be re-screened from stored text
    cfg.indexing.secret_scan_allow = []
    assert built.indexer.enforce_secret_policy() == 1
    assert built.store.get_document(normalize_path(str(p)))["extract_status"] == "secret_suspected"
    assert built.retriever.search("AKIAIOSFODNN7EXAMPLE", "literal", 1)["results"] == []
    # exemption added back: the quarantined file is re-queued and comes back
    cfg.indexing.secret_scan_allow = ["example.txt"]
    assert built.indexer.enforce_secret_policy() == 1
    assert [r for _, _, r in drain(built)] == ["indexed"]
    assert built.store.get_document(normalize_path(str(p)))["extract_status"] == "ok"


def test_policy_id_changes_with_exemptions(built, cfg):
    a = built.indexer.secret_policy_id()
    cfg.indexing.secret_scan_allow = ["x.txt"]
    b = built.indexer.secret_policy_id()
    cfg.indexing.skip_suspected_secrets = False
    assert a != b and a.startswith("on:") and built.indexer.secret_policy_id() == "off"


# ---------------------------------------------------------------- filters inside candidate collection

def test_filtered_search_is_exact_at_minimum_candidate_pool(built, root, cfg):
    cfg.retrieval.candidate_chunks = 1
    cfg.retrieval.candidate_docs = 1
    cfg.retrieval.use_windows_rank = False
    for i in range(12):
        write(str(root / "many" / f"strong{i}.txt"), f"zebrafish zebrafish zebrafish zebrafish {i}\n")
    write(str(root / "rare" / "weak.md"), "a single zebrafish mention in the rare folder\n")
    built.indexer.index_path(str(root))
    drain(built)
    for mode in ("literal", "semantic", "hybrid"):
        r = built.retriever.search("zebrafish", mode, 5, roots=[str(root / "rare")])
        assert [h["filename"] for h in r["results"]] == ["weak.md"], mode
    r = built.retriever.search("zebrafish", "hybrid", 5, extensions=["md"])
    names = [h["filename"] for h in r["results"]]
    assert names[0] == "weak.md" and all(n.endswith(".md") for n in names)  # the 12 strong .txt files never crowd it out
    # a glob respects filters too
    r = built.retriever.search("*.md", "literal", 50, roots=[str(root / "rare")])
    assert [h["filename"] for h in r["results"]] == ["weak.md"]


# ---------------------------------------------------------------- containment: junction roots, cache ttl, swaps

@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_junction_root_is_not_a_boundary(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("x")
    link = tmp_path / "root"
    _mklink_j(link, outside)
    reset_reparse_cache()
    with pytest.raises(PathRejected):
        check_indexable(str(link / "a.txt"), [str(link)], follow_reparse=False)
    assert list(walk_safe(str(link), [str(link)], [])) == []
    # opting in to follow still requires the resolved target to be inside a configured root
    with pytest.raises(PathRejected):
        check_indexable(str(link / "a.txt"), [str(link)], follow_reparse=True)
    assert check_indexable(str(link / "a.txt"), [str(link), str(outside)], follow_reparse=True).size == 1


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_reparse_cache_is_time_bounded(tmp_path, monkeypatch):
    import semsearch.security as sec
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("x")
    reset_reparse_cache()
    assert check_indexable(str(sub / "a.txt"), [str(root)]).size == 1  # caches sub as a plain directory
    # the directory is replaced by a junction to somewhere else
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.txt").write_text("y")
    (sub / "a.txt").unlink()
    sub.rmdir()
    _mklink_j(sub, elsewhere)
    monkeypatch.setattr(sec, "REPARSE_CACHE_TTL_S", 0.0)
    with pytest.raises(PathRejected):
        check_indexable(str(sub / "a.txt"), [str(root)])


def test_file_changed_during_indexing_is_requeued(built, root):
    p = root / "volatile.txt"
    write(str(p), "firstly the volatile file\n")
    real_extract = built.indexer.extractor.extract
    calls = {"n": 0}

    def swapping_extract(path, ext):
        res = real_extract(path, ext)
        if calls["n"] == 0:
            calls["n"] += 1
            time.sleep(0.02)
            write(str(p), "secondly, written while extraction ran\n")
        return res

    built.indexer.extractor.extract = swapping_extract
    built.indexer.index_path(str(p))
    res = [r for _, _, r in drain(built)]
    assert res == ["changed_during_index", "indexed"]
    assert built.retriever.search("secondly", "literal", 1)["results"][0]["filename"] == "volatile.txt"
    assert built.retriever.search("firstly", "literal", 1)["results"] == []


def test_wanted_uses_live_size_when_inventory_is_stale(built, root, cfg):
    from semsearch.models import FileEntry
    p = root / "shrunk.txt"
    write(str(p), "small now\n")
    fe = FileEntry(path=str(p), size=cfg.indexing.max_file_bytes + 1, mtime=time.time(), source="windows_search", extension=".txt")
    assert built.indexer._wanted(fe) is True
    fe_missing = FileEntry(path=str(root / "nope.txt"), size=cfg.indexing.max_file_bytes + 1, mtime=time.time(), source="windows_search", extension=".txt")
    assert built.indexer._wanted(fe_missing) is False


# ---------------------------------------------------------------- DirectML run serialization

def test_gpu_sessions_get_one_run_lock_each():
    from semsearch.embed.onnx_provider import OnnxProvider
    p = OnnxProvider.__new__(OnnxProvider)
    p._lock = threading.Lock()
    p._run_locks = {}
    assert p._run_lock_for(("cpu", 0)) is None
    a = p._run_lock_for(("dml", 0))
    assert a is p._run_lock_for(("dml", 0)) and a is not p._run_lock_for(("dml", 1))


# ---------------------------------------------------------------- API: opaque errors, /document paging, read token

def test_unhandled_errors_do_not_leak_exception_text(built, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret path C:\\Users\\someone\\private.txt")
    monkeypatch.setattr(built.retriever, "search", boom)
    with TestClient(create_app(built.cfg, state=built), base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        r = c.get("/search?q=x")
        assert r.status_code == 500
        assert "private.txt" not in r.text and r.json()["error"] == "internal error" and len(r.json()["ref"]) == 8


def test_document_chunks_are_paged(built, root):
    write(str(root / "long.md"), "# Long\n\n" + "\n\n".join(f"paragraph {i} " + ("word " * 300) for i in range(12)) + "\n")
    built.indexer.index_path(str(root / "long.md"))
    drain(built)
    with TestClient(create_app(built.cfg, state=built), base_url="http://127.0.0.1") as c:
        d = c.get("/document", params={"path": str(root / "long.md"), "chunks": True, "limit": 2}).json()
        assert d["chunk_count"] > 2 and len(d["chunks"]) == 2 and d["chunks"][0]["ordinal"] == 0
        d2 = c.get("/document", params={"path": str(root / "long.md"), "chunks": True, "limit": 2, "offset": 2}).json()
        assert d2["chunks"][0]["ordinal"] == 2


def test_read_token_gates_reads_but_not_health(built, cfg):
    cfg.api.read_token = True
    app = create_app(cfg, state=built)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/health").status_code == 200
        for path in ("/search?q=gpu", "/status", "/stats", "/errors", "/document?path=x"):
            assert c.get(path).status_code == 403, path
        assert c.post("/search", json={"query": "gpu"}).status_code == 403
    with TestClient(app, base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as c:
        assert c.get("/search?q=gpu").status_code == 200 and c.get("/status").status_code == 200
    cfg.api.read_token = False


# ---------------------------------------------------------------- Office container bounds

def test_office_container_expansion_limit(tmp_path):
    from semsearch.extract.office import DocxExtractor, zip_expanded_size
    p = tmp_path / "big.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document>" + ("A" * 2_000_000) + "</w:document>")
    assert zip_expanded_size(str(p)) > 2_000_000
    r = DocxExtractor(max_expanded=1_000_000).extract(str(p), ".docx")
    assert r.status == "too_large" and "expands" in r.error


def test_docx_text_is_capped_while_collecting(tmp_path):
    docx = pytest.importorskip("docx")
    p = tmp_path / "many.docx"
    d = docx.Document()
    for i in range(200):
        d.add_paragraph(f"paragraph number {i} with some filler words in it")
    d.save(str(p))
    from semsearch.extract.office import DocxExtractor
    r = DocxExtractor(max_chars=500).extract(str(p), ".docx")
    assert r.status == "ok" and len(r.text) <= 500 and r.meta.get("truncated") is True
    full = DocxExtractor().extract(str(p), ".docx")
    assert "paragraph number 199" in full.text and not full.meta.get("truncated")


# ---------------------------------------------------------------- preprocessing identity

def test_chunking_change_reextracts_everything(built, cfg):
    n = built.store.count_documents()
    assert built.indexer._check_preprocess_version() == 0  # unchanged: nothing to do
    cfg.chunking.target_chars = cfg.chunking.target_chars - 100
    queued = built.indexer._check_preprocess_version()
    assert queued == n
    res = [r for _, _, r in drain(built)]
    assert res and all(r in ("indexed", "empty", "binary") for r in res) and "unchanged" not in res and "touched" not in res
    assert built.indexer._check_preprocess_version() == 0


# ---------------------------------------------------------------- model identity

def test_local_model_dir_fingerprint_follows_file_contents(tmp_path):
    from semsearch.embed.onnx_provider import _resolve_model_files
    d = tmp_path / "model"
    d.mkdir()
    (d / "model.onnx").write_bytes(b"\x00" * 100)
    (d / "tokenizer.json").write_text("{}")
    _, _, rev1 = _resolve_model_files(str(d), None, False)
    (d / "model.onnx").write_bytes(b"\x01" * 100)
    _, _, rev2 = _resolve_model_files(str(d), None, False)
    assert rev1.startswith("local-") and rev2.startswith("local-") and rev1 != rev2


def test_bundled_model_layout_is_resolved_with_the_commit_as_revision(tmp_path):
    from semsearch.embed.onnx_provider import _resolve_model_files, bundled_model_dir
    commit = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    snap = tmp_path / "bundled" / "BAAI--bge-small-en-v1.5" / commit
    (snap / "onnx").mkdir(parents=True)
    (snap / "onnx" / "model.onnx").write_bytes(b"x")
    (snap / "tokenizer.json").write_text("{}")
    assert bundled_model_dir("BAAI/bge-small-en-v1.5", commit, tmp_path) == snap
    assert bundled_model_dir("BAAI/bge-small-en-v1.5", None, tmp_path) == snap  # single snapshot
    assert bundled_model_dir("BAAI/bge-small-en-v1.5", "other", tmp_path) is None
    onnx, tok, rev = _resolve_model_files("BAAI/bge-small-en-v1.5", commit, False, cache_dir=tmp_path)
    assert rev == commit and onnx == snap / "onnx" / "model.onnx"


def test_configured_model_cache_overrides_inherited_hf_home(cfg, monkeypatch):
    from semsearch.app_state import AppState
    monkeypatch.setenv("HF_HOME", r"C:\somebody\else")
    st = AppState(cfg, start_indexer=False, isolate_extractors=False)
    try:
        assert os.environ["HF_HOME"] == str(cfg.model_cache_dir)
    finally:
        st.close()


# ---------------------------------------------------------------- shutdown pieces

@pytest.mark.skipif(not WIN, reason="ReadDirectoryChangesW watcher")
def test_watcher_stop_joins_its_threads(tmp_path):
    from semsearch.watcher import DirectoryWatcher
    w = DirectoryWatcher([str(tmp_path)], lambda *a: None)
    w.start()
    time.sleep(0.3)
    t0 = time.time()
    assert w.stop(timeout_s=3.0) is True
    assert time.time() - t0 < 3.0 and not w.is_alive()


def test_isolated_extractor_close_does_not_wait_for_a_held_lock(cfg):
    from semsearch.extract.isolated import IsolatedExtractor
    from semsearch.extract.registry import build_default_registry
    ex = IsolatedExtractor(cfg, build_default_registry(cfg), timeout_s=5)
    ex._lock.acquire()  # simulate a worker blocked inside extract()
    try:
        t0 = time.time()
        ex.close(timeout_s=0.2)
        assert time.time() - t0 < 1.0
    finally:
        ex._lock.release()


def test_indexer_stop_reports_clean_shutdown(built):
    built.indexer.start()
    time.sleep(0.2)
    assert built.indexer.stop(timeout=10) is True
