"""Command-line interface.

    semsearch "find the old design for the blackboard read tracking"
    semsearch --literal "*.pdf"
    semsearch --semantic "GPU memory architecture"
    semsearch --status | --stats | --errors | --health
    semsearch --reindex <path> | --reindex --full | --reindex --wipe
    semsearch --remove <path>
    semsearch --serve            (run the API server in the foreground)
    semsearch --build            (one-shot: full build in-process, then exit)
    semsearch --init-config      (write an example semsearch.yaml)

Query/status commands talk to the running API (default http://127.0.0.1:<port>). Add
``--local`` to run against the index in-process without a server.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from .config import example_yaml, load_config


def _client(base: str):
    import httpx
    return httpx.Client(base_url=base, timeout=120.0)


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


def _fmt_ts(ts):
    if not ts:
        return "-"
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "?"


def _print_status(s: dict) -> None:
    ix = s["indexer"]
    print("Indexer:", "running" if ix["running"] else "stopped", "(paused)" if ix["paused"] else "", "full build in progress" if ix["full_build_in_progress"] else "")
    print("  roots:", ", ".join(ix["roots"]))
    print("  sources:", ix["sources"])
    print("  queue:", ix["queue"], " current:", ix["current_path"] or "-")
    print("  last full build:", _fmt_ts(ix["last_full_build_at"]), " last incremental:", _fmt_ts(ix["last_incremental_at"]), " last reconcile:", _fmt_ts(ix["last_reconcile_at"]))
    print("  counters:", ix["counters"])
    print("  throughput:", ix["throughput"], " watcher:", ix["watcher"], " extractor restarts:", ix["extractor_restarts"])
    print("  embedding:", ix["embedding"])
    if ix.get("last_error"):
        print("  last error:", ix["last_error"])
    ws = s.get("windows_search", {})
    if ws.get("available"):
        print("Windows Search:", ws["status"], "items:", ws["items"], "to index:", ws["to_index"], "indexing:", ws.get("url_being_indexed") or "-")
    else:
        print("Windows Search: unavailable", ws.get("error", ""))
    print("Store:", s["store"])


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to cp1252; excerpts contain arbitrary Unicode
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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
    a = ap.parse_args(argv)

    if a.init_config:
        print(example_yaml())
        return 0
    cfg = load_config(a.config)
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
                # drain the queue in the foreground
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
                _print_status({"indexer": st.indexer.status(), "windows_search": catalog_status() if st.windows else {"available": False},
                               "store": {"path": str(cfg.db_path), "fingerprint": st.store.fingerprint, "dim": st.store.dim}})
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
    try:
        with _client(base) as c:
            if a.health:
                print(json.dumps(c.get("/health").json(), indent=2))
            elif a.status:
                r = c.get("/status"); r.raise_for_status()
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
            print(f"cannot reach semsearch API at {base}. Start it with: semsearch --serve  (or add --local)", file=sys.stderr)
            return 3
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
