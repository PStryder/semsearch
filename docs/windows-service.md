# Running semsearch as a Windows service

This document covers the production deployment: the service host, the identity it runs
under and why, where files live, how accelerators are resolved, installation, upgrade,
uninstall, recovery and troubleshooting. Retrieval behaviour, indexing semantics and the
security boundary are unchanged from the development mode described in the other documents.

## Service host

semsearch runs as a native SCM service hosted in its own packaged CPython runtime
(`%ProgramFiles%\SemSearch\venv\Scripts\pythonw.exe -m semsearch.service`). There is no
console window, no NSSM/WinSW shim and no dependency on any Python installed on the machine.
The service class (`src/semsearch/service.py`) implements the SCM contract directly:

| Behaviour | Implementation |
|---|---|
| Start type | Automatic (Delayed Start): the index is not needed in the first seconds after boot and Windows Search itself starts delayed |
| Readiness | SCM sees `START_PENDING` (with wait hints) while the store opens, the model loads and the API binds; `RUNNING` is reported only after `GET /health` on the loopback port returns `ok` |
| Stop / shutdown / pre-shutdown | All three set one stop event; the runtime stops the indexer (job in progress finishes or is requeued), drains the API, checkpoints and closes the database within `service.shutdown_timeout_s` (30 s default) |
| Crash recovery | `sc failure`: restart after 5 s, 30 s, 120 s; failure counter resets after a day. Jobs left `running` by a crash are requeued at the next start; an interrupted document write is invisible because document, chunks and vectors commit in one transaction |
| Single instance | A global named mutex (`Global\SemSearch.Service`); a second instance logs to the event log and exits with `ERROR_SERVICE_ALREADY_RUNNING` |
| Priority | Below-normal process priority (`indexing.low_priority`) so background embedding never competes with foreground work |
| Status | `semsearch status` / `GET /status` report version, PID, queue, counters, resolved devices, Windows Search catalog state, schema version and corruption recovery |
| Event log | Lifecycle events (start, ready, stop, configuration errors, failures) and every WARNING+ log record go to the Application log under source `SemSearch`; detailed logs rotate in `%ProgramData%\SemSearch\logs` |

## Service identity

### What was measured

Phase 1 ran the same probe (`probes/service_probe.py`) interactively and as a service under
each candidate account (`probes/run_service_probes.ps1`, elevated), checking Windows Search
OLE DB access and result counts for a user-profile folder and a OneDrive folder, plain
filesystem access to the configured roots, the ReadDirectoryChangesW watcher, DirectML on both
adapters, the IFilter COM path, the isolated extractor child, and SQLite/sqlite-vec in
`%ProgramData%`.

RESULTS_TABLE_PLACEHOLDER

### Decision

IDENTITY_DECISION_PLACEHOLDER

## File layout

| Location | Contents | Written by |
|---|---|---|
| `%ProgramFiles%\SemSearch\python\` | relocatable CPython (python-build-standalone, managed by uv) | installer only |
| `%ProgramFiles%\SemSearch\venv\` | virtual environment: semsearch + dependencies incl. `onnxruntime-directml`, `sqlite-vec`, pywin32 | installer only |
| `%ProgramFiles%\SemSearch\semsearch.cmd` | operator CLI wrapper (install dir is added to the machine PATH) | installer only |
| `%ProgramData%\SemSearch\semsearch.yaml` | configuration; written once, never overwritten by upgrades | installer (first time), operator |
| `%ProgramData%\SemSearch\index\` | `semsearch.db` + WAL (documents, chunks, FTS5, vectors, job queue) | service |
| `%ProgramData%\SemSearch\state\` | `admin.token` (gates maintenance API calls), `devices.json` (last accelerator resolution) | service |
| `%ProgramData%\SemSearch\logs\` | `semsearch.log` rotating 10 MB x 5 | service |
| `%ProgramData%\SemSearch\models\` | Hugging Face cache for the embedding model (machine-wide, no user profile involved) | installer / service |

Nothing is per-user. The one thing that *would* have been per-user, the Hugging Face model
cache under `%USERPROFILE%\.cache`, is redirected to `models\` via `HF_HOME` so the service
never needs a loaded user profile. Upgrades replace only `%ProgramFiles%\SemSearch`.

ACLs set by the installer: `%ProgramData%\SemSearch` is readable only by SYSTEM,
Administrators, the service account (full control) and the operator account (read; write on
`semsearch.yaml`). Extracted text lives in the index, so ordinary local users cannot read it
from disk; they also cannot query it, because the admin token file is unreadable to them and
the search API is loopback-only (see below).

## Accelerator resolution

The measured device policy (integrated Radeon for steady-state document embedding, RTX 4080
for bulk, CPU for queries) is expressed with **logical device names**, never with DirectML
ordinals, because ordinals follow DXGI enumeration order and change with driver updates,
hardware changes and sometimes reboots:

```yaml
embedding:
  devices:
    integrated-gpu:   # AMD Radeon(TM) Graphics
      vendor: "0x1002"
      device: "0x164e"
      subsys: "0x7d701462"
      address: pci:22.0.0
    discrete-gpu:     # NVIDIA GeForce RTX 4080
      vendor: "0x10de"
      device: "0x2704"
      subsys: "0x51121462"
      address: pci:1.0.0
  steady_state_device: integrated-gpu
  bulk_device: discrete-gpu
  query_device: cpu
  fallback_device: cpu
```

At every start the service enumerates adapters through DXGI (`src/semsearch/devices.py`),
asks the kernel display driver for each adapter's PCI bus/device/function
(`D3DKMTOpenAdapterFromLuid` + `D3DKMTQueryAdapterInfo(ADAPTERADDRESS)`), matches each
selector, and resolves the role to the current ordinal. The selector written by the
installer is vendor + device + subsystem + PCI address, which stays unique even with two
identical cards; the DXGI LUID is logged but not used because it is regenerated per boot.
Selectors can also be loose (`{vendor: amd, integrated: true}`) for portable configs.

If a named device is not present, the role falls back to `fallback_device` (CPU), the
condition is logged at WARNING level (and therefore in the event log), `/status` shows
`fell back` in the role's explanation, and the service starts normally. `semsearch devices`
prints the adapters and ready-to-paste selectors. `state\devices.json` holds the last
resolution for post-mortems.

## Installation

Build a release once (unelevated, from the repo):

```
powershell -File installer\build_release.ps1          # -> dist\SemSearch-<version>\
```

Install (elevated PowerShell, inside the release directory):

```
.\install.ps1 -Roots "F:\HexyLab","C:\Users\you\Documents"
```

The installer: checks elevation, Windows build and the WSearch service; copies the runtime to
`%ProgramFiles%\SemSearch` (keeping the previous one as `SemSearch.previous` until success);
creates the data directories; writes `semsearch.yaml` if absent, with device selectors for
the adapters it finds; pre-fetches the embedding model into the machine cache; registers the
service (delayed auto start, failure actions); applies ACLs, including read access for the
service account on each root and start/stop rights for the operator; starts the service;
waits for `/health`; runs a status and a smoke query. Re-running is an upgrade (see below).

Then: edit `%ProgramData%\SemSearch\semsearch.yaml` if you want to change roots or policy,
and `semsearch service restart`.

### Validation

`.\validate.ps1` (from the install directory, as the operator) checks: service running in
Session 0 with no console, identity, loopback-only binding and LAN unreachability, Host
header rejection, device resolution, Windows Search reachability from the service, watcher,
admin-token enforcement, create/modify/rename/delete propagation inside a root with
timings, and a service restart with identical device resolution and no re-embedding.

## Day-to-day commands

```
semsearch "the document where I talked about receipts"      # hybrid search
semsearch query --literal "*.pdf"
semsearch status | health | stats | errors | devices
semsearch logs -n 200 [--follow]
semsearch reindex F:\HexyLab\notes | reindex --full
semsearch rebuild --yes                                      # drop and rebuild the index
semsearch pause | resume | retry-failed
semsearch service status | start | stop | restart
semsearch config --validate
```

Search, health, status and stats are open to any local process on the loopback interface.
Maintenance commands (`reindex`, `rebuild`, `remove`, `pause`, `resume`, `retry-failed`)
send the token from `%ProgramData%\SemSearch\state\admin.token`; only the operator account,
Administrators and the service can read it. Service control uses the service DACL the
installer grants to the operator, so no elevation is needed.

## Upgrade

1. Build the new release (`build_release.ps1`).
2. From an elevated prompt in the new release directory: `.\install.ps1`.
   It stops the service, moves the old install to `SemSearch.previous`, copies the new runtime,
   keeps `%ProgramData%\SemSearch` untouched, re-registers the service, starts it and verifies
   health. Schema migrations are additive and run at first open (`meta.schema_version`); the
   log shows `migrating index schema vN -> vM`.
3. Verify: `semsearch status` shows the new version.

Rollback if the new build fails to start: stop the service, delete `%ProgramFiles%\SemSearch`,
rename `SemSearch.previous` back, `semsearch service start`. An index written by a newer schema
is refused by an older build with a clear message rather than modified; restore the index from
backup in that case (or rebuild).

## Uninstall

```
.\uninstall.ps1              # service + binaries removed; index/config/logs KEPT
.\uninstall.ps1 -PurgeData   # also delete %ProgramData%\SemSearch (asks for confirmation)
```

## Startup reconciliation

A boot does not start a full crawl. Sequence at service start:

1. Open and validate the store (`PRAGMA quick_check` when the database is under
   `service.integrity_check_max_mb`; schema version check; vector/model binding).
2. Requeue jobs interrupted by the previous stop; enforce scope (roots/exclusions changes).
3. Incremental pass immediately: for roots in the Windows Search index, everything whose
   `System.Search.GatherTime` is newer than the stored checkpoint (Windows did the change
   tracking while the service was offline); for other roots an mtime scan.
4. Watcher starts; normal incremental polling resumes.
5. One full reconcile after `indexing.startup_reconcile_delay_s` (10 min), then every
   `reconcile_interval_s`: enumeration diff in both directions, bounded by the enumeration
   cost (about 6k items/s from Windows Search, plus a filesystem walk). Deleted files are
   tombstoned and purged after a grace period; renames reclaim their vectors by NTFS file id.

The USN change journal would be the ideal offline-change source but requires privileges the
service deliberately does not hold; the Windows Search gather time covers the same need for
indexed roots.

## Index durability

- SQLite in WAL mode with `synchronous=NORMAL`: every committed transaction survives a crash;
  a power loss can lose only the last un-checkpointed commits, never corrupt committed pages.
- A document's row, chunks, FTS entries and vectors are written in one transaction after
  embedding succeeds; there is no state in which a document has new metadata and old vectors.
- `quick_check` runs at open. A corrupt file is moved aside as `semsearch.db.corrupt-<stamp>`
  (plus WAL/SHM), an ERROR is logged and sent to the event log, `/status` shows
  `recovered_from_corruption: true`, and a full build starts automatically.
- Changing the embedding model drops vectors and re-embeds from stored chunk text in the
  background; unchanged content is never re-embedded on restart.

Backup: stop the service (or accept a slightly stale copy) and copy `%ProgramData%\SemSearch`
(`index\`, `state\`, `semsearch.yaml`). Restore by copying back and starting the service.
Rebuild from scratch: `semsearch rebuild --yes`.

## File identity across operations

| Operation | What happens | Re-embedding |
|---|---|---|
| content modified | hash differs → re-extract, chunk; vectors reused for unchanged chunk text | only changed chunks |
| rename (same volume) | new path, same NTFS file id + same hash → row re-pointed | none |
| move within a root (same volume) | as rename | none |
| move to another configured root (same volume) | as rename | none |
| move across volumes | new file id; old row tombstoned; new path indexed, vectors reused by chunk-text hash | none (hash reuse) |
| duplicate copy | new row; vectors reused by chunk-text hash | none |
| delete | tombstoned at once (watcher) or at reconcile; purged after the grace period | n/a |
| delete then recreate at same path | same size/mtime: restored; otherwise re-indexed | as modification |
| service restart mid-change | job requeued; previous version intact until the new one commits | none extra |

Path is the lookup key users see; identity for change detection is the NTFS
`(volume serial, file id)` plus content hash, as measured in Phase 1 (Windows Search's own
`EntryID` is not stable across renames).

## Troubleshooting

| Symptom | Where to look | Likely cause / fix |
|---|---|---|
| service stops immediately after start | Event log (source SemSearch), `logs\semsearch.log` | configuration error: the message names the setting; `semsearch config --validate` |
| `semsearch` says it cannot reach the API | `semsearch service status`, `netstat -ano \| findstr :8765` | service not running, or port changed in config |
| results missing for a root | `semsearch status` → sources, `semsearch errors` | root not readable by the service account (installer grants read on each root; re-run install after adding roots), or excluded |
| `fell back to cpu` in status | `semsearch devices` | the named adapter is absent or the driver changed its identity; update the selector or accept CPU |
| indexing slow | `semsearch status` → embedding.bulk_mode and device | bulk device unavailable; queue above `bulk_threshold` runs on the steady device |
| `RECOVERED FROM CORRUPTION` in status | `index\*.corrupt-*` | the previous database was unreadable; a rebuild is in progress; delete the quarantined files when satisfied |
| access denied controlling the service | `installer\install.ps1 -Operator DOMAIN\user` | the account is not the operator; re-run the installer or use an elevated prompt |

Logs never contain extracted document text; they contain paths, counts, timings and errors.
