"""Retrieval: literal, semantic, and hybrid ranking with explainable component scores.

literal   = FTS5 BM25 over chunk text (AND of terms, falling back to OR) + filename match
            + Windows Search FREETEXT rank when available
semantic  = cosine similarity of the query vector to chunk vectors, best chunk per document
hybrid    = reciprocal rank fusion of the semantic and lexical rankings (weighted), with the
            raw components preserved on every hit

Scores: ``score`` is always in [0, 1]. For hybrid it is the weighted RRF sum normalized by
its maximum possible value (both lists rank the document first).
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Config
from .models import ScoreComponents, SearchHit
from .security import is_within, normalize_path
from .store.db import Store

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "was", "were", "be", "it", "that", "this",
         "with", "as", "at", "by", "from", "about", "my", "i", "we", "you", "our", "where", "which", "what", "find", "file",
         "document", "notes", "note", "discussed", "discussing", "talk", "talked", "old", "one", "some", "any", "me"}


@dataclass
class _Cand:
    doc_id: int
    semantic: float | None = None
    semantic_chunk: int | None = None
    semantic_rank: int | None = None
    lexical: float | None = None
    lexical_chunk: int | None = None
    lexical_rank: int | None = None
    filename: float | None = None
    windows_rank: float | None = None
    terms: list[str] = field(default_factory=list)
    rrf: float | None = None


class Retriever:
    def __init__(self, cfg: Config, store: Store, embedder, windows=None, cache_size: int = 256):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.windows = windows
        self.roots = cfg.normalized_roots()
        self._cache: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._cache_size = cache_size

    # ---------- public ----------
    def search(self, query: str, mode: str | None = None, limit: int = 20, roots: list[str] | None = None,
               extensions: list[str] | None = None, cache: bool = True) -> dict[str, Any]:
        """Repeated identical queries are served from a small cache that is invalidated by any
        index mutation (store.version). ``cache=False`` bypasses it (benchmarks)."""
        key = (query, mode, limit, tuple(roots or ()), tuple(extensions or ()), self.store.version, self.cfg.retrieval.fusion)
        hit = self._cache.get(key) if cache else None
        if hit is not None:
            self._cache.move_to_end(key)
            out = dict(hit)
            out["cached"] = True
            return out
        out = self._search(query, mode, limit, roots, extensions)
        self._cache[key] = out
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return out

    def _search(self, query: str, mode: str | None, limit: int, roots: list[str] | None, extensions: list[str] | None) -> dict[str, Any]:
        t0 = time.perf_counter()
        mode = (mode or self.cfg.retrieval.default_mode).lower()
        if mode not in ("literal", "semantic", "hybrid"):
            raise ValueError(f"unknown mode {mode}")
        query = (query or "").strip()
        if not query:
            return {"query": query, "mode": mode, "results": [], "took_ms": 0.0, "candidates": 0}
        cands: dict[int, _Cand] = {}
        timings: dict[str, float] = {}
        glob_mode = self._is_glob(query)
        if mode in ("literal", "hybrid"):
            t = time.perf_counter()
            self._lexical(query, cands, glob_mode)
            timings["lexical_ms"] = round((time.perf_counter() - t) * 1000, 1)
        if mode in ("semantic", "hybrid") and not glob_mode:
            t = time.perf_counter()
            self._semantic(query, cands)
            timings["semantic_ms"] = round((time.perf_counter() - t) * 1000, 1)

        docs = self.store.get_documents(cands.keys())
        root_filter = [normalize_path(r) for r in roots] if roots else None
        ext_filter = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions} if extensions else None
        scored: list[tuple[float, _Cand]] = []
        kept: list[_Cand] = []
        for c in cands.values():
            d = docs.get(c.doc_id)
            if d is None or d["extract_status"] == "missing":
                continue
            if root_filter and not is_within(d["display_path"], root_filter):
                continue
            if ext_filter and (d["extension"] or "") not in ext_filter:
                continue
            kept.append(c)
        norms = self._minmax(kept)
        for c in kept:
            scored.append((self._final_score(c, mode, norms), c))
        scored.sort(key=lambda x: (-x[0], x[1].doc_id))
        hits = [self._hit(score, c, docs[c.doc_id], mode) for score, c in scored[:limit]]
        took = (time.perf_counter() - t0) * 1000
        return {"query": query, "mode": mode, "results": [h.to_dict() for h in hits], "took_ms": round(took, 1),
                "candidates": len(scored), "timings": timings, "glob": glob_mode,
                "weights": {"semantic": self.cfg.retrieval.semantic_weight, "lexical": self.cfg.retrieval.lexical_weight, "rrf_k": self.cfg.retrieval.rrf_k}}

    # ---------- lexical ----------
    @staticmethod
    def _is_glob(q: str) -> bool:
        return (("*" in q) or ("?" in q)) and (" " not in q.strip())

    @staticmethod
    def terms(q: str) -> list[str]:
        toks = [t.lower() for t in _TOKEN.findall(q)]
        out = [t for t in toks if t not in _STOP and len(t) > 1]
        return out or toks

    def fts_expr(self, terms: list[str], conjunctive: bool) -> str:
        quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
        return (" AND " if conjunctive else " OR ").join(quoted)

    def _lexical(self, query: str, cands: dict[int, _Cand], glob_mode: bool) -> None:
        rc = self.cfg.retrieval
        if glob_mode:
            for doc_id, fn in self.store.filename_glob(query, limit=rc.candidate_docs * 2):
                c = cands.setdefault(doc_id, _Cand(doc_id))
                c.filename = 1.0
                c.lexical = 1.0
                c.terms = [query]
            self._assign_ranks(cands, "lexical")
            return
        terms = self.terms(query)
        if not terms:
            return
        rows: list[tuple[int, int, float]] = []
        if len(terms) > 1:
            rows = self.store.fts(self.fts_expr(terms, True), rc.candidate_chunks)
        if not rows:
            rows = self.store.fts(self.fts_expr(terms, False), rc.candidate_chunks)
        best: dict[int, tuple[int, float]] = {}
        for chunk_id, doc_id, s in rows:
            if doc_id not in best or s > best[doc_id][1]:
                best[doc_id] = (chunk_id, s)
        max_s = max((s for _, s in best.values()), default=0.0) or 1.0
        for doc_id, (chunk_id, s) in best.items():
            c = cands.setdefault(doc_id, _Cand(doc_id))
            c.lexical = s / max_s
            c.lexical_chunk = chunk_id
            c.terms = terms
        # filename substring match (all terms present in the filename)
        fn_hits: dict[int, int] = {}
        for t in terms[:6]:
            for doc_id, fn in self.store.filename_like(t, limit=500):
                fn_hits[doc_id] = fn_hits.get(doc_id, 0) + 1
        for doc_id, n in fn_hits.items():
            frac = n / max(1, min(len(terms), 6))
            if frac >= 0.5 or len(terms) == 1:
                c = cands.setdefault(doc_id, _Cand(doc_id))
                c.filename = round(frac, 3)
                c.terms = terms
                c.lexical = max(c.lexical or 0.0, 0.5 * frac)
        # Windows Search relevance (when the file has content in SystemIndex)
        if rc.use_windows_rank and self.windows is not None:
            try:
                for path, rank in self.windows.freetext(query, [os.fspath(r) for r in self.cfg.roots], limit=rc.candidate_docs):
                    row = self.store.get_document(normalize_path(path))
                    if row is None:
                        continue
                    c = cands.setdefault(int(row["id"]), _Cand(int(row["id"])))
                    c.windows_rank = round(float(rank) / 1000.0, 3)
                    c.lexical = max(c.lexical or 0.0, c.windows_rank * 0.8)
                    if not c.terms:
                        c.terms = terms
            except Exception as e:
                log.debug("windows freetext failed: %s", e)
        self._assign_ranks(cands, "lexical")

    # ---------- semantic ----------
    def _semantic(self, query: str, cands: dict[int, _Cand]) -> None:
        rc = self.cfg.retrieval
        q = self.embedder.embed([query], "query")[0]
        hits = self.store.knn(q, rc.candidate_chunks)
        if not hits:
            return
        chunk_rows = self.store.get_chunks([cid for cid, _ in hits])
        best: dict[int, tuple[int, float]] = {}
        for cid, sim in hits:
            r = chunk_rows.get(cid)
            if r is None:
                continue
            doc_id = int(r["doc_id"])
            if doc_id not in best or sim > best[doc_id][1]:
                best[doc_id] = (cid, sim)
        for doc_id, (cid, sim) in best.items():
            c = cands.setdefault(doc_id, _Cand(doc_id))
            c.semantic = float(max(0.0, min(1.0, sim)))
            c.semantic_chunk = cid
        self._assign_ranks(cands, "semantic")

    @staticmethod
    def _assign_ranks(cands: dict[int, _Cand], which: str) -> None:
        items = [c for c in cands.values() if getattr(c, which) is not None]
        items.sort(key=lambda c: -getattr(c, which))
        for i, c in enumerate(items, 1):
            setattr(c, f"{which}_rank", i)

    # ---------- fusion ----------
    @staticmethod
    def _minmax(cands: list[_Cand]) -> dict[str, tuple[float, float]]:
        """Per-component (min, max) over the candidate set, for convex fusion."""
        out: dict[str, tuple[float, float]] = {}
        for which in ("semantic", "lexical"):
            vals = [getattr(c, which) for c in cands if getattr(c, which) is not None]
            if vals:
                lo, hi = min(vals), max(vals)
                # keep a floor so a lone/flat list does not collapse to 0 or blow up to 1 for junk
                out[which] = (min(lo, hi - 1e-9) if hi > lo else lo - 0.5, hi)
        return out

    def _rrf(self, c: _Cand) -> float:
        rc = self.cfg.retrieval
        k = rc.rrf_k
        ws, wl = rc.semantic_weight, rc.lexical_weight
        rrf = 0.0
        if c.semantic_rank:
            rrf += ws / (k + c.semantic_rank)
        if c.lexical_rank:
            rrf += wl / (k + c.lexical_rank)
        if c.filename:
            rrf += 0.5 * wl * c.filename / (k + 1)
        norm = (ws + wl + 0.5 * wl) / (k + 1)
        return rrf / norm if norm else 0.0

    def _final_score(self, c: _Cand, mode: str, norms: dict[str, tuple[float, float]]) -> float:
        rc = self.cfg.retrieval
        if mode == "semantic":
            return c.semantic or 0.0
        if mode == "literal":
            s = c.lexical or 0.0
            if c.filename:
                s = max(s, 0.6 + 0.4 * c.filename)
            return min(1.0, s)
        c.rrf = self._rrf(c)
        if rc.fusion == "rrf":
            return c.rrf
        ws, wl = rc.semantic_weight, rc.lexical_weight

        def mm(which: str) -> float:
            v = getattr(c, which)
            if v is None or which not in norms:
                return 0.0
            lo, hi = norms[which]
            return (v - lo) / (hi - lo) if hi > lo else 1.0

        s = ws * mm("semantic") + wl * mm("lexical")
        if c.filename:
            if c.filename >= 1.0:
                # every query term is in the filename: the user remembered the name; rank it first
                s = max(s, 0.9 + 0.1 * mm("lexical"))
            else:
                s = max(s, min(1.0, s + 0.25 * wl * c.filename))
        return min(1.0, s)

    def _hit(self, score: float, c: _Cand, d, mode: str) -> SearchHit:
        rc = self.cfg.retrieval
        comps = ScoreComponents(semantic=c.semantic, lexical=c.lexical, filename=c.filename, windows_rank=c.windows_rank,
                                semantic_rank=c.semantic_rank, lexical_rank=c.lexical_rank,
                                rrf=c.rrf if mode == "hybrid" else None)
        if c.semantic is not None and c.lexical is not None:
            mt = "both"
        elif c.semantic is not None:
            mt = "semantic"
        elif c.filename and c.lexical_chunk is None and c.windows_rank is None:
            mt = "filename"
        else:
            mt = "lexical"
        # pick the chunk to show: lexical chunk if it exists and there are terms, else semantic chunk
        chunk_id = None
        if mode == "semantic":
            chunk_id = c.semantic_chunk
        elif mode == "literal":
            chunk_id = c.lexical_chunk
        else:
            chunk_id = c.semantic_chunk if (c.semantic or 0) >= (c.lexical or 0) else (c.lexical_chunk or c.semantic_chunk)
            if chunk_id is None:
                chunk_id = c.lexical_chunk or c.semantic_chunk
        excerpt, ordinal = "", None
        if chunk_id is not None:
            ch = self.store.get_chunks([chunk_id]).get(chunk_id)
            if ch is not None:
                ordinal = int(ch["ordinal"])
                excerpt = self._excerpt(ch["text"], c.terms, rc.excerpt_chars)
        why: list[str] = []
        if c.semantic is not None:
            why.append(f"semantic: best chunk {c.semantic_chunk} cosine {c.semantic:.3f} (rank {c.semantic_rank})")
        if c.lexical is not None and c.lexical_chunk is not None:
            matched = [t for t in c.terms if t and re.search(r"\b" + re.escape(t), excerpt or "", re.I)]
            why.append(f"lexical: bm25 {c.lexical:.3f} (rank {c.lexical_rank}); terms in excerpt: {matched or c.terms}")
        if c.filename:
            why.append(f"filename matches {c.filename:.0%} of terms")
        if c.windows_rank is not None:
            why.append(f"windows search rank {c.windows_rank:.3f}")
        return SearchHit(path=d["display_path"], filename=d["filename"], score=float(score), match_type=mt, excerpt=excerpt,
                         chunk_ordinal=ordinal, modified=d["mtime"], file_type=(d["extension"] or "").lstrip(".") or "file",
                         size=d["size"], components=comps, why=why, doc_id=int(d["id"]))

    @staticmethod
    def _excerpt(text: str, terms: list[str], n: int) -> str:
        t = " ".join(text.split())
        if len(t) <= n:
            return t
        pos = -1
        low = t.lower()
        for term in terms:
            i = low.find(term.lower())
            if i >= 0 and (pos < 0 or i < pos):
                pos = i
        if pos < 0:
            return t[:n].rstrip() + "..."
        start = max(0, pos - n // 3)
        end = min(len(t), start + n)
        s = t[start:end]
        return ("..." if start > 0 else "") + s.strip() + ("..." if end < len(t) else "")
