"""Wires the subsystems together (used by the API server, the CLI's local mode, and tests)."""
from __future__ import annotations

import logging
import os
import sys
import time

from .config import Config
from .embed import create_provider
from .extract.registry import build_default_registry
from .indexer import Indexer
from .inventory.filesystem import FilesystemInventory
from .retrieval import Retriever
from .store.db import Store

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, cfg: Config, start_indexer: bool = True, isolate_extractors: bool = True):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        t0 = time.perf_counter()
        self.embedder = create_provider(cfg.embedding)
        log.info("embedding provider ready in %.1fs (%s)", time.perf_counter() - t0, self.embedder.fingerprint)
        self.store = Store(cfg.db_path, vector_cache=cfg.retrieval.vector_cache)
        action = self.store.ensure_vectors(self.embedder.fingerprint, self.embedder.dim, cfg.embedding.on_model_change)
        if action == "reset":
            log.warning("vectors were reset because the embedding model changed; re-embedding will run in the background")
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
        self.retriever = Retriever(cfg, self.store, self.embedder, windows=self.windows)
        if start_indexer:
            self.indexer.start()

    def close(self) -> None:
        try:
            self.indexer.stop()
        finally:
            if hasattr(self.extractor, "close"):
                self.extractor.close()
            self.store.close()
            self.embedder.close()
