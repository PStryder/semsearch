"""Tests for guarantees the mutation audit found unguarded (each was mutated and survived the
previous suite). Grouped by the layer the guarantee lives in."""
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app
from semsearch.security import PathRejected, check_indexable, normalize_path

WIN = sys.platform == "win32"


def _client(st, **headers):
    return TestClient(create_app(st.cfg, state=st), base_url="http://127.0.0.1", headers=headers)


# ---------------------------------------------------------------- API gates

def test_every_maintenance_and_read_endpoint_is_gated(built):
    with _client(built) as c:
        for path, body in [("/backup", {"path": "x.db"}), ("/indexer/prune", None), ("/config/excludes", {"excludes": []}),
                           ("/config/roots", {"set": []}), ("/ui/nonce", None)]:
            r = c.post(path, json=body) if body is not None else c.post(path)
            assert r.status_code == 403, path
        for path in ("/config", "/config/windows-scope", "/errors", "/stats", "/status", "/document?path=x"):
            assert c.get(path).status_code == 403, path


@pytest.mark.parametrize("bad", ["prefix", "short", "long", "case"])
def test_near_miss_tokens_are_refused(built, bad):
    t = built.admin_token
    tok = {"prefix": t[:8] + "x" * (len(t) - 8), "short": t[:-1], "long": t + "0", "case": t.upper() if t != t.upper() else t.lower()}[bad]
    with _client(built, **{"x-semsearch-token": tok}) as c:
        assert c.post("/indexer/pause").status_code == 403
        assert c.get("/search?q=x").status_code == 403


@pytest.mark.parametrize("host", ["", "evil-localhost", "localhost.evil.com", "127.0.0.1.evil.com", "127.0.0.2", "0.0.0.0"])
def test_host_guard_refuses_lookalikes(built, host):
    with _client(built) as c:
        assert c.get("/health", headers={"host": host}).status_code == 421


def test_document_hides_tombstoned_rows(built, root):
    n = normalize_path(str(root / "gpu.txt"))
    built.store.tombstone_document(int(built.store.get_document(n)["id"]))
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.get("/document", params={"path": str(root / "gpu.txt")}).status_code == 404


def test_backup_refuses_the_directory_itself_and_an_existing_folder(built):
    (built.cfg.backup_dir / "afolder.db").mkdir(parents=True)
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.post("/backup", json={"path": "."}).status_code == 403
        assert c.post("/backup", json={"path": str(built.cfg.backup_dir)}).status_code == 403
        assert c.post("/backup", json={"path": "afolder.db"}).status_code == 400


# ---------------------------------------------------------------- containment reasons

def test_outside_root_is_rejected_for_the_right_reason(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("x")
    with pytest.raises(PathRejected, match="outside configured roots"):
        check_indexable(str(tmp_path / "outside.txt"), [str(root)])


@pytest.mark.parametrize("attr", [0x1000, 0x400000])   # OFFLINE, RECALL_ON_DATA_ACCESS
def test_cloud_placeholders_are_not_recalled(tmp_path, monkeypatch, attr):
    import semsearch.security as sec
    root = tmp_path / "root"
    root.mkdir()
    f = root / "cloud.txt"
    f.write_text("x")
    real = sec.lstat_info

    def fake(p):
        info = real(p)
        info.attributes |= attr
        return info
    monkeypatch.setattr(sec, "lstat_info", fake)
    with pytest.raises(PathRejected, match="placeholder"):
        check_indexable(str(f), [str(root)])


# ---------------------------------------------------------------- secret patterns: one hit and one miss each

SECRET_CASES = {
    "private_key_block": ("-----BEGIN OPENSSH PRIVATE KEY-----", "the BEGIN PRIVATE KEY marker is discussed here"),
    "aws_access_key": ("key AKIAIOSFODNN7EXAMPLE end", "AKIA is a prefix"),
    "openai_key": ("sk-proj-" + "a1B2" * 9, "sk-short"),
    "anthropic_key": ("sk-ant-" + "Q9w8" * 9, "sk-ant-short"),
    "github_token": ("ghp_" + "Ab3D" * 9, "ghp_short"),
    "slack_token": ("xoxb-1234567890-abcdefghij", "xoxb-"),
    "stripe_key": ("sk_live_" + "Zz9" * 8, "sk_test_" + "Zz9" * 8),
    "google_api_key": ("AIza" + "B" * 35, "AIza" + "B" * 10),
    "google_oauth_client_secret": ('"client_secret": "' + "c" * 24 + '"', '"client_secret": "short"'),
    "service_account_key": ('"private_key": "-----BEGIN', '"private_key": "<redacted>"'),
    "jwt": ("eyJ" + "a" * 22 + ".eyJ" + "b" * 22 + "." + "c" * 22, "eyJhbGci.eyJzdWIi.sig"),
    "generic_assignment": ("api_key = 'Q1w2E3r4T5y6U7i8O9p0A1s2D3f4'", "api_key = 'short'"),
}


@pytest.mark.parametrize("name", sorted(SECRET_CASES))
def test_each_secret_pattern_hits_and_misses(name):
    from semsearch.security import suspected_secret
    hit, miss = SECRET_CASES[name]
    assert suspected_secret(f"some text\n{hit}\nmore text") == name
    assert suspected_secret(f"some text\n{miss}\nmore text") is None


def test_secret_after_the_first_kilobytes_is_still_found():
    from semsearch.security import suspected_secret
    assert suspected_secret("x" * 5000 + " AKIAIOSFODNN7EXAMPLE ") == "aws_access_key"


def test_secret_screening_is_what_hides_the_file(built, root, cfg):
    """The absence test means something only if the same query finds the file without screening."""
    write(str(root / "keys.txt"), "deployment notes AKIAIOSFODNN7EXAMPLE end\n")
    built.indexer.index_path(str(root / "keys.txt"))
    drain(built)
    assert built.retriever.search("deployment notes", "literal", 5)["results"] == [] or \
        all(h["filename"] != "keys.txt" for h in built.retriever.search("deployment notes", "literal", 5)["results"])
    cfg.indexing.skip_suspected_secrets = False
    built.indexer.enforce_secret_policy()
    drain(built)
    assert any(h["filename"] == "keys.txt" for h in built.retriever.search("deployment notes", "literal", 5)["results"])


def test_policy_rescan_respects_the_exemption_list(built, root, cfg):
    write(str(root / "docs" / "example.md"), "# example\n\nAKIAIOSFODNN7EXAMPLE\n")
    cfg.indexing.skip_suspected_secrets = False
    built.indexer.enforce_secret_policy()
    built.indexer.index_path(str(root / "docs"))
    drain(built)
    cfg.indexing.skip_suspected_secrets = True
    cfg.indexing.secret_scan_allow = ["**/docs/**"]
    built.indexer.enforce_secret_policy()
    assert built.store.get_document(normalize_path(str(root / "docs" / "example.md")))["extract_status"] == "ok"


# ---------------------------------------------------------------- exclusions before job time

def test_full_build_never_enqueues_excluded_files(built, root, cfg):
    write(str(root / "node_modules" / "pkg" / "index.md"), "# vendored\n")
    write(str(root / "build" / "out.md"), "# build output\n")
    built.store.clear_jobs()
    built.indexer.full_build()
    queued = [r[0] for r in built.store.conn.execute("SELECT path FROM jobs")]
    assert queued and not any("node_modules" in p or "\\build\\" in p for p in queued)


def test_default_excludes_protect_profiles():
    from semsearch.config import Config
    from semsearch.security import is_excluded
    ex = Config().excludes
    assert "**/AppData/**" in ex and "**/Users/*/.*/**" in ex
    assert is_excluded(r"C:\Users\me\AppData\Local\app\state.json", ex)
    assert is_excluded(r"C:\Users\me\.vscode\extensions\x.md", ex)
    assert not is_excluded(r"C:\Users\me\Documents\notes.md", ex)
    assert not is_excluded(r"F:\HexyLab\.github\workflows\ci.yml", ex)   # dot dirs outside profiles stay indexable


def test_rejected_job_removes_a_previously_indexed_row(built, root, cfg):
    n = normalize_path(str(root / "gpu.txt"))
    assert built.store.get_document(n) is not None
    cfg.excludes = list(cfg.excludes) + ["**/gpu.txt"]
    built.store.enqueue(n, "index", 1)
    drain(built)
    assert built.store.get_document(n) is None


# ---------------------------------------------------------------- watcher events -> queue

def _queued(built):
    return {(r[1], r[0]) for r in built.store.conn.execute("SELECT path, op FROM jobs WHERE state='pending'")}


def test_watcher_events_enqueue_the_right_jobs(built, root, cfg):
    built.store.clear_jobs()
    gpu = normalize_path(str(root / "gpu.txt"))
    built.indexer._on_watch_event("removed", str(root / "gpu.txt"))
    assert ("vanish", gpu) in _queued(built)
    built.store.clear_jobs()
    write(str(root / "renamed.md"), "# renamed\n")
    agents = normalize_path(str(root / "agents.md"))
    built.indexer._on_watch_event("changed", str(root / "renamed.md"), str(root / "agents.md"))
    q = _queued(built)
    assert ("vanish", agents) in q and ("index", normalize_path(str(root / "renamed.md"))) in q
    built.store.clear_jobs()
    write(str(root / "node_modules" / "x.md"), "# vendored\n")
    built.indexer._on_watch_event("changed", str(root / "node_modules" / "x.md"))
    built.indexer._on_watch_event("changed", str(root / "node_modules"))
    write(str(root / "id_rsa"), "key\n")
    built.indexer._on_watch_event("changed", str(root / "id_rsa"))
    assert _queued(built) == set()


# ---------------------------------------------------------------- move detection edges

def test_rename_with_changed_bytes_is_reindexed_not_moved(built, root):
    src, dst = root / "gpu.txt", root / "gpu_v2.txt"
    os.rename(src, dst)
    with open(dst, "a", encoding="utf-8") as f:
        f.write("appended zirconium paragraph\n")
    built.indexer.index_path(str(dst))
    res = [r for _, _, r in drain(built)]
    assert "moved" not in res and "indexed" in res
    assert built.retriever.search("zirconium", "literal", 1)["results"][0]["filename"] == "gpu_v2.txt"


def test_hard_link_is_not_treated_as_a_move(built, root):
    a = root / "agents.md"
    b = root / "agents_link.md"
    os.link(a, b)
    built.indexer.index_path(str(b))
    res = [r for _, _, r in drain(built)]
    assert "moved" not in res
    assert built.store.get_document(normalize_path(str(a))) is not None   # the original row is untouched
    assert built.store.get_document(normalize_path(str(b))) is not None


def test_vanish_does_not_tombstone_a_file_that_still_exists(built, root):
    n = normalize_path(str(root / "gpu.txt"))
    built.store.enqueue(n, "vanish", 1)
    drain(built)
    assert built.store.get_document(n)["extract_status"] == "ok"


def test_reconcile_keeps_existing_unseen_files_and_drops_excluded_ones(built, root, cfg):
    gpu = normalize_path(str(root / "gpu.txt"))
    agents = normalize_path(str(root / "agents.md"))
    cfg.excludes = list(cfg.excludes) + ["**/agents.md"]
    built.indexer._reconcile_root(normalize_path(str(root)), seen=set())
    assert built.store.get_document(gpu)["extract_status"] == "ok"            # exists, merely unseen
    assert built.store.get_document(agents)["extract_status"] == "missing"     # excluded now


# ---------------------------------------------------------------- changed-during-index, each signal

def _swap_during_extract(built, path, mutate):
    real = built.indexer.extractor.extract
    state = {"done": False}

    def ex(p, ext):
        r = real(p, ext)
        if not state["done"]:
            state["done"] = True
            mutate()
        return r
    built.indexer.extractor.extract = ex
    built.indexer.index_path(str(path))
    return [r for _, _, r in drain(built)]


def test_same_size_rewrite_during_extraction_is_requeued(built, root):
    p = root / "same.txt"
    write(str(p), "alpha version text\n")

    def rewrite():
        time.sleep(0.02)
        write(str(p), "omega version text\n")   # same length, new mtime
    res = _swap_during_extract(built, p, rewrite)
    assert res[0] == "changed_during_index"
    assert built.retriever.search("omega", "literal", 1)["results"]


def test_file_replaced_by_rename_during_extraction_is_requeued(built, root, tmp_path):
    p = root / "replaced.txt"
    write(str(p), "original content here\n")
    other = tmp_path / "other.txt"
    write(str(other), "replacement content!!\n")   # same length, different file id

    def replace():
        os.replace(other, p)
    res = _swap_during_extract(built, p, replace)
    assert res[0] == "changed_during_index"


# ---------------------------------------------------------------- stop interrupts every sweep

def _slow_store_call(built, monkeypatch, name, per_call=0.05):
    real = getattr(built.store, name)
    calls = []

    def slow(*a, **k):
        calls.append(1)
        time.sleep(per_call)
        return real(*a, **k)
    monkeypatch.setattr(built.store, name, slow)
    return calls


def _many_docs(built, root, n=150):
    for i in range(n):
        write(str(root / "bulk" / f"d{i}.md"), f"# d{i}\n\nbody {i}\n")
    built.indexer.index_path(str(root / "bulk"))
    drain(built)


def _run_and_stop(built, target):
    built.indexer.state.running = True
    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(0.4)
    built.indexer._stop.set()
    t0 = time.time()
    t.join(5)
    elapsed = time.time() - t0
    built.indexer._stop.clear()
    built.indexer.state.running = False
    return elapsed, t.is_alive()


def test_stop_interrupts_reconcile(built, root, monkeypatch, cfg):
    _many_docs(built, root)
    cfg.indexing.reconcile_min_fraction = 0.0   # an empty `seen` must reach the loop, not the mass-deletion guard
    monkeypatch.setattr("os.path.exists", lambda p, _real=os.path.exists: (time.sleep(0.05), _real(p))[1])
    elapsed, alive = _run_and_stop(built, lambda: built.indexer._reconcile_root(normalize_path(str(root)), set()))
    assert not alive and elapsed < 1.0


def test_stop_interrupts_the_secret_rescan(built, root, monkeypatch, cfg):
    _many_docs(built, root)
    built.store.set_meta("policy:secret_scan", "off")
    import semsearch.indexer as ix
    monkeypatch.setattr(ix, "suspected_secret", lambda t, _r=ix.suspected_secret: (time.sleep(0.05), _r(t))[1])
    elapsed, alive = _run_and_stop(built, built.indexer.enforce_secret_policy)
    assert not alive and elapsed < 1.0
    assert built.indexer._policy_rescan_pending is True


def test_stop_interrupts_full_build_enumeration(built, root, monkeypatch):
    _many_docs(built, root)
    calls = _slow_store_call(built, monkeypatch, "enqueue_many", 0.0)
    real_wanted = built.indexer._wanted
    monkeypatch.setattr(built.indexer, "_wanted", lambda fe: (time.sleep(0.02), real_wanted(fe))[1])
    elapsed, alive = _run_and_stop(built, built.indexer.full_build)
    assert not alive and elapsed < 1.0


def test_stop_interrupts_queue_prune(built, root, monkeypatch, cfg):
    built.store.enqueue_many((f"c:\\x\\{i}.md", "index", 5) for i in range(30000))
    import semsearch.indexer as ix
    real = ix.compile_excludes

    def slow_compile(p):
        m = real(p)
        return lambda path, is_dir=False: (time.sleep(0.0005), m(path, is_dir))[1]
    monkeypatch.setattr(ix, "compile_excludes", slow_compile)
    elapsed, alive = _run_and_stop(built, built.indexer.prune_queue)
    assert not alive and elapsed < 3.0


def test_stop_interrupts_the_reembed_backlog(built, root, monkeypatch):
    _many_docs(built, root)
    real = built.store.documents_needing_embedding
    monkeypatch.setattr(built.store, "documents_needing_embedding",
                        lambda fp, n=1000, after_id=0: (time.sleep(0.3), real("other-fingerprint", 5, after_id))[1])
    elapsed, alive = _run_and_stop(built, lambda: built.indexer.reembed_stale(batch_docs=5))
    assert not alive and elapsed < 1.0


# ---------------------------------------------------------------- DirectML: embed() really serialises Run()

def test_embed_serialises_runs_on_one_gpu_session():
    from semsearch.embed.onnx_provider import OnnxProvider
    import numpy as np
    p = OnnxProvider.__new__(OnnxProvider)
    p._lock = threading.Lock()
    p._run_locks = {}
    p.devices = {"steady": ("dml", 0), "bulk": ("dml", 0), "query": ("dml", 0)}
    p.bulk_mode = False
    p.batch_size = 1
    p.query_prefix = p.document_prefix = ""
    p.normalize = False
    p.dim = 4
    p._sessions = {("dml", 0): object()}
    state = {"in": 0, "max": 0}
    guard = threading.Lock()

    def fake_run(session, texts):
        with guard:
            state["in"] += 1
            state["max"] = max(state["max"], state["in"])
        time.sleep(0.01)
        with guard:
            state["in"] -= 1
        return np.zeros((len(texts), 4), dtype=np.float32)
    p._run = fake_run
    p._session = lambda dev: p._sessions[dev]
    ts = [threading.Thread(target=p.embed, args=(["a", "b", "c"], "document")) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert state["max"] == 1


# ---------------------------------------------------------------- response cache is accessed under its lock

def test_response_cache_is_only_touched_under_its_lock(built):
    from collections import OrderedDict
    r = built.retriever
    lock = r._cache_lock

    class Checked(OrderedDict):
        def __getitem__(self, k):
            assert lock.locked(); return super().__getitem__(k)

        def get(self, k, d=None):
            assert lock.locked(); return super().get(k, d)

        def __setitem__(self, k, v):
            assert lock.locked(); return super().__setitem__(k, v)

        def move_to_end(self, *a, **k):
            assert lock.locked(); return super().move_to_end(*a, **k)

        def popitem(self, *a, **k):
            assert lock.locked(); return super().popitem(*a, **k)
    r._cache = Checked()
    r._cache_size = 1
    for q in ("gpu", "agents", "gpu"):
        r.search(q, "literal", 2)


# ---------------------------------------------------------------- interrupted incremental keeps its checkpoint

def test_incremental_stopped_midway_does_not_advance_the_checkpoint(built, root, monkeypatch):
    from semsearch.models import FileEntry
    r = normalize_path(str(root))
    old = time.time() - 3600
    built.store.set_meta(f"checkpoint:{r}", str(old))
    built.indexer._last_fs_scan = {}

    def changed_since(rt, since):
        yield FileEntry(path=str(root / "gpu.txt"), size=1, mtime=time.time(), source="fs", extension=".txt")
        built.indexer._stop.set()   # shutdown arrives after a newer file was seen
        yield FileEntry(path=str(root / "agents.md"), size=1, mtime=old + 10, source="fs", extension=".md")
    monkeypatch.setattr(built.indexer.fs, "changed_since", changed_since)
    try:
        built.indexer.incremental(force=True)
    finally:
        built.indexer._stop.clear()
    assert float(built.store.get_meta(f"checkpoint:{r}")) == old


# ---------------------------------------------------------------- extraction hardening

ADVERSARIAL = {"eyj-dash": lambda: "eyJ-" * 100_000, "eyj-run": lambda: "eyJ" + "a" * 400_000, "sk-run": lambda: "sk-" * 130_000,
               "akia-run": lambda: "AKIA" * 100_000, "dotted": lambda: ("a" * 30 + ".") * 13_000,
               "eyj-dotted": lambda: ("eyJ" + "a" * 25 + ".") * 10_000 + "x", "assign": lambda: "api_key=" * 50_000}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))   # ids, not payloads: pytest puts the id in an env var
def test_secret_scan_is_linear_on_adversarial_input(name):
    from semsearch.security import suspected_secret
    payload = ADVERSARIAL[name]()
    t0 = time.perf_counter()
    suspected_secret(payload)
    assert time.perf_counter() - t0 < 1.0     # the jwt rule took 34 s on the first payload


def test_jwt_still_found_after_a_token_run():
    from semsearch.security import suspected_secret
    tok = "eyJ" + "a" * 22 + ".eyJ" + "b" * 22 + "." + "c" * 22
    assert suspected_secret("Authorization: Bearer " + tok) == "jwt"
    assert suspected_secret("x-" + tok) == "jwt"


def test_control_characters_are_stripped_from_extracted_text():
    from semsearch.extract.registry import clean_text
    s = "title\x1b]0;pwned\x07 \x1b[2J\x1b[Hbody\ttab\nline\r\nend\x0cpage\x00"
    out = clean_text(s)
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out
    assert "\t" in out and "\n" in out and "\x0c" in out


def test_cli_escapes_bidi_and_controls_in_paths_and_excerpts(capsys):
    from semsearch.cli import _print_results
    res = {"query": "q", "mode": "hybrid", "took_ms": 1, "candidates": 1, "results": [
        {"path": "C:\\docs\\invoice\u202egpj.exe", "filename": "x", "score": 0.9, "match_type": "both", "scores": {},
         "excerpt": "before \x1b[2J after \u2066hidden\u2069", "file_type": "md", "modified": ""}]}
    _print_results(res, False)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out and "\u2066" not in out
    assert "\\u202e" in out and "\\u001b" in out


def test_pdf_extractor_caps_a_single_huge_page(monkeypatch, tmp_path):
    import semsearch.extract.pdf as pdfmod

    class Page:
        def extract_text(self):
            return "x" * 5_000_000

    class Reader:
        is_encrypted = False
        pages = [Page(), Page()]

        def __init__(self, *a, **k):
            pass
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    f = tmp_path / "big.pdf"
    f.write_bytes(b"%PDF-1.4")
    r = pdfmod.PdfExtractor(max_chars=1000).extract(str(f), ".pdf")
    assert len(r.text) <= 1000


def test_child_reply_with_control_characters_fits_the_cap(cfg, tmp_path, monkeypatch):
    """A PDF of 1.9M control characters used to encode to 11.5 MB and kill the child."""
    from semsearch.extract.isolated import _encode
    from semsearch.extract.registry import clean_text
    cfg.indexing.max_text_chars = 2_000_000
    text = clean_text("\x01" * 1_920_010 + "real words")
    assert len(_encode([text, "ok", "pypdf", None, {}])) < 6 * cfg.indexing.max_text_chars + (1 << 20)
    assert text == "real words"


def test_whitespace_only_lines_do_not_become_a_giant_chunk(cfg):
    from semsearch.chunking import chunk_text
    chunks = chunk_text("a\n" + " " * 1_000_000 + "\nb\n" + "\t" * 500_000 + "\nc", cfg.chunking, ".txt")
    assert chunks and max(len(c.text) for c in chunks) <= cfg.chunking.max_chars


def test_failed_extraction_is_not_retried_every_reconcile(built, root, monkeypatch):
    import semsearch.indexer as ix
    from semsearch.models import ExtractResult
    p = root / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 broken")
    calls = []
    real = built.indexer.extractor.extract

    def failing(path, ext):
        if path.endswith("bad.pdf"):
            calls.append(1)
            return ExtractResult("", "error", "pypdf", error="timed out")
        return real(path, ext)
    built.indexer.extractor.extract = failing
    built.indexer.index_path(str(p))
    drain(built)
    built.indexer.index_path(str(p))      # what an hourly reconcile does
    drain(built)
    assert len(calls) == 1
    monkeypatch.setattr(ix, "ERROR_RETRY_S", 0.0)   # a day later it is retried
    built.indexer.index_path(str(p))
    drain(built)
    assert len(calls) == 2


def test_a_job_that_keeps_killing_the_process_ends_failed(store_factory):
    s = store_factory()
    s.enqueue("c:\\x\\poison.pdf", "index", 1)
    for _ in range(3):
        assert s.next_job() is not None       # picked up ...
        s.requeue_running(max_attempts=3)     # ... and the process died
    assert s.queue_stats()["failed"] == 1 and s.next_job() is None


def test_wanted_drops_inventory_entries_under_an_exclusion(built, root, cfg):
    from semsearch.models import FileEntry
    fe = FileEntry(path=str(root / "node_modules" / "x.md"), size=10, mtime=time.time(), source="windows_search", extension=".md")
    assert built.indexer._wanted(fe) is False


@pytest.mark.skipif(not WIN, reason="IFilter stream path is Windows-only")
def test_ifilter_releases_its_stream(monkeypatch, tmp_path):
    import semsearch.extract.ifilter as ifm
    released = []
    monkeypatch.setattr(ifm, "_release_raw", lambda ptr: released.append(ptr))
    ex = ifm.IFilterExtractor()

    class FakeFlt:
        _semsearch_stream = "STREAM"
    monkeypatch.setattr(ex, "_extract", lambda path, ext, holder: (holder.append(FakeFlt()), "result")[1])
    assert ex.extract(str(tmp_path / "x.pdf"), ".pdf") == "result"
    assert released == ["STREAM"]


# ---------------------------------------------------------------- event log redaction

def test_event_log_text_carries_no_paths():
    from semsearch.service import _redact_paths
    s = "job C:\\Users\\alice\\Private\\diary.md failed; also \\\\srv\\share\\x.pdf and F:/HexyLab/a.txt and //srv/s/y"
    out = _redact_paths(s)
    assert "alice" not in out and "srv" not in out and "HexyLab" not in out
    assert out.count("<path>") == 4 and out.startswith("job <path> failed")


def test_event_log_handler_only_takes_errors():
    import logging
    from semsearch.service import _event_log_handler
    h = _event_log_handler("SemSearch")
    if h is None:
        pytest.skip("servicemanager unavailable")
    assert h.level == logging.ERROR


# ---------------------------------------------------------------- release manifest covers the whole stage

def test_release_manifest_detects_a_tampered_script_outside_python(tmp_path):
    import subprocess
    stage = tmp_path / "stage"
    (stage / "python").mkdir(parents=True)
    (stage / "python" / "a.py").write_text("x")
    (stage / "install.ps1").write_text("original")
    (stage / "VERSION").write_text("1")
    tool = os.path.join(os.path.dirname(__file__), "..", "installer", "release_manifest.py")
    py = sys.executable
    assert subprocess.run([py, tool, "write", str(stage)], capture_output=True).returncode == 0
    assert subprocess.run([py, tool, "verify", str(stage), str(stage / "release-manifest.json")], capture_output=True).returncode == 0
    (stage / "install.ps1").write_text("tampered")
    assert subprocess.run([py, tool, "verify", str(stage), str(stage / "release-manifest.json")], capture_output=True).returncode != 0


def test_installer_verifies_the_whole_release_not_only_python():
    src = open(os.path.join(os.path.dirname(__file__), "..", "installer", "install.ps1"), encoding="utf-8").read()
    first = src[src.index("verifying the release"):src.index("transactional section")]
    assert 'release_manifest.py" verify "$src" ' in first and "--subdir" not in first


# ---------------------------------------------------------------- installer safety rules exist where they must

def test_installer_scripts_never_use_recursive_remove_item():
    """Windows PowerShell 5.1's Remove-Item -Recurse follows junctions into their targets."""
    base = os.path.join(os.path.dirname(__file__), "..", "installer")
    for name in ("install.ps1", "uninstall.ps1"):
        code = [ln for ln in open(os.path.join(base, name), encoding="utf-8") if not ln.lstrip().startswith("#")]
        assert not any("Remove-Item -Recurse" in ln for ln in code), name


def test_installer_checks_data_dir_ownership_before_touching_the_machine():
    src = open(os.path.join(os.path.dirname(__file__), "..", "installer", "install.ps1"), encoding="utf-8").read()
    assert src.index("Assert-OwnedDataDir $DataDir") < src.index("stopping existing service")
    assert "-Operator explicitly" in src and "not a per-service virtual account" in src


def test_model_licence_text_is_reproduced_in_full():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "installer", "model_manifest.py")
    spec = importlib.util.spec_from_file_location("model_manifest", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    t = m.MIT_FLAGEMBEDDING
    assert "Copyright (c) 2022 staoxiao" in t and "Permission is hereby granted" in t and "THE SOFTWARE IS PROVIDED \"AS IS\"" in t


# ---------------------------------------------------------------- licence notices

def test_directml_licence_texts_are_vendored_for_the_bundled_version():
    import onnxruntime
    dll = os.path.join(os.path.dirname(onnxruntime.__file__), "capi", "DirectML.dll")
    if not os.path.isfile(dll):
        pytest.skip("not the DirectML build of onnxruntime")
    import win32api
    vi = win32api.GetFileVersionInfo(dll, chr(92))
    ver = f"{vi['FileVersionMS'] >> 16}.{vi['FileVersionMS'] & 0xffff}.{vi['FileVersionLS'] >> 16}"
    d = os.path.join(os.path.dirname(__file__), "..", "installer", "notices", f"directml-{ver}")
    for name in ("LICENSE.txt", "LICENSE-CODE.txt", "ThirdPartyNotices.txt"):
        assert os.path.getsize(os.path.join(d, name)) > 500, name
    assert "DIRECTML" in open(os.path.join(d, "LICENSE.txt"), encoding="utf-8-sig").read().upper()


def test_tray_never_startfiles_a_non_directory(tmp_path, monkeypatch, cfg):
    import semsearch.tray as tray
    started, warned = [], []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    exe = tmp_path / "payload.exe"
    exe.write_bytes(b"MZ")
    cfg.log_dir_override = exe
    t = tray.TrayApp(cfg)
    monkeypatch.setattr(t, "message", lambda *a, **k: warned.append(a))
    t.open_logs()
    assert started == [] and warned


# ---------------------------------------------------------------- CLI subcommand routing

def test_every_cli_subparser_is_routed_as_a_subcommand():
    """`semsearch backup x.db` fell through to the legacy flag parser because "backup" was not in
    SUBCOMMANDS. Every subparser the CLI defines must be listed."""
    import re
    import semsearch.cli as cli
    src = open(cli.__file__, encoding="utf-8").read()
    defined = set(re.findall(r'sub\.add_parser\("([a-z-]+)"', src))
    looped = re.search(r'for name in \(([^)]*)\):\s*\n\s*sp = sub\.add_parser\(name', src)
    if looped:
        defined |= set(re.findall(r'"([a-z-]+)"', looped.group(1)))
    assert defined and defined <= cli.SUBCOMMANDS, defined - cli.SUBCOMMANDS


def test_backup_subcommand_parses(monkeypatch, built, tmp_path, capsys):
    import semsearch.cli as cli
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: built.cfg)
    dest = tmp_path / "copy.db"
    rc = cli.main(["backup", str(dest), "--direct"])
    assert rc == 0 and dest.is_file() and "copy.db" in capsys.readouterr().out


# ---------------------------------------------------------------- retrieval performance paths

def test_or_fallback_drops_common_terms_on_a_large_index(built, root, monkeypatch):
    """The pruning only engages above 20k chunks, which no test corpus reaches: simulate it."""
    write(str(root / "rare.md"), "# zirconium\n\nzirconium appears here only\n")
    built.indexer.index_path(str(root / "rare.md"))
    drain(built)
    monkeypatch.setattr(built.store, "chunk_count", lambda: 1_000_000)
    real = built.store.fts_count
    monkeypatch.setattr(built.store, "fts_count", lambda expr: 900_000 if "the" in expr.lower() and "zirc" not in expr else real(expr))
    seen = []
    real_fts = built.store.fts
    monkeypatch.setattr(built.store, "fts", lambda expr, *a, **k: (seen.append(expr), real_fts(expr, *a, **k))[1])
    res = built.retriever.search("zirconium theory", "literal", 5, cache=False)
    assert seen[0] == '"zirconium" AND "theory"'                   # the AND attempt finds nothing ...
    assert seen[-1] == '"zirconium"'                               # ... and the OR fallback kept only the rare term
    assert res["results"][0]["filename"] == "rare.md"


def test_or_fallback_keeps_the_two_rarest_when_all_terms_are_common(built, monkeypatch):
    monkeypatch.setattr(built.store, "chunk_count", lambda: 1_000_000)
    dfs = {'"alpha"': 900_000, '"beta"': 400_000, '"gamma"': 500_000}
    monkeypatch.setattr(built.store, "fts_count", lambda expr: dfs.get(expr, 0))
    assert built.retriever._or_terms(["alpha", "beta", "gamma"]) == ["beta", "gamma"]


def test_masked_vector_search_ranks_only_inside_the_filter(store_factory):
    import numpy as np
    from semsearch.store.db import VectorCache
    vc = VectorCache(4)
    vc.add([1, 2, 3, 4], np.eye(4, dtype=np.float32))
    q = np.array([1.0, 0.9, 0.0, 0.0], dtype=np.float32)
    assert [i for i, _ in vc.search(q, 2)] == [1, 2]
    assert {i for i, _ in vc.search(q, 2, allowed=np.array([3, 4, 99]))} == {3, 4}   # tied scores: order is free
    vc.remove([3])
    assert [i for i, _ in vc.search(q, 5, allowed=np.array([3, 4]))] == [4]
    assert vc.search(q, 5, allowed=np.array([99])) == []


def test_root_filter_uses_a_path_range_not_like(built, root):
    flt, params = built.store._doc_filter_sql([str(root).lower()], None)
    assert "LIKE" not in flt and ">=" in flt and params[2].endswith("]")


def test_root_filter_excludes_sibling_folders_with_the_same_prefix(built, root):
    write(str(root / "rare" / "inside.md"), "# inside\n\nkumquat in the folder\n")
    write(str(root / "rare2" / "sibling.md"), "# sibling\n\nkumquat next door\n")
    write(str(root / "rare]" / "bracket.md"), "# bracket\n\nkumquat behind the bracket\n")
    built.indexer.index_path(str(root))
    drain(built)
    for mode in ("literal", "semantic", "hybrid"):
        r = built.retriever.search("kumquat", mode, 10, roots=[str(root / "rare")], cache=False)
        assert [h["filename"] for h in r["results"]] == ["inside.md"], mode
