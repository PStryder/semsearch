"""SQLite-backed store: documents, chunks, FTS5 lexical index, sqlite-vec vectors, job queue,
error log, and metadata. One file, one process, WAL mode, serialized by a re-entrant lock.

Identity model
  * ``documents.path`` (normalized, case-folded) is the primary identity
  * ``(volume_serial, file_id)`` is the NTFS identity used to recognize renames/moves
  * ``content_hash`` (BLAKE2b of the bytes) decides whether re-extraction is needed
  * ``chunks.text_hash`` lets identical chunk text reuse an existing vector

Vectors are stored in a ``vec0`` virtual table (sqlite-vec) keyed by chunk id and, when
enabled, mirrored into an in-memory float32 matrix for fast repeated queries.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Iterator, Sequence

import numpy as np

from ..models import Chunk

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  display_path TEXT NOT NULL,
  root TEXT,
  filename TEXT NOT NULL,
  extension TEXT,
  size INTEGER,
  mtime REAL,
  ctime REAL,
  file_id TEXT,
  volume_serial TEXT,
  content_hash TEXT,
  win_entry_id INTEGER,
  gather_time REAL,
  extract_status TEXT,
  extract_method TEXT,
  extract_error TEXT,
  text_chars INTEGER DEFAULT 0,
  n_chunks INTEGER DEFAULT 0,
  embedding_fingerprint TEXT,
  indexed_at REAL,
  last_seen REAL,
  source TEXT,
  missing_since REAL,
  title TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_fileid ON documents(volume_serial, file_id);
CREATE INDEX IF NOT EXISTS idx_documents_root ON documents(root);
CREATE INDEX IF NOT EXISTS idx_documents_filename ON documents(filename);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(extract_status);
CREATE INDEX IF NOT EXISTS idx_documents_fp ON documents(embedding_fingerprint);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(text_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  op TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 5,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  enqueued_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, priority, enqueued_at);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY,
  path TEXT,
  stage TEXT,
  message TEXT,
  at REAL
);
CREATE INDEX IF NOT EXISTS idx_errors_at ON errors(at);
"""


def text_hash(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


def file_hash(path: str, block: int = 1 << 20) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def vec_to_blob(v: np.ndarray) -> bytes:
    return np.ascontiguousarray(v, dtype=np.float32).tobytes()


def blob_to_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


class VectorCache:
    """In-memory mirror of vec_chunks for fast brute-force cosine search."""

    def __init__(self, dim: int):
        self.dim = dim
        self.ids = np.zeros((0,), dtype=np.int64)
        self.mat = np.zeros((0, dim), dtype=np.float32)
        self.pos: dict[int, int] = {}
        self.deleted = np.zeros((0,), dtype=bool)
        self.n_deleted = 0

    def load(self, rows: Iterable[tuple[int, bytes]]) -> None:
        ids, vecs = [], []
        for cid, blob in rows:
            ids.append(cid)
            vecs.append(blob_to_vec(blob))
        self.ids = np.array(ids, dtype=np.int64)
        self.mat = np.vstack(vecs).astype(np.float32) if vecs else np.zeros((0, self.dim), dtype=np.float32)
        self.pos = {int(c): i for i, c in enumerate(self.ids)}
        self.deleted = np.zeros((len(ids),), dtype=bool)
        self.n_deleted = 0

    def add(self, ids: Sequence[int], vecs: np.ndarray) -> None:
        if len(ids) == 0:
            return
        base = len(self.ids)
        self.ids = np.concatenate([self.ids, np.asarray(ids, dtype=np.int64)])
        self.mat = np.vstack([self.mat, np.asarray(vecs, dtype=np.float32)])
        self.deleted = np.concatenate([self.deleted, np.zeros((len(ids),), dtype=bool)])
        for i, c in enumerate(ids):
            self.pos[int(c)] = base + i

    def remove(self, ids: Iterable[int]) -> None:
        for c in ids:
            i = self.pos.pop(int(c), None)
            if i is not None and not self.deleted[i]:
                self.deleted[i] = True
                self.n_deleted += 1
        if self.n_deleted > 1000 and self.n_deleted > len(self.ids) // 5:
            self.compact()

    def compact(self) -> None:
        keep = ~self.deleted
        self.ids = self.ids[keep]
        self.mat = self.mat[keep]
        self.deleted = np.zeros((len(self.ids),), dtype=bool)
        self.pos = {int(c): i for i, c in enumerate(self.ids)}
        self.n_deleted = 0

    def __len__(self) -> int:
        return len(self.ids) - self.n_deleted

    def search(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        if len(self.ids) == 0:
            return []
        sims = self.mat @ np.asarray(q, dtype=np.float32)
        if self.n_deleted:
            sims = np.where(self.deleted, -2.0, sims)
        k = min(k, len(sims))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(self.ids[i]), float(sims[i])) for i in idx if not self.deleted[i]]


class Store:
    def __init__(self, path: str | os.PathLike, vector_cache: bool = True):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA cache_size=-65536")
        self._load_vec()
        self.conn.executescript(_SCHEMA)
        self._migrate()
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.dim: int | None = int(self.get_meta("dim")) if self.get_meta("dim") else None
        self.fingerprint: str | None = self.get_meta("embedding_fingerprint")
        self.use_cache = vector_cache
        self.cache: VectorCache | None = None
        self.version = 0  # bumped on every content mutation; lets callers cache query results safely
        if self.dim and self._vec_table_exists():
            self._init_cache()

    # ---- setup ----
    def _load_vec(self) -> None:
        import sqlite_vec
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.vec_version = self.conn.execute("select vec_version()").fetchone()[0]

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (CREATE IF NOT EXISTS skips them)."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(documents)")}
        for col, decl in (("missing_since", "REAL"), ("title", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")

    def _vec_table_exists(self) -> bool:
        r = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'").fetchone()
        return r is not None

    def _init_cache(self) -> None:
        if not self.use_cache or not self.dim:
            self.cache = None
            return
        t0 = time.perf_counter()
        self.cache = VectorCache(self.dim)
        rows = self.conn.execute("SELECT rowid, embedding FROM vec_chunks").fetchall()
        self.cache.load(((int(r[0]), r[1]) for r in rows))
        log.info("vector cache loaded: %d vectors in %.2fs", len(self.cache), time.perf_counter() - t0)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---- meta ----
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.lock:
            if value is None:
                self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---- embedding model lifecycle ----
    def ensure_vectors(self, fingerprint: str, dim: int, on_change: str = "reembed") -> str:
        """Bind the store to an embedding fingerprint. Returns 'ok', 'created', or 'reset'.
        A changed fingerprint invalidates all vectors (they are not comparable) and clears
        documents.embedding_fingerprint so the indexer re-embeds from stored chunk text."""
        with self.lock:
            if self.fingerprint == fingerprint and self.dim == dim and self._vec_table_exists():
                if self.cache is None and self.use_cache:
                    self._init_cache()
                return "ok"
            if self.fingerprint and self.fingerprint != fingerprint:
                if on_change == "refuse":
                    raise RuntimeError(f"store is bound to embedding {self.fingerprint}; configured {fingerprint}. Set embedding.on_model_change=reembed to migrate.")
                log.warning("embedding model changed (%s -> %s): dropping vectors, scheduling re-embed", self.fingerprint, fingerprint)
            self.conn.execute("DROP TABLE IF EXISTS vec_chunks")
            self.conn.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{int(dim)}] distance_metric=cosine)")
            self.conn.execute("UPDATE documents SET embedding_fingerprint=NULL")
            self.set_meta("embedding_fingerprint", fingerprint)
            self.set_meta("dim", str(dim))
            self.set_meta("vectors_reset_at", str(time.time()))
            created = self.fingerprint is None
            self.fingerprint, self.dim = fingerprint, dim
            self._init_cache()
            return "created" if created else "reset"

    # ---- documents ----
    def get_document(self, path_norm: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE path=?", (path_norm,)).fetchone()

    def get_document_by_id(self, doc_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def get_documents(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(ids)
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            q = f"SELECT * FROM documents WHERE id IN ({','.join('?' * len(part))})"
            for r in self.conn.execute(q, part):
                out[int(r["id"])] = r
        return out

    def find_by_file_id(self, volume_serial: int | str, file_id: int | str) -> list[sqlite3.Row]:
        # NTFS/ReFS file ids can exceed a signed 64-bit integer, so they are stored as text
        return self.conn.execute("SELECT * FROM documents WHERE volume_serial=? AND file_id=?", (str(volume_serial), str(file_id))).fetchall()

    def upsert_document(self, **f: Any) -> int:
        cols = list(f.keys())
        with self.lock:
            self.version += 1
            sql = (f"INSERT INTO documents({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                   f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'path')}")
            self.conn.execute(sql, [f[c] for c in cols])
            return int(self.conn.execute("SELECT id FROM documents WHERE path=?", (f["path"],)).fetchone()[0])

    def update_document(self, doc_id: int, **f: Any) -> None:
        if not f:
            return
        with self.lock:
            if set(f) - {"last_seen"}:
                self.version += 1
            self.conn.execute(f"UPDATE documents SET {','.join(f'{c}=?' for c in f)} WHERE id=?", [*f.values(), doc_id])

    def remove_document(self, path_norm: str) -> bool:
        with self.lock:
            r = self.conn.execute("SELECT id FROM documents WHERE path=?", (path_norm,)).fetchone()
            if not r:
                return False
            self.version += 1
            self._delete_doc_vectors(int(r[0]))
            self.conn.execute("DELETE FROM documents WHERE id=?", (int(r[0]),))
            return True

    def remove_document_id(self, doc_id: int) -> None:
        with self.lock:
            self.version += 1
            self._delete_doc_vectors(doc_id)
            self.conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def tombstone_document(self, doc_id: int) -> None:
        """Mark a document as vanished. Chunks and vectors are kept for a grace period so that a
        rename/move indexed shortly afterwards can reclaim them via the NTFS file id."""
        with self.lock:
            self.version += 1
            self.conn.execute("UPDATE documents SET extract_status='missing', missing_since=COALESCE(missing_since, ?) WHERE id=? AND extract_status != 'missing'",
                              (time.time(), doc_id))

    def restore_document(self, doc_id: int, **f: Any) -> None:
        f = dict(f)
        f["missing_since"] = None
        f["extract_status"] = "ok"
        self.update_document(doc_id, **f)

    def purge_missing(self, older_than_s: float) -> int:
        cutoff = time.time() - older_than_s
        with self.lock:
            self.version += 1
            ids = [int(r[0]) for r in self.conn.execute("SELECT id FROM documents WHERE extract_status='missing' AND missing_since <= ?", (cutoff,))]
            for i in ids:
                self._delete_doc_vectors(i)
                self.conn.execute("DELETE FROM documents WHERE id=?", (i,))
        return len(ids)

    def iter_paths(self, root_norm: str | None = None) -> Iterator[tuple[int, str, float | None]]:
        if root_norm:
            cur = self.conn.execute("SELECT id, path, last_seen FROM documents WHERE root=?", (root_norm,))
        else:
            cur = self.conn.execute("SELECT id, path, last_seen FROM documents")
        for r in cur:
            yield int(r[0]), r[1], r[2]

    def documents_needing_embedding(self, fingerprint: str, limit: int = 1000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, path, display_path FROM documents WHERE n_chunks > 0 AND (embedding_fingerprint IS NULL OR embedding_fingerprint != ?) LIMIT ?",
            (fingerprint, limit)).fetchall()

    # ---- chunks & vectors ----
    def _delete_doc_vectors(self, doc_id: int) -> None:
        ids = [int(r[0]) for r in self.conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        if ids and self._vec_table_exists():
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                self.conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({','.join('?' * len(part))})", part)
            if self.cache is not None:
                self.cache.remove(ids)

    def chunks_for_doc(self, doc_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc_id,)).fetchall()

    def get_chunks(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(ids)
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.conn.execute(f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
                out[int(r["id"])] = r
        return out

    def vectors_for_hashes(self, hashes: Iterable[str]) -> dict[str, np.ndarray]:
        """Reuse: existing vectors for identical chunk text (same fingerprint by construction)."""
        hs = list(set(hashes))
        out: dict[str, np.ndarray] = {}
        if not hs or not self._vec_table_exists():
            return out
        for i in range(0, len(hs), 400):
            part = hs[i:i + 400]
            rows = self.conn.execute(
                f"SELECT c.text_hash, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id "
                f"WHERE c.text_hash IN ({','.join('?' * len(part))})", part).fetchall()
            for h, blob in rows:
                if h not in out:
                    out[h] = blob_to_vec(blob)
        return out

    def replace_chunks(self, doc_id: int, chunks: Sequence[Chunk], vectors: np.ndarray | None, fingerprint: str | None) -> list[int]:
        """Atomically replace a document's chunks (and vectors). Returns new chunk ids."""
        with self.lock:
            self.version += 1
            self.conn.execute("BEGIN")
            try:
                self._delete_doc_vectors(doc_id)
                self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                ids: list[int] = []
                for ch in chunks:
                    # text_hash covers what was embedded (title header + text) so vector reuse is exact
                    cur = self.conn.execute("INSERT INTO chunks(doc_id, ordinal, start, end, text, text_hash) VALUES(?,?,?,?,?,?)",
                                            (doc_id, ch.ordinal, ch.start, ch.end, ch.text, text_hash(ch.for_embedding)))
                    ids.append(int(cur.lastrowid))
                if vectors is not None and len(ids):
                    assert vectors.shape[0] == len(ids)
                    for cid, v in zip(ids, vectors):
                        self.conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)", (cid, vec_to_blob(v)))
                self.conn.execute("UPDATE documents SET n_chunks=?, embedding_fingerprint=? WHERE id=?",
                                  (len(ids), fingerprint if vectors is not None else None, doc_id))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            if vectors is not None and self.cache is not None and len(ids):
                self.cache.add(ids, vectors)
            return ids

    def set_vectors(self, chunk_ids: Sequence[int], vectors: np.ndarray, doc_id: int, fingerprint: str) -> None:
        with self.lock:
            self.version += 1
            self.conn.execute("BEGIN")
            try:
                for cid, v in zip(chunk_ids, vectors):
                    self.conn.execute("INSERT OR REPLACE INTO vec_chunks(rowid, embedding) VALUES(?, ?)", (int(cid), vec_to_blob(v)))
                self.conn.execute("UPDATE documents SET embedding_fingerprint=? WHERE id=?", (fingerprint, doc_id))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            if self.cache is not None:
                self.cache.remove(chunk_ids)
                self.cache.add(list(chunk_ids), vectors)

    # ---- search primitives ----
    def knn(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        """Return [(chunk_id, cosine_similarity)] best first."""
        if self.cache is not None:
            return self.cache.search(q, k)
        if not self._vec_table_exists():
            return []
        rows = self.conn.execute("SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                                 (vec_to_blob(q), int(k))).fetchall()
        return [(int(r[0]), 1.0 - float(r[1])) for r in rows]

    def fts(self, match_expr: str, limit: int) -> list[tuple[int, int, float]]:
        """Return [(chunk_id, doc_id, bm25_score)] where higher score is better."""
        rows = self.conn.execute(
            "SELECT f.rowid, c.doc_id, bm25(chunks_fts) AS r FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY r LIMIT ?", (match_expr, int(limit))).fetchall()
        return [(int(r[0]), int(r[1]), -float(r[2])) for r in rows]

    def filename_like(self, needle: str, limit: int = 200) -> list[tuple[int, str]]:
        like = "%" + needle.replace("%", "[%]").replace("_", "[_]") + "%"
        rows = self.conn.execute("SELECT id, filename FROM documents WHERE filename LIKE ? LIMIT ?", (like, int(limit))).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def filename_glob(self, pattern: str, limit: int = 200) -> list[tuple[int, str]]:
        rows = self.conn.execute("SELECT id, filename FROM documents WHERE filename GLOB ? LIMIT ?", (pattern, int(limit))).fetchall()
        if not rows and pattern != pattern.lower():
            rows = self.conn.execute("SELECT id, filename FROM documents WHERE lower(filename) GLOB ? LIMIT ?", (pattern.lower(), int(limit))).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    # ---- job queue ----
    def enqueue(self, path_norm: str, op: str, priority: int = 5) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO jobs(path, op, priority, state, attempts, enqueued_at, updated_at) VALUES(?,?,?,'pending',0,?,?) "
                "ON CONFLICT(path) DO UPDATE SET op=excluded.op, priority=min(jobs.priority, excluded.priority), "
                "state=CASE WHEN jobs.state='running' THEN 'running' ELSE 'pending' END, attempts=0, error=NULL, updated_at=excluded.updated_at",
                (path_norm, op, priority, now, now))

    def enqueue_many(self, items: Iterable[tuple[str, str, int]]) -> int:
        now = time.time()
        n = 0
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                for path_norm, op, priority in items:
                    self.conn.execute(
                        "INSERT INTO jobs(path, op, priority, state, attempts, enqueued_at, updated_at) VALUES(?,?,?,'pending',0,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET op=excluded.op, priority=min(jobs.priority, excluded.priority), "
                        "state=CASE WHEN jobs.state='running' THEN 'running' ELSE 'pending' END, attempts=0, error=NULL, updated_at=excluded.updated_at",
                        (path_norm, op, priority, now, now))
                    n += 1
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return n

    def next_job(self) -> sqlite3.Row | None:
        with self.lock:
            r = self.conn.execute("SELECT id FROM jobs WHERE state='pending' ORDER BY priority, enqueued_at LIMIT 1").fetchone()
            if not r:
                return None
            self.conn.execute("UPDATE jobs SET state='running', attempts=attempts+1, updated_at=? WHERE id=?", (time.time(), r["id"]))
            return self.conn.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone()

    def complete_job(self, job_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM jobs WHERE id=? AND state='running'", (job_id,))

    def fail_job(self, job_id: int, error: str, max_attempts: int) -> None:
        with self.lock:
            r = self.conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r:
                return
            state = "failed" if r[0] >= max_attempts else "pending"
            self.conn.execute("UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?", (state, error[:1000], time.time(), job_id))

    def requeue_running(self) -> int:
        with self.lock:
            cur = self.conn.execute("UPDATE jobs SET state='pending', updated_at=? WHERE state='running'", (time.time(),))
            return cur.rowcount

    def retry_failed(self) -> int:
        with self.lock:
            cur = self.conn.execute("UPDATE jobs SET state='pending', attempts=0, updated_at=? WHERE state='failed'", (time.time(),))
            return cur.rowcount

    def clear_jobs(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM jobs")

    def queue_stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
        d = {"pending": 0, "running": 0, "failed": 0}
        for s, n in rows:
            d[s] = int(n)
        return d

    # ---- diagnostics ----
    def record_error(self, path: str | None, stage: str, message: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO errors(path, stage, message, at) VALUES(?,?,?,?)", (path, stage, message[:2000], time.time()))
            self.conn.execute("DELETE FROM errors WHERE id NOT IN (SELECT id FROM errors ORDER BY at DESC LIMIT 5000)")

    def recent_errors(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute("SELECT path, stage, message, at FROM errors ORDER BY at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        c = self.conn
        by_status = {r[0] or "none": int(r[1]) for r in c.execute("SELECT extract_status, COUNT(*) FROM documents GROUP BY extract_status")}
        by_ext = {r[0] or "": int(r[1]) for r in c.execute("SELECT extension, COUNT(*) AS n FROM documents GROUP BY extension ORDER BY n DESC LIMIT 40")}
        by_method = {r[0] or "none": int(r[1]) for r in c.execute("SELECT extract_method, COUNT(*) FROM documents GROUP BY extract_method")}
        n_docs = int(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        n_chunks = int(c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        n_vec = int(c.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]) if self._vec_table_exists() else 0
        n_stale = int(c.execute("SELECT COUNT(*) FROM documents WHERE n_chunks>0 AND (embedding_fingerprint IS NULL OR embedding_fingerprint != ?)", (self.fingerprint,)).fetchone()[0]) if self.fingerprint else 0
        n_missing = int(c.execute("SELECT COUNT(*) FROM documents WHERE extract_status='missing'").fetchone()[0])
        try:
            size = os.path.getsize(self.path) + (os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0)
        except OSError:
            size = None
        return {
            "documents": n_docs, "chunks": n_chunks, "vectors": n_vec, "documents_awaiting_embedding": n_stale, "documents_missing": n_missing,
            "by_extract_status": by_status, "by_extension": by_ext, "by_extract_method": by_method,
            "db_bytes": size, "embedding_fingerprint": self.fingerprint, "dim": self.dim,
            "vector_cache": None if self.cache is None else len(self.cache), "queue": self.queue_stats(),
            "errors": int(c.execute("SELECT COUNT(*) FROM errors").fetchone()[0]),
            "sqlite_vec": getattr(self, "vec_version", None),
        }

    def wipe(self) -> None:
        """Delete all documents/chunks/vectors/jobs (keeps schema and model binding)."""
        with self.lock:
            self.version += 1
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM documents")
            if self._vec_table_exists():
                self.conn.execute("DELETE FROM vec_chunks")
            self.conn.execute("DELETE FROM jobs")
            self.conn.execute("DELETE FROM errors")
            for k in [r[0] for r in self.conn.execute("SELECT key FROM meta WHERE key LIKE 'checkpoint:%'")]:
                self.conn.execute("DELETE FROM meta WHERE key=?", (k,))
            if self.cache is not None:
                self.cache = VectorCache(self.dim or 0)
