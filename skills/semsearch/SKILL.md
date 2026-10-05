---
name: semsearch
description: Search the user's local files by meaning with the SemSearch service (semantic + lexical hybrid search over F:\HexyLab, Documents, Downloads). Use when asked to find a document, note, spec, essay, email export, PDF, or code file by topic, half-remembered phrasing, or filename ("the doc where I discussed...", "find my notes on...", "which file mentions...", "*.pdf about X"). Works even when the exact words are not in the file. Read-only; never modifies files.
---

# SemSearch: find local files by meaning

A Windows service keeps a semantic + full-text index of the user's configured folders and
answers on `http://127.0.0.1:8765`. The `semsearch` command (on PATH; `"C:\Program Files\SemSearch\semsearch.cmd"`
if a shell predates the install) is the easiest way to use it. It is read-only toward files.

## Quick use

```
semsearch "the document where I discussed why agents should not perform destructive actions"
semsearch query "GPU memory architecture" --semantic -n 5
semsearch query "Tavoliere consensus" --literal
semsearch query "*.pdf" --literal -n 50                 # filename glob
semsearch query "receipts constitutive" --ext md -n 10 --root F:\HexyLab\LV_Stack
semsearch --json query "..."                           # machine-readable (preferred for agents); --json goes before the subcommand
```

Or over HTTP. Every read needs the admin token header (the `semsearch` command adds it by itself,
after checking it is talking to the real service, so prefer the command):

```
GET  http://127.0.0.1:8765/search?q=<url-encoded>&mode=hybrid&limit=10[&ext=md,pdf][&root=F:\HexyLab]
POST http://127.0.0.1:8765/search   {"query": "...", "mode": "hybrid", "limit": 10, "extensions": ["md"], "roots": ["F:\\HexyLab"]}
GET  http://127.0.0.1:8765/document?path=<full path>&chunks=true     # extracted text of an indexed file (PDF/DOCX too);
                                                                      # paged: &offset=0&limit=50 (chunk_count says how many)
```

Header: `X-SemSearch-Token: <contents of %ProgramData%\SemSearch\state\admin.token>`. The token
changes at every service restart: read the file for each request, never cache it, and never
put it in a URL or paste it anywhere.

## Choosing a mode

| mode | use when |
|---|---|
| `hybrid` (default) | almost always: a concept, a half-remembered idea, a mix of topic and a distinctive word |
| `--semantic` | pure concept, paraphrase, no distinctive terms; also to find *related* material |
| `--literal` | exact phrase, identifier, acronym, filename, or a glob like `*.docx`; fastest and exact |

Phrase queries the way the *document* would say it, not as a question. Drop filler like
"find the file where I" (the ranker ignores stop words but the embedding still sees them).
Try two phrasings if the first top-5 looks wrong; queries cost 10 to 300 ms.

## Reading results

`semsearch --json query "..."` (or the HTTP endpoint) returns `results[]` with, per hit:

- `path`, `filename`, `file_type`, `modified`, `size`
- `score` (0..1), `match_type` (`both` | `semantic` | `lexical` | `filename`)
- `scores`: `semantic` (cosine of the best chunk), `lexical` (normalized BM25), `filename`, `semantic_rank`, `lexical_rank`
- `excerpt`: the best-matching chunk, windowed around the first matching term
- `chunk_ordinal`: which chunk of the file matched (chunks are ~1400 characters in document order)
- `duplicates`: other paths holding byte-identical content (shown once; every copy is still indexed)
- `--root` / `--ext` filters are applied while candidates are collected, so a narrow folder or
  type is never crowded out by a popular one; use them freely
- `why`: human-readable reasons

Guidance:

- `match_type: both` with `score >= 0.7` is a confident hit; a lone `semantic` hit around 0.55 to 0.65 is "related, verify".
- Several hits from one folder usually mean the topic lives there; search again with `--root` to narrow.
- The excerpt is enough to decide relevance. To use the content, open the file with your file
  tools (Markdown, code, text) or call `/document?path=...&chunks=true` for PDFs and Office files,
  which returns the extracted text in order.
- `candidates` in the response is how many documents matched at all; `0` with a sensible query
  usually means the topic is outside the indexed roots or the index is still building.

## Scope and freshness

- Indexed roots (check `semsearch status`): `F:\HexyLab`, `F:\Documents`, `F:\Downloads`. Virtual
  environments, `node_modules`, `.git`, build output and credential-looking files are excluded.
  Nothing outside the roots is ever returned.
- Changes propagate within seconds (watcher) and a full reconcile runs hourly. A brand-new file
  may take a few seconds to appear; a result for a deleted file disappears within seconds.
- During an initial build `semsearch stats` shows `queue.pending > 0`; results are then partial.

## Health and troubleshooting (do this before concluding "not found")

```
semsearch health            # {"ok": true, "documents": N, ...}
semsearch status            # indexer state, queue, roots, resolved GPUs
semsearch service status    # Windows service state; `semsearch service start` if stopped
semsearch logs -n 50
```

If the API is unreachable, the service is stopped: `semsearch service start` (no elevation needed
for the operator account). Do not restart or reindex just because a query returned nothing.

## Changing what is indexed (only when the user asks)

`semsearch roots` lists the indexed folders; `semsearch roots add <folder>` / `remove <folder>`
change them live (the add grants the service read access on the folder, so run it as the user);
`semsearch scope` shows what Windows Search indexes for content and `semsearch roots import-windows`
adopts it. The user also has a tray icon and `http://127.0.0.1:8765/ui` for this.

## What not to do

- Do not call maintenance endpoints (`/reindex`, `/remove/path`, `/index/path`, `/indexer/*`) or
  `semsearch reindex | rebuild | remove | pause` unless the user explicitly asks; they change the index
  and need the admin token. Search authority is not write authority over the user's files.
- Do not paste large excerpts of personal documents into places the user did not ask for; quote the
  path and the relevant lines.
- Do not try to reach the API from another machine or bind it elsewhere; it is loopback-only by design.
