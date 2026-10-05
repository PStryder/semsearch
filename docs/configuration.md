# Configuration

semsearch reads one YAML file. Resolution order:

1. `--config <path>` on the command line
2. `$SEMSEARCH_CONFIG`
3. `./semsearch.yaml`
4. `%LOCALAPPDATA%\semsearch\semsearch.yaml`
5. `%ProgramData%\SemSearch\semsearch.yaml` (the service install; the service itself reads only this one)

A malformed file (bad YAML, unknown device name, out-of-range port, non-loopback host without
`allow_non_loopback`, wrong types) is rejected with a message naming the setting; the service
refuses to start on it and logs the reason to the event log. `semsearch config --validate`
checks a file without starting anything. `server:` is accepted as a synonym for `api:` and
`embedding.steady_state_device` for `embedding.device`.

`semsearch --init-config` prints a starter file. With no file at all the defaults apply and
`roots` is empty, so nothing is indexed until you add at least one root.

`roots` and `excludes` can be changed while the service runs: `semsearch roots add|remove`,
the tray icon's Folders menu, the settings page (`/ui`, exclusions) or `POST /config/roots`
and `POST /config/excludes` with the admin token. The change is written back into this file
by a textual edit of just those two blocks (comments survive) and applied live. The
installer seeds a new file's roots and excludes from the Windows Search content scope
(`semsearch scope` shows it; `semsearch roots import-windows` adds its folders later, and
`--with-excludes` also adopts its exclusion rules, which removes documents already indexed
under them).

## Reference

```yaml
data_dir: "%LOCALAPPDATA%/semsearch"   # Environment variables and ~ are expanded. Sub-dirs default under it:
index_dir: null                         #   <data_dir>/index  (semsearch.db)
state_dir: null                         #   <data_dir>/state  (admin.token, devices.json)
log_dir: null                           #   <data_dir>/logs
roots:                                  # directories to index (absolute paths)
  - "F:/HexyLab"
excludes:                               # setting this REPLACES the default list
  - "**/.git/**"                        # a pattern with '/' matches the full path (forward slashes)
  - "*.pem"                             # a bare pattern matches the FILE name only, never a folder name
  # defaults: VCS dirs, node_modules, venvs, caches, build output, minified bundles, lockfiles,
  # AppData and the dot-directories of user profiles (**/AppData/**, **/Users/*/.*/**: a profile
  # used as a root otherwise queues hundreds of thousands of application-state files),
  # credential locations (.ssh, .aws, .azure, .gnupg, .kube) and credential-looking file names
  # (.env*, *.pem, *.key, *.pfx, *.p12, id_rsa*, *secret*, *credential*, *password*, *api_key*, ...)
text_extensions: [...]                  # read directly as text (code, markdown, json, yaml, ...)
document_extensions: [.pdf, .docx, .doc, .pptx, .ppt, .xlsx, .xls, .rtf]
extra_extensions: []                    # additional extensions treated as text

embedding:
  provider: onnx                        # onnx | sentence-transformers | hashing
  model: BAAI/bge-small-en-v1.5         # HF repo id with onnx/model.onnx, or a local directory
  revision: 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a   # pinned commit of the default model (null = whatever `main` is today)
  device: cpu                           # steady-state document embedding: cpu | cuda[:n] | dml[:n] | auto | <name in devices>
  bulk_device: same                     # used during full builds / deep queues (see "Devices")
  query_device: same                    # query embedding (latency matters)
  fallback_device: cpu                  # used when a named device is not present (logged, never fatal)
  device_profile: null                  # light | gpu: the tray's hardware choice (see "Devices"); null = not asked yet
  devices:                              # logical names -> stable selectors (vendor/device/subsys/address/name/integrated)
    integrated-gpu: {integrated: true}
    discrete-gpu: {integrated: false}
  bulk_threshold: 500                   # pending jobs above which bulk_device is used
  bulk_doc_chunks: 200                  # one document needing this many NEW vectors uses bulk_device (0 = off)
  cache_dir: null                       # model cache (default <data_dir>/models; HF_HOME)
  batch_size: 32
  max_seq_length: 512
  pooling: cls                          # cls (bge) | mean (MiniLM and most sentence-transformers)
  normalize: true
  query_prefix: "Represent this sentence for searching relevant passages: "  # bge convention
  document_prefix: ""
  allow_download: true                  # false = must already be in the HF cache or a local dir
  threads: 0                            # onnxruntime intra-op threads, 0 = default
  on_model_change: reembed              # reembed | refuse

chunking:                               # changing the first four re-extracts every document at the next start
  target_chars: 1400                    # (the stored chunks no longer match what this configuration would produce)
  max_chars: 2200
  overlap_chars: 180
  min_chars: 40
  # Coverage (changing these re-extracts only the documents they affect):
  max_chunks_per_doc: 100000            # safety ceiling; every chunk up to it is in the full-text index
  embed_chunks_data: 2000               # data formats get vectors for their first N chunks only ...
  data_extensions: [.json, .jsonl, .ndjson, .csv, .tsv, .log, .html, .htm, .xml]
                                        # ... the rest of such a file is searchable literally, not semantically;
                                        # prose and code are embedded in full

api:
  host: 127.0.0.1
  port: 8765
  allow_non_loopback: false
  allowed_hosts: []                     # extra Host header values; loopback names are always allowed
  log_requests: false
  backup_dir: null                      # where POST /backup may write (default <data_dir>/backups); nowhere else
  read_token: true                      # reads need the admin token or a settings-page session; false = any local process may search

indexing:
  use_windows_search: true              # inventory + GatherTime deltas + FREETEXT when the root is indexed
  poll_interval_s: 30                   # incremental pass cadence
  reconcile_interval_s: 3600            # full enumeration diff (deletes) and tombstone purge
  auto_start: true                      # run a full build on first start if none was done
  max_file_bytes: 52428800              # 50 MB
  max_text_chars: 50000000              # text beyond this is not indexed; the document is flagged (text_truncated)
  max_expanded_bytes: 536870912         # an Office container whose members would expand past this (512 MB) is not parsed
  extractor_memory_mb: 2048             # commit limit of the extractor child process (Windows job object); 0 = unlimited
  follow_reparse_points: false          # a root that is itself a junction/symlink is not walked unless this is true
  max_attempts: 3                       # per job before it is marked failed
  extract_timeout_s: 120                # per file, document formats (child process)
  watch_filesystem: true                # ReadDirectoryChangesW watcher per root
  fs_poll_interval_s: 600               # roots NOT in the Windows index are mtime-scanned at most this often
  skip_suspected_secrets: true          # refuse text containing private keys / API tokens (status secret_suspected)
  reconcile_min_fraction: 0.5           # skip tombstoning when an enumeration returns fewer than this share of known files
  startup_reconcile_delay_s: 600        # first full reconcile this long after start
  low_priority: true                    # below-normal process priority
  secret_scan_allow: []                 # path globs exempt from secret screening (e.g. "**/docs/examples/**"); any change
                                        # to this list or to skip_suspected_secrets re-screens stored text at the next start
  ocr_scanned_pdfs: false               # OCR image-only PDFs with the Windows OCR engine (needs `uv sync --extra ocr`)
  ocr_max_pages: 50
  bulk_yield_gpu_percent: 40            # stay off the bulk GPU while other processes keep it above this utilization
  vacuum_interval_s: 86400              # idle-time reclaim of free database pages

service:                                # Windows service host (docs/windows-service.md)
  name: SemSearch
  shutdown_timeout_s: 30
  startup_timeout_s: 180
  event_log: true
  integrity_check: quick                # quick | none  (SQLite quick_check at open)
  integrity_check_max_mb: 4096

retrieval:
  default_mode: hybrid                  # literal | semantic | hybrid
  fusion: convex                        # convex | rrf
  rrf_k: 60
  semantic_weight: 0.6
  lexical_weight: 0.4
  candidate_chunks: 300                 # top-k chunks pulled from each signal before fusion
  candidate_docs: 100
  use_windows_rank: true
  windows_rank_disable_after: 20        # pause the Windows FREETEXT signal for an hour after this many empty answers
  collapse_duplicates: true             # identical content shows once, other copies listed under `duplicates`
  excerpt_chars: 420
  vector_cache: true                    # keep vectors in RAM for fast queries
  vector_cache_dtype: float32           # float16 halves RAM (≈0.75 KB/chunk) at some query-latency cost

log_level: INFO
```

## Choosing an embedding model

| Model | dim | CPU speed (this machine) | Notes |
|---|---|---|---|
| `BAAI/bge-small-en-v1.5` (default) | 384 | ~26 chunks/s | Good quality/speed balance; `pooling: cls` |
| `BAAI/bge-base-en-v1.5` | 768 | ~7.6 chunks/s | See docs/evaluation.md for the measured quality difference; 2x RAM for the vector cache |
| `sentence-transformers/all-MiniLM-L6-v2` | 384 | ~2x faster | Set `pooling: mean`, `query_prefix: ""` |
| `nomic-ai/nomic-embed-text-v1.5` | 768 | slower | Needs `query_prefix: "search_query: "`, `document_prefix: "search_document: "`, `pooling: mean` |

Any HF repo that ships `onnx/model.onnx` and `tokenizer.json` works. For a fully offline
install copy those two files into a directory and set `model` to that path.

The default model is English-only. For mixed-language material use a multilingual model, for
example `intfloat/multilingual-e5-small` (384 dims, `pooling: mean`, `query_prefix: "query: "`,
`document_prefix: "passage: "`) or `BAAI/bge-m3` (1024 dims, `pooling: cls`, no prefixes;
about 2 GB). Changing the model re-embeds the index from stored chunk text in the background.

Changing `model`, `pooling` or `revision` changes the fingerprint; existing vectors are dropped
and re-embedded from stored chunk text in the background (`on_model_change: reembed`).

## Devices

Exactly one ONNX Runtime build is installed, chosen by extra:

| extra | runtime | devices available |
|---|---|---|
| `cpu` | `onnxruntime` | `cpu` |
| `dml` | `onnxruntime-directml` | `cpu`, `dml:<adapter>` (any DirectX 12 GPU, including integrated graphics) |
| `gpu` | `onnxruntime-gpu` | `cpu`, `cuda:<n>` (needs CUDA 12 + cuDNN 9) |

`uv sync --extra dml` switches an existing environment (the previous runtime is removed).
DirectML adapter numbering follows DXGI enumeration: on this workstation `dml:0` is the RTX
4080 and `dml:1` is the Ryzen's integrated Radeon. A device that is not available at runtime
falls back to CPU with a logged warning, never an error.

Three roles can run on different devices with one model file loaded once:

| setting | role | guidance |
|---|---|---|
| `device` | steady-state document embedding (the trickle of changed files) | the idle integrated GPU is enough here and keeps the CPU free |
| `bulk_device` | document embedding while a full build runs or more than `bulk_threshold` jobs are pending | the fastest device you have; the first pass is throughput-bound |
| `query_device` | query embedding (one short text per search) | CPU has the lowest latency for a single text |

Measured on this workstation (fp32, 1,800-character chunks, batch 8; vectors agree across
devices to 1e-7):

| device | bge-small chunks/s | bge-base chunks/s | query latency (small) |
|---|---|---|---|
| CPU (Ryzen 7 7800X3D, 16 threads) | 26 | 7.6 | 2.9 ms |
| `dml:1` integrated Radeon | 6.9 | 3.3 | 8.0 ms |
| `dml:0` RTX 4080 | 254 (batch 32) | 156 (batch 32) | 13.9 ms (launch overhead) |

Recommended on this machine:

```yaml
embedding:
  device: dml:1          # integrated GPU: ~7 chunks/s, zero CPU load, idle otherwise
  bulk_device: dml:0     # RTX 4080 for the first pass and big backlogs
  query_device: cpu
```

### The hardware choice (tray)

After setup the tray asks once, when the machine has a dedicated GPU, which of two profiles to use
(`embedding.device_profile`). It can be changed later under tray icon > *Indexing hardware*, or
with `POST /config/devices`; either way the change is written into this file and applied live.

| profile | `device` (everyday changes) | `bulk_device` (full builds, > `bulk_threshold` queued) |
|---|---|---|
| `light` | the integrated GPU, or `cpu` when there is none | the dedicated GPU |
| `gpu` | the dedicated GPU | the dedicated GPU |

`query_device` is left alone (CPU is fastest for one query). Cancel leaves the profile unset and
the tray asks again at its next start; without a dedicated GPU there is nothing to choose and it
never asks. Under `light`, one document that needs `bulk_doc_chunks` (default 200) or more new
vectors goes to `bulk_device` whatever the queue depth; vectors reused from the previous version
do not count, so editing a paragraph of a long document stays on the integrated GPU. 200 chunks
is about 280,000 characters: ~30 s on the integrated Radeon (~7 chunks/s), ~1 s on the RTX 4080.
Set it to 0 to keep single documents off the bulk device. `gpu` is the profile for catching up
quickly.

With the light policy a modified 10-chunk document costs about 1.5 s on the integrated GPU, a full
first pass over ~500k chunks about 35 minutes on the 4080 (versus ~5 h on CPU, ~20 h on the
integrated GPU), and a query stays at a few milliseconds.

## Running as a background service

The server is a plain console process (`semsearch --serve`). To keep it running at logon
create a Scheduled Task (`schtasks /Create /SC ONLOGON /TN semsearch /TR "...\.venv\Scripts\semsearch-serve.exe"`)
or a shortcut in `shell:startup`. No elevation is needed. Do not run it as SYSTEM: the index
should see exactly the files your account can read.
