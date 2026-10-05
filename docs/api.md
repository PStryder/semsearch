# API

Base URL: `http://127.0.0.1:8765` (configurable). JSON in, JSON out. The service exposes no
interactive docs or OpenAPI schema (`/docs` and `/openapi.json` are off).
The API is loopback-only; nothing here can modify, move or delete a source file. Requests whose
`Host` header is not a loopback name (or a configured `api.allowed_hosts` entry) are answered
with **421** to defeat DNS rebinding from a browser.

**Who can call what.** Everything except `/health` and the static `/ui` page needs a
credential, because the index holds text the *service* account could read and the API would
otherwise hand it to any local process:

| credential | how it is obtained | grants |
|---|---|---|
| admin token, header `X-SemSearch-Token` | `<state_dir>/admin.token`, readable by the operator account, Administrators and the service only; **rotated at every service start** | everything |
| settings-page session, header `X-SemSearch-Session` | the tray calls `POST /ui/nonce` (admin token) and opens `/ui#n=<nonce>`; the page redeems the single-use, 60 s nonce at `POST /ui/redeem` and keeps the session in that tab's `sessionStorage` (12 h) | reads, and `POST /config/excludes`; nothing else (no roots, backups, wipes, maintenance or new nonces) |

`api.read_token: false` reopens reads to every local process (only for a machine with a single
interactive user). `/health` without a credential answers liveness only (`ok`, `version`).

The CLI and the tray attach the admin token only after checking that the process listening on
the configured loopback port **is the SemSearch service** (its PID from the Service Control
Manager against the owner of the listening socket). A server named by a stray
`semsearch.yaml`, or another account's process holding the port while the service restarts,
never receives it. A `semsearch.yaml` in the current folder is ignored when the machine
configuration exists.

Limits: query 2,000 characters; at most 32 roots and 32 extensions per search; at most 1,000
exclusion patterns of 512 characters; request bodies 1 MB, and POSTs need a Content-Length.

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
| `POST /indexer/prune` | delete pending jobs the current roots/exclusions reject and remove out-of-scope documents (runs automatically after a scope change) |
| `POST /indexer/incremental` | run an incremental pass now |
| `POST /indexer/reconcile` | run a reconcile pass now |

## Scope configuration

| Endpoint | Auth | Effect |
|---|---|---|
| `POST /ui/nonce` | admin | single-use 60 s nonce for the settings page |
| `POST /ui/redeem` `{"nonce": ...}` | none | trades a nonce for a tab session (see above) |
| `GET /config` | read | `{config, roots, excludes, read_token, editable}` |
| `POST /config/roots` `{"add": path}` / `{"remove": path}` / `{"set": [paths]}` | admin | change the indexed folders: persisted into the configuration file, applied live (watcher restarted, new root enumerated, out-of-scope documents removed in the background). Every folder the call ADDS must carry an explicit (not inherited) read grant for the service's account, which `semsearch roots add` and the tray place as the folder's owner: proof that someone with change-permission rights shared it. 400 if it does not exist, 403 without the grant; network and device paths are refused. Values with control characters are refused, and the file is only rewritten if it parses back to exactly the requested lists |
| `POST /config/excludes` `{"excludes": [globs]}` | admin | replace the exclusion list, persisted and applied live |
| `GET /config/windows-scope` | read | what the Windows Search indexer covers for content in the operator's profile and its exclusion rules, as semsearch roots/globs; read-only |
| `GET /ui` | none | the settings page (HTML) |

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
