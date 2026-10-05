"""The hardware choice the tray asks for after setup: profile -> devices on this machine, the
textual YAML edit that records it, the token-gated API that applies it live, and the tray's
one-time question."""
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

from semsearch.api import create_app
from semsearch.config import Config
from semsearch.config_edit import set_section_scalars, update_config_scalars
from semsearch.devices import Adapter, named_present, profile_specs

WIN = sys.platform == "win32"


def _adapter(i, name, integrated, vendor_id, device_id, software=False):
    return Adapter(ordinal=i, name=name, vendor_id=vendor_id, device_id=device_id, subsys_id=0x1, revision=0,
                   dedicated_vram_mb=0 if integrated else 16048, shared_memory_mb=48283, luid=f"luid-{i}",
                   software=software, integrated=integrated, address=f"pci:{i}.0.0")


RADEON = _adapter(0, "AMD Radeon(TM) Graphics", True, 0x1002, 0x164E)
RTX = _adapter(1, "NVIDIA GeForce RTX 4080", False, 0x10DE, 0x2704)
BASIC = _adapter(2, "Microsoft Basic Render Driver", True, 0x1414, 0x8C, software=True)
NAMED = {"integrated-gpu": {"vendor": "0x1002", "device": "0x164e"}, "discrete-gpu": {"vendor": "0x10de", "device": "0x2704"}}


# ---------------------------------------------------------------- profile -> devices

def test_profiles_on_a_machine_with_both_gpus():
    both = [RADEON, RTX, BASIC]
    assert profile_specs("light", NAMED, both) == {"device": "integrated-gpu", "bulk_device": "discrete-gpu"}
    assert profile_specs("gpu", NAMED, both) == {"device": "discrete-gpu", "bulk_device": "discrete-gpu"}


def test_profiles_degrade_with_missing_adapters():
    # no integrated GPU: light means the CPU, with the dedicated GPU still taking the big jobs
    assert profile_specs("light", NAMED, [RTX]) == {"device": "cpu", "bulk_device": "discrete-gpu"}
    # no dedicated GPU: there is no "gpu" choice at all
    assert profile_specs("light", NAMED, [RADEON]) == {"device": "integrated-gpu", "bulk_device": "integrated-gpu"}
    with pytest.raises(ValueError, match="no dedicated GPU"):
        profile_specs("gpu", NAMED, [RADEON, BASIC])
    # a name defined in the config but not present does not count (the software adapter never matches)
    assert named_present({"x": {"vendor": "0x1414"}}, [BASIC]) == {"integrated": None, "dedicated": None}
    with pytest.raises(ValueError, match="unknown hardware profile"):
        profile_specs("turbo", NAMED, [RADEON, RTX])


# ---------------------------------------------------------------- YAML edit

LIVE = """# SemSearch configuration (machine-wide). Edit, then: semsearch service restart
data_dir: "C:/ProgramData/SemSearch"
roots:
  - "F:/HexyLab"
embedding:
  model: BAAI/bge-small-en-v1.5
  devices:              # stable selectors written by the installer from the adapters it found
    integrated-gpu:   # AMD Radeon(TM) Graphics
      vendor: "0x1002"
      device: "0x164e"
    discrete-gpu:     # NVIDIA GeForce RTX 4080
      vendor: "0x10de"
      device: "0x2704"
  steady_state_device: integrated-gpu
  bulk_device: discrete-gpu   # big jobs
  query_device: cpu
  bulk_threshold: 500
# indexing follows
indexing:
  low_priority: true
"""


def test_section_scalars_replace_in_place_and_append_missing():
    out = set_section_scalars(LIVE, "embedding", {"steady_state_device": "discrete-gpu", "bulk_device": "discrete-gpu", "device_profile": "gpu"})
    d = yaml.safe_load(out)
    assert d["embedding"]["steady_state_device"] == "discrete-gpu" and d["embedding"]["device_profile"] == "gpu"
    assert d["embedding"]["devices"]["integrated-gpu"]["device"] == "0x164e"  # nested selectors untouched
    assert "bulk_device: discrete-gpu   # big jobs" in out  # trailing comment kept
    assert "# indexing follows\nindexing:" in out and "  device_profile: gpu\n# indexing follows" in out  # appended inside the section
    # nothing outside the edited keys moved
    assert out.replace("  steady_state_device: discrete-gpu\n", "  steady_state_device: integrated-gpu\n").replace("  device_profile: gpu\n", "") == LIVE
    # the nested `device:` under a selector is NOT a direct child and is never matched
    out2 = set_section_scalars(LIVE, "embedding", {"device": "cpu"})
    assert yaml.safe_load(out2)["embedding"]["devices"]["integrated-gpu"]["device"] == "0x164e"


def test_section_scalars_keep_crlf_and_create_a_missing_section():
    crlf = LIVE.replace("\n", "\r\n")
    out = set_section_scalars(crlf, "embedding", {"device_profile": "light"})
    assert "\n" not in out.replace("\r\n", "") and yaml.safe_load(out)["embedding"]["device_profile"] == "light"
    out2 = set_section_scalars("roots: []\n", "embedding", {"device_profile": "light"})
    assert yaml.safe_load(out2) == {"roots": [], "embedding": {"device_profile": "light"}}


def test_update_config_scalars_refuses_injection(tmp_path):
    p = tmp_path / "semsearch.yaml"
    p.write_text(LIVE, encoding="utf-8")
    for bad in ("cpu\nroots: []", "cpu # x", "a b", '"cpu"', ""):
        with pytest.raises(ValueError):
            update_config_scalars(p, "embedding", {"device_profile": bad})
    assert p.read_text(encoding="utf-8") == LIVE
    update_config_scalars(p, "embedding", {"device_profile": "light"})
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["embedding"]["device_profile"] == "light"


@pytest.mark.parametrize("text", [
    "embedding:\n  model: a\nembedding:\n  model: b\n",   # duplicate section: the parser keeps the last, the editor edits the first
    "embedding: {model: a}\n",                              # flow style: the line editor cannot extend it
])
def test_update_config_scalars_refuses_what_it_cannot_round_trip(tmp_path, text):
    p = tmp_path / "semsearch.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to write"):
        update_config_scalars(p, "embedding", {"device_profile": "light"})
    assert p.read_text(encoding="utf-8") == text


def test_config_reads_the_profile_and_the_alias():
    c = Config.model_validate({"embedding": {"steady_state_device": "integrated-gpu", "device_profile": "gpu"}})
    assert c.embedding.device == "integrated-gpu" and c.embedding.device_profile == "gpu"
    assert Config().embedding.device_profile is None
    with pytest.raises(Exception):
        Config.model_validate({"embedding": {"device_profile": "turbo"}})


# ---------------------------------------------------------------- API, applied live

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)


@pytest.fixture
def hw_app(app, tmp_path, monkeypatch):
    cfg_file = tmp_path / "semsearch.yaml"
    cfg_file.write_text(LIVE, encoding="utf-8")
    app.cfg.source_path = cfg_file
    app.raw_cfg.embedding.devices = dict(NAMED)
    adapters = [RADEON, RTX, BASIC]
    monkeypatch.setattr(type(app), "_adapters", staticmethod(lambda: adapters))
    rec = _Recorder()
    app.embedder.set_role_devices = rec  # the hashing provider has no devices; record what onnx would get
    app._hw = (cfg_file, adapters, rec)
    return app


def _client(st, token=None):
    return TestClient(create_app(st.cfg, state=st), base_url="http://127.0.0.1", headers={"x-semsearch-token": token} if token else {})


def test_api_asks_until_chosen_then_applies_live(hw_app):
    cfg_file, adapters, rec = hw_app._hw
    with _client(hw_app, hw_app.admin_token) as c:
        d = c.get("/config/devices").json()
        assert d["ask"] is True and d["profile"] is None
        assert d["labels"] == {"integrated": "AMD Radeon(TM) Graphics", "dedicated": "NVIDIA GeForce RTX 4080"}
        assert d["options"]["gpu"] == {"device": "discrete-gpu", "bulk_device": "discrete-gpu"}
        r = c.post("/config/devices", json={"profile": "gpu"})
        assert r.status_code == 200, r.text
        d2 = c.get("/config/devices").json()
    assert d2["ask"] is False and d2["profile"] == "gpu"
    on_disk = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["embedding"]
    # the file's own spelling of the steady role is kept (no duplicate `device:` key)
    assert on_disk["steady_state_device"] == "discrete-gpu" and "device" not in on_disk and on_disk["device_profile"] == "gpu"
    assert rec.calls and rec.calls[-1]["steady"] == hw_app.cfg.embedding.device
    assert hw_app.raw_cfg.embedding.device == "discrete-gpu" and hw_app.raw_cfg.embedding.device_profile == "gpu"
    with _client(hw_app, hw_app.admin_token) as c:
        assert c.post("/config/devices", json={"profile": "light"}).status_code == 200
    assert yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["embedding"]["steady_state_device"] == "integrated-gpu"
    assert hw_app.raw_cfg.embedding.bulk_device == "discrete-gpu"


def test_api_needs_admin_and_rejects_unavailable_choices(hw_app, monkeypatch):
    cfg_file, adapters, rec = hw_app._hw
    with _client(hw_app) as c:
        assert c.post("/config/devices", json={"profile": "gpu"}).status_code == 403
        assert c.get("/config/devices").status_code == 403  # reads are token-gated by default
    hw_app.cfg.api.read_token = False  # with open reads, changing the hardware still needs the admin token
    with _client(hw_app) as c:
        assert c.get("/config/devices").status_code == 200
        assert c.post("/config/devices", json={"profile": "gpu"}).status_code == 403
    hw_app.cfg.api.read_token = True
    with _client(hw_app, hw_app.admin_token) as c:
        assert c.post("/config/devices", json={"profile": "turbo"}).status_code == 422
        monkeypatch.setattr(type(hw_app), "_adapters", staticmethod(lambda: [RADEON]))
        assert c.get("/config/devices").json()["ask"] is False  # no dedicated GPU: nothing to ask
        assert c.post("/config/devices", json={"profile": "gpu"}).status_code == 400
    assert cfg_file.read_text(encoding="utf-8") == LIVE and not rec.calls


# ---------------------------------------------------------------- tray

@pytest.mark.skipif(not WIN, reason="Win32 message box constants")
def test_tray_asks_once_and_posts_the_answer(built, monkeypatch):
    import win32api
    import win32con
    from semsearch.tray import TrayApp, hardware_question
    opts = {"ask": True, "editable": True, "profile": None, "present": {"integrated": "integrated-gpu", "dedicated": "discrete-gpu"},
            "labels": {"integrated": "AMD Radeon(TM) Graphics", "dedicated": "NVIDIA GeForce RTX 4080"},
            "options": {"light": {"device": "integrated-gpu"}, "gpu": {"device": "discrete-gpu"}}}
    q = hardware_question(opts)
    assert "YES = Light" in q and "NO = Dedicated GPU" in q and "CANCEL" in q and "NVIDIA GeForce RTX 4080" in q and "AMD Radeon" in q
    t = TrayApp(built.cfg)
    posted, shown = [], []
    monkeypatch.setattr(t, "token", lambda: "tok")
    monkeypatch.setattr(t, "fetch_devices", lambda: opts)
    monkeypatch.setattr(t, "set_hardware", lambda p: posted.append(p) or True)
    for answer, expect in ((win32con.IDYES, "light"), (win32con.IDNO, "gpu"), (win32con.IDCANCEL, None)):
        monkeypatch.setattr(win32api, "MessageBox", lambda *a, _ans=answer: shown.append(a) or _ans)
        assert t.ask_hardware_if_unset() == expect
    assert posted == ["light", "gpu"] and len(shown) == 3
    # already chosen, or no real choice: never shown
    for o in (dict(opts, ask=False), dict(opts, editable=False)):
        monkeypatch.setattr(t, "fetch_devices", lambda _o=o: _o)
        assert t.ask_hardware_if_unset() is None
    assert len(shown) == 3


def test_tray_hardware_menu_marks_the_current_choice(built):
    from semsearch.tray import TrayApp
    t = TrayApp(built.cfg)
    t.health, t.status = {"version": "0.6.0", "documents": 3}, {"indexer": {"paused": False, "queue": {}}}
    t.roots = lambda: []
    t.devices = {"profile": "gpu", "editable": True, "labels": {"integrated": "Radeon", "dedicated": "RTX 4080"},
                 "options": {"light": {"device": "integrated-gpu"}, "gpu": {"device": "discrete-gpu"}}}
    hw = next(i for i in t.build_menu().items if str(i.text) == "Indexing hardware")
    light, gpu = list(hw.submenu.items)
    assert "Light (Radeon" in str(light.text) and "RTX 4080" in str(gpu.text)
    assert gpu.checked and not light.checked and light.enabled and gpu.enabled
    t.devices = dict(t.devices, options={"light": {"device": "integrated-gpu"}, "gpu": None})
    hw = next(i for i in t.build_menu().items if str(i.text) == "Indexing hardware")
    assert not list(hw.submenu.items)[1].enabled
