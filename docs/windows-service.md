# Running semsearch as a Windows service

This document covers the production deployment: the service host, the identity it runs
under and why, where files live, how accelerators are resolved, installation, upgrade,
uninstall, recovery and troubleshooting. Retrieval behaviour, indexing semantics and the
security boundary are unchanged from the development mode described in the other documents.

## Service host

semsearch runs as a native SCM service hosted in its own packaged CPython runtime
(`%ProgramFiles%\SemSearch\python\cpython-3.12...\pythonw.exe -m semsearch.service`). There is
no console window, no NSSM/WinSW shim and no dependency on any Python installed on the
machine. The service class (`src/semsearch/service.py`) implements the SCM contract directly:

Four Session-0 facts shaped the host and the installer. None of them is visible when the
same code runs from a terminal; each was found by running the real service and reading the
event log:

1. A venv's `python.exe` on Windows is a launcher that spawns the real interpreter as a
   child. The Service Control Manager logs "a service process other than the one launched
   connected", and stop/kill accounting applies to the wrong process. The runtime therefore
   has no venv layer; the binary is the real `pythonw.exe`.
2. A windowless process has `sys.stdout = None`; uvicorn's default logging configuration
   calls `.isatty()` on it and the API never starts. The host installs null standard streams
   (not `NUL`, whose `isatty()` is true on Windows) and keeps its own logging configuration.
3. The interpreter honours the *installing user's* `%APPDATA%\Python` site-packages. `pip`
   reported packages found there as "already satisfied" and left them out of the runtime,
   the verification step passed because it ran as that user, and the service, which has no
   user profile, failed with `No module named 'tokenizers'`. Everything now runs with
   `python -s` (build, verification, the service binary, the CLI wrapper).
4. Failure-recovery actions fire during an upgrade: while the installer was copying the new
   runtime, the SCM restarted the previous instance against a half-copied tree. The installer
   disables recovery and sets the service to disabled for the duration of the upgrade, then
   restores both.

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

Measured 2026-10-02 (full reports in `probes/results/`; the `interactive` column is the
same probe run from the logged-in desktop session):

| check | interactive (pstry, session 1) | LocalSystem | LocalService | NetworkService | NT SERVICE\SemSearchProbe (virtual account) |
|---|---|---|---|---|---|
| session | 1 | 0 | 0 | 0 | 0 |
| HKCU / user profile | yes | none | none | none | none |
| read `F:\HexyLab` (Users:RX) | ok | ok | ok | ok | ok (after installer-style grant) |
| read `C:\Users\pstry\Documents` | ok | ok | **denied** | **denied** | **denied** (no grant was made for the probe) |
| read `F:\Personal OneDrive\OneDrive` | ok | ok | ok | ok | ok |
| Windows Search OLE DB ping | ok | ok | ok | ok | ok |
| Windows Search items under Documents | 38,402 | 38,402 | 0 | 0 | 0 |
| Windows Search items under OneDrive | 4,873 | 4,873 | 4,873 | 4,873 | 4,873 |
| Windows Search catalog COM | idle | idle | idle | idle | idle |
| ReadDirectoryChangesW watcher | event | event | event | event | event |
| DXGI order (ordinal: adapter) | 0: RTX 4080, 1: Radeon | **0: Radeon, 1: RTX 4080** | 0: Radeon, 1: RTX 4080 | 0: Radeon, 1: RTX 4080 | 0: Radeon, 1: RTX 4080 |
| embed on RTX 4080 (bge-small, batch 32) | 150 chunks/s | 168 | 155 | 159 | 169 |
| embed on integrated Radeon | 4.8 chunks/s | 4.8 | 4.9 | 5.1 | 5.0 |
| embed on CPU | 18 chunks/s | 22 | 22 | 22 | 23 |
| IFilter (in-process COM) on .docx | ok | ok | ok | ok | ok |
| isolated extractor child process | ok | ok | ok | ok | ok |
| SQLite WAL + sqlite-vec in `%ProgramData%` | ok | ok | ok | ok | ok |

What this says:

1. **Windows Search does not need a user profile or an interactive session.** The OLE DB
   provider and the catalog COM interface work from Session 0 under every identity, and the
   watcher, both GPUs, IFilters and the extractor child all behave as they do interactively.
2. **Windows Search results are security-trimmed to the calling identity.** An account that
   cannot read `C:\Users\pstry\Documents` on NTFS gets zero Search results for it, while the
   OneDrive folder on F: (readable by Users) returns identical counts for everyone. So the
   service sees exactly the files its account can read: no more, no less.
3. **DXGI adapter order is different in Session 0 than on the desktop** (Radeon first instead
   of the RTX 4080). A config written as `dml:0` / `dml:1` from a terminal would have run
   steady-state embedding on the 4080 and bulk work on the integrated GPU the moment it ran
   as a service. This is why roles are resolved by stable adapter identity inside the service
   process, never by ordinal.
4. The `HKCU` failure is irrelevant: nothing in semsearch reads per-user registry, and the
   model cache is redirected to `%ProgramData%\SemSearch\models`.

### Decision

The service runs as the **virtual service account `NT SERVICE\SemSearch`**, and the
installer grants that account read-only access (`(OI)(CI)RX`) on each configured root.

Why this one:

- It is the least-privileged option that preserves the behaviour we need. Everything
  measured above works under it, and Windows Search's security trimming makes its results
  exactly consistent with the NTFS grants, so what the index contains is governed by one
  explicit, auditable ACL per root.
- It has **no password**: the SCM manages the identity, so nothing is stored and nothing
  expires. The user's own account would give the same view of the files, but a service
  logging on as a user needs that user's password in the SCM (and "Log on as a service"),
  and breaks on password change. That option is documented below for people who want
  exactly the interactive view of everything, but it is not the default.
- **LocalSystem was rejected** although it is the most convenient: it reads every file on
  the machine, so a bug anywhere in the extraction path or the API would expose every
  user's documents, and Windows Search would return other users' profiles too. Search
  authority must not imply read authority over the whole machine.
- LocalService and NetworkService behave identically to the virtual account in the
  measurements, but they are shared with other Windows services; a per-service virtual
  account keeps ACL grants attributable to semsearch alone and is removed with it.

Consequence to know about: adding a root that lives inside a user profile (for example
`C:\Users\you\Documents`) requires the read grant, which the installer applies when the root
is listed at install time (`-Roots`), or re-run `install.ps1` after editing the config. The
grant is a read-only ACE for `NT SERVICE\SemSearch`; `uninstall.ps1` removes the account,
which invalidates the ACE.

Alternative: `.\install.ps1 -Account DOMAIN\you` installs the service under your own
account (you will be prompted by the SCM for the password via `sc config`, and the account
needs the "Log on as a service" right). Use this only if you need Search results for
locations you do not want to grant explicitly.

## File layout

| Location | Contents | Written by |
|---|---|---|
| `%ProgramFiles%\SemSearch\python\` | relocatable CPython (python-build-standalone) with semsearch and every dependency (`onnxruntime-directml`, `sqlite-vec`, pywin32, parsers) installed into its own `site-packages`; the service binary is its `pythonw.exe` | installer only |
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

The slow step is the read grant on each root: Windows propagates the inheritable entry to
every object in the tree, which took several minutes on `F:\HexyLab` (hundreds of thousands
of files once virtual environments and `node_modules` are counted). The installer grants the
entry on the root only (no `/T`), so the propagation is the kernel's, not a per-file rewrite.

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

Measured on this workstation on 2026-10-02 against the live install (run as the operator
account, unelevated; root `F:\HexyLab`, initial build in progress at the time):

| check | result |
|---|---|
| service installed, running, `pythonw.exe`, Session 0, account `NT SERVICE\SemSearch` | pass |
| `/health` ok | pass (version 0.2.0) |
| API not reachable on the LAN address; listening on 127.0.0.1 only | pass |
| Host header `evil.example` rejected | pass (421) |
| device roles inside the service | `integrated-gpu -> dml:0` (Radeon), `discrete-gpu -> dml:1` (RTX 4080), `cpu`; note the ordinals are the reverse of the desktop session |
| Windows Search reachable from the service | pass (catalog `processing_notifications`, 282,402 items) |
| watcher active, schema v2, no corruption recovery | pass |
| operator can read the admin token; maintenance call without token | pass / 403 |
| create → searchable | 2 s |
| modify → searchable | < 2 s |
| rename → path updated | < 2 s |
| delete → gone from results | < 2 s |
| `semsearch service restart` as the operator (no elevation) | pass; healthy again in < 2 s with the index intact and identical device resolution |
| event log lifecycle entries, rotating log file | pass |

Bulk indexing in service context ran on the RTX 4080 (`bulk_mode: true`, `dml:1` in the
service's numbering) at 80 to 103 chunks/s, 11 to 13 documents/s, with the process at
below-normal priority. The upgrade path (re-running `install.ps1` over the live install)
kept the index and configuration, skipped the already-present ACL grant, and brought the
service back healthy; the suite of 133 unit and integration tests passes.

Not demonstrated in this session: a full Windows reboot. The service is registered
`AUTO_START` with delayed start and failure recovery, and a cold start was exercised through
`sc start` by the installer, but the reboot itself was left to the operator.

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
