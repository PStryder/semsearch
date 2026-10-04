# Windows Search API findings

Machine examined: Windows 11 Pro for Workstations 25H2, build 26200.9457, indexer
10.0.26100.9444, AMD Ryzen 7 7800X3D, RTX 4080 (16 GB), no NPU. All probes were run
without elevation from the interactive user account. The probe scripts live in
`probes/` and are reproducible.

## Summary of what can be reused

| Capability | Available? | How | Verdict |
|---|---|---|---|
| Indexed file paths | Yes | OLE DB `Search.CollatorDSO`, `SELECT System.ItemUrl ... WHERE SCOPE='file:F:/...'` | **Reuse** as the primary inventory source |
| Stable item identity | Partly | `System.Search.EntryID` exists but is **re-issued on rename or move** | Do not rely on it. NTFS file id (`os.stat().st_ino`) survives rename/move within a volume and is used instead |
| Filename, extension, type | Yes | `System.FileName`, `System.FileExtension`, `System.ItemType`, `System.ItemFolderPathDisplay` | Reuse |
| Timestamps, size | Yes | `System.DateModified`, `System.DateCreated`, `System.Size` | Reuse (cross-checked with `os.stat` at extraction time) |
| Extracted text | No | `System.Search.Contents` is query-only; `System.Search.AutoSummary` was empty for every sampled file | Sidecar extracts its own text |
| Full-text relevance | Partial | `CONTAINS(System.Search.Contents, ...)`, `FREETEXT(...)`, `System.Search.Rank` | Reused as one lexical signal, but content coverage is unreliable (see below) |
| Incremental change info | Yes, limited | `System.Search.GatherTime` is queryable and orderable; add/rename/delete become visible in 1 to 2 s | Reuse for add/modify detection. Deletes are only visible as absence, so a reconciliation pass is required |
| Catalog status, counts, roots, scope rules | Yes | `ISearchManager` / `ISearchCatalogManager` / `ISearchCrawlScopeManager` COM (hand-declared via comtypes) | Reuse for `/status` and for deciding whether a root is covered by Windows Search |
| Native semantic or embedding search | **No** | `HKLM\...\WindowsAI\LastConfiguration\HardwareCompatibility = 0`; machine is not Copilot+ class; no public WinRT file-embedding API shipped | Sidecar provides embeddings |
| USN change journal | No (unelevated) | `fsutil usn queryjournal` works, `CreateFile(\\.\F:)` is Access denied | Not used; would require a service running elevated |
| IFilter text extractors | Yes | `LoadIFilter` from `query.dll`; PDF filter needs `IInitializeWithStream` | Reused for Office formats; see per-format notes |

## 1. Query interface: OLE DB `Search.CollatorDSO`

Connection string: `Provider=Search.CollatorDSO;Extended Properties='Application=Windows';`.
Reachable from .NET `System.Data.OleDb` and from Python via pywin32 `ADODB.Connection`.

Measured behaviour (probe_contents.py, probe_paging.py):

- Enumerating every item under `F:\HexyLab` (92,191 items) with three columns takes about
  15 s, roughly 6,000 items/s. Server and client cursors perform the same.
- `ORDER BY System.ItemUrl` with a `System.ItemUrl > 'last'` predicate works, so keyset
  paging is possible. `ORDER BY System.Search.GatherTime` with a `GatherTime > '...'`
  predicate also works and is the incremental hook.
- `COUNT(*)`, `GROUP BY`, `System.IsFolder = 1`, bitwise `FileAttributes & 16` are all
  rejected. Folders are selected with `System.ItemType = 'Directory'`. `DIRECTORY='...'`
  gives a shallow listing, `SCOPE='...'` a deep one.
- A seven-column unordered enumeration failed mid-rowset with 0x80041607 once; narrower
  column lists and keyset paging did not reproduce it. The adapter pages by `ItemUrl`.
- Point lookups by `System.ItemUrl = 'file:F:/...'` return in about 10 ms.

Change latency measured by creating, modifying, renaming, moving and deleting a file
under an indexed scope (probe_change_detection.py):

| Event | Visible in SystemIndex after |
|---|---|
| Create | 1.0 s |
| Modify (size change) | immediate on next poll |
| Rename | 2.0 s (old path gone immediately) |
| Move to subfolder | 1.0 s |
| Delete | 1.0 s |

`System.Search.EntryID` changed on the rename (322808 to 322809) and again on the move
(322811). The NTFS file id from `os.stat` was unchanged across both. Conclusion: use path
as the primary key, NTFS `(volume, file id)` as the move detector, content hash as the
re-embedding guard.

## 2. Full-text content in the Windows index is not trustworthy on this machine

`CONTAINS(System.Search.Contents, word)` returned hits for `.docx`, `.pdf` and `.txt`
files under `C:\Users\me\Documents`, but **zero** hits for `.txt`, `.docx`, `.pdf`
and `.xlsx` under `F:\HexyLab`, including a 58 KB text file that was gathered in March
and definitely contains the probe word. A freshly created `.txt` under `F:\HexyLab`
became visible by path in 1 s but its content token was still not searchable after
120 s. The Application event log has many `Microsoft-Windows-Search` 10024 events:
"The filter host process ... did not respond and is being forcibly terminated".

Separately, these extensions have no IFilter registered at all (`PersistentHandler`
absent), so Windows never indexes their content regardless of health: `.md`,
`.markdown`, `.py`, `.ts`, `.rs`, `.go`, `.ps1`, `.sh`, `.json`, `.yaml`, `.yml`,
`.toml`, `.log`, `.epub`. These are the bulk of what the user wants to search.

Decision: Windows full-text rank is consumed as an optional lexical signal when present,
but the sidecar extracts its own text for every document and maintains its own lexical
index (SQLite FTS5) so that literal search is complete.

## 3. Reusing the installed IFilters

The indexer's own extractors can be loaded in-process (probe_ifilter.py,
probe_pdf_stream.py). Registered on this machine:

| Extension | Filter | Loads via `LoadIFilter`? | Notes |
|---|---|---|---|
| `.txt .csv .ini .js .cs .cpp .c .h .java` | Plain Text filter, `query.dll` | Yes | Not needed, the sidecar reads text directly with encoding detection |
| `.docx` | Office Open XML Word, `OFFFILTX.DLL` (Click-to-Run VFS path) | Yes, 36 ms for 12.5 KB of text | Reused |
| `.xlsx` | Office Open XML Excel | Yes, 103 ms / 159 KB | Reused; emits one cell per line |
| `.pptx` | Office Open XML PowerPoint | Registered, no sample available locally | Reused with python-pptx fallback |
| `.doc .xls .ppt` | Legacy Office filter, `OffFilt.dll` | Registered | Reused (no Python fallback for legacy binaries) |
| `.pdf` | "Reader Search Handler", `Windows.Data.Pdf.dll` | `LoadIFilter` fails (E_NOINTERFACE); works via `CoCreateInstance` + `IInitializeWithStream` + `IFilter` | Output drops inter-word spaces for the sampled paper. pypdf produced correctly spaced text for the same file in 136 ms for 5 pages, so pypdf is primary and the Windows filter is the fallback |
| `.rtf` | `rtffilt.dll` | Registered | Reused |
| `.html .htm` | `nlhtml.dll` | Yes | Reused |
| `.xml` | `xmlfilter.dll` | Registered | Sidecar reads XML as text instead |
| `.msg .eml` | Office MSG filter, `mimefilt.dll` | Registered | Available for later |

`LoadIFilter`, `LoadIFilterEx`, `BindIFilterFromStream` and `BindIFilterFromStorage`
are all exported by `query.dll`.

## 4. Management COM API

`CSearchManager` (`{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}`) works from an unelevated
process for read operations. There is no registered type library, so the interfaces are
declared by hand (probe_search_api.py; the vtable order follows `searchapi.h`).

Returned on this machine: catalog status `PROCESSING_NOTIFICATIONS`, 427,288 items,
roots `file:///C:\` and `file:///F:\` (plus mail, OneNote, winrt roots), and the full
exclusion rule list (Program Files, AppData, dot-directories, many developer trees such
as `llama.cpp`, `ollama`, `ComfyUI`). `GetURLIndexingState` returns E_NOTIMPL.
`IncludedInCrawlScope` accepted `file:///F:\...` and `file:///F:/...` URL forms but
returned `True` for paths that the rule list excludes, so it is not used to decide
coverage. Coverage is decided by querying SystemIndex for the root itself.

### Crawl scope rules as a configuration source (2026-10-04)

The rule set is also mirrored in the registry under
`HKLM\SOFTWARE\Microsoft\Windows Search\CrawlScopeManager\Windows\SystemIndex\{DefaultRules,WorkingSetRules}`,
readable by any account. Each rule is a `file:///X:\[<id>]\path\` URL with `Include`,
`NoContent` (properties only) and `Default` flags. On this machine the whole volumes are
included `NoContent` (that is why filename search works everywhere) and a short list of
folders is content-indexed; 202 working-set exclusions are the user's own. semsearch reads
this (`inventory/scope.py`) to seed its roots and excludes. The bracketed id is not the
volume GUID (`GetVolumeNameForVolumeMountPoint`), not the NTFS serial and not the NTFS
object id (all three compared); it is internal to the indexer, so rules are matched by the
drive letter they were written with, guarded by the letter being mounted and, for letters
that have carried several volumes, the path existing.

## 5. Semantic search in Windows itself

Windows 11 24H2/25H2 ships semantic file search only on Copilot+ PCs (NPU with 40+
TOPS). This machine reports `HardwareCompatibility = 0`; `WSAIFabricSvc` is running and
`Microsoft.AIFabric.CBS` is installed, but no file-embedding or semantic query API is
exposed to applications, and the Settings toggle for semantic indexing is absent. Windows
ML (`onnxruntime.dll`, `winml.dll` in System32) is an inference runtime, not a search
API, and the pip `onnxruntime` package is functionally equivalent for this purpose.

## 6. What the sidecar therefore does

- Inventory and metadata: Windows Search OLE DB (deep `SCOPE` enumeration, keyset
  paged). Direct filesystem crawl only for roots that are not in the Windows index.
- Change detection: poll `System.Search.GatherTime > checkpoint` for adds, modifies and
  renames; periodic reconciliation of the full ItemUrl set for deletes; NTFS file id to
  turn rename/move into a cheap re-point instead of a re-embed; content hash to skip
  unchanged bytes; chunk-text hash to skip re-embedding identical chunks.
- Text: own extractors (plain text family, pypdf, IFilter for Office, python-docx /
  python-pptx / openpyxl fallbacks).
- Lexical: SQLite FTS5 over extracted chunks plus filename match, with Windows
  `FREETEXT` rank folded in as an additional component when Windows has content.
- Semantic: local ONNX embedding model, vectors in SQLite via sqlite-vec with an
  in-memory matrix cache for fast repeated queries.
- Nothing in this design requires elevation, modifies the Windows index, or writes to
  source files.
