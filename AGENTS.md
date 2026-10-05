# AGENTS.md

Guidance for coding agents (and people) working **on** this repository. To *use* SemSearch from
an agent, see `skills/semsearch/SKILL.md` instead.

SemSearch is a Windows service: a semantic + full-text index of the user's files, sidecar to
Windows Search. Python 3.11+ (releases ship CPython 3.12.12), managed with `uv`. Start with
`README.md`, then `docs/architecture.md` (module map, identity model, storage, security posture).

## Commands

```
uv sync --extra dml                       # exactly ONE ONNX runtime extra: dml | cpu | gpu (plus optional ocr)
uv run pytest -q                          # full suite (~40 s); Windows-only tests skip elsewhere
uv run pytest -q tests/test_x.py -k name  # one area
uv run python eval/run_eval.py --device dml:0   # retrieval quality; needs a corpus (see Evaluation)
powershell -File installer\build_release.ps1 -Zip   # self-contained release -> dist\SemSearch-<version>[.zip]
```

Installing a release (`dist\SemSearch-<v>\Install.cmd`, or `install.ps1` from an elevated
PowerShell) replaces the machine's running service. Do it only when the user asks: it needs their
UAC approval and restarts their index.

## Rules that are not optional

**A test must fail when its guarantee is removed.** Every fix and feature is checked by
mutation: apply a one-line change that removes the guarantee, run the relevant tests, confirm
they fail, restore the bytes. A test that passes either way is not a test; fix it before moving
on, and test at the layer where the guarantee actually lives (API auth, not the helper behind it).

**Keep docs in sync.** Behaviour, settings, endpoints and installer steps are documented in
`docs/` and `README.md`; a change that alters them updates them in the same commit. Numbers in
the docs are measured, never estimated: if you cannot measure it, do not write it.

**Version** lives in two places: `pyproject.toml` and `src/semsearch/__init__.py` (then `uv lock`).

## Windows and PowerShell traps (each one has bitten this repo)

- `.ps1` files must be **pure ASCII**: Windows PowerShell 5.1 reads them as ANSI, and one em dash
  breaks parsing. `.ps1` and `.cmd` are CRLF (`.gitattributes`).
- PowerShell 5.1: no `&&`, `||`, ternary or `??`. Variable names are case-insensitive, so a
  `$zip` local silently collides with a `[switch]$Zip` parameter.
- `Remove-Item -Recurse` in 5.1 follows directory junctions into their targets; use the
  installer's `Remove-Tree` (rmdir) helper.
- An elevated process (`Start-Process -Verb RunAs`) does **not** inherit the caller's environment
  variables; pass data through a file or arguments.
- Do not push content with backslashes, regexes or nested quotes through shell heredocs or
  `python -c`: escapes get rewritten (a `\a` once became a BEL character in a shipped file).
  Write files with a real editor or tool, or write a script file first and run it.
- The `.cmd` wrappers run twice (unelevated, then elevated after relaunching themselves): test
  both passes. A downloaded release carries the internet-zone mark, and every relaunch of a marked
  file shows Windows' security warning again, which is why the unelevated pass unblocks first.

## Architecture invariants

- **File identity** is the NTFS file id (`os.stat().st_ino`) plus the volume serial, stored as
  TEXT (they overflow SQLite INTEGER). `System.Search.EntryID` is NOT stable across rename/move.
- **Deletes are tombstone-then-purge**, so a rename noticed late still reuses the vectors.
- **Store:** one writer connection serialized by `Store.lock`, one read-only connection per
  thread (WAL). Writes go through the store's methods / `_tx()`; never `conn.commit()` from
  outside, which could commit another mutation's half-built transaction. `Store.version` is
  bumped only after COMMIT (the response cache keys on it).
- **Preprocessing identity:** anything that changes extracted text, chunking or what gets
  embedded must change the corresponding identity (`preprocess_id` / `coverage_id` / the
  embedding fingerprint) so existing documents are re-processed on upgrade, and nothing else is.
- **Devices** are named by stable adapter identity (vendor/device/subsys/PCI address), never by
  DirectML ordinal: in Session 0 the DXGI order is reversed from the desktop's.
- **Session 0 service host:** runs the real interpreter (`pythonw.exe -s`), never a venv launcher
  (the SCM treats it as a foreign process); `sys.stdout` is `None`; user site-packages must stay
  disabled (`-s`) or they mask missing dependencies.

## Security posture (do not weaken without saying so)

- The API is loopback-only and token-gated, reads included (`api.read_token: true`). The admin
  token rotates at every service start. Clients send it only after verifying that the listener's
  PID is the service's PID (`clientauth.py`).
- Document parsers run in an isolated, memory-capped child process that replies in JSON (never
  pickle: a parser exploit must not become code execution in the service).
- Paths are re-checked after extraction (swap, reparse point, resolves outside the roots); there
  is deliberately no reparse cache.
- A new root added through the API must carry an explicit, non-inherited read grant for the
  service's own SID: proof that the folder's owner chose to share it.
- `semsearch.yaml` is edited textually (`config_edit.py`), refusing control characters and
  refusing to write unless the result parses back to exactly the requested change.
- Files that look like credentials are skipped; the allow list is `indexing.secret_scan_allow`.

## Releases

- Inputs are pinned: `uv export --require-hashes`, exact CPython, the default model at a fixed
  revision, bundled as plain files. `release-manifest.json` hashes every file, and `install.ps1`
  verifies the whole release before touching anything.
- Never patch files inside a built or extracted release: the manifest check rejects it (by
  design). Change the source and rebuild.
- The installer is transactional (rollback on failure) and its re-run is the upgrade path. It
  writes `semsearch.yaml` only for a new install; later setting changes go through the API/tray.

## Evaluation

`eval/run_eval.py` and `eval/build_corpus.py` read their data from `eval/private/` when it exists
(gitignored), else the `*.example.yaml` files show the format. The author's labelled queries name
private documents: **never commit `eval/private/`**, never copy its contents into docs, issues or
commit messages. Aggregate metrics in `docs/evaluation.md` are fine. Re-run the eval after any
change to chunking, embedding or ranking, and record the run it came from.

## Commits

Small, single-purpose commits whose message says what changed, why, and how it was verified
(tests added, mutations caught, measurements). Do not commit `dist/`, `.venv/`, indexes,
`semsearch.yaml`, tokens or anything under `eval/private/`.
