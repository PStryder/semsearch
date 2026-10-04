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
SCHEMA_VERSION = 2  # v2: documents.missing_since/title, jobs.dirty (additive; migrated in _migrate)

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
    """In-memory mirror of vec_chunks for fast brute-force cosine search.

    Storage is a pre-allocated buffer that doubles when full, so appending a document is
    amortized O(rows added), not O(total). Reads and writes are serialized by a lock; the
    matrix product itself runs on a view of the live rows."""

    def __init__(self, dim: int, capacity: int = 4096, dtype: str = "float32"):
        self.dim = dim
        self.n = 0
        self.dtype = np.float16 if dtype == "float16" else np.float32  # float16: half the RAM, slower matmul
        self.ids = np.zeros((capacity,), dtype=np.int64)
        self.mat = np.zeros((capacity, dim), dtype=self.dtype)
        self.deleted = np.zeros((capacity,), dtype=bool)
        self.pos: dict[int, int] = {}
        self.n_deleted = 0
        self._lock = threading.Lock()

    def _ensure(self, extra: int) -> None:
        need = self.n + extra
        if need <= len(self.ids):
            return
        cap = max(need, len(self.ids) * 2, 4096)
        ids = np.zeros((cap,), dtype=np.int64)
        mat = np.zeros((cap, self.dim), dtype=self.dtype)
        deleted = np.zeros((cap,), dtype=bool)
        ids[: self.n] = self.ids[: self.n]
        mat[: self.n] = self.mat[: self.n]
        deleted[: self.n] = self.deleted[: self.n]
        self.ids, self.mat, self.deleted = ids, mat, deleted

    def load(self, rows: Iterable[tuple[int, bytes]]) -> None:
        ids, vecs = [], []
        for cid, blob in rows:
            ids.append(cid)
            vecs.append(blob_to_vec(blob))
        with self._lock:
            self.n = 0
            self.pos = {}
            self.n_deleted = 0
            self.ids = np.zeros((0,), dtype=np.int64)
            self.mat = np.zeros((0, self.dim), dtype=self.dtype)
            self.deleted = np.zeros((0,), dtype=bool)
            self._ensure(len(ids))
            if ids:
                self.ids[: len(ids)] = np.array(ids, dtype=np.int64)
                self.mat[: len(ids)] = np.vstack(vecs).astype(self.dtype)
                self.n = len(ids)
                self.pos = {int(c): i for i, c in enumerate(ids)}

    def add(self, ids: Sequence[int], vecs: np.ndarray) -> None:
        if len(ids) == 0:
            return
        with self._lock:
            self._ensure(len(ids))
            base = self.n
            self.ids[base: base + len(ids)] = np.asarray(ids, dtype=np.int64)
            self.mat[base: base + len(ids)] = np.asarray(vecs, dtype=self.dtype)
            self.deleted[base: base + len(ids)] = False
            for i, c in enumerate(ids):
                self.pos[int(c)] = base + i
            self.n = base + len(ids)

    def remove(self, ids: Iterable[int]) -> None:
        with self._lock:
            for c in ids:
                i = self.pos.pop(int(c), None)
                if i is not None and not self.deleted[i]:
                    self.deleted[i] = True
                    self.n_deleted += 1
            if self.n_deleted > 1000 and self.n_deleted > self.n // 5:
                self._compact()

    def compact(self) -> None:
        with self._lock:
            self._compact()

    def _compact(self) -> None:
        keep = ~self.deleted[: self.n]
        live_ids = self.ids[: self.n][keep]
        live_mat = self.mat[: self.n][keep]
        self.n = len(live_ids)
        self.ids[: self.n] = live_ids
        self.mat[: self.n] = live_mat
        self.deleted[:] = False
        self.pos = {int(c): i for i, c in enumerate(live_ids)}
        self.n_deleted = 0

    def __len__(self) -> int:
        return self.n - self.n_deleted

    def search(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        with self._lock:
            n = self.n
            if n == 0:
                return []
            sims = (self.mat[:n] @ np.asarray(q, dtype=self.dtype)).astype(np.float32)
            if self.n_deleted:
                sims = np.where(self.deleted[:n], -2.0, sims)
            k = min(k, n)
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx])]
            return [(int(self.ids[i]), float(sims[i])) for i in idx if not self.deleted[i]]


class _Transaction:
    """BEGIN/COMMIT with deferred side effects: the vector cache and the version counter are
    updated only after a successful COMMIT; a rollback leaves both untouched."""

    def __init__(self, store):
        self.store = store
        self.removed: list[int] = []
        self.added: list[tuple[list[int], np.ndarray]] = []

    def __enter__(self):
        self.store.conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.store.conn.execute("ROLLBACK")
            return False
        self.store.conn.execute("COMMIT")
        cache = self.store.cache
        if cache is not None:
            if self.removed:
                cache.remove(self.removed)
            for ids, vecs in self.added:
                if len(ids):
                    cache.add(ids, vecs)
        self.store.version += 1
        return False


class StoreCorrupt(Exception):
    """The database file failed to open or its quick_check reported damage."""


class StoreIncompatible(Exception):
    """The database schema is newer than this build understands."""


class Store:
    def __init__(self, path: str | os.PathLike, vector_cache: bool = True, integrity_check: str = "quick", integrity_check_max_mb: int = 4096,
                 cache_dtype: str = "float32"):
        self.path = str(path)
        self.cache_dtype = cache_dtype
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self._tls = threading.local()
        self._readers: list[sqlite3.Connection] = []
        existed = os.path.exists(self.path)
        # one writer connection (serialized by self.lock) + one read-only connection per thread:
        # WAL readers see a consistent committed snapshot and never observe a half-written transaction
        try:
            self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            if not existed:
                self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")  # must precede table creation; lets vacuum() reclaim in steps
            self.conn.execute("PRAGMA journal_mode=WAL")      # crash-consistent: committed transactions survive power loss
            self.conn.execute("PRAGMA synchronous=NORMAL")    # WAL + NORMAL: durable at checkpoint, no torn pages
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.execute("PRAGMA cache_size=-65536")
            if existed and integrity_check == "quick":
                size_mb = os.path.getsize(self.path) / (1024 * 1024)
                if size_mb <= integrity_check_max_mb:
                    t0 = time.perf_counter()
                    res = self.conn.execute("PRAGMA quick_check").fetchall()
                    verdict = res[0][0] if res else "no result"
                    log.info("quick_check on %s (%.0f MB): %s in %.1fs", self.path, size_mb, verdict, time.perf_counter() - t0)
                    if verdict != "ok":
                        raise StoreCorrupt(f"quick_check: {verdict}")
                else:
                    log.info("skipping quick_check: database is %.0f MB (> %d MB)", size_mb, integrity_check_max_mb)
            self._load_vec()
            self.conn.executescript(_SCHEMA)
            self._migrate()
        except sqlite3.DatabaseError as e:
            try:
                self.conn.close()  # release the file so the caller can quarantine it
            except Exception:  # noqa: BLE001
                pass
            raise StoreCorrupt(str(e)) from e
        have = self.get_meta("schema_version")
        if have is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(have) > SCHEMA_VERSION:
            self.conn.close()
            raise StoreIncompatible(f"index schema v{have} is newer than this build supports (v{SCHEMA_VERSION}); upgrade semsearch or restore a matching index")
        elif int(have) < SCHEMA_VERSION:
            log.info("migrating index schema v%s -> v%d", have, SCHEMA_VERSION)
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.dim: int | None = int(self.get_meta("dim")) if self.get_meta("dim") else None
        self.fingerprint: str | None = self.get_meta("embedding_fingerprint")
        self.use_cache = vector_cache
        self.cache: VectorCache | None = None
        self.version = 0  # bumped on every content mutation; lets callers cache query results safely
        if self.dim and self._vec_table_exists():
            self._init_cache()

    # ---- setup ----
    @staticmethod
    def _load_vec_into(conn: sqlite3.Connection) -> None:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    def _load_vec(self) -> None:
        self._load_vec_into(self.conn)
        self.vec_version = self.conn.execute("select vec_version()").fetchone()[0]

    def _r(self) -> sqlite3.Connection:
        """Per-thread read-only connection. Writers' uncommitted transactions are invisible to
        it, so a search never observes a document between 'old vectors deleted' and 'new
        vectors written'. Methods that read inside their own transaction use self.conn."""
        c = getattr(self._tls, "rconn", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=ON")
            self._load_vec_into(c)
            self._tls.rconn = c
            with self.lock:
                self._readers.append(c)
        return c

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (CREATE IF NOT EXISTS skips them)."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(documents)")}
        for col, decl in (("missing_since", "REAL"), ("title", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")
        have_jobs = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "dirty" not in have_jobs:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0")

    def _vec_table_exists(self) -> bool:
        r = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'").fetchone()
        return r is not None

    def _init_cache(self) -> None:
        if not self.use_cache or not self.dim:
            self.cache = None
            return
        t0 = time.perf_counter()
        self.cache = VectorCache(self.dim, dtype=self.cache_dtype)
        rows = self.conn.execute("SELECT rowid, embedding FROM vec_chunks").fetchall()
        self.cache.load(((int(r[0]), r[1]) for r in rows))
        log.info("vector cache loaded: %d vectors in %.2fs", len(self.cache), time.perf_counter() - t0)

    def close(self) -> None:
        """Close every connection (reader connections too) so the last close checkpoints and
        removes the WAL; a lingering reader would keep stale WAL pages masking the main file."""
        with self.lock:
            for c in self._readers:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._readers.clear()
            self._tls = threading.local()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001
                pass
            self.conn.close()

    # ---- meta ----
    def get_meta(self, key: str) -> str | None:
        r = self._r().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
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
        return self._r().execute("SELECT * FROM documents WHERE path=?", (path_norm,)).fetchone()

    def get_document_by_id(self, doc_id: int) -> sqlite3.Row | None:
        return self._r().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def get_documents(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(ids)
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            q = f"SELECT * FROM documents WHERE id IN ({','.join('?' * len(part))})"
            for r in self._r().execute(q, part):
                out[int(r["id"])] = r
        return out

    def find_by_file_id(self, volume_serial: int | str, file_id: int | str) -> list[sqlite3.Row]:
        # NTFS/ReFS file ids can exceed a signed 64-bit integer, so they are stored as text
        return self._r().execute("SELECT * FROM documents WHERE volume_serial=? AND file_id=?", (str(volume_serial), str(file_id))).fetchall()

    # Consistency rules for every mutation below:
    #   * SQL runs inside one transaction;
    #   * the in-memory vector cache is touched ONLY after COMMIT (a rollback must leave it
    #     exactly as SQLite is);
    #   * self.version is bumped ONLY after COMMIT, so a search that ran against the
    #     pre-commit snapshot is cached under the old version and dies with the commit.
    def _tx(self):
        return _Transaction(self)

    def upsert_document(self, **f: Any) -> int:
        cols = list(f.keys())
        with self.lock:
            sql = (f"INSERT INTO documents({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                   f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'path')}")
            self.conn.execute(sql, [f[c] for c in cols])
            doc_id = int(self.conn.execute("SELECT id FROM documents WHERE path=?", (f["path"],)).fetchone()[0])
            self.version += 1
            return doc_id

    def update_document(self, doc_id: int, **f: Any) -> None:
        if not f:
            return
        with self.lock:
            self.conn.execute(f"UPDATE documents SET {','.join(f'{c}=?' for c in f)} WHERE id=?", [*f.values(), doc_id])
            if set(f) - {"last_seen"}:
                self.version += 1

    def remove_document(self, path_norm: str) -> bool:
        with self.lock:
            r = self.conn.execute("SELECT id FROM documents WHERE path=?", (path_norm,)).fetchone()
            if not r:
                return False
            self.remove_document_id(int(r[0]))
            return True

    def remove_document_id(self, doc_id: int) -> None:
        with self.lock, self._tx() as tx:
            tx.removed += self._delete_doc_vectors(doc_id)
            self.conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def tombstone_document(self, doc_id: int) -> None:
        """Mark a document as vanished. Chunks and vectors are kept for a grace period so that a
        rename/move indexed shortly afterwards can reclaim them via the NTFS file id."""
        with self.lock:
            self.conn.execute("UPDATE documents SET extract_status='missing', missing_since=? WHERE id=? AND (extract_status != 'missing' OR missing_since IS NULL)",
                              (time.time(), doc_id))
            self.version += 1

    def restore_document(self, doc_id: int, **f: Any) -> None:
        f = dict(f)
        f["missing_since"] = None
        f["extract_status"] = "ok"
        self.update_document(doc_id, **f)

    def quarantine_document(self, doc_id: int, status: str, error: str | None) -> None:
        """Drop a document's text and vectors and record why (policy change on unchanged files)."""
        with self.lock, self._tx() as tx:
            tx.removed += self._delete_doc_vectors(doc_id)
            self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self.conn.execute("UPDATE documents SET extract_status=?, extract_error=?, text_chars=0, n_chunks=0, embedding_fingerprint=NULL WHERE id=?",
                              (status, error, doc_id))

    def purge_missing(self, older_than_s: float) -> int:
        cutoff = time.time() - older_than_s
        with self.lock:
            ids = [int(r[0]) for r in self.conn.execute("SELECT id FROM documents WHERE extract_status='missing' AND missing_since <= ?", (cutoff,))]
            if not ids:
                return 0
            with self._tx() as tx:
                for i in ids:
                    tx.removed += self._delete_doc_vectors(i)
                    self.conn.execute("DELETE FROM documents WHERE id=?", (i,))
        return len(ids)

    def iter_paths(self, root_norm: str | None = None) -> Iterator[tuple[int, str, float | None]]:
        if root_norm:
            cur = self._r().execute("SELECT id, path, last_seen FROM documents WHERE root=?", (root_norm,))
        else:
            cur = self._r().execute("SELECT id, path, last_seen FROM documents")
        for r in cur:
            yield int(r[0]), r[1], r[2]

    def iter_documents_with_text(self):
        """(doc_id, display_path, concatenated chunk text) for every document that has chunks."""
        for r in self._r().execute("SELECT id, display_path FROM documents WHERE n_chunks > 0 AND extract_status='ok'").fetchall():
            parts = [c[0] for c in self._r().execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY ordinal", (int(r[0]),))]
            yield int(r[0]), r[1], "\n".join(parts)

    def documents_needing_embedding(self, fingerprint: str, limit: int = 1000, after_id: int = 0) -> list[sqlite3.Row]:
        return self._r().execute(
            "SELECT id, path, display_path FROM documents WHERE id > ? AND n_chunks > 0 AND extract_status='ok' "
            "AND (embedding_fingerprint IS NULL OR embedding_fingerprint != ?) ORDER BY id LIMIT ?",
            (int(after_id), fingerprint, limit)).fetchall()

    def paths_with_prefix(self, prefix_norm: str) -> list[tuple[int, str]]:
        """Documents whose normalized path starts with prefix (a directory, with trailing backslash).
        Uses the unique index on path: a range scan, not a table scan."""
        p = prefix_norm if prefix_norm.endswith("\\") else prefix_norm + "\\"
        rows = self._r().execute("SELECT id, path FROM documents WHERE path >= ? AND path < ?", (p, p + "￿")).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def count_documents(self, root_norm: str | None = None) -> int:
        if root_norm is None:
            return int(self._r().execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        return int(self._r().execute("SELECT COUNT(*) FROM documents WHERE root=? AND extract_status != 'missing'", (root_norm,)).fetchone()[0])

    # ---- chunks & vectors ----
    def _delete_doc_vectors(self, doc_id: int) -> list[int]:
        """SQL only (inside the caller's transaction); returns the chunk ids whose vectors were
        deleted so the caller can drop them from the cache after commit."""
        ids = [int(r[0]) for r in self.conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        if ids and self._vec_table_exists():
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                self.conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({','.join('?' * len(part))})", part)
        return ids

    def chunks_for_doc(self, doc_id: int) -> list[sqlite3.Row]:
        return self._r().execute("SELECT * FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc_id,)).fetchall()

    def get_chunks(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(ids)
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self._r().execute(f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
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
            rows = self._r().execute(
                f"SELECT c.text_hash, v.embedding FROM chunks c JOIN vec_chunks v ON v.rowid = c.id "
                f"WHERE c.text_hash IN ({','.join('?' * len(part))})", part).fetchall()
            for h, blob in rows:
                if h not in out:
                    out[h] = blob_to_vec(blob)
        return out

    def _replace_chunks_inner(self, tx, doc_id: int, chunks: Sequence[Chunk], vectors: np.ndarray | None, fingerprint: str | None) -> list[int]:
        """Inside an open transaction on self.conn: swap a document's chunks and vectors."""
        tx.removed += self._delete_doc_vectors(doc_id)
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
            tx.added.append((ids, vectors))
        self.conn.execute("UPDATE documents SET n_chunks=?, embedding_fingerprint=? WHERE id=?",
                          (len(ids), fingerprint if vectors is not None else None, doc_id))
        return ids

    def replace_chunks(self, doc_id: int, chunks: Sequence[Chunk], vectors: np.ndarray | None, fingerprint: str | None) -> list[int]:
        """Atomically replace a document's chunks (and vectors). Returns new chunk ids."""
        with self.lock, self._tx() as tx:
            return self._replace_chunks_inner(tx, doc_id, chunks, vectors, fingerprint)

    def write_document(self, fields: dict[str, Any], chunks: Sequence[Chunk], vectors: np.ndarray | None, fingerprint: str | None) -> int:
        """Document row + chunks + vectors in ONE transaction. Called only after extraction and
        embedding have succeeded, so a failure anywhere leaves the previous version of the
        document fully intact and searchable, and a retry sees the old stat/hash."""
        cols = list(fields.keys())
        with self.lock, self._tx() as tx:
            sql = (f"INSERT INTO documents({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                   f"ON CONFLICT(path) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'path')}")
            self.conn.execute(sql, [fields[c] for c in cols])
            doc_id = int(self.conn.execute("SELECT id FROM documents WHERE path=?", (fields["path"],)).fetchone()[0])
            self._replace_chunks_inner(tx, doc_id, chunks, vectors, fingerprint)
            return doc_id

    def set_vectors(self, chunk_ids: Sequence[int], vectors: np.ndarray, doc_id: int, fingerprint: str,
                    text_hashes: Sequence[str] | None = None) -> None:
        """Replace a document's vectors (re-embed). `text_hashes`, when given, rewrites the
        chunks' reuse keys in the same transaction so they match the new embedding input."""
        with self.lock, self._tx() as tx:
            ids = [int(c) for c in chunk_ids]
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                self.conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({','.join('?' * len(part))})", part)
            for cid, v in zip(ids, vectors):
                self.conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)", (cid, vec_to_blob(v)))  # vec0 rejects OR REPLACE
            if text_hashes is not None:
                assert len(text_hashes) == len(ids)
                self.conn.executemany("UPDATE chunks SET text_hash=? WHERE id=?", list(zip(text_hashes, ids)))
            self.conn.execute("UPDATE documents SET embedding_fingerprint=? WHERE id=?", (fingerprint, doc_id))
            tx.removed += ids
            tx.added.append((ids, vectors))

    # ---- search primitives ----
    def knn(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        """Return [(chunk_id, cosine_similarity)] best first."""
        if self.cache is not None:
            return self.cache.search(q, k)
        if not self._vec_table_exists():
            return []
        rows = self._r().execute("SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                                 (vec_to_blob(q), int(k))).fetchall()
        return [(int(r[0]), 1.0 - float(r[1])) for r in rows]

    @staticmethod
    def _like_escape(s: str) -> str:
        # SQLite LIKE has no bracket escapes; use ESCAPE so '_' and '%' in the needle are literal
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _doc_filter_sql(self, roots: Sequence[str] | None, extensions: Sequence[str] | None, alias: str = "d") -> tuple[str, list]:
        """WHERE fragments restricting documents to normalized root prefixes / extensions, so a
        filtered search ranks within the filter instead of filtering a global top-k."""
        where: list[str] = []
        params: list = []
        if roots:
            where.append("(" + " OR ".join(f"({alias}.path = ? OR {alias}.path LIKE ? ESCAPE '\\')" for _ in roots) + ")")
            for r in roots:
                # escape the prefix INCLUDING its trailing separator, then the bare wildcard
                params += [r, self._like_escape(r.rstrip("\\") + "\\") + "%"]
        if extensions:
            exts = list(extensions)
            where.append(f"{alias}.extension IN ({','.join('?' * len(exts))})")
            params += exts
        return (" AND " + " AND ".join(where)) if where else "", params

    def fts(self, match_expr: str, limit: int, roots: Sequence[str] | None = None, extensions: Sequence[str] | None = None) -> list[tuple[int, int, float]]:
        """Return [(chunk_id, doc_id, bm25_score)] where higher score is better. Optional root /
        extension filters are applied inside the query (the top-k is taken within the filter)."""
        flt, params = self._doc_filter_sql(roots, extensions)
        join = " JOIN documents d ON d.id = c.doc_id" if flt else ""
        rows = self._r().execute(
            f"SELECT f.rowid, c.doc_id, bm25(chunks_fts) AS r FROM chunks_fts f JOIN chunks c ON c.id = f.rowid{join} "
            f"WHERE chunks_fts MATCH ?{flt} ORDER BY r LIMIT ?", [match_expr, *params, int(limit)]).fetchall()
        return [(int(r[0]), int(r[1]), -float(r[2])) for r in rows]

    def filename_like(self, needle: str, limit: int = 200, roots: Sequence[str] | None = None, extensions: Sequence[str] | None = None) -> list[tuple[int, str]]:
        flt, params = self._doc_filter_sql(roots, extensions)
        rows = self._r().execute(f"SELECT d.id, d.filename FROM documents d WHERE d.filename LIKE ? ESCAPE '\\'{flt} LIMIT ?",
                                 ["%" + self._like_escape(needle) + "%", *params, int(limit)]).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def filename_glob(self, pattern: str, limit: int = 200) -> list[tuple[int, str]]:
        rows = self._r().execute("SELECT id, filename FROM documents WHERE filename GLOB ? LIMIT ?", (pattern, int(limit))).fetchall()
        if not rows and pattern != pattern.lower():
            rows = self._r().execute("SELECT id, filename FROM documents WHERE lower(filename) GLOB ? LIMIT ?", (pattern.lower(), int(limit))).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    # ---- job queue ----
    # A job that is re-enqueued while 'running' is marked dirty; complete_job then returns it to
    # 'pending' instead of deleting it, so a change that lands mid-index is not lost.
    _ENQUEUE_SQL = (
        "INSERT INTO jobs(path, op, priority, state, attempts, enqueued_at, updated_at, dirty) VALUES(?,?,?,'pending',0,?,?,0) "
        "ON CONFLICT(path) DO UPDATE SET op=excluded.op, priority=min(jobs.priority, excluded.priority), "
        "state=CASE WHEN jobs.state='running' THEN 'running' ELSE 'pending' END, "
        "dirty=CASE WHEN jobs.state='running' THEN 1 ELSE 0 END, attempts=0, error=NULL, updated_at=excluded.updated_at")

    def enqueue(self, path_norm: str, op: str, priority: int = 5) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(self._ENQUEUE_SQL, (path_norm, op, priority, now, now))

    def enqueue_many(self, items: Iterable[tuple[str, str, int]]) -> int:
        now = time.time()
        n = 0
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                for path_norm, op, priority in items:
                    self.conn.execute(self._ENQUEUE_SQL, (path_norm, op, priority, now, now))
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
            # re-enqueued while running -> run it again; otherwise done
            self.conn.execute("UPDATE jobs SET state='pending', dirty=0, attempts=0, updated_at=? WHERE id=? AND state='running' AND dirty=1", (time.time(), job_id))
            self.conn.execute("DELETE FROM jobs WHERE id=? AND state='running'", (job_id,))

    def fail_job(self, job_id: int, error: str, max_attempts: int) -> None:
        with self.lock:
            r = self.conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r:
                return
            state = "failed" if r[0] >= max_attempts else "pending"
            self.conn.execute("UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?", (state, error[:1000], time.time(), job_id))

    def failed_jobs(self, limit: int = 50) -> list[dict]:
        rows = self._r().execute("SELECT path, op, attempts, error, updated_at FROM jobs WHERE state='failed' ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

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

    def prune_pending_jobs(self, reject, should_stop=None) -> int:
        """Delete pending jobs whose path `reject(path)` says is out of scope now (new exclusion,
        removed root). Scans in batches so a queue of hundreds of thousands stays cheap, and
        stops between batches when `should_stop()` says so (shutdown)."""
        removed = 0
        last = 0
        while not (should_stop and should_stop()):
            rows = self._r().execute("SELECT id, path FROM jobs WHERE state='pending' AND id > ? ORDER BY id LIMIT 5000", (last,)).fetchall()
            if not rows:
                break
            last = int(rows[-1][0])
            doomed = [int(r[0]) for r in rows if reject(r[1])]
            if doomed:
                with self.lock:
                    for i in range(0, len(doomed), 500):
                        part = doomed[i:i + 500]
                        self.conn.execute(f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(part))}) AND state='pending'", part)
                removed += len(doomed)
        return removed

    def queue_stats(self) -> dict[str, int]:
        rows = self._r().execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
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
        rows = self._r().execute("SELECT path, stage, message, at FROM errors ORDER BY at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        c = self._r()
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

    # ---- housekeeping ----
    def vacuum(self, incremental_pages: int = 20000) -> dict[str, Any]:
        """Return free pages to the OS. Databases created with auto_vacuum=INCREMENTAL release
        pages in bounded steps; older ones get a one-time full VACUUM (which also enables
        incremental mode) when the caller decides the service is idle."""
        with self.lock:
            mode = int(self.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
            free_before = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
            t0 = time.perf_counter()
            if mode == 2:
                self.conn.execute(f"PRAGMA incremental_vacuum({int(incremental_pages)})")
                kind = "incremental"
            else:
                self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self.conn.execute("VACUUM")
                kind = "full"
            free_after = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        self.set_meta("last_vacuum_at", str(time.time()))
        return {"kind": kind, "free_pages_before": free_before, "free_pages_after": free_after, "seconds": round(time.perf_counter() - t0, 2)}

    def backup(self, dest: str | os.PathLike) -> dict[str, Any]:
        """Consistent online copy of the index (SQLite backup API; safe while the service writes)."""
        dest = str(dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        t0 = time.perf_counter()
        out = sqlite3.connect(dest)
        try:
            with self.lock:
                self.conn.backup(out, pages=4096)
        finally:
            out.close()
        return {"path": dest, "bytes": os.path.getsize(dest), "seconds": round(time.perf_counter() - t0, 2)}

    def invalidate_content(self) -> int:
        """Force re-extraction of every present document: clear the stored hash and mtime so the
        indexer's unchanged/touched shortcuts miss, and queue an index job per document. The
        old chunks and vectors stay searchable until each document is rewritten."""
        with self.lock, self._tx():
            rows = self.conn.execute("SELECT path FROM documents WHERE extract_status != 'missing'").fetchall()
            self.conn.execute("UPDATE documents SET content_hash='', mtime=NULL WHERE extract_status != 'missing'")
        return self.enqueue_many((r[0], "index", 5) for r in rows)

    def vector_count(self) -> int:
        if self.cache is not None:
            return len(self.cache)
        if not self._vec_table_exists():
            return 0
        return int(self._r().execute("SELECT count(*) FROM vec_chunks").fetchone()[0])

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
