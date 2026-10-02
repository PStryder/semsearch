"""Phase-1 probe: what changes when semsearch runs as a Windows service (Session 0) under a
given identity? Installed by run_service_probes.ps1 once per candidate account; each run
writes C:\\ProgramData\\SemSearchProbe\\report-<identity>.json and stops itself.

Usage:
  python service_probe.py direct            # run the checks in this console (baseline)
  python service_probe.py                   # service entry point (SCM dispatcher)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback

REPORT_DIR = r"C:\ProgramData\SemSearchProbe"
MODEL_DIR = os.path.join(REPORT_DIR, "models", "bge-small")
ROOTS = [r"F:\HexyLab\semsearch\eval\corpus", r"C:\Users\me\Documents", r"F:\Personal OneDrive\OneDrive", r"F:\HexyLab"]
DOCX = r"F:\HexyLab\semsearch\eval\corpus\root\Content_Ethics_Whitepaper.docx"


def _safe(fn):
    try:
        return {"ok": True, "value": fn()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}


def run_checks(tag: str, rep: dict | None = None) -> dict:
    """Fills `rep` in place so that a crash part-way still leaves the earlier sections."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    if rep is None:
        rep = {}
    rep.update({"tag": tag, "started": time.time(), "python": sys.executable})
    import win32api
    import win32process
    import win32ts
    rep["identity"] = _safe(lambda: {
        "user": win32api.GetUserNameEx(2) if False else win32api.GetUserName(),
        "domain_user": os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", ""),
        "session": win32ts.ProcessIdToSessionId(win32process.GetCurrentProcessId()),
        "userprofile": os.environ.get("USERPROFILE"), "temp": tempfile.gettempdir(),
        "localappdata": os.environ.get("LOCALAPPDATA"),
    })
    rep["hkcu"] = _safe(lambda: __import__("winreg").QueryValueEx(__import__("winreg").OpenKey(__import__("winreg").HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer"), "ShellState")[0][:4].hex())
    # filesystem access to candidate roots
    fs = {}
    for r in ROOTS:
        def chk(r=r):
            names = os.listdir(r)
            first = next((os.path.join(r, n) for n in names if os.path.isfile(os.path.join(r, n))), None)
            head = None
            if first:
                with open(first, "rb") as f:
                    head = len(f.read(4096))
            return {"entries": len(names), "read_first_file_bytes": head, "first": first}
        fs[r] = _safe(chk)
    rep["filesystem"] = fs
    # Windows Search
    sys.path.insert(0, r"F:\HexyLab\semsearch\src")
    from semsearch.inventory.windows_search import WindowsSearchInventory
    from semsearch.inventory.catalog import catalog_status
    w = WindowsSearchInventory(ROOTS, [])
    rep["windows_search"] = {
        "ping": _safe(w.ping),
        "catalog": _safe(catalog_status),
        "covers": {r: _safe(lambda r=r: w.covers(r)) for r in ROOTS[:3]},
        "count_eval_corpus": _safe(lambda: sum(1 for _ in w.enumerate(ROOTS[0]))),
        "count_documents_profile": _safe(lambda: sum(1 for _ in w.enumerate(ROOTS[1]))),
        "count_onedrive": _safe(lambda: sum(1 for _ in w.enumerate(ROOTS[2]))),
        "freetext": _safe(lambda: w.freetext("consciousness compression", [ROOTS[0]], 5)),
    }
    # watcher
    def watch():
        from semsearch.watcher import DirectoryWatcher
        d = os.path.join(REPORT_DIR, "watch-" + tag)
        os.makedirs(d, exist_ok=True)
        got = []
        ev = threading.Event()
        dw = DirectoryWatcher([d], lambda a, p, o=None: (got.append((a, os.path.basename(p))), ev.set()), settle_s=0.5)
        dw.start()
        time.sleep(1.0)
        with open(os.path.join(d, "x.txt"), "w") as f:
            f.write("hello")
        ev.wait(8)
        dw.stop()
        return {"alive": dw.is_alive(), "events": got}
    rep["watcher"] = _safe(watch)
    # adapters + DirectML + CPU
    from semsearch.devices import enumerate_adapters
    ads = enumerate_adapters()
    rep["adapters"] = [a.to_dict() for a in ads]
    import onnxruntime as ort
    rep["ort_providers"] = ort.get_available_providers()
    from semsearch.config import EmbeddingConfig
    from semsearch.embed.factory import create_provider
    chunk = ("Receipts make irreversible operations auditable. " * 40)[:1800]
    emb = {}
    for dev in ["cpu"] + [f"dml:{a.ordinal}" for a in ads if not a.software]:
        def bench(dev=dev):
            p = create_provider(EmbeddingConfig(model=MODEL_DIR, device=dev, allow_download=False))
            p.embed([chunk] * 4)
            t0 = time.perf_counter()
            v = p.embed([chunk] * 16)
            dt = time.perf_counter() - t0
            return {"resolved": p.device, "chunks_per_s": round(16 / dt, 1), "dim": int(v.shape[1]), "norm": float((v[0] ** 2).sum())}
        emb[dev] = _safe(bench)
    rep["embedding"] = emb
    # IFilter (COM in-process) and the extractor child process
    from semsearch.extract.ifilter import IFilterExtractor
    rep["ifilter_docx"] = _safe(lambda: {"status": IFilterExtractor().extract(DOCX, ".docx").status, "chars": len(IFilterExtractor().extract(DOCX, ".docx").text)})
    from semsearch.config import Config
    from semsearch.extract.isolated import IsolatedExtractor
    from semsearch.extract.registry import build_default_registry
    def iso():
        cfg = Config(roots=[ROOTS[0]], data_dir=os.path.join(REPORT_DIR, "data-" + tag))
        ex = IsolatedExtractor(cfg, build_default_registry(cfg), timeout_s=60)
        try:
            r = ex.extract(DOCX, ".docx")
            return {"status": r.status, "method": r.method, "chars": len(r.text)}
        finally:
            ex.close()
    rep["isolated_extractor"] = _safe(iso)
    # sqlite + sqlite-vec store open under this identity in ProgramData
    def store():
        from semsearch.store.db import Store
        s = Store(os.path.join(REPORT_DIR, "data-" + tag, "probe.db"))
        s.ensure_vectors("probe:x:1:4:cls", 4)
        return {"journal": s.conn.execute("PRAGMA journal_mode").fetchone()[0], "vec": s.vec_version}
    rep["store"] = _safe(store)
    rep["finished"] = time.time()
    return rep


def write_report(tag: str, rep: dict) -> None:
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, f"report-{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, default=str)


def main_direct(tag: str) -> None:
    rep: dict = {}
    try:
        run_checks(tag, rep)
    except Exception:  # noqa: BLE001
        rep["fatal"] = traceback.format_exc()
    write_report(tag, rep)
    print(json.dumps(rep, indent=1, default=str)[:4000])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "direct":
        main_direct(sys.argv[2] if len(sys.argv) > 2 else "interactive")
        sys.exit(0)
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class ProbeService(win32serviceutil.ServiceFramework):
        _svc_name_ = "SemSearchProbe"
        _svc_display_name_ = "SemSearch identity probe"
        _exe_name_ = sys.executable
        _exe_args_ = f'"{os.path.abspath(__file__)}"'

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            tag = os.environ.get("SEMSEARCH_PROBE_TAG") or open(os.path.join(REPORT_DIR, "tag.txt")).read().strip()
            rep: dict = {}
            try:
                run_checks(tag, rep)
            except Exception:  # noqa: BLE001
                rep["fatal"] = traceback.format_exc()
            write_report(tag, rep)
            self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ProbeService)
    servicemanager.StartServiceCtrlDispatcher()
