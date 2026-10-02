"""Tests for the improvement pass: duplicate collapse (presentation only), failed-job
visibility, adaptive Windows relevance, secret-screen allow list, vacuum/backup, GPU courtesy
parsing, config defaults, OCR plumbing."""
import os
import shutil
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app
from semsearch.config import Config
from semsearch.gpu_monitor import busy_by_others, parse_instance
from semsearch.security import normalize_path


# ---------------------------------------------------------------- duplicates: one hit, N locations, N document rows

def test_identical_copies_collapse_in_results_but_stay_separate_documents(built, root):
    src = root / "gpu.txt"
    for i in range(1, 5):
        shutil.copy2(src, root / "sub" / f"gpu_copy{i}.txt")
    built.indexer.index_path(str(root / "sub"))
    drain(built)
    # five document rows exist (identity is per path)
    rows = [built.store.get_document(normalize_path(str(root / "sub" / f"gpu_copy{i}.txt"))) for i in range(1, 5)]
    assert all(r is not None and r["n_chunks"] > 0 for r in rows)
    assert built.store.count_documents() >= 10
    r = built.retriever.search("GPU memory architecture", "literal", 10)
    names = [h["filename"] for h in r["results"]]
    assert names.count("gpu.txt") + sum(n.startswith("gpu_copy") for n in names) == 1  # presented once
    top = next(h for h in r["results"] if h["filename"] in ("gpu.txt",) or h["filename"].startswith("gpu_copy"))
    assert len(top["duplicates"]) == 4
    assert all(os.path.isabs(p) for p in top["duplicates"])
    # deleting one copy forgets only that instance
    (root / "sub" / "gpu_copy1.txt").unlink()
    built.indexer.reconcile()
    assert built.store.get_document(normalize_path(str(root / "sub" / "gpu_copy1.txt")))["extract_status"] == "missing"
    assert built.store.get_document(normalize_path(str(src)))["extract_status"] == "ok"
    r = built.retriever.search("GPU memory architecture", "literal", 10)
    top = next(h for h in r["results"] if h["filename"] == "gpu.txt" or h["filename"].startswith("gpu_copy"))
    assert len(top["duplicates"]) == 3


def test_collapse_can_be_disabled(built, root, cfg):
    shutil.copy2(root / "gpu.txt", root / "gpu2.txt")
    built.indexer.index_path(str(root / "gpu2.txt"))
    drain(built)
    cfg.retrieval.collapse_duplicates = False
    names = [h["filename"] for h in built.retriever.search("GPU memory architecture", "literal", 10, )["results"]]
    assert "gpu.txt" in names and "gpu2.txt" in names


# ---------------------------------------------------------------- failed jobs are visible

def test_failed_jobs_appear_in_status_and_errors(built, cfg, root, monkeypatch):
    cfg.indexing.max_attempts = 1
    bad = root / "sub" / "poison.md"
    write(str(bad), "# poison\n\nboom\n")
    real = built.indexer.extractor.extract

    def explode(path, ext):
        if path.lower().endswith("poison.md"):
            raise RuntimeError("simulated extractor crash")
        return real(path, ext)

    monkeypatch.setattr(built.indexer.extractor, "extract", explode)
    built.indexer.index_path(str(bad))
    job = built.store.next_job()
    with pytest.raises(RuntimeError):
        built.indexer.process_job(job)
    built.store.fail_job(int(job["id"]), "simulated extractor crash", cfg.indexing.max_attempts)
    st = built.indexer.status()
    assert st["queue"]["failed"] == 1
    assert st["failed_jobs"][0]["path"].endswith("poison.md") and "simulated" in st["failed_jobs"][0]["error"]
    with TestClient(create_app(cfg, state=built), base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as c:
        body = c.get("/errors").json()
        assert body["failed_jobs"][0]["path"].endswith("poison.md")
        assert c.get("/errors", params={"stage": "policy"}).json()["errors"] == [] or all(e["stage"] == "policy" for e in c.get("/errors", params={"stage": "policy"}).json()["errors"])
        assert "crawl backlog" in c.get("/status").json()["windows_search"].get("note", "crawl backlog")  # wording present when Windows is available


# ---------------------------------------------------------------- adaptive Windows relevance

class EmptyWindows:
    def __init__(self):
        self.calls = 0

    def freetext(self, query, roots, limit=50):
        self.calls += 1
        return []


def test_windows_relevance_pauses_after_consecutive_empty_answers(built, cfg):
    w = EmptyWindows()
    built.retriever.windows = w
    cfg.retrieval.windows_rank_disable_after = 3
    for i in range(3):
        built.retriever.search(f"gpu query {i}", "literal", 3, cache=False)
    assert w.calls == 3
    assert built.retriever.windows_rank_state()["active"] is False
    built.retriever.search("gpu query again", "literal", 3, cache=False)
    assert w.calls == 3  # paused: not called
    built.retriever._windows_paused_until = 0.0  # the hour elapses
    built.retriever.search("gpu query later", "literal", 3, cache=False)
    assert w.calls == 4


# ---------------------------------------------------------------- secret-screen allow list

def test_secret_scan_allow_list_exempts_documented_examples(built, root, cfg):
    cfg.indexing.secret_scan_allow = ["**/docs/examples/**"]
    write(str(root / "docs" / "examples" / "howto.md"), "# How to\n\nSet AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE in your shell.\n")
    write(str(root / "notes" / "real.md"), "# real\n\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
    built.indexer.index_path(str(root))
    drain(built)
    assert built.store.get_document(normalize_path(str(root / "docs" / "examples" / "howto.md")))["extract_status"] == "ok"
    assert built.store.get_document(normalize_path(str(root / "notes" / "real.md")))["extract_status"] == "secret_suspected"


# ---------------------------------------------------------------- vacuum and backup

def test_vacuum_and_backup(built, root, tmp_path):
    for i in range(40):
        write(str(root / "bulk" / f"b{i}.md"), f"# b{i}\n\n" + ("filler text " * 400) + "\n")
    built.indexer.index_path(str(root / "bulk"))
    drain(built)
    assert built.indexer.remove_path(str(root / "bulk"))["removed"] == 40
    r = built.store.vacuum()
    assert r["kind"] in ("incremental", "full") and r["free_pages_after"] <= r["free_pages_before"]
    assert built.store.get_meta("last_vacuum_at") is not None
    assert built.store.conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    dest = tmp_path / "bk" / "semsearch-backup.db"
    b = built.store.backup(dest)
    assert b["bytes"] > 0 and dest.exists()
    c = sqlite3.connect(dest)
    assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == built.store.count_documents()
    c.close()
    # still works through the API
    with TestClient(create_app(built.cfg, state=built), base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as cl:
        r = cl.post("/backup", json={"path": str(tmp_path / "bk" / "via-api.db")}).json()
        assert r["bytes"] > 0


# ---------------------------------------------------------------- GPU courtesy parsing

def test_gpu_counter_attribution():
    assert parse_instance("pid_1234_luid_0x00000000_0x000122AE_phys_0_eng_0_engtype_3D") == (1234, "00000000-000122ae", "3D")
    assert parse_instance("something else") is None
    samples = [
        ("pid_1234_luid_0x00000000_0x000122AE_phys_0_eng_0_engtype_3D", 60.0),       # other process, our GPU
        ("pid_1234_luid_0x00000000_0x000122AE_phys_0_eng_1_engtype_Copy", 5.0),
        ("pid_777_luid_0x00000000_0x000122AE_phys_0_eng_0_engtype_3D", 30.0),        # ourselves
        ("pid_1234_luid_0x00000000_0x000136E7_phys_0_eng_0_engtype_3D", 90.0),       # other GPU
        ("pid_1234_luid_0x00000000_0x000122AE_phys_0_eng_2_engtype_VideoDecode", 40.0),  # not compute
    ]
    assert busy_by_others(samples, "00000000-000122ae", my_pid=777) == 65.0
    assert busy_by_others(samples, "00000000-000136e7", my_pid=777) == 90.0


def test_bulk_mode_yields_when_other_processes_use_the_gpu(built, cfg, monkeypatch):
    built.indexer.bulk_luid = "00000000-000122ae"
    cfg.indexing.bulk_yield_gpu_percent = 40

    class FakeMon:
        def __init__(self):
            self.busy = 80.0

        def others_busy_percent(self, luid):
            return self.busy

    mon = FakeMon()
    built.indexer._gpu_monitor = mon
    assert built.indexer._bulk_gpu_busy_elsewhere() is True
    assert built.indexer.status()["gpu_yielding"] is True
    mon.busy = 10.0
    assert built.indexer._bulk_gpu_busy_elsewhere() is False
    cfg.indexing.bulk_yield_gpu_percent = 0
    mon.busy = 100.0
    assert built.indexer._bulk_gpu_busy_elsewhere() is False  # disabled


# ---------------------------------------------------------------- defaults and OCR plumbing

def test_defaults_cover_new_items():
    c = Config()
    assert ".msg" in c.document_extensions and ".eml" in c.document_extensions
    assert any("eval/corpus" in e for e in c.excludes) and any("semsearch/dist" in e for e in c.excludes)
    from semsearch.security import is_excluded
    assert is_excluded(r"F:\HexyLab\semsearch\eval\corpus\root\x.md", c.excludes)
    assert c.retrieval.collapse_duplicates and c.indexing.ocr_scanned_pdfs is False


@pytest.mark.skipif(os.name != "nt", reason="Windows OCR")
def test_windows_ocr_reads_an_image_only_pdf(tmp_path):
    from semsearch.extract.ocr import WindowsOcrPdfExtractor, available
    if not available():
        pytest.skip("pywinrt OCR packages not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1200, 400), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((40, 60), "Scanned invoice for zebrafish tanks", fill="black", font=font)
    d.text((40, 160), "Total due 4200 dollars by October", fill="black", font=font)
    p = tmp_path / "scan.pdf"
    img.save(p, "PDF", resolution=150)
    import pypdf
    assert "".join(pg.extract_text() or "" for pg in pypdf.PdfReader(p).pages).strip() == ""  # really has no text layer
    r = WindowsOcrPdfExtractor().extract(str(p), ".pdf")
    if r.status == "error" and "language" in (r.error or ""):
        pytest.skip(r.error)
    assert r.ok and "zebrafish" in r.text and "4200" in r.text and r.meta["pages_ocr"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows OCR")
def test_ocr_extractor_is_wired_when_enabled():
    from semsearch.extract.ocr import available
    from semsearch.extract.registry import build_default_registry
    cfg = Config()
    cfg.indexing.ocr_scanned_pdfs = True
    reg = build_default_registry(cfg)
    names = [e.name for e in reg.extractors_for(".pdf")]
    if available():
        assert names[-1] == "windows-ocr" and names[0] == "pypdf"
    else:
        assert "windows-ocr" not in names
