# Evaluation

The question the harness answers: does semantic retrieval actually find the right file for a
half-remembered concept, and does hybrid ranking beat either signal alone? The answer on this
machine's own documents is yes on every metric, with hybrid as the default.

## Harness

The numbers below come from the author's own documents. Those files, the labelled queries that
name them and the per-query result files stay private (`eval/private/`, gitignored); the
harness, the metrics and the aggregate results are public, and `eval/*.example.yaml` show the
format for running the same evaluation on your own files.

- `eval/private/corpus_manifest.yaml` + `eval/build_corpus.py` copy a corpus out of `F:\HexyLab`
  (sources are never modified; credential-looking filenames are skipped). Current corpus:
  976 files, 8,778 chunks: the loose documents at the top of HexyLab (.md/.txt/.docx/.pdf/.vtt),
  the Medium essay archive, project specs and READMEs, and ~700 source/config/data files as
  distractors.
- `eval/private/queries.yaml`: 54 hand-labelled queries. `relevant` lists path substrings; the labels
  were written from the documents' content, and the *vague* queries deliberately avoid the
  documents' own title words (for example "my chatbot quoted a stale stock price for Palantir"
  → `2025-07-17_latency_of_drift.md`).
  - 42 **vague** (the target use case), 8 **exact** (a phrase or term that does occur), 4 **filename**.
- `eval/run_eval.py` indexes the corpus with the configured provider, runs every query in all
  three modes (cache bypassed, best of 3 timings), and writes `eval/private/results/<stamp>_<model>.{json,md}`.

Metrics: **top-1** (rank-1 result is relevant), **recall@5** (relevant files found in the top 5
over min(|relevant|, 5)), **MRR** (reciprocal rank of the first relevant result, 0 if beyond 20),
query latency p50/p95, and indexing throughput.

## Results: `BAAI/bge-small-en-v1.5`, ONNX, CPU (run `20261002_143208`)

All 54 queries:

| mode | top-1 | recall@5 | MRR | p50 ms | p95 ms |
|---|---|---|---|---|---|
| literal | 83.3% | 88.9% | 0.883 | 22.2 | 53.0 |
| semantic | 83.3% | 86.6% | 0.858 | 6.5 | 7.9 |
| **hybrid** | **92.6%** | **94.0%** | **0.942** | 32.0 | 64.2 |

42 vague queries (the "I remember the idea, not the words" case):

| mode | top-1 | recall@5 | MRR |
|---|---|---|---|
| literal | 78.6% | 85.7% | 0.849 |
| semantic | 85.7% | 87.5% | 0.877 |
| **hybrid** | **90.5%** | **92.3%** | **0.925** |

8 exact-phrase queries: literal and hybrid 100% top-1, semantic 87.5%.
4 filename queries: literal and hybrid 100%, semantic 50% (globs are not semantic queries).

Reading: semantic alone beats literal on vague queries by 7 points top-1 and loses on exact
phrases and names; hybrid keeps the best of both and adds 5 more points on vague queries,
because a document that is merely good on both signals outranks one that is excellent on one.

### Fusion method

Same index, same queries, `retrieval.fusion`:

| fusion | top-1 (all) | MRR (all) | top-1 (vague) | MRR (vague) |
|---|---|---|---|---|
| **convex** (default: weighted min-max normalized scores, 0.6/0.4) | 92.6% | 0.942 | 90.5% | 0.925 |
| rrf (k=60) | 88.9% | 0.913 | 85.7% | 0.888 |

Reciprocal rank fusion discards score magnitudes; with a strong embedding model the cosine gap
between the right document and the runner-up is informative, so convex fusion wins here.

### What changed between the first and second run

Run 1 (`20261002_141826`) scored hybrid 87.0% top-1 / MRR 0.889. Three changes, each motivated
by a specific miss:

1. **Title header in embeddings** — chunks are embedded as `<title>\n\n<chunk>` where the title
   is the first heading or a humanized filename. Documents whose body never restates their
   subject (REPO_MAP, SLICE_ZERO_PROMPT) became reachable. Hybrid vague top-1 88.1% → 90.5%.
2. **Exact filename signal** — a filename containing every query term scores ≥ 0.9 in hybrid
   ("punchlist" went from rank 9 to 1). Filename queries 50% → 100%.
3. **`.vtt`/`.srt` added to text extensions** — the meeting transcript was not indexed at all.

### Remaining misses (hybrid)

| query | first relevant rank | why |
|---|---|---|
| what a language model actually is while it is generating | not in top 20 | `not_just_tokens.md` (25 KB essay) is paraphrased too far for bge-small; the title "What an LLM Is While It's Happening" shares no stem with the query after stopwords |
| overview table describing every folder in the workspace | 6 | code files mentioning folders and tables outrank the two index documents |
| docker container running the ternary model server with reproducible seeds | 5 | "ternary" never appears; the handoff note says BitNet |
| which projects still need receipt ledger and artifact storage hookups | 2 | the LegiVellum README is a legitimate near-match |

A larger model was tried for these; it did not help (next section).

## Indexing throughput (CPU, Ryzen 7 7800X3D)

| | value |
|---|---|
| documents / chunks | 976 / 8,778 |
| wall time (extract + hash + chunk + embed) | 579 s |
| chunks embedded per second | 14.0 |
| documents per second | 1.7 |
| extraction status | 960 ok, 15 empty (templates/placeholders), 0 errors |
| extractors used | text 969, pypdf 3, IFilter (docx) 3 |

Embedding dominates: the microbenchmark gives ~26 chunks/s at 1,800 characters, and real
chunks run up to 2,200 characters (~512 tokens). Extrapolated: 100k documents ≈ 5 hours of CPU
for the first pass, or ~45 minutes on the RTX 4080 (see "Embedding devices" below); afterwards
only changed files cost anything.

## Query latency

Semantic queries are ~6.5 ms (one 8,778 × 384 matrix product plus chunk lookups). Literal is
~22 ms because it also asks Windows Search (`FREETEXT`) and runs filename `LIKE` probes; hybrid
is the sum, ~32 ms p50 / 64 ms p95. Repeated identical queries are served from the response
cache in well under a millisecond. These numbers are for 8.8k chunks; the vector product scales
linearly (≈ 0.4 ms per 10k chunks at 384 dims), so a 500k-chunk index is still well inside a
second.

## Larger model comparison: `BAAI/bge-base-en-v1.5` (run `20261002_145721`)

Same corpus, same queries, same fusion; documents embedded on the RTX 4080 (DirectML), queries on CPU.

| mode | top-1 | recall@5 | MRR | p50 ms |
|---|---|---|---|---|
| literal | 83.3% | 88.9% | 0.883 | 25.0 |
| semantic | 79.6% | 80.9% | 0.830 | 15.4 |
| hybrid | 88.9% | 89.7% | 0.916 | 42.8 |

bge-base is **worse** than bge-small on this corpus (hybrid 88.9% vs 92.6% top-1, semantic
79.6% vs 83.3%), costs 3.5x the embedding time and 2x the vector memory, and doubles query
latency. It fixed none of the four remaining misses listed above and introduced two new ones.
bge-small-en-v1.5 stays the default; the harness is the way to re-test this if the corpus or a
candidate model changes.

## Embedding devices (DirectML)

Same bge-small index built twice, once with CPU embedding and once on the RTX 4080 through the
DirectML provider (`--device dml:0 --query-device cpu`, run `20261002_145520`):

| document device | indexing wall time (976 docs, 8,778 chunks) | overall chunks/s | hybrid top-1 / MRR |
|---|---|---|---|
| CPU (16 threads) | 579 s | 14.0 | 92.6% / 0.942 |
| RTX 4080 (`dml:0`) | 86 s | 94.8 | 92.6% / 0.942 |

Identical retrieval results: vectors from CPU, the 4080 and the integrated Radeon agree to
within 1e-7 (fp32 everywhere), so an index can be built on one device and maintained on another.

Microbenchmark per device (1,800-character chunks, batch 8, no other load):

| device | bge-small chunks/s | bge-base chunks/s | single-query latency (bge-small) |
|---|---|---|---|
| CPU | 26.1 | 7.6 | 2.9 ms |
| integrated Radeon (`dml:1`, Ryzen 7 7800X3D) | 6.9 | 3.3 | 8.0 ms |
| RTX 4080 (`dml:0`) | 254 (batch 32) | 156 (batch 32) | 13.9 ms |

Interpretation for this workstation:

- **Steady state belongs on the integrated GPU.** After the first pass the indexer sees a
  trickle of changed files; at ~7 chunks/s a modified 10-chunk document is re-embedded in
  about 1.5 s, which is within the watcher's own settle time, and the CPU is not touched at all.
  It is 4x slower than the CPU, so it is the wrong device for a backlog.
- **Bulk belongs on the 4080** (or the CPU if the discrete GPU is busy): 6.7x faster than CPU
  end to end on the real pipeline, limited by extraction and hashing rather than the model.
- **Queries belong on the CPU**: a single short text is latency-bound and the CPU wins
  (2.9 ms vs 8 ms on the iGPU vs 14 ms launch overhead on the 4080).

This is what `embedding.device: dml:1`, `bulk_device: dml:0`, `query_device: cpu` encodes; the
indexer flips to the bulk device automatically during full builds or when more than
`bulk_threshold` jobs are pending.
