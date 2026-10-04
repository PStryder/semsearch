"""Local HTTP API (FastAPI). Binds to 127.0.0.1 only unless explicitly overridden.

Search authority is read-only with respect to the filesystem: no endpoint writes to or
deletes source files. Index mutations only touch the sidecar's own database.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

import secrets

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .app_state import AppState
from .config import Config, load_config
from .inventory.catalog import catalog_status
from .logging_setup import setup_logging
from .security import PathRejected

log = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    query: str
    mode: Literal["literal", "semantic", "hybrid"] | None = None
    limit: int = Field(default=20, ge=1, le=200)
    roots: list[str] | None = None
    extensions: list[str] | None = None


class PathRequest(BaseModel):
    path: str
    priority: int = Field(default=1, ge=1, le=9)


class ReindexRequest(BaseModel):
    path: str | None = None
    full: bool = False
    wipe: bool = False


class RootsRequest(BaseModel):
    add: str | None = None
    remove: str | None = None
    set: list[str] | None = None


class ExcludesRequest(BaseModel):
    excludes: list[str]


def create_app(cfg: Config, state: AppState | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.cfg = cfg
        app.state.started = time.time()
        if state is not None:
            app.state.st = state
        else:
            app.state.st = AppState(cfg, start_indexer=cfg.indexing.auto_start)
        try:
            yield
        finally:
            if state is None:
                app.state.st.close()

    app = FastAPI(title="semsearch", version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url=None)

    allowed_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if cfg.api.allow_non_loopback:
        allowed_hosts.add(cfg.api.host.lower())
    allowed_hosts |= {h.lower() for h in cfg.api.allowed_hosts}

    @app.middleware("http")
    async def _host_guard(request, call_next):
        # Loopback binding does not stop a web page from reaching us through DNS rebinding
        # (evil.example resolving to 127.0.0.1); the Host header does. Only loopback names,
        # the configured bind host and explicit allowed_hosts are served.
        host = (request.headers.get("host") or "").strip().lower()
        if host.startswith("["):                      # [::1]:8765
            name = host[1: host.find("]")] if "]" in host else host
        elif host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():   # 127.0.0.1:8765
            name = host.rsplit(":", 1)[0]
        else:
            name = host
        if name not in allowed_hosts:
            return JSONResponse(status_code=421, content={"detail": f"host '{host}' is not allowed; use http://127.0.0.1:{cfg.api.port}/"})
        return await call_next(request)

    def st() -> AppState:
        return app.state.st

    def _token_ok(request: Request) -> bool:
        tok = request.headers.get("x-semsearch-token", "")
        expected = getattr(st(), "admin_token", None)
        return bool(expected) and secrets.compare_digest(tok, expected)

    def require_admin(request: Request) -> None:
        """Mutating / maintenance endpoints need the admin token from <state_dir>/admin.token.
        Search, health, status and stats stay open to any local process unless api.read_token."""
        if not _token_ok(request):
            raise HTTPException(403, "admin token required (X-SemSearch-Token; see <state_dir>/admin.token)")

    def require_read(request: Request) -> None:
        """Read endpoints: open by default (single-user workstation); with api.read_token the same
        token gates them, which is the mitigation for a machine shared by several local accounts
        (the index holds text the service account could read, whoever asks)."""
        if cfg.api.read_token and not _token_ok(request):
            raise HTTPException(403, "token required for reads on this installation (api.read_token); see <state_dir>/admin.token")

    @app.get("/health")
    def health():
        s = st()
        return {"ok": True, "version": __version__, "uptime_s": round(time.time() - app.state.started, 1),
                "indexer_running": s.indexer.state.running, "embedding": s.embedder.fingerprint,
                "windows_search": s.windows is not None, "documents": s.store.count_documents()}

    @app.post("/search")
    def search_post(req: SearchRequest, _: None = Depends(require_read)):
        try:
            return st().retriever.search(req.query, req.mode, req.limit, req.roots, req.extensions)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/search")
    def search_get(q: str = Query(..., min_length=1), mode: str | None = None, limit: int = Query(20, ge=1, le=200),
                   ext: str | None = None, root: str | None = None, _: None = Depends(require_read)):
        try:
            return st().retriever.search(q, mode, limit, [root] if root else None, ext.split(",") if ext else None)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/status")
    def status(_: None = Depends(require_read)):
        s = st()
        ws = catalog_status() if s.windows is not None else {"available": False}
        if ws.get("available"):
            ws["note"] = "Windows Search's own catalog and crawl backlog (not semsearch's queue)"
            ws["relevance_signal"] = s.retriever.windows_rank_state()
        return {"version": __version__,
                "indexer": s.indexer.status(), "windows_search": ws,
                "store": {"path": str(cfg.db_path), "fingerprint": s.store.fingerprint, "dim": s.store.dim,
                          "schema_version": getattr(s, "store_info", {}).get("schema_version"),
                          "recovered_from_corruption": getattr(s, "store_info", {}).get("recovered_from_corruption", False)},
                "devices": getattr(s, "device_resolution", {}),
                "config_source": str(cfg.source_path) if cfg.source_path else None,
                "pid": os.getpid()}

    @app.get("/stats")
    def stats(_: None = Depends(require_read)):
        s = st()
        d = s.store.stats()
        d["indexer"] = s.indexer.status()["counters"]
        d["throughput"] = s.indexer.state.throughput()
        return d

    @app.get("/errors")
    def errors(limit: int = Query(100, ge=1, le=1000), stage: str | None = None, _: None = Depends(require_read)):
        s = st()
        errs = s.store.recent_errors(limit if not stage else limit * 10)
        if stage:
            errs = [e for e in errs if e["stage"] == stage][:limit]
        return {"errors": errs, "failed_jobs": s.store.failed_jobs(50)}

    @app.post("/backup")
    def backup(req: PathRequest, _: None = Depends(require_admin)):
        """Consistent online copy of the index. The destination must lie under <data_dir>/backups
        (or `api.backup_dir`): the admin token is search-maintenance authority, not a licence to
        write SQLite files wherever the service account can."""
        from .security import is_within, normalize_path
        base = cfg.backup_dir
        dest = os.path.abspath(os.path.join(base, req.path)) if not os.path.isabs(req.path) else os.path.abspath(req.path)
        if not is_within(dest, [str(base)]) or normalize_path(dest) == normalize_path(str(base)):
            raise HTTPException(403, f"backup destination must be a file under {base}")
        if os.path.exists(dest) and not os.path.isfile(dest):
            raise HTTPException(400, "backup destination exists and is not a file")
        return st().store.backup(dest)

    @app.post("/index/path")
    def index_path(req: PathRequest, _: None = Depends(require_admin)):
        try:
            return st().indexer.index_path(req.path, req.priority)
        except PathRejected as e:
            raise HTTPException(403, str(e))
        except FileNotFoundError:
            raise HTTPException(404, "path not found")

    @app.post("/remove/path")
    def remove_path(req: PathRequest, _: None = Depends(require_admin)):
        return st().indexer.remove_path(req.path)

    @app.post("/reindex")
    def reindex(req: ReindexRequest, _: None = Depends(require_admin)):
        s = st()
        if req.path:
            try:
                return s.indexer.index_path(req.path, 1)
            except PathRejected as e:
                raise HTTPException(403, str(e))
        if req.full or req.wipe:
            s.indexer.request_full_build(wipe=req.wipe)
            return {"full_build": "requested", "wipe": req.wipe}
        raise HTTPException(400, "provide path, full=true, or wipe=true")

    @app.post("/indexer/pause")
    def pause(_: None = Depends(require_admin)):
        st().indexer.pause()
        return {"paused": True}

    @app.post("/indexer/resume")
    def resume(_: None = Depends(require_admin)):
        st().indexer.resume()
        return {"paused": False}

    @app.post("/indexer/retry-failed")
    def retry_failed(_: None = Depends(require_admin)):
        return {"requeued": st().indexer.retry_failed()}

    @app.post("/indexer/incremental")
    def incremental_now(_: None = Depends(require_admin)):
        return st().indexer.incremental()

    @app.post("/indexer/reconcile")
    def reconcile_now(_: None = Depends(require_admin)):
        return st().indexer.reconcile()

    @app.get("/document")
    def document(path: str, chunks: bool = False, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), _: None = Depends(require_read)):
        """Stored metadata for one indexed file, optionally a page of its chunks (`offset`/`limit`;
        `chunk_count` in the answer says how many there are)."""
        from .security import is_within, normalize_path
        s = st()
        roots = cfg.normalized_roots()
        if not roots or not is_within(path, roots):
            raise HTTPException(404, "not indexed")  # same answer as unknown: never confirm paths outside the roots
        row = s.store.get_document(normalize_path(path))
        if row is None or row["extract_status"] == "missing":
            raise HTTPException(404, "not indexed")
        d = dict(row)
        if chunks:
            allc = s.store.chunks_for_doc(int(row["id"]))
            d["chunk_count"] = len(allc)
            d["chunks"] = [dict(c) for c in allc[offset: offset + limit]]
        return d

    # ---- configuration of the indexed scope (tray / settings page / CLI) ----
    @app.get("/config")
    def get_config(_: None = Depends(require_read)):
        c = st().cfg
        return {"config": str(c.source_path) if c.source_path else None,
                "roots": [os.path.abspath(str(r)) for r in c.roots], "excludes": list(c.excludes),
                "read_token": c.api.read_token, "editable": bool(c.source_path)}

    @app.post("/config/roots")
    def config_roots(req: RootsRequest, _: None = Depends(require_admin)):
        """Add or remove one indexed folder, or replace the whole list. Persisted into the
        configuration file and applied live (no restart). The caller must have granted the
        service account read access on a new folder beforehand (CLI/tray do)."""
        from .security import display_path, normalize_path
        s = st()
        cur = [display_path(str(r)) for r in s.cfg.roots]
        if req.set is not None:
            new = list(req.set)
        else:
            new = list(cur)
            if req.add:
                p = display_path(req.add)
                if not os.path.isdir(p):
                    raise HTTPException(400, f"not an existing directory: {p}")
                if not os.access(p, os.R_OK):
                    raise HTTPException(403, f"the service account cannot read {p}: grant it read access first (semsearch roots add does)")
                if normalize_path(p) not in {normalize_path(x) for x in new}:
                    new.append(p)
            if req.remove:
                n = normalize_path(req.remove)
                new = [x for x in new if normalize_path(x) != n]
        try:
            return s.apply_scope(roots=new)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/config/excludes")
    def config_excludes(req: ExcludesRequest, _: None = Depends(require_admin)):
        try:
            return st().apply_scope(excludes=req.excludes)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/config/windows-scope")
    def windows_scope(_: None = Depends(require_read)):
        """What the Windows Search indexer covers for content (this user's profile) and what it
        excludes, translated to semsearch roots/globs. Read-only; nothing is applied."""
        from .inventory.scope import windows_scope_suggestion
        s = windows_scope_suggestion(cfg.api.operator_profile or None)
        return {"roots": s.roots, "excludes": s.excludes, "skipped": s.skipped, "unmounted_rules": len(s.unmounted)}

    @app.get("/ui", response_class=HTMLResponse)
    def ui_page():
        from .ui import PAGE
        return PAGE

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):
        # the log gets the traceback; the client gets a reference, not the exception text
        # (which can carry file paths and library internals)
        ref = secrets.token_hex(4)
        log.exception("unhandled error [%s]: %s", ref, exc)
        return JSONResponse(status_code=500, content={"error": "internal error", "ref": ref, "hint": "see the service log"})

    return app


def serve(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="semsearch-serve", description="Run the semsearch local API")
    ap.add_argument("--config", help="path to semsearch.yaml")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-index", action="store_true", help="serve queries only; do not start the indexer")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.log_dir, cfg.log_level)
    host = args.host or cfg.api.host
    port = args.port or cfg.api.port
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg.api.allow_non_loopback:
        raise SystemExit(f"refusing to bind {host}: set api.allow_non_loopback=true to allow a non-loopback address")
    if args.no_index:
        cfg.indexing.auto_start = False
    state = AppState(cfg, start_indexer=not args.no_index)
    app = create_app(cfg, state)
    import uvicorn
    log.info("semsearch %s listening on http://%s:%d (data: %s)", __version__, host, port, cfg.data_dir)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=cfg.api.log_requests)
    finally:
        state.close()


if __name__ == "__main__":
    serve()
