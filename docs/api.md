# API

Base URL: `http://127.0.0.1:8765` (configurable). JSON in, JSON out. Interactive docs at `/docs`.
The API is loopback-only; nothing here can modify, move or delete a source file. Requests whose
`Host` header is not a loopback name (or a configured `api.allowed_hosts` entry) are answered
with **421** to defeat DNS rebinding from a browser.

**Who can call what.** Maintenance endpoints (`/index/path`, `/remove/path`, `/reindex`,
`/indexer/*`, `/backup`) require the admin token (`X-SemSearch-Token`, file
`<state_dir>/admin.token`, readable by the operator account only). Read endpoints are open to
every local process by default, which is the right trade-off on a single-user workstation and
the wrong one on a machine with several interactive accounts: the index holds text the
*service* account could read, and the API answers whoever asks on the loopback interface.
Set `api.read_token: true` to require the same token for `/search`, `/document`, `/status`,
`/stats` and `/errors` (`/health` stays open); the CLI sends it automatically when it can read
the token file.

## POST /search

```json
{ "query": "notes about preventing autonomous agents from deleting files",
  "mode": "hybrid",            // literal | semantic | hybrid (default from config)
  "limit": 20,                 // 1..200
  "roots": ["F:/HexyLab/docs"],   // optional: restrict to these directories
  "extensions": ["md", "pdf"] }   // optional
```

`GET /search?q=...&mode=hybrid&limit=20&ext=md,pdf&root=F:/HexyLab` is equivalent.

Response:

```json
{
  "query": "...", "mode": "hybrid", "took_ms": 6.1, "candidates": 143, "glob": false,
  "timings": {"lexical_ms": 2.0, "semantic_ms": 3.4},
  "weights": {"semantic": 0.6, "lexical": 0.4, "rrf_k": 60},
  "results": [
    {
      "path": "F:\\HexyLab\\agents.md",
      "filename": "agents.md",
      "score": 0.91,                       // 0..1, see docs/architecture.md "Retrieval"
      "match_type": "both",                // semantic | lexical | both | filename
      "excerpt": "...Autonomous agents must not be allowed to perform destructive actions...",
      "chunk_ordinal": 0,
      "modified": "2026-10-02T13:40:12", "modified_ts": 1790000000.0,
      "file_type": "md", "size": 1234,
      "scores": {
        "semantic": 0.799,                 // cosine of the best chunk
        "lexical": 1.0,                    // BM25 normalized within the result set
        "filename": null,                  // fraction of query terms in the filename
        "windows_rank": null,              // System.Search.Rank / 1000 when Windows has content
        "semantic_rank": 1, "lexical_rank": 1,
        "rrf": 0.833                       // reciprocal-rank-fusion value (hybrid only)
      },
      "why": [
        "semantic: best chunk 17 cosine 0.799 (rank 1)",
        "lexical: bm25 1.000 (rank 1); terms in excerpt: ['agents', 'destructive', 'actions']"
      ],
      "doc_id": 17
    }
  ]
}
```

A query consisting of a single token with `*` or `?` (for example `*.pdf`) is a filename glob
and returns `match_type: "filename"` hits.

## GET /health

`{"ok": true, "version": "0.1.0", "uptime_s": 12.3, "indexer_running": true, "embedding": "onnx:BAAI/bge-small-en-v1.5:...", "windows_search": true, "documents": 1234}`

## GET /status

```json
{
  "indexer": {
    "running": true, "paused": false, "full_build_in_progress": false,
    "current_path": "F:\\...\\x.pdf", "queue": {"pending": 120, "running": 1, "failed": 2},
    "started_at": ..., "last_incremental_at": ..., "last_reconcile_at": ..., "last_full_build_at": ...,
    "counters": {"indexed": 980, "skipped_unchanged": 4000, "touched": 3, "moved": 2, "removed": 1,
                 "failed": 2, "chunks_embedded": 5400, "chunks_reused": 300, "bytes_hashed": 123456789,
                 "extract_ms_total": 54000, "embed_ms_total": 190000},
    "throughput": {"window_s": 300, "docs_per_s": 1.2, "chunks_per_s": 25.0},
    "sources": {"f:\\hexylab": "windows_search+fs"}, "roots": ["F:\\HexyLab"],
    "watcher": true, "extractor_restarts": 0, "last_error": null,
    "embedding": {"fingerprint": "...", "dim": 384, "device": "cpu"}
  },
  "windows_search": {"available": true, "indexer_version": "10.0.26100.9444", "status": "idle",
                     "paused_reason": "none", "items": 427288,
                     "to_index": {"incremental": 0, "notification": 0, "high_priority": 0},
                     "url_being_indexed": null},
  "store": {"path": "...\\semsearch.db", "fingerprint": "...", "dim": 384},
  "config_source": "F:\\HexyLab\\semsearch\\semsearch.yaml"
}
```

## GET /stats

Store-level statistics: document/chunk/vector counts, documents awaiting embedding,
tombstoned documents, counts by extraction status, extension and method, DB size,
queue depth, error count, plus the indexer counters and throughput.

## GET /errors?limit=100

Most recent indexing errors: `{"errors": [{"path", "stage", "message", "at"}]}`. Stages:
`policy` (rejected by containment/reparse rules), `extract`, `job`.

## POST /index/path

`{"path": "F:/HexyLab/notes", "priority": 1}` → `{"enqueued": 57, "kind": "directory"}`.
Files and directories are accepted; paths outside the configured roots return 403.

## POST /remove/path

`{"path": "F:/HexyLab/old"}` → `{"removed": 12}`. Removes a file or everything under a
directory from the index immediately (the files themselves are untouched).

## POST /reindex

- `{"path": "F:/HexyLab/x.md"}` → re-index one path (same as `/index/path`)
- `{"full": true}` → enqueue a full build (unchanged files are skipped cheaply)
- `{"wipe": true}` → drop the index contents and rebuild from scratch

## Indexer controls

| Endpoint | Effect |
|---|---|
| `POST /indexer/pause` / `POST /indexer/resume` | stop/start job processing |
| `POST /indexer/retry-failed` | move `failed` jobs back to `pending` |
| `POST /indexer/incremental` | run an incremental pass now |
| `POST /indexer/reconcile` | run a reconcile pass now |

## GET /document?path=...&chunks=false&offset=0&limit=50

The stored document row (status, method, error, hash, timestamps) and optionally a page of
its chunks in document order (`limit` 1..500; `chunk_count` says how many exist). 404 if the
path is not indexed or lies outside the configured roots.

## POST /backup

`{"path": "semsearch-2026-10-04.db"}` (admin token) writes a consistent online copy with the
SQLite backup API. The destination must be a file under `<data_dir>/backups` (or
`api.backup_dir`); a relative name lands there, anything outside is refused with 403. The
token is maintenance authority over the index, not a licence to write SQLite files wherever
the service account can. `semsearch backup <name>` uses this; `semsearch backup <path> --direct`
copies the database file itself with the caller's own rights.

## Errors

4xx responses carry `{"detail": "..."}`. An unexpected failure returns 500 with
`{"error": "internal error", "ref": "<8 hex>", "hint": "see the service log"}`; the traceback
and the reference are in the log, never in the response (exception text can carry paths).
