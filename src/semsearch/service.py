"""Windows service host for semsearch.

This is a real SCM service: it is hosted in the installed runtime's python (no console),
reports START_PENDING while the store opens and the model loads, declares RUNNING only after
the HTTP API answers /health, handles STOP / SHUTDOWN / PRESHUTDOWN with a bounded drain,
holds a global mutex so a second instance exits immediately, and mirrors lifecycle events to
the Windows Application event log.

Registration (done by the installer, elevated):
    python -m semsearch.service install  --account "NT SERVICE\\SemSearch"  (or --account LocalSystem ...)
    python -m semsearch.service remove
Manual run for diagnostics (console, Ctrl-C to stop):
    python -m semsearch.service debug
"""
from __future__ import annotations

import io
import logging
import os
import socket
import sys
import threading
import time
import traceback

from . import __version__
from .config import Config, ConfigError, load_config, machine_config_path

log = logging.getLogger("semsearch.service")
MUTEX_NAME = "Global\\SemSearch.Service"


class ServiceRuntime:
    """Everything the service does between START_PENDING and STOPPED, independent of pywin32
    so it can be exercised in a console (``debug``) and in tests."""

    def __init__(self, cfg: Config, progress=None):
        self.cfg = cfg
        self.progress = progress or (lambda msg: None)
        self.state = None
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.admin_token: str | None = None

    # ---- startup ----
    def start(self) -> None:
        from .app_state import AppState
        cfg = self.cfg
        problems = cfg.validate_for_startup()
        hard = [p for p in problems if "no roots" in p or "malformed" in p or "not defined" in p or "out of range" in p or "not loopback" in p or "not an absolute" in p]
        if hard:
            raise ConfigError("configuration rejected:\n  " + "\n  ".join(hard))
        for r in cfg.roots:
            if not os.path.isdir(r):
                log.warning("configured root does not exist (will be skipped until it appears): %s", r)
        os.makedirs(cfg.index_path, exist_ok=True)
        os.makedirs(cfg.state_path, exist_ok=True)
        os.makedirs(cfg.model_cache_dir, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(cfg.model_cache_dir))
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        log.info("semsearch %s starting; config=%s index=%s state=%s logs=%s", __version__, cfg.source_path, cfg.index_path, cfg.state_path, cfg.log_dir)
        log.info("roots: %s", [str(r) for r in cfg.roots])
        if cfg.indexing.low_priority:
            self._lower_priority()
        self.progress("opening store and loading embedding model")
        self.state = AppState(cfg, start_indexer=False, isolate_extractors=True)
        self.admin_token = self.state.admin_token
        self.progress("starting API")
        self._start_api()
        self._wait_health()
        self.progress("starting indexer")
        self.state.indexer.start()
        log.info("service ready: http://%s:%d, devices=%s, documents=%d", cfg.api.host, cfg.api.port,
                 self.state.device_resolution, self.state.store.count_documents())

    def _lower_priority(self) -> None:
        try:
            import win32api
            import win32process
            win32process.SetPriorityClass(win32api.GetCurrentProcess(), win32process.BELOW_NORMAL_PRIORITY_CLASS)
            log.info("process priority: below normal")
        except Exception as e:  # noqa: BLE001
            log.debug("could not lower priority: %s", e)

    def _start_api(self) -> None:
        _null_std_streams()  # pythonw / Session 0: sys.stdout is None and libraries call .isatty()
        import uvicorn
        from .api import create_app
        app = create_app(self.cfg, self.state)
        # log_config=None: keep our logging setup instead of uvicorn's dictConfig (which probes the console)
        config = uvicorn.Config(app, host=self.cfg.api.host, port=self.cfg.api.port, log_level="warning",
                                access_log=self.cfg.api.log_requests, log_config=None)
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(target=self.server.run, name="semsearch-api", daemon=True)
        self.server_thread.start()
        deadline = time.time() + 60
        while not self.server.started and time.time() < deadline:
            if not self.server_thread.is_alive():
                raise RuntimeError(f"API server exited during startup (port {self.cfg.api.port} in use?)")
            time.sleep(0.1)
        if not self.server.started:
            raise RuntimeError("API server did not start within 60s")

    def _wait_health(self) -> None:
        import httpx
        url = f"http://{self.cfg.api.host}:{self.cfg.api.port}/health"
        deadline = time.time() + 30
        last = None
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=5.0)
                if r.status_code == 200 and r.json().get("ok"):
                    return
                last = f"{r.status_code} {r.text[:200]}"
            except Exception as e:  # noqa: BLE001
                last = str(e)
            time.sleep(0.5)
        raise RuntimeError(f"API did not report healthy: {last}")

    # ---- shutdown ----
    def stop(self, timeout_s: float | None = None) -> None:
        timeout_s = timeout_s if timeout_s is not None else self.cfg.service.shutdown_timeout_s
        t0 = time.time()
        deadline = t0 + timeout_s
        log.info("service stopping (budget %.0fs)", timeout_s)
        if self.state is not None:
            try:
                # end-to-end deadline: the indexer gets 70% of the budget including a stuck extractor
                self.state.indexer.stop(timeout=max(3.0, (deadline - time.time()) * 0.7))
            except Exception as e:  # noqa: BLE001
                log.warning("indexer stop: %s", e)
        if self.server is not None:
            self.server.should_exit = True
            if self.server_thread is not None:
                self.server_thread.join(max(1.0, deadline - time.time()))
        if self.state is not None:
            try:
                self.state.close_without_indexer()
            except Exception as e:  # noqa: BLE001
                log.warning("close: %s", e)
        log.info("service stopped in %.1fs", time.time() - t0)


class _NullStream(io.TextIOBase):
    """Discards writes and is not a TTY. (os.devnull on Windows is the NUL character device,
    whose isatty() is True, which would make libraries emit ANSI colour codes.)"""

    def write(self, s: str) -> int:
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def _null_std_streams() -> None:
    """A windowless process (pythonw.exe, services) has sys.stdout/sys.stderr == None; give
    them a null sink so any library that writes or probes them keeps working."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _NullStream())


def host_interpreter() -> str:
    """The real interpreter to register as the service binary. A venv's Scripts\\python.exe
    is a launcher that spawns the base interpreter as a child, which the SCM reports as 'a
    service process other than the one launched connected'; so always use the base
    interpreter, windowless variant when present."""
    base = getattr(sys, "_base_executable", None) or sys.executable
    w = os.path.join(os.path.dirname(base), "pythonw.exe")
    return w if os.path.exists(w) else base


def _single_instance_or_exit() -> object | None:
    """Return the held mutex handle, or None if another instance already holds it."""
    try:
        import pywintypes
        import win32api
        import win32event
        import winerror
        try:
            h = win32event.CreateMutex(None, False, MUTEX_NAME)
        except pywintypes.error as e:
            if e.winerror == winerror.ERROR_ACCESS_DENIED:
                return None  # exists and is owned by another account (the real service): same answer
            raise
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return None
        return h
    except ImportError:
        return object()


def _event_log_handler(name: str) -> logging.Handler | None:
    try:
        import servicemanager

        class EventLogHandler(logging.Handler):
            def emit(self, record):
                try:
                    msg = self.format(record)[:4000]
                    if record.levelno >= logging.ERROR:
                        servicemanager.LogErrorMsg(msg)
                    elif record.levelno >= logging.WARNING:
                        servicemanager.LogWarningMsg(msg)
                    else:
                        servicemanager.LogInfoMsg(msg)
                except Exception:  # noqa: BLE001
                    pass

        h = EventLogHandler(level=logging.WARNING)
        h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        return h
    except Exception:  # noqa: BLE001
        return None


def run_console(config_path: str | None = None) -> int:
    """``debug`` mode: the full service runtime in the foreground."""
    from .logging_setup import setup_logging
    cfg = load_config(config_path or (str(machine_config_path()) if machine_config_path().is_file() else None))
    setup_logging(cfg.log_dir, cfg.log_level, to_stderr=True)
    if _single_instance_or_exit() is None:
        print("another semsearch service instance is already running", file=sys.stderr)
        return 3
    rt = ServiceRuntime(cfg, progress=lambda m: log.info("startup: %s", m))
    try:
        rt.start()
    except ConfigError as e:
        log.error("%s", e)
        return 2
    print("running; Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    rt.stop()
    return 0


def _make_service_class():
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    import winerror

    class SemSearchService(win32serviceutil.ServiceFramework):
        _svc_name_ = "SemSearch"
        _svc_display_name_ = "Semantic Search (semsearch)"
        _svc_description_ = "Local semantic file search sidecar: maintains an embedding index of configured folders and serves a loopback HTTP API."
        _exe_name_ = host_interpreter()
        _exe_args_ = "-s -m semsearch.service"  # -s: never import from a user's site-packages

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.runtime: ServiceRuntime | None = None
            self.mutex = None

        def GetAcceptedControls(self):
            rc = super().GetAcceptedControls()
            return rc | win32service.SERVICE_ACCEPT_PRESHUTDOWN | win32service.SERVICE_ACCEPT_SHUTDOWN

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=int(1000 * (self.runtime.cfg.service.shutdown_timeout_s + 10) if self.runtime else 40000))
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcOtherEx(self, control, event_type, data):
            if control == win32service.SERVICE_CONTROL_PRESHUTDOWN:
                self.SvcStop()

        def SvcDoRun(self):
            cfg = None
            try:
                self.mutex = _single_instance_or_exit()
                if self.mutex is None:
                    servicemanager.LogErrorMsg("semsearch: another instance holds the service mutex; exiting")
                    self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=winerror.ERROR_SERVICE_ALREADY_RUNNING)
                    return
                cfg = load_config(CONFIG_PATH or (str(machine_config_path()) if machine_config_path().is_file() else None))
                from .logging_setup import setup_logging
                setup_logging(cfg.log_dir, cfg.log_level, to_stderr=False)
                if cfg.service.event_log:
                    h = _event_log_handler(cfg.service.name)
                    if h:
                        logging.getLogger().addHandler(h)
                servicemanager.LogInfoMsg(f"semsearch {__version__} starting (config {cfg.source_path})")

                def progress(msg: str):
                    log.info("startup: %s", msg)
                    self.ReportServiceStatus(win32service.SERVICE_START_PENDING, waitHint=int(cfg.service.startup_timeout_s * 1000))

                self.runtime = ServiceRuntime(cfg, progress=progress)
                self.runtime.start()
                self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                servicemanager.LogInfoMsg(f"semsearch {__version__} running on http://{cfg.api.host}:{cfg.api.port}")
                win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
                self.runtime.stop()
                servicemanager.LogInfoMsg("semsearch stopped")
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
            except ConfigError as e:
                servicemanager.LogErrorMsg(f"semsearch configuration error: {e}")
                log.error("configuration error: %s", e)
                self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=winerror.ERROR_SERVICE_SPECIFIC_ERROR, svcExitCode=2)
            except Exception:  # noqa: BLE001
                tb = traceback.format_exc()
                servicemanager.LogErrorMsg(f"semsearch failed: {tb[-3000:]}")
                log.error("service failed: %s", tb)
                try:
                    if self.runtime:
                        self.runtime.stop(10)
                except Exception:  # noqa: BLE001
                    pass
                self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=winerror.ERROR_SERVICE_SPECIFIC_ERROR, svcExitCode=1)

    return SemSearchService


def _service_exists(name: str) -> bool:
    import pywintypes
    import win32serviceutil
    try:
        win32serviceutil.QueryServiceStatus(name)
        return True
    except pywintypes.error as e:
        if e.winerror == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
            return False
        raise


CONFIG_PATH: str | None = None  # from --config on the service command line (set by the installer)


def config_path_from_argv(argv: list[str]) -> str | None:
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def install(account: str | None, start_type: str = "delayed", description: str | None = None, config_path: str | None = None) -> None:
    """Register (or re-configure, when it already exists) the service. Idempotent. The
    configuration file path is baked into the service command line so a custom data
    directory does not depend on the default %ProgramData% discovery."""
    import win32service
    import win32serviceutil
    cls = _make_service_class()
    exe_name = cls._exe_name_
    exe_args = cls._exe_args_ + (f' --config "{config_path}"' if config_path else "")
    st = {"auto": win32service.SERVICE_AUTO_START, "delayed": win32service.SERVICE_AUTO_START, "demand": win32service.SERVICE_DEMAND_START}[start_type]
    if _service_exists(cls._svc_name_):
        win32serviceutil.ChangeServiceConfig(None, cls._svc_name_, startType=st, exeName=exe_name, exeArgs=exe_args,
                                             displayName=cls._svc_display_name_, description=description or cls._svc_description_,
                                             userName=account, delayedstart=(start_type == "delayed"))
        print(f"reconfigured existing service {cls._svc_name_}")
    else:
        win32serviceutil.InstallService(None, cls._svc_name_, cls._svc_display_name_, startType=st, exeName=exe_name, exeArgs=exe_args,
                                        description=description or cls._svc_description_, userName=account, delayedstart=(start_type == "delayed"))
    try:
        import win32evtlogutil
        import servicemanager
        win32evtlogutil.AddSourceToRegistry(cls._svc_name_, servicemanager.__file__, "Application")
    except Exception as e:  # noqa: BLE001
        print(f"event log source not registered: {e}", file=sys.stderr)
    print(f"installed service {cls._svc_name_}: {exe_name} {exe_args} (account={account or 'LocalSystem'}, start={start_type})")


def remove() -> None:
    import win32serviceutil
    cls = _make_service_class()
    if not _service_exists(cls._svc_name_):
        print(f"service {cls._svc_name_} is not installed; nothing to remove")
        return
    try:
        win32serviceutil.StopService(cls._svc_name_)
    except Exception:  # noqa: BLE001
        pass
    win32serviceutil.RemoveService(cls._svc_name_)
    try:
        import win32evtlogutil
        win32evtlogutil.RemoveSourceFromRegistry(cls._svc_name_, "Application")
    except Exception:  # noqa: BLE001
        pass
    print(f"removed service {cls._svc_name_}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "debug":
        return run_console(argv[1] if len(argv) > 1 else None)
    if argv and argv[0] == "install":
        account = None
        start_type = "delayed"
        if "--account" in argv:
            account = argv[argv.index("--account") + 1]
        if "--start" in argv:
            start_type = argv[argv.index("--start") + 1]
        install(account, start_type, config_path=config_path_from_argv(argv))
        return 0
    if argv and argv[0] == "remove":
        remove()
        return 0
    # started by the SCM: the command line may carry --config
    global CONFIG_PATH
    CONFIG_PATH = config_path_from_argv(argv)
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(_make_service_class())
    servicemanager.StartServiceCtrlDispatcher()
    return 0


if __name__ == "__main__":
    sys.exit(main())
