"""Regression tests for the fifth (security sweep) round. Each test names the guarantee it
holds and was checked to fail when that guarantee is removed."""
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app

WIN = sys.platform == "win32"


def _client(st, **headers):
    return TestClient(create_app(st.cfg, state=st), base_url="http://127.0.0.1", headers=headers)


# ---------------------------------------------------------------- settings-page sessions

def test_nonce_is_single_use_and_session_grants_reads_and_admin(built):
    app = create_app(built.cfg, state=built)  # one app: nonces and sessions live in its state
    with TestClient(app, base_url="http://127.0.0.1") as anon, \
            TestClient(app, base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as admin:
        assert anon.post("/ui/nonce").status_code == 403           # minting needs the admin token
        n = admin.post("/ui/nonce").json()["nonce"]
        sess = anon.post("/ui/redeem", json={"nonce": n}).json()["session"]
        assert anon.post("/ui/redeem", json={"nonce": n}).status_code == 403   # single use
        assert anon.post("/ui/redeem", json={"nonce": "made-up"}).status_code == 403
        h = {"x-semsearch-session": sess}
        assert anon.get("/search?q=gpu", headers=h).status_code == 200
        assert anon.post("/config/excludes", headers=h, json={"excludes": list(built.cfg.excludes)}).status_code == 200
        # a session is NOT the admin token: no maintenance, no scope widening, no self-renewal
        for path, body in [("/indexer/pause", None), ("/ui/nonce", None), ("/backup", {"path": "x.db"}),
                           ("/config/roots", {"set": []}), ("/reindex", {"wipe": True}), ("/remove/path", {"path": "x"})]:
            r = anon.post(path, headers=h, json=body) if body is not None else anon.post(path, headers=h)
            assert r.status_code == 403, path
        assert anon.get("/search?q=gpu", headers={"x-semsearch-session": sess + "x"}).status_code == 403


def test_expired_nonce_and_session_are_refused(built, monkeypatch):
    app = create_app(built.cfg, state=built)
    with TestClient(app, base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as admin, \
            TestClient(app, base_url="http://127.0.0.1") as anon:
        n = admin.post("/ui/nonce").json()["nonce"]
        app.state.nonces[n] = time.time() - 1
        assert anon.post("/ui/redeem", json={"nonce": n}).status_code == 403
        n2 = admin.post("/ui/nonce").json()["nonce"]
        sess = anon.post("/ui/redeem", json={"nonce": n2}).json()["session"]
        app.state.sessions[sess] = time.time() - 1
        assert anon.get("/search?q=gpu", headers={"x-semsearch-session": sess}).status_code == 403


def test_settings_page_never_stores_the_admin_token():
    from semsearch.ui import PAGE
    assert "localStorage" not in PAGE and "x-semsearch-token" not in PAGE
    assert "sessionStorage" in PAGE and "/ui/redeem" in PAGE


# ---------------------------------------------------------------- live configuration in the API

def test_document_refuses_a_root_removed_at_runtime(built, root, tmp_path, cfg):
    other = tmp_path / "other"
    write(str(other / "zebra.md"), "# Zebra\n\nstriped document\n")
    built.apply_scope(roots=[str(root), str(other)])
    built.indexer.full_build()
    drain(built)
    # production builds the app from the loaded config while AppState holds a device-resolved
    # COPY (resolve_devices -> model_copy); the hashing provider skips that copy, so make it here
    app = create_app(built.cfg.model_copy(deep=True), state=built)
    with TestClient(app, base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as c:
        assert c.get("/document", params={"path": str(other / "zebra.md")}).status_code == 200
        built.apply_scope(roots=[str(root)])        # removed; the sweep has NOT run yet
        assert c.get("/document", params={"path": str(other / "zebra.md")}).status_code == 404
        assert c.post("/search", json={"query": "striped", "mode": "literal"}).json()["results"] == []


# ---------------------------------------------------------------- extractor child protocol

def test_child_reply_is_validated_not_unpickled():
    from semsearch.extract.isolated import _decode, _encode, _valid_reply
    assert _valid_reply(_decode(_encode(["text", "ok", "pypdf", None, {"pages": 1}]))).text == "text"
    for bad in (["x", "ok"], ["x", "pwned", "m", None, {}], [1, "ok", "m", None, {}], {"a": 1}, "str", None):
        assert _valid_reply(bad) is None
    import semsearch.extract.isolated as iso
    src = open(iso.__file__, encoding="utf-8").read()
    assert ".recv()" not in src and ".send(" not in src  # no pickle-based Connection.send/recv anywhere


@pytest.mark.skipif(not WIN, reason="child process protocol exercised on Windows")
def test_malicious_child_reply_cannot_run_code_in_the_parent(cfg, tmp_path):
    """A compromised child (parser exploit) answers with a pickle that would run code when
    unpickled. The parent must treat it as a malformed reply."""
    import pickle
    from semsearch.extract.isolated import IsolatedExtractor
    from semsearch.extract.registry import build_default_registry
    marker = tmp_path / "pwned.txt"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    ex = IsolatedExtractor(cfg, build_default_registry(cfg), timeout_s=10)
    parent, child = ex._ctx.Pipe()
    ex._conn = parent

    class Alive:
        def is_alive(self):
            return True
        pid = os.getpid()

        def terminate(self):
            pass

        def join(self, t=None):
            pass
    ex._proc = Alive()

    def fake_child():
        child.recv_bytes()
        child.send_bytes(pickle.dumps(Evil()))
    t = threading.Thread(target=fake_child, daemon=True)
    t.start()
    r = ex.extract(str(tmp_path / "x.pdf"), ".pdf")
    t.join(5)
    assert r.status == "error" and not marker.exists()


def test_oversized_child_reply_is_refused(cfg, tmp_path):
    from semsearch.extract.isolated import IsolatedExtractor, _encode
    from semsearch.extract.registry import build_default_registry
    cfg.indexing.max_text_chars = 10
    ex = IsolatedExtractor(cfg, build_default_registry(cfg), timeout_s=10)
    parent, child = ex._ctx.Pipe()
    ex._conn = parent

    class Alive:
        pid = os.getpid()

        def is_alive(self):
            return True

        def terminate(self):
            pass

        def join(self, t=None):
            pass
    ex._proc = Alive()

    def fake_child():
        child.recv_bytes()
        child.send_bytes(_encode(["A" * (ex._max_reply + 10), "ok", "pypdf", None, {}]))
    t = threading.Thread(target=fake_child, daemon=True)
    t.start()
    r = ex.extract(str(tmp_path / "x.pdf"), ".pdf")
    t.join(5)
    assert r.status == "error"


# ---------------------------------------------------------------- junction swaps

def _mklink_j(link, target):
    import subprocess
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"mklink failed: {r.stderr or r.stdout}")


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_directory_swapped_for_a_junction_after_a_check_is_rejected(tmp_path):
    """The probe that defeated the 120 s reparse cache: check once (directory is real), swap
    the directory for a junction to a victim folder, check again immediately."""
    from semsearch.security import PathRejected, check_indexable
    root = tmp_path / "root"
    (root / "D").mkdir(parents=True)
    (root / "D" / "a.txt").write_text("fine")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "a.txt").write_text("VICTIM SECRET")
    assert check_indexable(str(root / "D" / "a.txt"), [str(root)]).size == 4
    (root / "D" / "a.txt").unlink()
    (root / "D").rmdir()
    _mklink_j(root / "D", victim)
    with pytest.raises(PathRejected):
        check_indexable(str(root / "D" / "a.txt"), [str(root)])


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_swap_during_extraction_is_not_recorded(built, root, tmp_path):
    """The directory is real when the job checks it, becomes a junction while the extractor runs:
    nothing extracted through the junction may be stored."""
    sub = root / "swapme"
    write(str(sub / "doc.txt"), "ordinary text in the real folder\n")
    victim = tmp_path / "victim"
    write(str(victim / "doc.txt"), "VICTIM SECRET TEXT\n")
    real = built.indexer.extractor.extract

    def swapping(path, ext):
        res = real(path, ext)
        # after the read, replace the directory with a junction to the victim
        os.replace(str(sub), str(tmp_path / "moved_away"))
        _mklink_j(sub, victim)
        res.text = open(path, encoding="utf-8").read()  # what an extractor would read through the junction
        return res
    built.indexer.extractor.extract = swapping
    built.indexer.index_path(str(sub / "doc.txt"))
    res = [r for _, _, r in drain(built)]
    assert "indexed" not in res
    assert built.retriever.search("VICTIM SECRET", "literal", 5)["results"] == []


def test_backup_destination_rules(built, tmp_path):
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.post("/backup", json={"path": "ok-1.db"}).status_code == 200
        for bad in ("sub/x.db", "..\\x.db", "x.db:stream", "CON", "nul.db", "a b.db", str(tmp_path / "x.db"), ""):
            assert c.post("/backup", json={"path": bad}).status_code == 403, bad
        # an absolute path naming the backup dir itself is accepted
        assert c.post("/backup", json={"path": str(built.cfg.backup_dir / "ok-2.db")}).status_code == 200


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_backup_refuses_a_junctioned_backup_directory(built, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bdir = built.cfg.backup_dir
    if bdir.exists():
        import shutil
        shutil.rmtree(bdir)
    _mklink_j(bdir, elsewhere)
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.post("/backup", json={"path": "x.db"}).status_code == 403
    assert not (elsewhere / "x.db").exists()


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_resolves_inside_detects_a_junction_in_the_chain(tmp_path):
    from semsearch.security import resolves_inside
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.txt").write_text("x")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "a.txt").write_text("y")
    _mklink_j(root / "j", victim)
    assert resolves_inside(str(root / "real" / "a.txt"), [str(root)])
    assert not resolves_inside(str(root / "j" / "a.txt"), [str(root)])                 # resolves outside
    assert not resolves_inside(str(root / "j" / "a.txt"), [str(root), str(victim)])    # inside, but via a junction
    assert resolves_inside(str(root / "j" / "a.txt"), [str(root), str(victim)], follow_reparse=True)



# ---------------------------------------------------------------- scope changes need an explicit grant

def test_roots_api_requires_an_explicit_grant_and_refuses_network_paths(built, root, tmp_path):
    from conftest import grant_self
    target = tmp_path / "readable_but_not_shared"
    target.mkdir()
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.post("/config/roots", json={"add": str(target)}).status_code == 403
        assert c.post("/config/roots", json={"set": [str(root), str(target)]}).status_code == 403   # set is checked too
        assert c.post("/config/roots", json={"add": "\\\\server\\share\\docs"}).status_code == 403
        grant_self(target)
        assert c.post("/config/roots", json={"add": str(target)}).status_code == 200
        # roots that are already configured need no new grant (removing and keeping them is free)
        assert c.post("/config/roots", json={"set": [str(root), str(target)]}).status_code == 200


# ---------------------------------------------------------------- YAML injection through a saved value

def test_exclusion_with_a_newline_cannot_inject_configuration(built, tmp_path, cfg):
    cfg_file = tmp_path / "semsearch.yaml"
    cfg_file.write_text('server:\n  host: 127.0.0.1\n  read_token: true\nroots:\n  - "%s"\n' % str(cfg.roots[0]).replace("\\", "/"), encoding="utf-8")
    cfg.source_path = cfg_file
    evil = "*.tmp\napi:\n  read_token: false\n#"
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.post("/config/excludes", json={"excludes": [evil]}).status_code == 400
        assert c.post("/config/excludes", json={"excludes": ["*.tmp\x07"]}).status_code == 400
        assert c.post("/config/excludes", json={"excludes": ["*.tmp"]}).status_code == 200
        assert c.post("/config/excludes", json={"excludes": ["*.bak"]}).status_code == 200
    from semsearch.config import load_config
    reloaded = load_config(cfg_file)
    assert reloaded.api.read_token is True and reloaded.excludes == ["*.bak"]


def test_update_config_lists_refuses_a_write_that_does_not_round_trip(tmp_path):
    from semsearch.config_edit import update_config_lists
    p = tmp_path / "c.yaml"
    p.write_text("roots:\n  - \"C:/a\"\nlog_level: INFO\n", encoding="utf-8")
    with pytest.raises(ValueError):
        update_config_lists(p, excludes=["ok", "bad\nlog_level: DEBUG"])
    assert "DEBUG" not in p.read_text(encoding="utf-8")


def test_backslash_exclusions_apply_live_as_paths(built, root, cfg):
    write(str(root / "Private" / "diary.md"), "# diary\n\nsecret thoughts\n")
    built.indexer.index_path(str(root / "Private"))
    drain(built)
    assert built.retriever.search("secret thoughts", "literal", 1)["results"]
    built.apply_scope(excludes=list(cfg.excludes) + [str(root / "Private") + "\\**"])
    assert built.retriever.search("secret thoughts", "literal", 1)["results"] == []   # immediately, before the sweep


# ---------------------------------------------------------------- the token goes only to the service

def test_token_is_withheld_from_a_listener_that_is_not_the_service(monkeypatch):
    import semsearch.clientauth as ca
    monkeypatch.setattr(ca, "_service_pid", lambda name: 4242)
    monkeypatch.setattr(ca, "_listener_pids", lambda port: {9999})
    h, warn = ca.token_headers("http://127.0.0.1:8765", "T" * 64)
    assert h == {} and "not the SemSearch service" in warn
    monkeypatch.setattr(ca, "_listener_pids", lambda port: {4242})
    assert ca.token_headers("http://127.0.0.1:8765", "T" * 64)[0] == {"x-semsearch-token": "T" * 64}
    monkeypatch.setattr(ca, "_service_pid", lambda name: 0)   # installed but stopped: whoever listens is not it
    assert ca.token_headers("http://127.0.0.1:8765", "T" * 64)[0] == {}
    assert ca.token_headers("http://evil.example:8765", "T" * 64)[0] == {}
    assert ca.token_headers("http://10.1.2.3:8765", "T" * 64)[0] == {}


def test_cli_ignores_a_config_in_the_current_folder_when_a_machine_config_exists(tmp_path, monkeypatch):
    import semsearch.config as c
    machine = tmp_path / "machine.yaml"
    machine.write_text("log_level: INFO\n", encoding="utf-8")
    monkeypatch.setattr(c, "machine_config_path", lambda: machine)
    monkeypatch.delenv("SEMSEARCH_CONFIG", raising=False)
    work = tmp_path / "cloned_repo"
    work.mkdir()
    (work / "semsearch.yaml").write_text("server:\n  port: 18999\n", encoding="utf-8")
    monkeypatch.chdir(work)
    assert c.load_config().source_path == machine


def test_service_rotates_the_admin_token_at_start(cfg):
    from semsearch.app_state import load_or_create_admin_token
    a = load_or_create_admin_token(cfg)
    assert load_or_create_admin_token(cfg) == a                 # clients and --local reuse it
    b = load_or_create_admin_token(cfg, rotate=True)            # the service replaces it
    assert b != a and (cfg.state_path / "admin.token").read_text() == b


# ---------------------------------------------------------------- request limits and headers

def test_request_limits_and_ui_headers(built):
    with _client(built, **{"x-semsearch-token": built.admin_token}) as c:
        assert c.post("/search", json={"query": "x" * 2001}).status_code == 422
        assert c.post("/search", json={"query": "x", "roots": ["C:/r"] * 33}).status_code == 422
        assert c.post("/config/excludes", json={"excludes": ["a"] * 1001}).status_code == 422
        big = b'{"query": "' + b"x" * (1 << 20) + b'"}'
        assert c.post("/search", content=big, headers={"content-type": "application/json"}).status_code == 413
        r = c.get("/ui")
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    with _client(built) as anon:
        h = anon.get("/health").json()
        assert h["ok"] and "documents" not in h and "embedding" not in h   # liveness only without the token


def test_apply_scope_rejects_control_characters_without_a_config_file(built, cfg):
    cfg.source_path = None   # nothing persisted: the in-memory guard alone must refuse
    with pytest.raises(ValueError):
        built.apply_scope(excludes=["*.tmp\nnot-a-pattern"])
    with pytest.raises(ValueError):
        built.apply_scope(roots=[str(cfg.roots[0]) + "\x07"])
    assert "*.tmp\nnot-a-pattern" not in built.cfg.excludes


def test_round_trip_guard_catches_a_broken_block_writer(tmp_path, monkeypatch):
    import semsearch.config_edit as ce
    p = tmp_path / "c.yaml"
    p.write_text('roots:\n  - "C:/a"\nlog_level: INFO\n', encoding="utf-8")
    real = ce.set_list_block
    monkeypatch.setattr(ce, "set_list_block", lambda text, key, values, comment=None, **k: real(text, key, values, comment) + "log_level: DEBUG\n")
    with pytest.raises(ValueError):
        ce.update_config_lists(p, roots=["C:/b"])
    assert p.read_text(encoding="utf-8") == 'roots:\n  - "C:/a"\nlog_level: INFO\n'


def test_filtered_glob_is_not_starved_by_matches_outside_the_filter(built, root, cfg):
    cfg.retrieval.candidate_docs = 1
    for i in range(30):
        write(str(root / "many" / f"decoy{i:02d}.md"), f"# decoy {i}\n")
    write(str(root / "rare" / "target.md"), "# target\n")
    built.indexer.index_path(str(root))
    drain(built)
    r = built.retriever.search("*.md", "literal", 10, roots=[str(root / "rare")])
    assert [h["filename"] for h in r["results"]] == ["target.md"]
    r = built.retriever.search("*", "literal", 10, extensions=["yaml"])
    assert [h["filename"] for h in r["results"]] == ["report.yaml"]
