# Operations

For the supported production deployment (Windows service, `%ProgramFiles%` /
`%ProgramData%` layout, installer, upgrade, recovery) see [windows-service.md](windows-service.md).
This page covers running from a source checkout and the operational concepts shared by both.

## Install (development checkout)

```
cd F:\HexyLab\semsearch
uv sync --extra cpu                # CPU-only ONNX Runtime
uv sync --extra dml                # DirectML runtime: integrated and discrete GPUs on Windows (this machine)
uv sync --extra gpu                # CUDA runtime
uv sync --extra dml --extra dev    # + pytest
```

Pick one runtime extra; see docs/configuration.md "Devices" for the device policy.

Create a config (`uv run semsearch --init-config > semsearch.yaml`), set `roots`, then start
the server:

```
uv run semsearch --serve
```

The first start downloads the embedding model into the Hugging Face cache (about 130 MB for
bge-small) and, because `indexing.auto_start` is true, begins a full build in the background.
Queries work immediately against whatever has been indexed so far.

## Initial index

Two ways:

- **In the server** (default): the scheduler runs `full_build` once, then keeps the index
  current. Watch progress with `semsearch --status` (queue depth, current path, throughput).
- **One-shot, in the foreground**: `uv run semsearch --build` enumerates, indexes everything,
  prints counters and exits. Useful for a first build you want to watch, or on a schedule.

Both are resumable: the job queue lives in SQLite. Stop the process at any time; on the next
start, jobs left `running` are requeued and processing continues. Files that were already
indexed are skipped by a size/mtime check, so re-running a full build on an up-to-date index
costs one `lstat` per file.

Expected throughput on this workstation: ~26 chunks/s on CPU, ~7 on the integrated Radeon,
~250 on the RTX 4080 (bge-small); text extraction is negligible for text files, ~15 to 100 ms
for Office documents, ~30 ms/page for PDFs. A corpus of 100k documents averaging 5 chunks runs
on the order of 5 hours on CPU or ~35 minutes with `bulk_device: dml:0`. Subsequent passes only
touch changed files.

## Incremental operation

While the server runs:

1. the filesystem watcher enqueues changed paths within ~2 s,
2. every `poll_interval_s` an incremental pass asks Windows Search for items gathered since
   the per-root checkpoint (or scans mtimes for roots Windows does not index),
3. every `reconcile_interval_s` a full enumeration diff tombstones vanished files and purges
   old tombstones.

Unchanged bytes are never re-embedded: identical hash → metadata touch; rename/move →
row re-pointed via the NTFS file id; modified file → only chunks whose text changed are
embedded (chunk-text hash lookup).

## Rebuilding

| Situation | Command |
|---|---|
| Re-index one file or folder | `semsearch --reindex F:\path` |
| Re-check everything (cheap, skips unchanged) | `semsearch --reindex --full` |
| Start over (drops documents, chunks, vectors, jobs) | `semsearch --reindex --wipe` |
| Switch embedding model | edit `embedding.model`, restart; vectors are rebuilt from stored chunks automatically |
| Retry files that failed 3 times | `semsearch --retry-failed` |

Deleting `<data_dir>\semsearch.db` (with the server stopped) is equivalent to `--wipe`.

## Backup and housekeeping

```
semsearch backup semsearch-2026-10-02.db                 # consistent online copy via the running service -> <data_dir>\backups\
semsearch backup D:\backups\semsearch.db --direct        # copy straight from the database file, anywhere you can write (service may be stopped)
```

The service writes only under its own backup directory (`<data_dir>\backups`, or
`api.backup_dir`); the admin token is authority over the index, not over the filesystem.
Both use SQLite's backup API, so the copy is consistent even while the indexer writes. Restore
by stopping the service, replacing `<index_dir>\semsearch.db` (and deleting any `-wal`/`-shm`),
and starting it; the next incremental pass picks up anything that changed since the copy.

The database reclaims free pages on its own once per `indexing.vacuum_interval_s` when the
queue is idle (incremental vacuum on databases created by this version; a one-time full VACUUM
the first time on older databases).

## Monitoring

- `semsearch --status`: indexer state, queue, counters, throughput, Windows Search catalog
  state, embedding model.
- `semsearch --stats`: store counts by extraction status/method/extension, vectors,
  documents awaiting embedding, tombstones, DB size.
- `semsearch --errors`: last 100 indexing errors with stage and message.
- `semsearch --health` or `GET /health` for liveness.

## Logs

`<data_dir>\logs\semsearch.log`, rotating at 10 MB x 5. Set `log_level: DEBUG` to log every
indexed file with extraction/embedding timings. The server also logs to stderr.

## Troubleshooting

**No results at all** — `semsearch --stats`: if `documents` is 0 the build has not run; check
`roots` exist and `--status` shows a source for each root. If `vectors` is 0 but `chunks` is
not, vectors are being rebuilt after a model change (`documents_awaiting_embedding`).

**A file is missing from results** — `GET /document?path=...`: 404 means it was never
enqueued (extension not in `text_extensions`/`document_extensions`/`extra_extensions`, or
excluded by a glob, or outside the roots). A row with `extract_status` of `empty`, `binary`,
`unsupported`, `too_large`, `denied`, `secret_suspected` or `error` explains why there is no
text; `--errors` shows the message for `error` and the matched pattern for `secret_suspected`
(stage `policy`). A `policy` error for a path you expected to be indexed means it matches an
exclusion pattern or its extension is not configured.

**Stop indexing a folder** — add it to `excludes` (or remove the root) and restart: startup
scope enforcement removes everything the new configuration no longer covers.

**Windows Search shows `available: false`** — the `WSearch` service is stopped or the OLE DB
provider is unavailable; semsearch falls back to filesystem walks for every root. Start the
service (`Start-Service WSearch`).

**Windows Search is `paused` / `recovering`** — the indexer itself is throttled; semsearch
keeps working, only the GatherTime delta source lags.

**A file was refused as a suspected secret but is harmless** — `semsearch errors --stage policy`
lists the matched pattern; add its folder to `indexing.secret_scan_allow` and re-index it.

**Scanned PDFs show as `empty`** — they have no text layer. Enable `indexing.ocr_scanned_pdfs`
(after `uv sync --extra ocr`, or a release built with it) to OCR them with the Windows OCR
engine; the first reindex of those files takes seconds per page.

**Same document appears once but you know there are copies** — that is the duplicate collapse:
each copy is still indexed and listed under `duplicates` on the hit. Set
`retrieval.collapse_duplicates: false` to show every copy as its own result.

**Changes not picked up** — `--status` shows `watcher: false` if the watcher failed to start
(for example a root on a network share); the incremental pass still runs every
`poll_interval_s`. A watch-buffer overflow is logged and corrected by the next reconcile.

**Extractor restarts climbing** — a file is hanging a native filter; `--errors` names it with
`extraction timed out`. Add it to `excludes` or raise `extract_timeout_s`.

**High memory** — the vector cache holds every vector in RAM (≈ 1.5 KB per chunk at 384 dims).
Set `retrieval.vector_cache: false` to query sqlite-vec directly (slower, no RAM cost).

**Port in use** — change `api.port` or pass `--port`.

**Model download blocked** — download `onnx/model.onnx` and `tokenizer.json` elsewhere, put
them in a directory, set `embedding.model` to that path and `allow_download: false`.

## Tests and evaluation

```
uv run pytest -q                      # unit + integration (hashing provider; live Windows Search tests auto-skip)
uv run python eval/build_corpus.py    # copy the evaluation corpus (read-only on sources)
uv run python eval/run_eval.py        # index the corpus and report top-1 / recall@5 / MRR / latency
```

## Removal

Stop the server and delete `data_dir`. No files outside it are written; nothing is installed
system-wide.
