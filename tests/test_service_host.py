"""Service-host details that only bite in Session 0 (found by running the real service)."""
import os
import sys

import pytest

from semsearch.service import _null_std_streams, host_interpreter

WIN = sys.platform == "win32"


def test_host_interpreter_is_the_real_interpreter_not_a_venv_launcher():
    h = host_interpreter()
    assert os.path.isfile(h)
    base = getattr(sys, "_base_executable", None) or sys.executable
    assert os.path.dirname(h).lower() == os.path.dirname(base).lower()
    assert "scripts" not in os.path.basename(os.path.dirname(h)).lower()  # never <venv>\Scripts\python.exe


def test_null_std_streams_restores_missing_stdout(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    _null_std_streams()
    assert sys.stdout is not None and sys.stdout.isatty() is False
    sys.stdout.write("harmless")


@pytest.mark.skipif(not WIN, reason="pywin32")
def test_service_class_builds_and_uses_valid_win32_constants():
    import winerror
    from semsearch.service import _make_service_class
    cls = _make_service_class()
    assert cls._svc_name_ == "SemSearch"
    assert cls._exe_name_.lower().endswith(("pythonw.exe", "python.exe"))
    assert cls._exe_args_ == "-s -m semsearch.service"  # user site-packages must stay invisible to the service
    # the failure paths report these; a typo here only shows up inside the SCM
    assert isinstance(winerror.ERROR_SERVICE_SPECIFIC_ERROR, int) and isinstance(winerror.ERROR_SERVICE_ALREADY_RUNNING, int)
    src = open(__import__("semsearch.service", fromlist=["x"]).__file__, encoding="utf-8").read()
    assert "win32service.ERROR_" not in src


def test_uvicorn_config_does_not_touch_stdout(monkeypatch, cfg):
    """uvicorn.Config with log_config=None must not probe the console (the service runs windowless)."""
    import uvicorn
    monkeypatch.setattr(sys, "stdout", None)
    _null_std_streams()
    from semsearch.api import create_app
    app = create_app(cfg)
    uvicorn.Config(app, host="127.0.0.1", port=1, log_level="warning", log_config=None)
