"""Command-line interface.

Production (subcommand) form:
    semsearch query "the document where I talked about receipts"   (or just: semsearch "...")
    semsearch status | health | stats | errors | devices
    semsearch reindex <path> | reindex --full
    semsearch rebuild [--yes]
    semsearch remove <path>
    semsearch pause | resume | retry-failed
    semsearch logs [-n 200] [--follow]
    semsearch service status|start|stop|restart
    semsearch config [--validate]
    semsearch --init-config

Legacy flag form (kept for scripts and tests): semsearch --status, --literal "*.pdf", --reindex <path>,
--serve, --build, --local, ...

Query/status commands talk to the running API (default http://127.0.0.1:<port>). Maintenance
commands send the admin token from <state_dir>/admin.token. Add ``--local`` to run against the
index in-process without a server.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

from . import __version__
from .config import ConfigError, example_yaml, load_config, machine_config_path

SUBCOMMANDS = {"query", "status", "health", "stats", "errors", "devices", "reindex", "rebuild", "remove", "pause", "resume",
               "retry-failed", "logs", "service", "config", "version", "roots", "scope", "tray", "prune"}


def _client(base: str, token: str | None = None):
    """HTTP client for the service. The admin token is attached only after verifying that the
    process listening on <base> is the SemSearch service (clientauth); otherwise it is withheld
    with a warning, and the request goes out without it."""
    import httpx
    from .clientauth import token_headers
    headers, warning = token_headers(base, token)
    if warning:
        print(f"semsearch: {warning}", file=sys.stderr)
    return httpx.Client(base_url=base, timeout=120.0, headers=headers)


def _admin_token(cfg) -> str | None:
    p = cfg.state_path / "admin.token"
    try:
        return p.read_text(encoding="utf-8").strip()
    except PermissionError:
        print(f"cannot read the admin token at {p}: this account is not allowed to control the index "
              f"(run as the operator account that installed the service, or elevated)", file=sys.stderr)
        return None
    except FileNotFoundError:
        return None


def _print_results(res: dict, verbose: bool) -> None:
    hits = res.get("results", [])
    print(f"{len(hits)} results for {res.get('mode')} search in {res.get('took_ms')} ms  (candidates: {res.get('candidates')})")
    for i, h in enumerate(hits, 1):
        sc = h["scores"]
        comp = []
        if sc.get("semantic") is not None:
            comp.append(f"sem {sc['semantic']:.3f}")
        if sc.get("lexical") is not None:
            comp.append(f"lex {sc['lexical']:.3f}")
        if sc.get("filename"):
            comp.append(f"name {sc['filename']:.2f}")
        if sc.get("windows_rank") is not None:
            comp.append(f"win {sc['windows_rank']:.2f}")
        mod = h.get("modified") or ""
        print(f"\n{i:2d}. [{h['score']:.3f}] {h['path']}")
        print(f"    {h['match_type']:<8} {' | '.join(comp)}   {h['file_type']}  {mod}")
        if h.get("excerpt"):
            ex = h["excerpt"].replace("\n", " ")
            print(f"    {ex[:300]}")
        if verbose:
            for w in h.get("why", []):
                print(f"      - {w}")


def _roots_command(a, c, cfg, token: str | None) -> int:
    from .roots_admin import add_root, remove_root, service_account_from_config
    acct = service_account_from_config(cfg)
    if a.action == "list":
        r = c.get("/config")
        r.raise_for_status()
        d = r.json()
        if a.json:
            print(json.dumps(d, indent=2))
            return 0
        print("config:", d.get("config") or "(none: roots cannot be persisted)")
        for root in d["roots"]:
            print("  ", root)
        print(f"{len(d['excludes'])} exclusion patterns")
        return 0
    if token is None:
        print("changing roots needs the admin token (operator account)", file=sys.stderr)
        return 3
    if a.action in ("add", "remove"):
        if not a.path:
            print(f"usage: semsearch roots {a.action} <folder>", file=sys.stderr)
            return 2
        try:
            out = add_root(c, a.path, token, acct, grant=not a.no_grant) if a.action == "add" else remove_root(c, a.path, token, acct, revoke=not a.no_grant)
        except (ValueError, PermissionError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2) if a.json else f"{a.action}: {os.path.abspath(a.path)}  (read access {out.get('grant')}; now {len(out['roots'])} roots"
              + (f", new: {out['new_roots']}" if out.get("new_roots") else "") + ")")
        return 0
    # import-windows
    r = c.get("/config/windows-scope")
    r.raise_for_status()
    sg = r.json()
    cur = {os.path.normcase(x) for x in c.get("/config").json()["roots"]}
    new = [x for x in sg["roots"] if os.path.normcase(x) not in cur]
    if not new and not (a.with_excludes and sg["excludes"]):
        print("nothing to import (every content-indexed folder is already a root; add --with-excludes to adopt Windows' exclusion rules)")
        return 0
    print("Folders Windows Search indexes for content that SemSearch does not yet:" if new else "No new folders.")
    for x in new:
        print("  ", x)
    if a.with_excludes:
        print(f"{len(sg['excludes'])} exclusion rules from Windows will be merged into the configuration; documents already indexed under them are REMOVED.")
    else:
        print(f"({len(sg['excludes'])} exclusion rules from Windows are available with --with-excludes; not applied)")
    if not a.yes:
        ans = input("Apply? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("nothing changed")
            return 0
    failed = 0
    for x in new:
        try:
            out = add_root(c, x, token, acct)
            print(f"  added {x} (read access {out.get('grant')})")
        except (ValueError, PermissionError, RuntimeError) as e:
            failed += 1
            print(f"  FAILED {x}: {e}", file=sys.stderr)
    if a.with_excludes and sg["excludes"]:
        merged = sorted(set(cfg.excludes) | set(sg["excludes"]))
        rr = c.post("/config/excludes", json={"excludes": merged})
        print(f"  exclusions: {rr.json().get('excludes') if rr.status_code < 400 else rr.text}")
    return 1 if failed else 0


def _fmt_ts(ts):
    if not ts:
        return "-"
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "?"


def _print_status(s: dict) -> None:
    ix = s["indexer"]
    print(f"semsearch {s.get('version', '?')}  pid {s.get('pid', '?')}")
    print("Indexer:", "running" if ix["running"] else "stopped", "(paused)" if ix["paused"] else "", "full build in progress" if ix["full_build_in_progress"] else "")
    print("  roots:", ", ".join(ix["roots"]))
    print("  sources:", ix["sources"])
    print("  queue:", ix["queue"], " current:", ix["current_path"] or "-")
    print("  last full build:", _fmt_ts(ix["last_full_build_at"]), " last incremental:", _fmt_ts(ix["last_incremental_at"]), " last reconcile:", _fmt_ts(ix["last_reconcile_at"]))
    print("  counters:", ix["counters"])
    print("  throughput:", ix["throughput"], " watcher:", ix["watcher"], " extractor restarts:", ix["extractor_restarts"])
    print("  embedding:", ix["embedding"])
    for role, r in (s.get("devices") or {}).get("roles", {}).items():
        print(f"    {role:<13} {r['configured']:<18} -> {r['resolved']:<8} {r['why']}")
    if ix.get("last_error"):
        print("  last error:", ix["last_error"])
    ws = s.get("windows_search", {})
    if ws.get("available"):
        pend = ws["to_index"]
        backlog = sum(int(v) for v in pend.values()) if isinstance(pend, dict) else pend
        print(f"Windows Search (its own catalog, not semsearch's queue): {ws['status']}, {ws['items']} items, "
              f"Windows' own crawl backlog: {backlog}, now indexing: {ws.get('url_being_indexed') or '-'}")
        rs = ws.get("relevance_signal") or {}
        if rs and not rs.get("active", True):
            print("  Windows relevance signal paused (no usable hits recently)")
    else:
        print("Windows Search: unavailable", ws.get("error", ""))
    if ix.get("failed_jobs"):
        print("Failed jobs (retry with: semsearch retry-failed):")
        for j in ix["failed_jobs"][:10]:
            print(f"  {j['op']:<7} {j['path']}  attempts={j['attempts']}  {(j.get('error') or '')[:120]}")
    if ix.get("gpu_yielding"):
        print("GPU courtesy: bulk GPU is busy with other processes; embedding on the steady-state device")
    st = s["store"]
    print(f"Store: {st['path']}  schema v{st.get('schema_version')}  dim {st.get('dim')}" + ("  RECOVERED FROM CORRUPTION" if st.get("recovered_from_corruption") else ""))
    print("Config:", s.get("config_source"))


# ---------------------------------------------------------------- service control (pywin32)
def service_control(action: str, name: str = "SemSearch") -> int:
    try:
        import pywintypes
        import win32service
    except ImportError:
        print("service control needs pywin32 (Windows only)", file=sys.stderr)
        return 2
    states = {1: "stopped", 2: "start pending", 3: "stop pending", 4: "running", 5: "continue pending", 6: "pause pending", 7: "paused"}

    # pywin32's StartService/StopService helpers open the service with SERVICE_ALL_ACCESS, which an
    # operator granted only start/stop/query rights does not have; open with exactly what we need.
    def _open(rights):
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        return hscm, win32service.OpenService(hscm, name, rights)

    def _wait(hs, target, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = win32service.QueryServiceStatus(hs)
            if st[1] == target:
                return True
            time.sleep(0.5)
        return False

    def _stop(hs):
        st = win32service.QueryServiceStatus(hs)
        if st[1] != win32service.SERVICE_STOPPED:
            win32service.ControlService(hs, win32service.SERVICE_CONTROL_STOP)
        if not _wait(hs, win32service.SERVICE_STOPPED, 90):
            raise RuntimeError("service did not stop within 90s")

    def _start(hs):
        win32service.StartService(hs, None)
        if not _wait(hs, win32service.SERVICE_RUNNING, 240):
            raise RuntimeError("service did not reach Running within 240s (see the log / event log)")

    try:
        if action == "status":
            hscm, hs = _open(win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG)
            st = win32service.QueryServiceStatus(hs)
            print(f"{name}: {states.get(st[1], st[1])}")
            try:
                cfg = win32service.QueryServiceConfig(hs)
                print("  binary:", cfg[3], " account:", cfg[7])
            except pywintypes.error:
                pass
            return 0 if st[1] == 4 else 1
        rights = win32service.SERVICE_START | win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS
        hscm, hs = _open(rights)
        if action == "start":
            _start(hs)
            print(f"{name}: running")
            return 0
        if action == "stop":
            _stop(hs)
            print(f"{name}: stopped")
            return 0
        if action == "restart":
            _stop(hs)
            _start(hs)
            print(f"{name}: running")
            return 0
    except RuntimeError as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 1
    except pywintypes.error as e:
        if e.winerror == 5:
            print(f"access denied controlling {name}: run from an elevated prompt, or re-run the installer which grants "
                  f"start/stop rights to the operator account", file=sys.stderr)
        elif e.winerror == 1060:
            print(f"service {name} is not installed (see docs/windows-service.md)", file=sys.stderr)
        else:
            print(f"{name}: {e}", file=sys.stderr)
        return 1
    print(f"unknown service action {action}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- subcommand dispatcher
def dispatch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="semsearch", description="Semantic search over local files (Windows Search sidecar)")
    ap.add_argument("--config", help="path to semsearch.yaml")
    ap.add_argument("--url", help="API base URL (default from config)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--version", action="version", version=f"semsearch {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    q = sub.add_parser("query", help="search")
    q.add_argument("text", nargs="+")
    m = q.add_mutually_exclusive_group()
    m.add_argument("--literal", action="store_true")
    m.add_argument("--semantic", action="store_true")
    m.add_argument("--hybrid", action="store_true")
    q.add_argument("-n", "--limit", type=int, default=10)
    q.add_argument("--ext")
    q.add_argument("--root")
    q.add_argument("-v", "--verbose", action="store_true")
    q.add_argument("--json", action="store_true", dest="json_sub", help="machine-readable output")  # accepted after the subcommand too
    for name in ("status", "health", "stats", "errors", "devices", "pause", "resume", "retry-failed", "version"):
        sp = sub.add_parser(name)
        sp.add_argument("--json", action="store_true", dest="json_sub")
        if name == "errors":
            sp.add_argument("--stage", choices=["extract", "policy", "job", "reconcile"], help="only this stage (policy = refused by exclusion/secret screening)")
            sp.add_argument("-n", type=int, default=100)
    bk = sub.add_parser("backup", help="consistent online copy of the index database")
    bk.add_argument("dest", help="destination file, e.g. D:\\backups\\semsearch-2026-10-02.db")
    bk.add_argument("--direct", action="store_true", help="copy from the database file directly instead of asking the service (works while the service is stopped)")
    r = sub.add_parser("reindex", help="re-index a path, or --full")
    r.add_argument("path", nargs="?")
    r.add_argument("--full", action="store_true")
    rb = sub.add_parser("rebuild", help="drop the index and rebuild from scratch")
    rb.add_argument("--yes", action="store_true")
    rm = sub.add_parser("remove")
    rm.add_argument("path")
    lg = sub.add_parser("logs")
    lg.add_argument("-n", type=int, default=200)
    lg.add_argument("--follow", "-f", action="store_true")
    sv = sub.add_parser("service", help="Windows service control")
    sv.add_argument("action", choices=["status", "start", "stop", "restart"])
    cf = sub.add_parser("config")
    cf.add_argument("--validate", action="store_true")
    ro = sub.add_parser("roots", help="list / add / remove indexed folders on the running service (applied live, persisted to the config)")
    ro.add_argument("action", choices=["list", "add", "remove", "import-windows"], nargs="?", default="list")
    ro.add_argument("path", nargs="?", help="folder for add/remove")
    ro.add_argument("--no-grant", action="store_true", help="do not touch the folder's ACL (the service can already read it)")
    ro.add_argument("--yes", "-y", action="store_true", help="import-windows: apply without asking")
    ro.add_argument("--with-excludes", action="store_true", help="import-windows: ALSO adopt Windows' exclusion rules (documents already indexed under them are removed)")
    sub.add_parser("prune", help="drop queued jobs and indexed documents that the current roots/exclusions reject")
    sub.add_parser("scope", help="show what the Windows Search indexer covers for content in your profile, as semsearch roots/excludes")
    sub.add_parser("tray", help="run the tray icon in this session (normally started by the logon task)")
    a = ap.parse_args(argv)
    a.json = bool(a.json or getattr(a, "json_sub", False))

    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    base = a.url or f"http://{cfg.api.host}:{cfg.api.port}"

    if a.cmd == "version":
        print(f"semsearch {__version__}")
        return 0
    if a.cmd == "config":
        print("config file:", cfg.source_path or "(defaults; no file found)")
        print("machine config path:", machine_config_path())
        print("index:", cfg.db_path, "\nstate:", cfg.state_path, "\nlogs:", cfg.log_dir, "\nmodels:", cfg.model_cache_dir)
        problems = cfg.validate_for_startup()
        if a.validate or problems:
            for p in problems:
                print("  PROBLEM:", p)
            print("valid" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    if a.cmd == "service":
        return service_control(a.action, cfg.service.name)
    if a.cmd == "scope":
        from .inventory.scope import windows_scope_suggestion
        sg = windows_scope_suggestion()
        if a.json:
            print(json.dumps({"roots": sg.roots, "excludes": sg.excludes, "skipped": sg.skipped, "unmounted_rules": len(sg.unmounted)}, indent=2))
            return 0
        print("Folders Windows Search indexes for CONTENT (this profile):")
        for r in sg.roots:
            print("  ", r)
        print(f"Exclusion rules from Windows ({len(sg.excludes)}):")
        for e in sg.excludes[:40]:
            print("  ", e)
        if len(sg.excludes) > 40:
            print(f"   ... {len(sg.excludes) - 40} more")
        if sg.skipped:
            print("Skipped rules:")
            for x in sg.skipped:
                print("  ", x)
        print(f"({len(sg.unmounted)} rules are for volumes not mounted right now)")
        print("Apply with: semsearch roots import-windows")
        return 0
    if a.cmd == "tray":
        from .tray import main as tray_main
        return tray_main(["--config", str(cfg.source_path)] if cfg.source_path else [])
    if a.cmd == "logs":
        p = cfg.log_dir / "semsearch.log"
        if not p.exists():
            print(f"no log file at {p}", file=sys.stderr)
            return 1
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            sys.stdout.write("".join(lines[-a.n:]))
            if a.follow:
                try:
                    while True:
                        chunk = f.read()
                        if chunk:
                            sys.stdout.write(chunk)
                            sys.stdout.flush()
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
        return 0
    if a.cmd == "devices":
        from .devices import enumerate_adapters, selector_for
        for ad in enumerate_adapters():
            print(f"dml:{ad.ordinal}  {ad.name:<36} {ad.stable_id:<40} vram {ad.dedicated_vram_mb} MB  {'software' if ad.software else ('integrated' if ad.integrated else 'discrete')}")
            if not ad.software:
                print(f"       selector: {json.dumps(selector_for(ad))}")
        return 0

    needs_admin = a.cmd in ("reindex", "rebuild", "remove", "pause", "resume", "retry-failed", "prune") or (a.cmd == "roots" and a.action != "list")
    token = _admin_token(cfg) if (needs_admin or cfg.api.read_token) else None
    try:
        with _client(base, token) as c:
            if a.cmd == "roots":
                return _roots_command(a, c, cfg, token)
            if a.cmd == "prune":
                r = c.post("/indexer/prune", timeout=600.0)
                print(json.dumps(r.json(), indent=2))
                return 0 if r.status_code < 400 else 1
            if a.cmd == "health":
                r = c.get("/health")
                print(json.dumps(r.json(), indent=2))
                return 0 if r.status_code == 200 and r.json().get("ok") else 1
            if a.cmd == "status":
                r = c.get("/status")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2)) if a.json else _print_status(r.json())
                return 0
            if a.cmd == "stats":
                print(json.dumps(c.get("/stats").json(), indent=2))
                return 0
            if a.cmd == "errors":
                params = {"limit": a.n}
                if a.stage:
                    params["stage"] = a.stage
                body = c.get("/errors", params=params).json()
                if a.json:
                    print(json.dumps(body, indent=2))
                    return 0
                for e in body["errors"]:
                    print(_fmt_ts(e["at"]), e["stage"], e["path"], "-", (e["message"] or "")[:200])
                if body.get("failed_jobs") and not a.stage:
                    print("\nFailed jobs (semsearch retry-failed to re-queue):")
                    for j in body["failed_jobs"]:
                        print(f"  {j['op']:<7} {j['path']}  attempts={j['attempts']}  {(j.get('error') or '')[:160]}")
                return 0
            if a.cmd == "backup":
                dest = os.path.abspath(a.dest)
                if a.direct:
                    import sqlite3
                    src = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
                    try:
                        out = sqlite3.connect(dest)
                        src.backup(out)
                        out.close()
                    finally:
                        src.close()
                    print(json.dumps({"path": dest, "bytes": os.path.getsize(dest), "mode": "direct"}))
                    return 0
                tok = _admin_token(cfg)
                if tok is None:
                    print("backup via the service needs the admin token; or use --direct", file=sys.stderr)
                    return 3
                # the service only writes under its backup directory; a bare file name lands there,
                # anything else must be --direct (which runs with the caller's own file rights)
                api_dest = a.dest if not os.path.isabs(a.dest) and os.sep not in a.dest and "/" not in a.dest else dest
                r = c.post("/backup", json={"path": api_dest}, headers={"x-semsearch-token": tok})
                if r.status_code == 403:
                    print(f"{r.json().get('detail')}\nUse a bare file name (written under the service's backup directory) or --direct for another location", file=sys.stderr)
                    return 1
                print(json.dumps(r.json()))
                return 0 if r.status_code < 400 else 1
            if a.cmd == "query":
                mode = "literal" if a.literal else "semantic" if a.semantic else "hybrid" if a.hybrid else None
                body = {"query": " ".join(a.text), "mode": mode, "limit": a.limit}
                if a.root:
                    body["roots"] = [a.root]
                if a.ext:
                    body["extensions"] = a.ext.split(",")
                r = c.post("/search", json=body)
                if r.status_code != 200:
                    print("error:", r.text, file=sys.stderr)
                    return 1
                print(json.dumps(r.json(), indent=2)) if a.json else _print_results(r.json(), a.verbose)
                return 0
            # maintenance
            if token is None:
                print("admin token unavailable; maintenance commands need it", file=sys.stderr)
                return 3
            if a.cmd == "reindex":
                if not a.path and not a.full:
                    print("reindex: give a path or --full", file=sys.stderr)
                    return 2
                r = c.post("/reindex", json={"path": a.path, "full": a.full})
            elif a.cmd == "rebuild":
                if not a.yes:
                    print("rebuild drops the whole index and re-embeds everything; re-run with --yes", file=sys.stderr)
                    return 2
                r = c.post("/reindex", json={"wipe": True})
            elif a.cmd == "remove":
                r = c.post("/remove/path", json={"path": a.path})
            elif a.cmd == "pause":
                r = c.post("/indexer/pause")
            elif a.cmd == "resume":
                r = c.post("/indexer/resume")
            elif a.cmd == "retry-failed":
                r = c.post("/indexer/retry-failed")
            else:
                ap.print_help()
                return 1
            if r.status_code == 403:
                print("rejected: admin token not accepted (" + r.text + ")", file=sys.stderr)
                return 3
            print(json.dumps(r.json()))
            return 0 if r.status_code < 400 else 1
    except Exception as e:  # noqa: BLE001
        if "Connect" in type(e).__name__ or "connect" in str(e).lower():
            print(f"cannot reach semsearch API at {base}. Is the service running? (semsearch service status)", file=sys.stderr)
            return 3
        raise


# ---------------------------------------------------------------- legacy flag interface
def _ensure_isolated_interpreter() -> None:
    """The installed `semsearch.exe` launcher starts the runtime without -s, so a user's
    %APPDATA%\\Python site-packages could shadow the runtime's. Re-exec with -s once."""
    if sys.flags.no_user_site or os.environ.get("SEMSEARCH_REEXEC") == "1" or not getattr(sys, "frozen", False) and "site-packages" not in os.path.dirname(__file__).lower():
        return
    import subprocess
    env = dict(os.environ, SEMSEARCH_REEXEC="1")
    raise SystemExit(subprocess.call([sys.executable, "-s", "-m", "semsearch.cli", *sys.argv[1:]], env=env))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        _ensure_isolated_interpreter()
    # Windows consoles often default to cp1252; excerpts contain arbitrary Unicode
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    # subcommand form, also when global options precede it: semsearch --config x.yaml service restart
    for i, tok in enumerate(argv):
        if tok.startswith("-"):
            continue
        if i > 0 and argv[i - 1] in ("--config", "--url"):
            continue
        if tok in SUBCOMMANDS:
            return dispatch(argv)
        break
    ap = argparse.ArgumentParser(prog="semsearch", description="Semantic search over local files (Windows Search sidecar)")
    ap.add_argument("query", nargs="?", help="search text; wildcards like *.pdf run a filename search")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--literal", action="store_true")
    mode.add_argument("--semantic", action="store_true")
    mode.add_argument("--hybrid", action="store_true")
    ap.add_argument("--limit", "-n", type=int, default=10)
    ap.add_argument("--ext", help="comma-separated extension filter, e.g. md,pdf")
    ap.add_argument("--root", help="restrict results to this directory")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="show why each result matched")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--reindex", nargs="?", const="", metavar="PATH", help="re-index a path (or --full / --wipe)")
    ap.add_argument("--full", action="store_true", help="with --reindex: enqueue a full rebuild")
    ap.add_argument("--wipe", action="store_true", help="with --reindex: drop the index and rebuild from scratch")
    ap.add_argument("--remove", metavar="PATH", help="remove a path (file or directory) from the index")
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--serve", action="store_true", help="run the API server")
    ap.add_argument("--build", action="store_true", help="one-shot full build in-process (no server)")
    ap.add_argument("--init-config", action="store_true", help="print an example semsearch.yaml")
    ap.add_argument("--config", help="path to semsearch.yaml")
    ap.add_argument("--url", help="API base URL (default from config)")
    ap.add_argument("--local", action="store_true", help="run against the index in-process instead of the API")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--version", action="version", version=f"semsearch {__version__}")
    a = ap.parse_args(argv)

    if a.init_config:
        print(example_yaml())
        return 0
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    base = a.url or f"http://{cfg.api.host}:{cfg.api.port}"
    m = "literal" if a.literal else "semantic" if a.semantic else "hybrid" if a.hybrid else None

    if a.serve:
        from .api import serve
        argv2 = []
        if a.config:
            argv2 += ["--config", a.config]
        if a.host:
            argv2 += ["--host", a.host]
        if a.port:
            argv2 += ["--port", str(a.port)]
        serve(argv2)
        return 0

    if a.build or a.local:
        from .app_state import AppState
        from .logging_setup import setup_logging
        setup_logging(cfg.log_dir, cfg.log_level, to_stderr=True)
        st = AppState(cfg, start_indexer=False)
        try:
            if a.build:
                r = st.indexer.full_build()
                print("enqueued:", r)
                t0 = time.time()
                n = 0
                while True:
                    job = st.store.next_job()
                    if job is None:
                        break
                    try:
                        st.indexer.process_job(job)
                    except Exception as e:  # noqa: BLE001
                        st.store.fail_job(int(job["id"]), str(e), cfg.indexing.max_attempts)
                        st.store.record_error(job["path"], "job", str(e))
                    n += 1
                    if n % 200 == 0:
                        c = st.indexer.state
                        print(f"  {n} jobs, indexed {c.docs_indexed}, unchanged {c.docs_skipped}, failed {c.docs_failed}, chunks {c.chunks_embedded} ({time.time()-t0:.0f}s)", flush=True)
                print(json.dumps(st.indexer.status()["counters"], indent=2))
                return 0
            if a.status:
                from .inventory.catalog import catalog_status
                _print_status({"version": __version__, "indexer": st.indexer.status(), "windows_search": catalog_status() if st.windows else {"available": False},
                               "store": {"path": str(cfg.db_path), "fingerprint": st.store.fingerprint, "dim": st.store.dim,
                                         "schema_version": st.store_info.get("schema_version")}, "devices": st.device_resolution,
                               "config_source": str(cfg.source_path) if cfg.source_path else None, "pid": os.getpid()})
                return 0
            if a.stats:
                print(json.dumps(st.store.stats(), indent=2))
                return 0
            if a.errors:
                for e in st.store.recent_errors(100):
                    print(_fmt_ts(e["at"]), e["stage"], e["path"], "-", e["message"][:200])
                return 0
            if a.query:
                res = st.retriever.search(a.query, m, a.limit, [a.root] if a.root else None, a.ext.split(",") if a.ext else None)
                print(json.dumps(res, indent=2)) if a.json else _print_results(res, a.verbose)
                return 0
            ap.print_help()
            return 1
        finally:
            st.close()

    # ---- HTTP client mode ----
    needs_token = a.reindex is not None or a.remove or a.pause or a.resume or a.retry_failed
    token = _admin_token(cfg) if (needs_token or cfg.api.read_token) else None
    try:
        with _client(base, token) as c:
            if a.health:
                print(json.dumps(c.get("/health").json(), indent=2))
            elif a.status:
                r = c.get("/status")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2)) if a.json else _print_status(r.json())
            elif a.stats:
                print(json.dumps(c.get("/stats").json(), indent=2))
            elif a.errors:
                for e in c.get("/errors").json()["errors"]:
                    print(_fmt_ts(e["at"]), e["stage"], e["path"], "-", (e["message"] or "")[:200])
            elif a.reindex is not None:
                body = {"path": a.reindex or None, "full": a.full, "wipe": a.wipe}
                if not a.reindex and not (a.full or a.wipe):
                    print("specify a path, or --full / --wipe", file=sys.stderr)
                    return 2
                r = c.post("/reindex", json=body)
                print(r.json())
            elif a.remove:
                print(c.post("/remove/path", json={"path": a.remove}).json())
            elif a.pause:
                print(c.post("/indexer/pause").json())
            elif a.resume:
                print(c.post("/indexer/resume").json())
            elif a.retry_failed:
                print(c.post("/indexer/retry-failed").json())
            elif a.query:
                body = {"query": a.query, "mode": m, "limit": a.limit}
                if a.root:
                    body["roots"] = [a.root]
                if a.ext:
                    body["extensions"] = a.ext.split(",")
                r = c.post("/search", json=body)
                if r.status_code != 200:
                    print("error:", r.text, file=sys.stderr)
                    return 1
                res = r.json()
                print(json.dumps(res, indent=2)) if a.json else _print_results(res, a.verbose)
            else:
                ap.print_help()
                return 1
    except Exception as e:  # noqa: BLE001
        if "Connect" in type(e).__name__ or "connect" in str(e).lower():
            print(f"cannot reach semsearch API at {base}. Is the service running? (semsearch service status; or add --local)", file=sys.stderr)
            return 3
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
