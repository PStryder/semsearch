# Architecture

semsearch is a local sidecar that adds meaning-based retrieval to files on a Windows
workstation. It leans on the Windows Search indexer for what that service does well
(inventory, metadata, change timestamps, exclusion rules) and owns everything the indexer
cannot provide (reliable text extraction, chunking, embeddings, vector retrieval, hybrid
ranking, an explainable API).

```
Windows Search adapter  ──┐
                          ├─►  document inventory / change detection
filesystem walk + watcher ┘               │
                                          ▼
                                   text extraction  (text / pypdf / IFilter / Office fallbacks)
                                          │
                                          ▼
                                       chunking
                                          │
                                          ▼
                                 embedding provider  (ONNX bge-small by default)
                                          │
                                          ▼
                     SQLite store: documents, chunks, FTS5, sqlite-vec vectors, jobs
                                          │
                                          ▼
                             retrieval + hybrid ranker  ──►  HTTP API  /  CLI
```

Every box is a module with an interface; the Windows-specific pieces (`inventory/windows_search.py`,
`inventory/catalog.py`, `extract/ifilter.py`, `watcher.py`) sit behind protocols so the
semantic subsystem is testable with a deterministic embedding provider on any platform.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Pydantic config model, YAML loading, defaults (`semsearch --init-config`) |
| `security.py` | Path normalization, root containment, reparse-point (junction/symlink) refusal, exclusion globs, safe directory walk |
| `inventory/windows_search.py` | OLE DB queries against `SystemIndex`: deep enumeration with keyset paging, `GatherTime` deltas, point lookups, `FREETEXT` relevance, filename `LIKE` |
| `inventory/catalog.py` | Read-only COM access to `ISearchCatalogManager` for `/status` (catalog state, item counts, URL being indexed) |
| `inventory/filesystem.py` | `os.scandir` crawl that never follows reparse points or leaves the roots |
| `watcher.py` | `ReadDirectoryChangesW` per root; coalesced add/modify/delete/rename events |
| `extract/` | `ExtractorRegistry` with an ordered chain per extension; `TextExtractor` (encoding detection, binary detection), `PdfExtractor` (pypdf), `IFilterExtractor` (Windows filters via `LoadIFilter` or `IInitializeWithStream`), Office fallbacks, `WindowsOcrPdfExtractor` (image-only PDFs via Windows.Data.Pdf + Windows.Media.Ocr, optional), `IsolatedExtractor` (child process + timeout for native code) |
| `gpu_monitor.py` | GPU courtesy: per-adapter utilization by other processes from the `GPU Engine` performance counters |
| `chunking.py` | Heading/paragraph/definition-aware packing with overlap and character offsets |
| `embed/` | `EmbeddingProvider` protocol; `OnnxProvider` (default), `SentenceTransformersProvider` (optional), `HashingProvider` (tests) |
| `store/db.py` | SQLite schema, FTS5 external-content index with triggers, `vec0` vector table, in-memory vector cache, job queue, error log, model binding |
| `indexer.py` | Full build, incremental pass, reconcile, re-embed on model change, job worker and scheduler threads, rename/move detection, tombstones |
| `retrieval.py` | Literal / semantic / hybrid search with component scores, excerpts and `why` strings |
| `api.py` | FastAPI app bound to 127.0.0.1 |
| `cli.py` | `semsearch` command (HTTP client, or `--local` in-process) |
| `app_state.py` | Wiring |

## Identity and change model

Three identities are tracked per document, each used for one purpose:

| Identity | Source | Used for |
|---|---|---|
| normalized path (`documents.path`) | `security.normalize_path` | primary key; what the user sees and searches |
| NTFS `(volume_serial, file_id)` | `os.lstat` | recognizing a rename or move so vectors are re-pointed, not recomputed |
| content hash (BLAKE2b-160 of the bytes) | streamed read | deciding whether extraction and embedding must run again |

`System.Search.EntryID` from Windows Search is recorded but never used as identity because
the indexer issues a new ID on rename (measured, see the findings note).

Per-file decision ladder in `Indexer._index_file`:

1. containment, reparse-point and size policy (`security.check_indexable`)
2. same size and mtime as stored, and nothing left to do → `unchanged` (no I/O beyond `lstat`)
3. hash the bytes; same hash and same model → `touched` (metadata only)
4. unknown path, but a tombstoned or vanished row with the same NTFS id and hash → `moved` (row re-pointed, chunks and vectors kept)
5. extract → chunk → prepend the document title to each chunk for embedding → look up existing vectors by embed-text hash → embed only the new chunk texts → write document row, chunks and vectors in **one transaction, only after embedding succeeded**; a failure anywhere leaves the previous version intact and a retry re-extracts because the stored stat/hash is still the old one

The **title header** is the first Markdown heading near the top of the text, or a humanized
filename (with the parent folder for generic names like `README`). It is stored in
`documents.title`, prepended to each chunk's text before embedding (never shown in excerpts
and not part of the FTS index), and reproduced when vectors are rebuilt after a model change.
It gives deep chunks the document's subject and lets a semantic query reach a file whose body
never repeats its own title; it raised vague-query top-1 in the evaluation.

Deletions discovered automatically (reconcile, watcher, vanished at index time) become
**tombstones** (`extract_status='missing'`): the row, chunks and vectors stay for a grace
period (`max(600 s, reconcile_interval_s)`) and are purged on a later reconcile. This is what
lets a rename that is noticed after its old path has disappeared still reuse its vectors.
Explicit removal through the API or CLI deletes immediately.

### Change detection

| Signal | Latency | Covers | Notes |
|---|---|---|---|
| Watcher (`ReadDirectoryChangesW`) | ~1.5 s settle | add, modify, delete, rename | Buffer overflow falls back to the next incremental/reconcile |
| Windows Search `GatherTime >= checkpoint` | 1 to 2 s indexer lag + poll interval | add, modify, rename-as-new-path | Only for roots Windows indexes; checkpoint stored per root |
| Filesystem mtime scan | poll interval | add, modify | For roots Windows does not index |
| Reconcile (full enumeration diff) | `reconcile_interval_s` | delete, add, modify (anything the others missed) | Enumeration is Windows Search plus a filesystem walk, unioned; each enumerated file is compared against a fresh `os.stat` (never the inventory's possibly stale metadata) and enqueued when the store lacks it or holds a different size/mtime; tombstoning is skipped when the enumeration returns fewer than `reconcile_min_fraction` of the known files |

A job that is re-enqueued while that same path is being processed is flagged dirty; when the
running job completes it goes back to pending instead of being deleted, so a save that lands
mid-index is not lost.

Full builds use the same union: Windows Search first (fast, includes metadata and respects the
user's indexer exclusions), then a filesystem walk to pick up anything the indexer has not
gathered yet.

## Storage

One SQLite file (`<data_dir>/semsearch.db`, WAL mode):

- `documents` — identity, stat, hash, extraction status/method/error, chunk count, embedding fingerprint, tombstone timestamp
- `chunks` — text with character offsets and a text hash; `chunks_fts` is an FTS5
  external-content index kept in sync by triggers (porter stemming, unicode61)
- `vec_chunks` — sqlite-vec `vec0` table, cosine metric, one row per chunk id. Created with the
  embedding dimension and dropped/recreated when the model changes
- `jobs` — persistent work queue (`index`, `remove`, `vanish`, `reembed`) with priority, attempts, state; survives restarts
- `errors` — capped diagnostic log surfaced through `/errors`
- `meta` — schema version, embedding fingerprint and dimension, per-root checkpoints, last full build

An in-memory float32 matrix mirrors `vec_chunks` (`retrieval.vector_cache: true`) so a query is
one matrix-vector product; sqlite-vec remains the persistent store and the fallback. Every
mutation runs in one SQLite transaction, and both the matrix and the store's version counter
(which keys the response cache) are updated only after COMMIT: a rollback leaves the cache
exactly as SQLite is, and a search that read the pre-commit snapshot is cached under the old
version and dies with the commit.
At 384 dimensions the cache costs about 1.5 KB per chunk (≈ 750 MB per 500k chunks).

## Embedding devices

The ONNX provider keeps one weight file and up to three runtime sessions: a steady-state
document device, a bulk device the indexer switches to during full builds or when more than
`bulk_threshold` jobs are pending, and a query device. The fingerprint excludes the device, and
vectors from CPU, DirectML and CUDA sessions agree to ~1e-7, so they are freely mixed in one
index. On this workstation the measured policy is: integrated Radeon for steady state (no CPU
load), RTX 4080 for bulk, CPU for queries (docs/configuration.md "Devices").

## Embedding model lifecycle

The provider's `fingerprint` (`provider:model:revision:dim:pooling`) is written into `meta`.
On startup `Store.ensure_vectors` compares it with the configured provider:

- same → nothing happens
- different and `embedding.on_model_change: reembed` → `vec_chunks` is dropped and recreated
  with the new dimension, every document's `embedding_fingerprint` is cleared, and the
  scheduler enqueues `reembed` jobs that recompute vectors from the stored chunk text (no
  re-extraction). Semantic search returns nothing for a document until its vectors exist
- different and `refuse` → startup fails with a clear message

## Retrieval

- **literal**: FTS5 BM25 over chunk text (AND of terms, falling back to OR), filename
  substring matches, Windows `FREETEXT` rank for documents that have content in `SystemIndex`.
  A query containing `*` or `?` and no spaces is a filename glob.
- **semantic**: query embedding (with the model's query prefix) → top-k chunks from the vector
  cache → best chunk per document, cosine similarity as the score.
- When a root or extension filter is given, the candidate pools are collected eight times
  deeper (capped), because filtering happens after collection and a narrow filter could
  otherwise be starved by the global top-k.
- **hybrid** (default): both lists; final score is a convex combination of min-max normalized
  component scores (`semantic_weight`, `lexical_weight`), with a small filename bonus.
  Reciprocal rank fusion is available (`retrieval.fusion: rrf`) and the RRF value is reported
  either way.

Every hit returns `scores` (semantic, lexical, filename, windows_rank, both ranks, rrf), the
chunk ordinal and an excerpt windowed around the first matching term, and `why` strings.
A filename that contains every query term scores at least 0.9 in hybrid mode, on the grounds
that the user remembered the name.

Repeated identical queries are answered from a small in-process cache keyed by the query, mode,
limit, filters and the store's mutation counter (`Store.version`), so any index change
invalidates it; cached responses carry `"cached": true`.

## Security posture

**Trust boundary, stated plainly.** The service reads files as *its own* account (the
`NT SERVICE\SemSearch` virtual account granted read on each root, or whatever identity the
installer was told to use) and stores the extracted text in its index. The API answers any
process on the loopback interface; callers are not impersonated and their NTFS rights are not
checked against the source file. So: everything the service account can read, any local
account can search, and a later ACL change on a file does not revoke what the index already
holds. On a single-user workstation this is the intended design. On a machine with several
interactive accounts, set `api.read_token: true` (reads then need the token file that only the
operator can read) or give each user their own instance with their own data directory. The
Windows Search security trimming described in docs/windows-service.md bounds what the service
*sees*, not who may *ask*.

- API binds 127.0.0.1; a non-loopback bind requires `api.allow_non_loopback: true`
- Every request's Host header must be a loopback name (or a configured `allowed_hosts` entry);
  anything else gets 421. This closes DNS rebinding, where a web page resolves its own
  hostname to 127.0.0.1 and reads extracted file text through the browser
- Credential-looking file names and directories are excluded by default, and extracted text is
  scanned for private-key blocks and known API-token shapes before it is stored; a hit is
  recorded as `secret_suspected` (visible in `/stats` and `/errors`) and the text is dropped
- Exclusion and extension policy is enforced when a job runs, not only when it is enqueued, so
  an explicit `/index/path` cannot pull in an excluded file; at startup `enforce_scope` removes
  documents that a removed root or a new exclusion no longer covers, and both retrieval and
  `/document` refuse anything outside the configured roots regardless of what the store holds.
  An empty roots list makes nothing searchable but never deletes the index (a configuration
  mistake must not destroy data)
- The screening policy's identity (on/off plus the exemption list) is recorded in the store;
  any change rescans the stored text of already-indexed, unchanged documents at the next start:
  new hits are quarantined, quarantined documents that the new policy allows are re-queued, and
  turning screening off re-queues everything quarantined. Screening is a heuristic over the
  first 400k characters, not a guarantee
- No endpoint writes to, moves or deletes source files; index mutations touch only the sidecar
  DB, and `/backup` writes only under `<data_dir>/backups`
- Every indexed path must lie inside a configured root after normalization; `..`, long-path
  prefixes and `file:` URLs are normalized before the check
- Reparse points are not followed by default: a root that is itself a junction or symlink is
  not walked (warned at startup), a path that passes through one is rejected even if the final
  component is a plain file, and the per-directory reparse cache expires after two minutes so a
  directory later replaced by a junction is caught. With `follow_reparse_points: true` the
  resolved target must still lie inside a configured root. Hard-link counts are recorded in
  `StatInfo`. Known limit: the stat, the hash and the extractor open the path separately; a
  file swapped between those opens is detected by a final stat (identity, size, mtime) and
  re-queued, which narrows but does not close that race to a single handle
- Cloud/offline placeholder files are not recalled
- No telemetry, no network calls except the one-time model download (a release bundles the
  pinned model, so an installed service never reaches the network; disable downloads with
  `embedding.allow_download: false`)
- Native extractors run in a child process with a timeout and, on Windows, inside a job object
  with a commit limit (`indexing.extractor_memory_mb`) that also kills the child with the
  service; Office containers are size-checked before parsing (`indexing.max_expanded_bytes`)
  and text is capped while it is collected. A hung filter costs one restart. This is fault
  isolation, not a privilege sandbox: the child runs with the service's rights
- Chunking parameters and the chunker/title algorithm version form a preprocessing identity
  (`policy:preprocess` in the store); a change re-extracts every document instead of serving
  chunks that the current configuration would not have produced
