# SemSearch release directory

This directory is a self-contained SemSearch release:

| item | what it is |
|---|---|
| `python\` | relocatable CPython (exact version) with semsearch and every dependency installed from the locked, hash-verified set (`requirements.lock.txt`); includes the ONNX Runtime DirectML build and sqlite-vec |
| `models\bundled\...` | the pinned embedding model as plain files (installing needs no network) |
| `install.ps1`, `uninstall.ps1`, `validate.ps1` | installer (transactional: a failed upgrade rolls back), uninstaller (manifest-driven), post-install validation |
| `*.py` | installer helpers: runtime verification, launcher regeneration, release/model manifests, notice collection |
| `THIRD-PARTY-NOTICES.txt`, `sbom.json` | licence texts of every bundled component; CycloneDX component inventory |
| `model-manifest.json` | model repo, commit, file hashes, licence (MIT for the default BAAI/bge-small-en-v1.5) |
| `release-manifest.json` | SHA-256 of every file here; the installer verifies the runtime against it before and after copying |
| `LICENSE`, `VERSION`, `semsearch.example.yaml` | |

From an **elevated** PowerShell in this directory:

```
.\install.ps1 -Roots "F:\HexyLab","C:\Users\you\Documents"    # install (or upgrade), start, verify
.\validate.ps1                                                  # optional: full service-mode validation
.\uninstall.ps1                                                 # remove service + binaries + grants, KEEP the index
.\uninstall.ps1 -PurgeData                                      # ... and delete the data directory too
```

Afterwards, from any prompt:

```
semsearch "the document where I discussed ..."
semsearch status | health | stats | logs
semsearch service status | start | stop | restart
```

On a machine shared by several interactive accounts, set `read_token: true` under `server:` in
`%ProgramData%\SemSearch\semsearch.yaml` (every local process can otherwise search the index).

Full documentation: docs/windows-service.md in the source repository.
