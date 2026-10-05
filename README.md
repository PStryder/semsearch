# semsearch

Search local Windows files by meaning, not just by name or exact words.

```
semsearch "find the document where I discussed why agents should not be allowed to perform destructive actions"
semsearch --literal "*.pdf"
semsearch --semantic "GPU memory architecture"
semsearch --status
semsearch --reindex F:\HexyLab\notes
```

semsearch is a sidecar to the Windows Search indexer. Windows Search supplies the file
inventory, metadata, change timestamps and (where it has content) lexical relevance; the
sidecar supplies reliable text extraction, chunking, local embeddings, a persistent vector
index, hybrid ranking with explainable scores, a loopback HTTP API and a CLI.

- No cloud calls. The embedding model runs locally on CPU (GPU optional) and is downloaded once.
- Never writes to source files. All state lives in one SQLite database under `%LOCALAPPDATA%\semsearch`.
- Incremental: unchanged bytes are never re-embedded; renames and moves re-point existing vectors.
- Hybrid by default: every result carries its semantic score, lexical score, filename signal and ranks.

## Install as a Windows service (the supported way)

```
powershell -File installer\build_release.ps1                       # stage dist\SemSearch-<version>\ (self-contained runtime)
cd dist\SemSearch-<version>                                        # then, from an ELEVATED PowerShell:
.\install.ps1 -Roots "F:\HexyLab","C:\Users\you\Documents"         # install, start, verify
```

After that the service starts with Windows, keeps the index current in the background and
answers on `http://127.0.0.1:8765`. From any prompt:

```
semsearch "notes about preventing autonomous agents from deleting files"
semsearch status | health | stats | logs
semsearch service status | start | stop | restart
```

Details, identity rationale, upgrade/uninstall and troubleshooting: [docs/windows-service.md](docs/windows-service.md).

A tray icon (per-user logon task) shows status, manages the indexed folders and, after setup, asks
whether indexing should run light (integrated GPU/CPU) or on the dedicated GPU; `http://127.0.0.1:8765/ui`
is the settings page. A new install starts with the folders Windows Search already indexes for content.

## Development quick start

```
uv sync --extra dml                                 # or --extra cpu / --extra gpu (one ONNX runtime)
uv run semsearch --init-config > semsearch.yaml     # set roots: (see semsearch.example.yaml)
uv run semsearch --serve                            # API on http://127.0.0.1:8765, background indexing
uv run semsearch "notes about preventing autonomous agents from deleting files"
```

On this workstation the recommended device policy is: steady-state embedding on the idle
integrated Radeon (`device: dml:1`), full builds on the RTX 4080 (`bulk_device: dml:0`),
queries on the CPU (`query_device: cpu`). See docs/configuration.md "Devices".

## Documents

| Document | Contents |
|---|---|
| [docs/windows-search-findings.md](docs/windows-search-findings.md) | What Windows Search exposes on this machine, what was measured, what is reused |
| [docs/architecture.md](docs/architecture.md) | Subsystems, identity/change model, storage, retrieval, security posture |
| [docs/configuration.md](docs/configuration.md) | Every setting, model choice, GPU, running at logon |
| [docs/api.md](docs/api.md) | HTTP endpoints and response shapes |
| [docs/operations.md](docs/operations.md) | Initial index, incremental operation, rebuilding, monitoring, logs, troubleshooting |
| [docs/evaluation.md](docs/evaluation.md) | Evaluation harness and measured results (literal vs semantic vs hybrid) |
| [docs/windows-service.md](docs/windows-service.md) | Windows service: host, identity rationale with measurements, file layout, accelerator resolution, install/upgrade/uninstall, recovery |

## Layout

```
src/semsearch/        the package (see docs/architecture.md for the module map)
tests/                pytest suite; live Windows Search tests skip when unavailable
eval/                 corpus manifest, labelled queries, harness, results
probes/               the Phase 1 scripts that produced the Windows Search findings
docs/                 documentation
```

## Requirements

Windows 11 (tested on 25H2), Python 3.11+, `uv`. The Windows-specific pieces degrade
gracefully: without the Windows Search service the sidecar falls back to filesystem walks.
