# SemSearch

**Find files on your Windows PC by what they're about, not by what they're called.**

> "the doc where I compared renting vs buying and decided to wait"
> "my notes on why the cache was moved to disk"
> "that PDF about the warranty terms"

SemSearch is a small background service that reads your documents, understands them with a local
embedding model, and lets you (or your AI agent) search them by meaning. It runs entirely on your
machine: no cloud, no account, nothing sent anywhere by SemSearch, and it never modifies your files.

It works *alongside* Windows Search rather than replacing it: Windows supplies the file inventory
and change tracking, SemSearch adds reliable text extraction, a local vector index and hybrid
(meaning + keyword) ranking.

## What you get

- **Search by meaning.** Half-remembered ideas find the right file even when none of your words are in it.
- **Hybrid ranking.** Every result blends semantic similarity, keyword relevance (BM25) and the file name, and shows its scores, so you can see *why* it matched.
- **Lives in the tray.** Status, pause/resume, add or remove folders, a settings page. It starts with the folders Windows Search already indexes.
- **Keeps itself current.** Edits, renames, moves and deletes show up within seconds; unchanged files are never re-processed, and a renamed file keeps its vectors.
- **Made for agents too.** A command-line tool with JSON output, a local HTTP API, and a ready-made **Claude Code skill** (`skills/semsearch/SKILL.md`), so Claude (or any agent that can run a command) can find your documents for you.
- **Uses your GPU sensibly.** After install the tray asks once: *Light* (everyday indexing on the integrated GPU or CPU, the dedicated GPU only for big jobs) or *Dedicated GPU* (everything on the fast card). Searches stay on the CPU, which is quickest for a single query.

**File types:** PDF (with OCR for scanned pages), Word, PowerPoint, Excel, RTF, Outlook `.msg`/`.eml`,
Markdown, plain text, subtitles/transcripts, HTML/XML, JSON/YAML/CSV and source code in most languages.

## Install

1. Download `SemSearch-<version>.zip` from [Releases](https://github.com/PStryder/semsearch/releases/latest) and extract it anywhere.
2. Double-click **`Install.cmd`** and approve the administrator prompt.
3. Answer the tray icon's one question (Light or Dedicated GPU). Indexing starts in the background.

Then search from any terminal:

```
semsearch "the notes where I decided to switch the backup schedule"
semsearch query "*.pdf" --literal              # file-name globs and exact phrases
semsearch query "invoice" --ext pdf --root D:\Finance
semsearch --json query "..."                   # machine-readable, for scripts and agents
```

or use the tray icon > *Open settings page*. Running a newer `Install.cmd` upgrades in place
(your index is kept); `Uninstall.cmd` in `C:\Program Files\SemSearch` removes it.

> The release is not code-signed yet, so Windows SmartScreen may warn on first run
> ("More info" > "Run anyway"). The zip's SHA-256 is published next to it.

### Use it from Claude Code

Copy `skills/semsearch` into `%USERPROFILE%\.claude\skills\`. Claude then reaches for SemSearch
whenever you ask it to find a document, note or file by topic ("find my notes on the Q3 vendor
review"), reads PDFs and Office files through the extracted text, and narrows by folder or type.
Any other agent that can run shell commands can use `semsearch --json query ...` the same way.

## How well does it work?

On a test set of 54 hand-labelled queries over ~1,000 of the author's own files (notes, essays,
specs, PDFs, Word documents, with ~700 code and data files as distractors), 42 of them deliberately
phrased *without* the target document's own words:

| mode | right file ranked first | right file in the top 5 | median query time |
|---|---|---|---|
| keyword only | 85% | 89% | 13 ms |
| meaning only | 83% | 87% | 10 ms |
| **hybrid (default)** | **93%** | **95%** | 24 ms |

That is a small, personal benchmark, not a published one. The harness is in `eval/` so you can
run it on your own files ([docs/evaluation.md](docs/evaluation.md)). On a real index of 28,000
documents a filtered search takes 25 to 90 ms.

## Requirements and limits

- Windows 10 (2004 or later) or 11, x64. About 500 MB on disk for the program, plus the index:
  the author's 28,000 documents (including some very large data exports) make a 3.5 GB index.
- A DirectX 12 GPU is optional; without one everything runs on the CPU.
- The first index takes a while. End to end, about 100 chunks/s on an RTX 4080 and about 14 on a
  modern 8-core CPU, so a big Documents folder can take hours on CPU alone. After that, only
  changes cost anything.
- The bundled model ([BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), MIT) is
  English-first: other languages still work for keyword search, but semantic quality drops.
- Very large data files (JSON, CSV, logs) are fully keyword-searchable, but only their first
  ~2,000 chunks are searchable by meaning.
- OneDrive "online-only" files are skipped rather than downloaded.
- Searching is for the account that installed it (the API is token-protected); per-user
  isolation on shared PCs is not built yet.
- GPU acceleration uses Microsoft's DirectML runtime, whose licence says it may send usage data
  to Microsoft (see `THIRD-PARTY-NOTICES.txt`). SemSearch itself makes no network calls: the model
  ships in the zip.
- Tested so far on one Windows 11 desktop (Ryzen 7 7800X3D, integrated Radeon + RTX 4080). Bug
  reports from other hardware are very welcome.

## How it works

```
Windows Search inventory --+                        +--> SQLite FTS5 (BM25) --+
filesystem watcher --------+--> extract --> chunk --+                         +--> hybrid ranking --> CLI / API / tray / agents
periodic reconcile --------+   (isolated child)     +--> ONNX embeddings -----+
                                                        (bge-small, local)
```

- Runs as a Windows service under its own low-privilege virtual account, which can read only the
  folders you grant it.
- Document parsers run in a separate, memory-capped child process, so a malformed file cannot
  take the service down.
- Files that look like they contain credentials (keys, tokens, `.env`) are skipped by default.
- One SQLite database holds everything; the API listens on `127.0.0.1` only.

Working on the code (or pointing a coding agent at it)? Read [AGENTS.md](AGENTS.md) first.

Details: [architecture](docs/architecture.md) · [configuration](docs/configuration.md) ·
[HTTP API](docs/api.md) · [operations](docs/operations.md) ·
[Windows service internals](docs/windows-service.md) · [what Windows Search can and cannot do](docs/windows-search-findings.md)

## Building from source

```
uv sync --extra dml                                  # or --extra cpu / --extra gpu (one ONNX runtime)
uv run pytest -q                                     # test suite
powershell -File installer\build_release.ps1 -Zip    # self-contained release in dist\
```

For development without the service: `uv run semsearch --init-config > semsearch.yaml`, set
`roots:`, then `uv run semsearch --serve`.

## License

MIT. Third-party components and their licences are listed in `THIRD-PARTY-NOTICES.txt` in every release.
