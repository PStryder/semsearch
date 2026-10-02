"""Evaluation harness: index eval/corpus, run eval/queries.yaml in every mode, report metrics.

Metrics (per mode and per query kind):
  top1        fraction of queries whose rank-1 result is relevant
  recall@5    mean over queries of |relevant found in top 5| / min(|relevant|, 5)
  mrr         mean reciprocal rank of the first relevant result (0 if not in top 20)
  latency     p50 / p95 query latency in ms
Indexing throughput (docs/s, chunks/s) and embedding details are recorded as well.

Usage:
  uv run python eval/run_eval.py                      # bge-small (default config)
  uv run python eval/run_eval.py --model BAAI/bge-base-en-v1.5
  uv run python eval/run_eval.py --reuse              # skip re-indexing if the eval index exists
Results: eval/results/<timestamp>_<model>.json and .md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from semsearch.app_state import AppState  # noqa: E402
from semsearch.config import Config  # noqa: E402
from semsearch.logging_setup import setup_logging  # noqa: E402

CORPUS = os.path.join(HERE, "corpus")
MODES = ["literal", "semantic", "hybrid"]


def rel(path: str) -> str:
    return os.path.relpath(path, CORPUS).replace("\\", "/").lower()


def is_relevant(path: str, relevant: list[str]) -> bool:
    r = rel(path)
    return any(x.lower() in r for x in relevant)


def pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def run(args) -> dict:
    cfg = Config(roots=[CORPUS], data_dir=os.path.join(HERE, "index_" + args.model.replace("/", "_")))
    cfg.embedding.model = args.model
    cfg.embedding.provider = args.provider
    cfg.embedding.device = args.device
    cfg.embedding.query_device = args.query_device
    cfg.indexing.watch_filesystem = False
    cfg.indexing.use_windows_search = not args.no_windows_search
    cfg.retrieval.fusion = args.fusion
    cfg.retrieval.semantic_weight = args.semantic_weight
    cfg.retrieval.lexical_weight = 1.0 - args.semantic_weight
    setup_logging(None, "WARNING")
    st = AppState(cfg, start_indexer=False, isolate_extractors=True)
    out: dict = {"model": st.embedder.fingerprint, "device": st.embedder.device, "corpus": CORPUS, "fusion": args.fusion,
                 "weights": {"semantic": cfg.retrieval.semantic_weight, "lexical": cfg.retrieval.lexical_weight},
                 "windows_search_inventory": st.windows is not None, "started": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        stats = st.store.stats()
        if not args.reuse or stats["documents"] == 0:
            print(f"indexing {CORPUS} with {st.embedder.fingerprint} on {st.embedder.device} ...")
            t0 = time.time()
            st.indexer.full_build()
            n = 0
            while True:
                job = st.store.next_job()
                if job is None:
                    break
                try:
                    st.indexer.process_job(job)
                except Exception as e:  # noqa: BLE001
                    st.store.fail_job(int(job["id"]), str(e), 3)
                n += 1
                if n % 100 == 0:
                    print(f"  {n} jobs ...", flush=True)
            dt_index = time.time() - t0
            c = st.indexer.state
            stats = st.store.stats()
            out["indexing"] = {"seconds": round(dt_index, 1), "jobs": n, "documents": stats["documents"], "chunks": stats["chunks"],
                               "docs_per_s": round(c.docs_indexed / max(dt_index, 1e-6), 2), "chunks_per_s": round(c.chunks_embedded / max(dt_index, 1e-6), 2),
                               "extract_ms_total": round(c.extract_ms), "embed_ms_total": round(c.embed_ms),
                               "by_status": stats["by_extract_status"], "by_method": stats["by_extract_method"], "failed": c.docs_failed,
                               "source": st.indexer.state.sources}
            print(f"indexed {stats['documents']} docs / {stats['chunks']} chunks in {dt_index:.0f}s "
                  f"({out['indexing']['docs_per_s']} docs/s, {out['indexing']['chunks_per_s']} chunks/s)")
        else:
            out["indexing"] = {"reused": True, "documents": stats["documents"], "chunks": stats["chunks"]}
            print(f"reusing index: {stats['documents']} docs / {stats['chunks']} chunks")

        queries = yaml.safe_load(open(os.path.join(HERE, "queries.yaml"), encoding="utf-8"))["queries"]
        per_query = []
        # warm up (model + caches)
        st.retriever.search("warm up", "hybrid", 5)
        for q in queries:
            row = {"q": q["q"], "kind": q["kind"], "relevant": q["relevant"], "modes": {}}
            for mode in MODES:
                lat = []
                res = None
                for _ in range(args.repeats):
                    t0 = time.perf_counter()
                    res = st.retriever.search(q["q"], mode, 20, cache=False)
                    lat.append((time.perf_counter() - t0) * 1000)
                hits = res["results"]
                ranks = [i + 1 for i, h in enumerate(hits) if is_relevant(h["path"], q["relevant"])]
                top5_hits = len({rel(h["path"]) for h in hits[:5] if is_relevant(h["path"], q["relevant"])})
                n_rel = min(len(q["relevant"]), 5)
                row["modes"][mode] = {
                    "top1": bool(ranks and ranks[0] == 1),
                    "first_rank": ranks[0] if ranks else None,
                    "recall5": min(1.0, top5_hits / n_rel),  # directory-prefix labels can match more files than listed
                    "mrr": (1.0 / ranks[0]) if ranks else 0.0,
                    "latency_ms": round(min(lat), 1),
                    "top": [(rel(h["path"]), h["match_type"], h["score"]) for h in hits[:3]],
                }
            per_query.append(row)

        summary = {}
        kinds = sorted({q["kind"] for q in queries}) + ["all"]
        for mode in MODES:
            summary[mode] = {}
            for kind in kinds:
                rows = [r for r in per_query if kind == "all" or r["kind"] == kind]
                if not rows:
                    continue
                m = [r["modes"][mode] for r in rows]
                lats = sorted(x["latency_ms"] for x in m)
                summary[mode][kind] = {
                    "n": len(rows),
                    "top1": sum(x["top1"] for x in m) / len(m),
                    "recall5": sum(x["recall5"] for x in m) / len(m),
                    "mrr": sum(x["mrr"] for x in m) / len(m),
                    "p50_ms": lats[len(lats) // 2],
                    "p95_ms": lats[min(len(lats) - 1, int(len(lats) * 0.95))],
                }
        out["summary"] = summary
        out["per_query"] = per_query
    finally:
        st.close()
    return out


def write_report(out: dict, path_md: str) -> None:
    lines = [f"# semsearch evaluation", "", f"- model: `{out['model']}` on {out['device']}", f"- corpus: `{out['corpus']}`",
             f"- fusion: {out['fusion']} (weights {out['weights']})", f"- Windows Search used as inventory: {out['windows_search_inventory']}",
             f"- run: {out['started']}", ""]
    ix = out.get("indexing", {})
    if ix:
        lines += ["## Indexing", "", "| documents | chunks | seconds | docs/s | chunks/s | failed |", "|---|---|---|---|---|---|",
                  f"| {ix.get('documents')} | {ix.get('chunks')} | {ix.get('seconds', '-')} | {ix.get('docs_per_s', '-')} | {ix.get('chunks_per_s', '-')} | {ix.get('failed', '-')} |", ""]
        if "by_status" in ix:
            lines += [f"- extraction status: `{ix['by_status']}`", f"- extraction methods: `{ix['by_method']}`", ""]
    lines += ["## Retrieval quality", ""]
    for kind in ["all", "vague", "exact", "filename"]:
        lines += [f"### {kind}", "", "| mode | n | top-1 | recall@5 | MRR | p50 ms | p95 ms |", "|---|---|---|---|---|---|---|"]
        for mode in MODES:
            s = out["summary"][mode].get(kind)
            if s:
                lines.append(f"| {mode} | {s['n']} | {pct(s['top1'])} | {pct(s['recall5'])} | {s['mrr']:.3f} | {s['p50_ms']} | {s['p95_ms']} |")
        lines.append("")
    lines += ["## Per query (first relevant rank per mode; - = not in top 20)", "", "| kind | query | literal | semantic | hybrid |", "|---|---|---|---|---|"]
    for r in out["per_query"]:
        cells = [str(r["modes"][m]["first_rank"] or "-") for m in MODES]
        lines.append(f"| {r['kind']} | {r['q'][:70]} | {' | '.join(cells)} |")
    lines.append("")
    with open(path_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    ap.add_argument("--provider", default="onnx")
    ap.add_argument("--device", default="cpu", help="cpu | cuda[:n] | dml[:n] | auto (documents); queries use --query-device")
    ap.add_argument("--query-device", default="same")
    ap.add_argument("--fusion", default="convex", choices=["convex", "rrf"])
    ap.add_argument("--semantic-weight", type=float, default=0.6)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--no-windows-search", action="store_true")
    args = ap.parse_args()
    if not os.path.isdir(CORPUS):
        print("corpus missing: run eval/build_corpus.py first", file=sys.stderr)
        return 2
    out = run(args)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = args.model.replace("/", "_") + ("_" + args.fusion if args.fusion != "convex" else "") + ("_" + args.device.replace(":", "") if args.device != "cpu" else "")
    base = os.path.join(HERE, "results", f"{stamp}_{tag}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    write_report(out, base + ".md")
    print()
    for kind in ["all", "vague", "exact", "filename"]:
        print(f"[{kind}]")
        for mode in MODES:
            s = out["summary"][mode].get(kind)
            if s:
                print(f"  {mode:<9} n={s['n']:2d} top1={pct(s['top1'])} recall@5={pct(s['recall5'])} mrr={s['mrr']:.3f} p50={s['p50_ms']}ms p95={s['p95_ms']}ms")
    print(f"\nwritten: {base}.md / .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
