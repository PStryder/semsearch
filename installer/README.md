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

The installer needs no console of its own: nothing in `install.ps1` is interactive (only
`uninstall.ps1 -PurgeData` without `-Yes` asks for confirmation), so it can be run elevated
and hidden from an ordinary prompt, with the output kept in a transcript:

```
Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',
  "Start-Transcript $env:TEMP\semsearch-install.log -Force; Set-Location '<release dir>'; .\install.ps1; Stop-Transcript"
```

Only the UAC consent prompt is shown (Windows draws it; it cannot be suppressed). From an
**elevated** PowerShell in this directory the plain commands are:

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

Reads need the admin token by default (`read_token: true` under `server:`), so other local
accounts cannot search the index. Only on a machine with a single interactive user may it be
set to `false`.

Full documentation: docs/windows-service.md in the source repository.
