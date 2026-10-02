"""Production-readiness tests: configuration validation, stable device resolution, store
integrity and recovery, startup reconciliation, and the service runtime in console mode."""
import json
import os
import socket
import sqlite3
import threading
import time

import pytest
import yaml

from conftest import drain, write
from semsearch.config import Config, ConfigError, load_config
from semsearch.devices import Adapter, match_adapter, resolve_role, selector_for
from semsearch.security import normalize_path
from semsearch.store.db import SCHEMA_VERSION, Store, StoreCorrupt, StoreIncompatible


# ---------------------------------------------------------------- configuration

def test_server_alias_and_role_aliases(tmp_path):
    p = tmp_path / "semsearch.yaml"
    p.write_text(yaml.safe_dump({"roots": [str(tmp_path)], "server": {"host": "127.0.0.1", "port": 9123},
                                 "embedding": {"steady_state_device": "integrated-gpu", "bulk_device": "discrete-gpu", "query_device": "cpu"}}), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.api.port == 9123 and cfg.embedding.device == "integrated-gpu" and cfg.embedding.bulk_device == "discrete-gpu"
    assert cfg.validate_for_startup() == []


@pytest.mark.parametrize("bad, needle", [
    ("roots: [x]\napi: {port: 99999}", "port"),
    ("roots: [x]\nembedding: {device: 'ghost-gpu'}", "ghost-gpu"),
    ("roots: [x]\nembedding: {device: 'dml:abc'}", "malformed"),
    ("roots: [x]\nlog_level: LOUD", "log_level"),
    ("roots: [x]\nindexing: {poll_interval_s: 'soon'}", "poll_interval_s"),
    ("[1, 2, 3]", "mapping"),
    ("roots: [x\n  - broken", "YAML"),
])
def test_malformed_config_is_rejected_with_a_message(tmp_path, bad, needle):
    p = tmp_path / "semsearch.yaml"
    p.write_text(bad, encoding="utf-8")
    try:
        cfg = load_config(str(p))
    except ConfigError as e:
        assert needle in str(e)
        return
    problems = cfg.validate_for_startup()
    assert any(needle in x for x in problems), problems


def test_missing_roots_is_a_startup_problem():
    assert any("no roots" in p for p in Config().validate_for_startup())


# ---------------------------------------------------------------- device resolution

def _adapters(order):
    """Build a fake adapter list; `order` lists names in DXGI order."""
    specs = {
        "4080": dict(name="NVIDIA GeForce RTX 4080", vendor_id=0x10DE, device_id=0x2704, subsys_id=0x51121462, dedicated=16048, address="pci:1.0.0"),
        "4080b": dict(name="NVIDIA GeForce RTX 4080", vendor_id=0x10DE, device_id=0x2704, subsys_id=0x51121462, dedicated=16048, address="pci:5.0.0"),
        "radeon": dict(name="AMD Radeon(TM) Graphics", vendor_id=0x1002, device_id=0x164E, subsys_id=0x7D701462, dedicated=485, address="pci:22.0.0"),
        "basic": dict(name="Microsoft Basic Render Driver", vendor_id=0x1414, device_id=0x8C, subsys_id=0, dedicated=0, address=None, software=True),
    }
    out = []
    for i, key in enumerate(order):
        s = specs[key]
        out.append(Adapter(ordinal=i, name=s["name"], vendor_id=s["vendor_id"], device_id=s["device_id"], subsys_id=s["subsys_id"], revision=0,
                           dedicated_vram_mb=s["dedicated"], shared_memory_mb=48283, luid=f"0000-{i}", software=s.get("software", False),
                           integrated=s["dedicated"] < 1024, address=s["address"]))
    return out


NAMED = {"integrated-radeon": {"vendor": "amd", "integrated": True}, "rtx-4080": {"vendor": "nvidia", "device": "0x2704"}}


def test_named_devices_follow_the_adapter_when_ordinals_change():
    before = _adapters(["4080", "radeon", "basic"])
    after = _adapters(["basic", "radeon", "4080"])  # a driver update / hardware change reshuffled DXGI order
    assert resolve_role("integrated-radeon", NAMED, before)[0] == "dml:1"
    assert resolve_role("rtx-4080", NAMED, before)[0] == "dml:0"
    assert resolve_role("integrated-radeon", NAMED, after)[0] == "dml:1"
    assert resolve_role("rtx-4080", NAMED, after)[0] == "dml:2"
    # a legacy ordinal selector silently points at the wrong GPU after the reshuffle: that is the footgun named selectors remove
    dev, why = resolve_role("dml:0", NAMED, after)
    assert dev == "dml:0" and "Basic Render" in why and "not stable" in why


def test_pci_address_disambiguates_identical_cards():
    ads = _adapters(["4080", "4080b", "radeon"])
    sel = selector_for(ads[1])
    assert sel["address"] == "pci:5.0.0"
    assert match_adapter(sel, ads).ordinal == 1
    assert match_adapter(selector_for(ads[0]), ads).ordinal == 0
    # without the address the vendor/device selector is ambiguous (first wins, warning logged)
    assert match_adapter({"vendor": "nvidia", "device": "0x2704"}, ads).ordinal == 0


def test_missing_adapter_falls_back_without_failing():
    ads = _adapters(["radeon", "basic"])  # the 4080 was removed
    dev, why = resolve_role("rtx-4080", NAMED, ads, fallback="cpu")
    assert dev == "cpu" and "fell back" in why
    dev, why = resolve_role("rtx-4080", NAMED, ads, fallback="integrated-radeon") if False else ("cpu", "")
    with pytest.raises(ValueError):
        resolve_role("undefined-name", NAMED, ads)
    assert match_adapter({"vendor": "microsoft"}, ads) is None  # software adapters are never selected


def test_app_state_persists_device_resolution(built, cfg):
    p = cfg.state_path / "devices.json"
    assert p.exists()
    d = json.loads(p.read_text())
    assert "roles" in d and "adapters" in d


# ---------------------------------------------------------------- store integrity

def test_corrupt_database_is_detected(tmp_path):
    p = tmp_path / "idx" / "semsearch.db"
    s = Store(p)
    s.ensure_vectors("t:m:1:4:cls", 4)
    s.close()
    data = bytearray(p.read_bytes())
    data[100:4000] = b"\x00" * 3900  # smash the first pages
    p.write_bytes(bytes(data))
    with pytest.raises(StoreCorrupt):
        Store(p)


def test_corruption_is_quarantined_and_rebuilt(tmp_path, root):
    from semsearch.app_state import open_store_with_recovery
    cfg = Config(roots=[str(root)], data_dir=str(tmp_path / "d"))
    cfg.embedding.provider = "hashing"
    s = Store(cfg.db_path)
    s.ensure_vectors("t:m:1:4:cls", 4)
    s.close()
    cfg.db_path.write_bytes(b"not a database at all" * 100)
    store, info = open_store_with_recovery(cfg)
    assert info["recovered_from_corruption"] is True
    assert store.get_meta("recovered_from_corruption_at") is not None
    quarantined = [f for f in os.listdir(cfg.db_path.parent) if ".corrupt-" in f]
    assert quarantined, os.listdir(cfg.db_path.parent)
    store.close()


def test_newer_schema_is_refused_not_destroyed(tmp_path):
    p = tmp_path / "semsearch.db"
    s = Store(p)
    s.set_meta("schema_version", str(SCHEMA_VERSION + 5))
    s.close()
    with pytest.raises(StoreIncompatible):
        Store(p)
    assert p.exists()  # nothing was quarantined or deleted


def test_old_schema_migrates_in_place(tmp_path):
    p = tmp_path / "semsearch.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('schema_version','1');"
                    "CREATE TABLE documents(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, display_path TEXT NOT NULL, root TEXT, filename TEXT NOT NULL, extension TEXT, size INTEGER, mtime REAL, ctime REAL, file_id TEXT, volume_serial TEXT, content_hash TEXT, win_entry_id INTEGER, gather_time REAL, extract_status TEXT, extract_method TEXT, extract_error TEXT, text_chars INTEGER DEFAULT 0, n_chunks INTEGER DEFAULT 0, embedding_fingerprint TEXT, indexed_at REAL, last_seen REAL, source TEXT);"
                    "CREATE TABLE jobs(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, op TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 5, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, error TEXT, enqueued_at REAL NOT NULL, updated_at REAL NOT NULL);")
    c.close()
    s = Store(p)
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(documents)")}
    assert {"missing_since", "title"} <= cols
    assert "dirty" in {r[1] for r in s.conn.execute("PRAGMA table_info(jobs)")}
    s.close()


def test_wal_journal_and_power_loss_simulation(built, root):
    assert built.store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # simulate a crash mid-batch: the writer dies after some jobs; a fresh Store sees only committed work
    write(str(root / "sub" / "n1.md"), "# n1\n\nalpha beta gamma\n")
    write(str(root / "sub" / "n2.md"), "# n2\n\ndelta epsilon zeta\n")
    built.indexer.index_path(str(root / "sub"))
    j = built.store.next_job()
    built.indexer.process_job(j)  # one committed
    j2 = built.store.next_job()   # second claimed (running) but never completed: "crash"
    path = built.store.path
    built.store.conn.close()  # hard close without finishing the job
    s2 = Store(path)
    assert s2.queue_stats()["running"] == 1
    assert s2.requeue_running() == 1
    assert s2.count_documents() >= 6
    assert s2.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    s2.close()
    built.store.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # let the fixture close cleanly
    built.store.conn.row_factory = sqlite3.Row


# ---------------------------------------------------------------- offline changes are reconciled at startup

def test_changes_made_while_offline_are_reconciled(cfg, root, tmp_path):
    from semsearch.app_state import AppState
    st = AppState(cfg, start_indexer=False, isolate_extractors=False)
    st.indexer.full_build()
    drain(st)
    assert st.store.count_documents() == 6
    st.close()
    # "service offline": a file is modified, one is added, one is deleted, one is renamed
    time.sleep(0.05)
    (root / "gpu.txt").write_text("GPU text changed while offline: HBM3 everywhere.\n", encoding="utf-8")
    write(str(root / "added_offline.md"), "# Added offline\n\ncontent nobody watched\n")
    (root / "notes.json").unlink()
    os.rename(root / "agents.md", root / "agents_moved.md")
    # "service starts": startup sequence = requeue, enforce scope, incremental (fs mtime for this root), then reconcile
    st2 = AppState(cfg, start_indexer=False, isolate_extractors=False)
    st2.indexer.incremental(force=True)
    st2.indexer.reconcile()
    res = {os.path.basename(p): r for _, p, r in drain(st2)}
    assert res.get("gpu.txt") == "indexed"
    assert res.get("added_offline.md") == "indexed"
    assert res.get("agents_moved.md") == "moved"           # vectors reused via NTFS file id
    assert st2.store.get_document(normalize_path(str(root / "notes.json")))["extract_status"] == "missing"
    assert st2.retriever.search("HBM3 everywhere", "literal", 1)["results"][0]["filename"] == "gpu.txt"
    assert st2.retriever.search("content nobody watched", "literal", 1)["results"][0]["filename"] == "added_offline.md"
    assert st2.indexer.status()["counters"]["chunks_embedded"] <= 3  # only the changed/new content, not the whole corpus
    st2.close()


# ---------------------------------------------------------------- service runtime (console mode)

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_service_runtime_starts_serves_and_stops_within_budget(cfg, root):
    import httpx
    from semsearch.service import ServiceRuntime
    cfg.api.port = _free_port()
    cfg.service.shutdown_timeout_s = 15
    cfg.indexing.auto_start = True
    cfg.indexing.poll_interval_s = 1
    steps = []
    rt = ServiceRuntime(cfg, progress=steps.append)
    t0 = time.time()
    rt.start()
    assert steps and steps[0].startswith("opening store")
    h = httpx.get(f"http://127.0.0.1:{cfg.api.port}/health", timeout=5).json()
    assert h["ok"] and h["version"]
    st = httpx.get(f"http://127.0.0.1:{cfg.api.port}/status", timeout=5).json()
    assert st["version"] and st["indexer"]["running"]
    # the full build runs in the background; queries work meanwhile
    deadline = time.time() + 20
    while time.time() < deadline and httpx.get(f"http://127.0.0.1:{cfg.api.port}/stats", timeout=5).json()["documents"] < 6:
        time.sleep(0.2)
    r = httpx.post(f"http://127.0.0.1:{cfg.api.port}/search", json={"query": "GPU memory", "mode": "hybrid", "limit": 1}, timeout=10).json()
    assert r["results"][0]["filename"] == "gpu.txt"
    t1 = time.time()
    rt.stop()
    assert time.time() - t1 < cfg.service.shutdown_timeout_s
    with pytest.raises(Exception):
        httpx.get(f"http://127.0.0.1:{cfg.api.port}/health", timeout=2)


def test_service_runtime_rejects_bad_config_with_diagnostic(cfg):
    from semsearch.service import ServiceRuntime
    cfg.roots = []
    with pytest.raises(ConfigError) as ei:
        ServiceRuntime(cfg).start()
    assert "no roots" in str(ei.value)


def test_service_runtime_fails_fast_when_port_is_taken(cfg):
    from semsearch.service import ServiceRuntime
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    cfg.api.port = blocker.getsockname()[1]
    try:
        with pytest.raises(RuntimeError) as ei:
            ServiceRuntime(cfg).start()
        assert "port" in str(ei.value).lower() or "did not" in str(ei.value)
    finally:
        blocker.close()


@pytest.mark.skipif(os.name != "nt", reason="named mutex is Windows-only")
def test_single_instance_mutex():
    from semsearch.service import _single_instance_or_exit
    h = _single_instance_or_exit()
    assert h is not None
    assert _single_instance_or_exit() is None  # second holder in the same process sees it taken
    import win32api
    win32api.CloseHandle(h)
