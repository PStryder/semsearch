"""Windows Search scope import, textual config editing, live root/exclusion changes through the
API, the roots CLI helpers and the tray's pure parts."""
import os
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import drain, write
from semsearch.api import create_app
from semsearch.config_edit import list_block_values, set_list_block, update_config_lists
from semsearch.inventory.scope import ScopeRule, parse_rule, resolve_rules, suggest
from semsearch.security import normalize_path

WIN = sys.platform == "win32"

G_C = "50bef29e-274d-4c48-88ea-6cb293b528be"
G_D1 = "150e164d-6e9f-436d-8ca2-f80bc51d3bf3"
G_D2 = "153c833f-77e0-402d-bf27-7baed76b7775"


def rule(url, include=1, nocontent=0, default=0):
    return (url, {"URL": url, "Include": include, "NoContent": nocontent, "Default": default})


# ---------------------------------------------------------------- scope rules

def test_parse_rule_fields():
    r = parse_rule(*rule(f"file:///C:\\[{G_C}]\\Users\\*\\AppData\\", include=0, default=1))
    assert r.drive == "C" and r.volume_guid == G_C and r.rest == "Users\\*\\AppData" and not r.include and r.content and r.default
    assert r.has_wildcard
    nf = parse_rule(*rule("winrt://{S-1-5-21}/"))
    assert not nf.is_file_rule


def test_resolve_by_letter_with_ambiguity_guard(tmp_path):
    raw = [rule(f"file:///C:\\[{G_C}]\\Users\\"), rule(f"file:///D:\\[{G_D1}]\\", nocontent=1), rule(f"file:///D:\\[{G_D2}]\\", nocontent=1),
           rule(f"file:///D:\\[{G_D1}]\\Photos\\"), rule(f"file:///Z:\\[{G_C}]\\x\\")]
    rs = resolve_rules(raw, mounted={"C", "D"})
    by = {r.url: r for r in rs}
    assert by[raw[0][0]].mounted_drive == "C"                    # unambiguous, mounted
    assert by[raw[1][0]].mounted_drive == "D"                    # ambiguous letter but the volume root exists
    assert by[raw[3][0]].mounted_drive is None                   # ambiguous letter, path does not exist -> not applied
    assert by[raw[4][0]].mounted_drive is None                   # Z: not mounted


def test_suggest_narrows_users_to_profile_and_translates_excludes(tmp_path, monkeypatch):
    users = tmp_path / "Users"
    me = users / "me"
    (me / "Documents").mkdir(parents=True)
    (tmp_path / "Data").mkdir()
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
    (tmp_path / "Program Files" / "Office").mkdir(parents=True)
    drive = str(tmp_path)[0].upper()
    rel = str(tmp_path)[3:]

    def mk(sub, **kw):
        r = parse_rule(*rule(f"file:///{drive}:\\[{G_C}]\\{rel}\\{sub}\\", **kw))
        r.mounted_drive = drive
        return r

    rules = [mk("Users"), mk("Users\\me\\Documents"), mk("Data"), mk("Program Files\\Office", default=1), mk("", nocontent=1),
             mk("Users\\*\\AppData", include=0), mk("Users\\me\\.*", include=0), mk("Missing")]
    s = suggest(rules, profile_dir=str(me), users_dir=str(users))
    assert [os.path.normcase(r) for r in s.roots] == [os.path.normcase(str(tmp_path / "Data")), os.path.normcase(str(me))]  # Documents nested in the profile; Users -> me
    assert any("system location" in x for x in s.skipped) and any("does not exist" in x for x in s.skipped)
    g = str(tmp_path).replace("\\", "/").lower()
    assert f"{g}/users/*/appdata/**" in s.excludes and f"{g}/users/me/.*/**" in s.excludes


@pytest.mark.skipif(not WIN, reason="reads the live Windows Search registry")
def test_live_windows_scope_reads_without_elevation():
    from semsearch.inventory.scope import windows_scope_rules, windows_scope_suggestion
    rules = windows_scope_rules()
    assert rules and any(r.is_file_rule for r in rules)
    s = windows_scope_suggestion()
    assert all(os.path.isdir(r) for r in s.roots)
    assert all(e.endswith("/**") and ":" in e for e in s.excludes)


# ---------------------------------------------------------------- textual config editing

YAML = """# SemSearch configuration
data_dir: "C:/ProgramData/SemSearch"
roots:
  - "F:/HexyLab"   # main
  # - "F:/Other"

server:
  host: 127.0.0.1
  port: 8765
# retrieval follows
retrieval:
  fusion: convex
"""


def test_set_list_block_replaces_only_the_block():
    out = set_list_block(YAML, "roots", ["F:/HexyLab", "C:/Users/me/Documents"])
    assert list_block_values(out, "roots") == ["F:/HexyLab", "C:/Users/me/Documents"]
    assert "# SemSearch configuration" in out and "  host: 127.0.0.1" in out and "# retrieval follows\nretrieval:" in out
    assert "F:/Other" not in out
    import yaml
    assert yaml.safe_load(out)["server"]["port"] == 8765


def test_set_list_block_appends_missing_key_and_empty_list():
    out = set_list_block(YAML, "excludes", [])
    assert list_block_values(out, "excludes") == []
    out2 = set_list_block(out, "excludes", ["**/.git/**", "C:/x y/**"])
    assert list_block_values(out2, "excludes") == ["**/.git/**", "C:/x y/**"]


def test_set_list_block_keeps_crlf_endings():
    crlf = YAML.replace("\n", "\r\n")
    out = set_list_block(crlf, "roots", ["D:/docs", "E:/more"])
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")  # no mixed endings
    assert list_block_values(out, "roots") == ["D:/docs", "E:/more"]


def test_update_config_lists_keeps_bom_free_utf8(tmp_path):
    p = tmp_path / "semsearch.yaml"
    p.write_bytes(b"\xef\xbb\xbf" + YAML.encode("utf-8"))  # PowerShell Set-Content -Encoding utf8 writes a BOM
    text = update_config_lists(p, roots=["D:/docs"])
    assert list_block_values(text, "roots") == ["D:/docs"] and not p.read_bytes().startswith(b"\xef\xbb\xbf")


# ---------------------------------------------------------------- live scope changes

def _client(st, token=None):
    return TestClient(create_app(st.cfg, state=st), base_url="http://127.0.0.1", headers={"x-semsearch-token": token} if token else {})


def test_add_and_remove_root_live(built, root, tmp_path, cfg):
    cfg_file = tmp_path / "semsearch.yaml"
    cfg_file.write_text(f'roots:\n  - "{str(root).replace(chr(92), "/")}"\n', encoding="utf-8")
    cfg.source_path = cfg_file
    other = tmp_path / "other"
    write(str(other / "zebra.md"), "# Zebra\n\nthe striped zebra document\n")
    with _client(built, built.admin_token) as c:
        assert c.post("/config/roots", json={"add": str(other)}).status_code == 200
        assert c.post("/config/roots", json={"add": str(tmp_path / "nope")}).status_code == 400
        d = c.get("/config").json()
        assert [os.path.normcase(r) for r in d["roots"]] == [os.path.normcase(str(root)), os.path.normcase(str(other))]
    assert list_block_values(cfg_file.read_text(encoding="utf-8"), "roots")[-1] == str(other).replace("\\", "/")
    # the scheduler full-builds a root without a checkpoint; drive it by hand here
    built.indexer.full_build()
    drain(built)
    assert built.retriever.search("striped zebra", "literal", 1)["results"][0]["filename"] == "zebra.md"
    with _client(built, built.admin_token) as c:
        assert c.post("/config/roots", json={"remove": str(other)}).status_code == 200
    built.indexer._run_pending_policy()
    assert built.retriever.search("striped zebra", "literal", 1)["results"] == []
    assert built.store.get_document(normalize_path(str(other / "zebra.md"))) is None
    assert built.retriever.search("GPU memory", "literal", 1)["results"][0]["filename"] == "gpu.txt"  # the original root is untouched


def test_root_changes_need_admin_and_excludes_apply_live(built, root, cfg):
    with _client(built) as c:
        assert c.post("/config/roots", json={"add": str(root)}).status_code == 403
        assert c.get("/config").status_code == 200
        assert c.get("/ui").status_code == 200 and "SemSearch" in c.get("/ui").text
    with _client(built, built.admin_token) as c:
        r = c.post("/config/excludes", json={"excludes": list(cfg.excludes) + ["**/sub/**"]})
        assert r.status_code == 200
    built.indexer._run_pending_policy()
    assert built.store.get_document(normalize_path(str(root / "sub" / "blackboard.py"))) is None
    assert "**/sub/**" in cfg.excludes


def test_reconfigure_restarts_watcher_when_running(built, tmp_path):
    other = tmp_path / "w"
    other.mkdir()
    built.cfg.indexing.watch_filesystem = True
    built.indexer.start()
    try:
        out = built.indexer.reconfigure([str(other)], list(built.cfg.excludes))
        assert out["roots"] == [str(other)] and out["new_roots"] == [str(other)]
        assert built.indexer._watcher is not None and built.indexer._watcher.roots == [str(other)]
    finally:
        built.indexer.stop(timeout=10)


# ---------------------------------------------------------------- roots admin + tray pure parts

def test_add_root_grants_then_posts(monkeypatch, tmp_path):
    from semsearch import roots_admin
    calls = []
    monkeypatch.setattr(roots_admin, "grant_service_read", lambda p, a: (calls.append(("grant", p, a)) or roots_admin.GrantResult(p, True)))

    class C:
        def post(self, url, json=None, headers=None):
            calls.append(("post", url, json, headers))

            class R:
                status_code = 200

                def json(self):
                    return {"roots": [json["add"]], "new_roots": [json["add"]]}
            return R()
    out = roots_admin.add_root(C(), str(tmp_path), "tok", "NT SERVICE\\X")
    assert calls[0][0] == "grant" and calls[1][1] == "/config/roots" and calls[1][3] == {"x-semsearch-token": "tok"}
    assert out["grant"] == "granted"
    with pytest.raises(ValueError):
        roots_admin.add_root(C(), str(tmp_path / "missing"), "tok")


def test_tray_status_line_and_icon_state():
    from semsearch.tray import icon_state, make_icon_image, status_line
    assert status_line(None, None) == "SemSearch: service not reachable" and icon_state(None, None) == "down"
    h = {"version": "0.3.0", "documents": 19317}
    assert status_line(h, {"indexer": {"queue": {"pending": 0}}}) == "SemSearch 0.3.0: 19,317 documents, idle"
    assert status_line(h, {"indexer": {"paused": True, "queue": {"pending": 5}}}).endswith("paused (5 queued)")
    assert icon_state(h, {"indexer": {"paused": True}}) == "paused"
    img = make_icon_image(state="paused")
    assert img.size == (64, 64)


def test_cli_scope_and_roots_list(built, cfg, capsys, monkeypatch):
    from semsearch import cli
    from semsearch.inventory.scope import ScopeSuggestion
    monkeypatch.setattr("semsearch.inventory.scope.windows_scope_suggestion", lambda *a, **k: ScopeSuggestion(roots=["C:\\Users\\me\\Documents"], excludes=["c:/users/me/.*/**"]))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    assert cli.main(["scope"]) == 0
    out = capsys.readouterr().out
    assert "C:\\Users\\me\\Documents" in out and "c:/users/me/.*/**" in out


# ---------------------------------------------------------------- extracted text must be valid Unicode

def test_lone_surrogates_from_a_parser_are_cleaned(built, root):
    from semsearch.extract.registry import clean_text
    bad = "gods help you\ud83dshe remembers\x00"
    assert clean_text(bad) == "gods help you�she remembers"
    real = built.indexer.extractor.extract

    def surrogate_extract(path, ext):
        r = real(path, ext)
        if path.endswith("broken.txt"):
            r.text = r.text + "\ud83d tail"
            r.status = "ok"
        return r
    # the extractor is the registry itself in tests (no child): patch below the registry's cleaning
    # so the provider's own guard is what saves the batch
    built.indexer.extractor.extract = surrogate_extract
    write(str(root / "broken.txt"), "a document whose parser output ends badly\n")
    built.indexer.index_path(str(root / "broken.txt"))
    assert [r for _, _, r in drain(built)] == ["indexed"]
    assert built.retriever.search("parser output ends badly", "literal", 1)["results"][0]["filename"] == "broken.txt"


def test_tray_menu_builds_without_pystray_backend(built, monkeypatch):
    """Every menu callback must exist: a typo there only shows up when a user clicks."""
    from semsearch.tray import TrayApp
    t = TrayApp(built.cfg)
    monkeypatch.setattr(t, "roots", lambda: ["C:\one", "D:\two"])
    t.health, t.status = {"version": "0.3.0", "documents": 3}, {"indexer": {"paused": False, "queue": {}}}
    menu = t.build_menu()
    items = list(menu.items)
    assert any("Folders" in str(i.text) for i in items) and any("Pause indexing" in str(i.text) for i in items)
    folders = next(i for i in items if "Folders" in str(i.text))
    sub = [str(i.text) for i in folders.submenu.items]
    assert sub[:2] == ["Add folder...", "Add the folders Windows Search indexes..."] and "Adopt Windows Search exclusion rules..." in sub
    assert any(s.startswith("Remove C:\one") for s in sub)
    for attr in ("add_folder", "use_windows_scope", "adopt_windows_excludes", "remove_folder", "toggle_pause", "open_settings", "open_logs", "quit"):
        assert callable(getattr(t, attr))
