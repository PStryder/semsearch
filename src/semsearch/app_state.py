"""Wires the subsystems together (used by the service host, the API server, the CLI's local
mode, and tests). Also owns the production concerns that sit between config and subsystems:
accelerator resolution, store integrity/recovery, and the admin token."""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sys
import time
from typing import Any

from .config import Config
from .embed import create_provider
from .extract.registry import build_default_registry
from .indexer import Indexer
from .inventory.filesystem import FilesystemInventory
from .retrieval import Retriever
from .store.db import SCHEMA_VERSION, Store, StoreCorrupt, StoreIncompatible

log = logging.getLogger(__name__)


def resolve_devices(cfg: Config) -> tuple[Config, dict[str, Any]]:
    """Resolve logical device roles to concrete runtime devices using stable adapter identity.
    Returns (config with concrete device strings, resolution report)."""
    emb = cfg.embedding
    report: dict[str, Any] = {"adapters": [], "roles": {}, "at": time.time()}
    if emb.provider != "onnx" or sys.platform != "win32":
        return cfg, report
    from .devices import enumerate_adapters, resolve_role
    adapters = enumerate_adapters()
    report["adapters"] = [a.to_dict() for a in adapters]
    resolved: dict[str, str] = {}
    for role in ("device", "bulk_device", "query_device"):
        spec = getattr(emb, role)
        effective = emb.device if spec == "same" else spec
        try:
            dev, why = resolve_role(effective, emb.devices, adapters, fallback=emb.fallback_device)
        except ValueError as e:
            raise
        resolved[role] = dev
        entry = {"configured": spec, "effective": effective, "resolved": dev, "why": why}
        report["roles"][role] = entry
        if "fell back" in why:
            log.warning("accelerator fallback for %s: %s", role, why)
        else:
            log.info("device %s: %s", role, why)
    new_emb = emb.model_copy(update=resolved)
    return cfg.model_copy(update={"embedding": new_emb}), report


def open_store_with_recovery(cfg: Config) -> tuple[Store, dict[str, Any]]:
    """Open the index; on corruption quarantine the files and start fresh (logged loudly); on a
    newer-than-supported schema refuse with a clear diagnostic (no automatic data loss)."""
    path = cfg.db_path
    os.makedirs(path.parent, exist_ok=True)
    info: dict[str, Any] = {"path": str(path), "recovered_from_corruption": False}
    try:
        store = Store(path, vector_cache=cfg.retrieval.vector_cache, cache_dtype=cfg.retrieval.vector_cache_dtype,
                      integrity_check=cfg.service.integrity_check, integrity_check_max_mb=cfg.service.integrity_check_max_mb)
    except StoreIncompatible:
        raise
    except StoreCorrupt as e:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log.error("index database is corrupt (%s); quarantining to *.corrupt-%s and rebuilding from scratch", e, stamp)
        for suffix in ("", "-wal", "-shm"):
            p = str(path) + suffix
            if os.path.exists(p):
                shutil.move(p, f"{p}.corrupt-{stamp}")
        store = Store(path, vector_cache=cfg.retrieval.vector_cache, cache_dtype=cfg.retrieval.vector_cache_dtype, integrity_check="none")
        store.set_meta("recovered_from_corruption_at", str(time.time()))
        info["recovered_from_corruption"] = True
    info["schema_version"] = int(store.get_meta("schema_version") or SCHEMA_VERSION)
    return store, info


def load_or_create_admin_token(cfg: Config, rotate: bool = False) -> str:
    """A random token in <state_dir>/admin.token gates the API. The file's ACL is set by the
    installer (service account + Administrators + the operator); the CLI and tray read it.
    The service rotates it at every start (rotate=True), so a token that ever leaked stops
    working at the next restart; clients read the file for every request."""
    os.makedirs(cfg.state_path, exist_ok=True)
    p = cfg.state_path / "admin.token"
    if not rotate:
        try:
            tok = p.read_text(encoding="utf-8").strip()
            if len(tok) >= 32:
                return tok
        except FileNotFoundError:
            pass
    tok = secrets.token_hex(32)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tok)
    os.replace(tmp, p)
    return tok


class AppState:
    def __init__(self, cfg: Config, start_indexer: bool = True, isolate_extractors: bool = True, rotate_token: bool = False):
        self.raw_cfg = cfg
        cfg, self.device_resolution = resolve_devices(cfg)
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        os.makedirs(cfg.state_path, exist_ok=True)
        try:
            with open(cfg.state_path / "devices.json", "w", encoding="utf-8") as f:
                json.dump(self.device_resolution, f, indent=1)
        except OSError as e:
            log.debug("could not persist device resolution: %s", e)
        self.admin_token = load_or_create_admin_token(cfg, rotate=rotate_token)
        # the configured cache is authoritative: an HF_HOME inherited from the environment must
        # not redirect the service to another profile's cache (huggingface_hub reads it at import)
        os.environ["HF_HOME"] = str(cfg.model_cache_dir)
        os.environ.pop("HF_HUB_CACHE", None)
        t0 = time.perf_counter()
        self.embedder = create_provider(cfg.embedding)
        log.info("embedding provider ready in %.1fs (%s)", time.perf_counter() - t0, self.embedder.fingerprint)
        self.store, self.store_info = open_store_with_recovery(cfg)
        action = self.store.ensure_vectors(self.embedder.fingerprint, self.embedder.dim, cfg.embedding.on_model_change)
        if action == "reset":
            log.warning("vectors were reset because the embedding model changed; re-embedding will run in the background")
        log.info("store: %s schema v%s, %d documents, fingerprint %s", self.store.path, self.store_info["schema_version"],
                 self.store.count_documents(), self.store.fingerprint)
        self.registry = build_default_registry(cfg)
        self.windows = None
        if cfg.indexing.use_windows_search and sys.platform == "win32":
            try:
                from .inventory.windows_search import WindowsSearchInventory
                w = WindowsSearchInventory([str(r) for r in cfg.roots], cfg.excludes)
                if w.ping():
                    self.windows = w
            except Exception as e:
                log.warning("Windows Search adapter unavailable: %s", e)
        self.fs = FilesystemInventory([str(r) for r in cfg.roots], cfg.excludes, cfg.indexing.follow_reparse_points)
        extractor = self.registry
        if isolate_extractors and sys.platform == "win32":
            from .extract.isolated import IsolatedExtractor
            extractor = IsolatedExtractor(cfg, self.registry, timeout_s=cfg.indexing.extract_timeout_s)
        self.extractor = extractor
        self.indexer = Indexer(cfg, self.store, extractor, self.embedder, windows_inventory=self.windows, fs_inventory=self.fs)
        # GPU courtesy needs the bulk adapter's LUID (per boot; from this start's resolution)
        try:
            bulk = self.device_resolution.get("roles", {}).get("bulk_device", {}).get("resolved", "")
            if bulk.startswith("dml:"):
                ordinal = int(bulk.split(":")[1])
                for a in self.device_resolution.get("adapters", []):
                    if a["ordinal"] == ordinal:
                        self.indexer.bulk_luid = a["luid"]
        except Exception:  # noqa: BLE001
            pass
        self.retriever = Retriever(cfg, self.store, self.embedder, windows=self.windows)
        if start_indexer:
            self.indexer.start()

    # ---- runtime scope changes (tray / API) ----
    def apply_scope(self, roots: list[str] | None = None, excludes: list[str] | None = None) -> dict:  # noqa: C901
        """Change the indexed roots and/or the exclusion patterns: persist them into the
        configuration file (textual edit, comments kept) and apply them live. Paths must be
        absolute existing directories; the service account must already be able to read a
        new root (the tray / CLI grant that before calling)."""
        from .security import display_path, normalize_path
        new_roots = [display_path(str(r)) for r in self.cfg.roots] if roots is None else []
        if roots is not None:
            seen: set[str] = set()
            for r in roots:
                p = display_path(str(r))
                if not os.path.isabs(p):
                    raise ValueError(f"root is not an absolute path: {r}")
                if not os.path.isdir(p):
                    raise ValueError(f"root is not an existing directory: {p}")
                n = normalize_path(p)
                if n in seen:
                    continue
                seen.add(n)
                new_roots.append(p)
        def _clean(v: str, what: str) -> str:
            # control characters would let a value break out of its YAML line on the next save
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
                raise ValueError(f"{what} contains a control character: {v!r}")
            return v
        for r in new_roots:
            _clean(r, "root")
        # one spelling for patterns: forward slashes, which is what is persisted and what a
        # pattern with a path in it must use to be matched as a path (a backslash pattern would
        # silently act as a bare file-name pattern until the next restart)
        new_excl = list(self.cfg.excludes) if excludes is None else \
            [_clean(str(e).strip(), "exclusion").replace("\\", "/") for e in excludes if str(e).strip()]
        if self.cfg.source_path:
            from .config_edit import update_config_lists
            update_config_lists(self.cfg.source_path, roots=[p.replace("\\", "/") for p in new_roots] if roots is not None else None,
                                excludes=new_excl if excludes is not None else None)
        out = self.indexer.reconfigure(new_roots, new_excl)
        self.retriever.roots = self.cfg.normalized_roots()
        self.retriever._cache.clear()
        out["config"] = str(self.cfg.source_path) if self.cfg.source_path else None
        return out

    def close_without_indexer(self) -> None:
        if hasattr(self.extractor, "close"):
            self.extractor.close()
        self.store.close()
        self.embedder.close()

    def close(self) -> None:
        try:
            self.indexer.stop()
        finally:
            self.close_without_indexer()
