# SemSearch release directory

This directory is a self-contained SemSearch release: a relocatable CPython runtime, a
virtual environment with semsearch and every dependency (including the ONNX Runtime DirectML
build and sqlite-vec), and the installer scripts.

From an **elevated** PowerShell in this directory:

```
.\install.ps1 -Roots "F:\HexyLab","C:\Users\you\Documents"    # install (or upgrade), start, verify
.\validate.ps1                                                  # optional: full service-mode validation
.\uninstall.ps1                                                 # remove service + binaries, KEEP the index
.\uninstall.ps1 -PurgeData                                      # ... and delete %ProgramData%\SemSearch too
```

Afterwards, from any prompt:

```
semsearch "the document where I discussed ..."
semsearch status | health | stats | logs
semsearch service status | start | stop | restart
```

Full documentation: docs/windows-service.md in the source repository.
