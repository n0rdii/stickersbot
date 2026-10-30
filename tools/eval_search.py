#!/usr/bin/env python3
"""Offline search eval: hit@k, MRR, recall@k and nDCG@k over a labelled query set."""
import argparse
import json
import logging
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))

import bot as botmod  # noqa: E402  (bot/bot.py)


def ranked_packs(results: list[dict]) -> list[str]:
    out: list[str] = []
    for r in results:
        pk = (r.get("set_name") or "").casefold()
        if pk and pk not in out:
            out.append(pk)
    return out


def query_metrics(results: list[dict], expected, k: int) -> dict:
    exp = {p.casefold() for p in expected}
    packs = ranked_packs(results)[:k]
    first = next((i for i, p in enumerate(packs) if p in exp), None)
    dcg = sum(1.0 / math.log2(i + 2) for i, p in enumerate(packs) if p in exp)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(exp), k)))
    return {"hit": 1.0 if first is not None else 0.0,
            "rr": 1.0 / (first + 1) if first is not None else 0.0,
            "recall": len(set(packs) & exp) / len(exp) if exp else 0.0,
            "ndcg": dcg / idcg if idcg else 0.0}


def load_labels(path) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"{e} - copy evals/queries.example.jsonl to {path} and fill it in")
    queries: list[dict] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            raise SystemExit(f"{path}:{n}: {e}")
        if not isinstance(row.get("query"), str) or not row["query"].strip():
            raise SystemExit(f"{path}:{n}: no 'query'")
        if not isinstance(row.get("packs"), list) or not row["packs"]:
            raise SystemExit(f"{path}:{n}: 'packs' must be a non-empty list")
        queries.append({"query": row["query"], "packs": row["packs"]})
    if not queries:
        raise SystemExit(f"{path}: no queries")
    return queries


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default=str(ROOT / "evals" / "queries.jsonl"),
                    help='JSONL, one {"query": ..., "packs": [...]} per line')
    ap.add_argument("-k", "--k", type=int, default=10,
                    help="cut-off in distinct packs (1..50)")
    ap.add_argument("--log-level", default="WARNING")
    args, rest = ap.parse_known_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(message)s")
    cfg = botmod.parse_args(rest + ["--top-k", "50"])   # packs are ranked over the full answer
    queries = load_labels(args.labels)
    search = botmod.HybridSearch(cfg)                # no store: evals are personalized-free

    total = {"hit": 0.0, "rr": 0.0, "recall": 0.0, "ndcg": 0.0}
    print(f"{'query':32} {'hit':>4} {'rr':>6} {'rec@' + str(args.k):>7} {'ndcg@' + str(args.k):>8}")
    try:
        for row in queries:
            m = query_metrics(search.search(row["query"]), row["packs"], args.k)
            for key in total:
                total[key] += m[key]
            print(f"{row['query'][:32]:32} {m['hit']:4.0f} {m['rr']:6.3f} "
                  f"{m['recall']:7.2f} {m['ndcg']:8.3f}")
    finally:
        search.db.close()
    n = len(queries)
    print(f"{'MEAN':32} {total['hit'] / n:4.2f} {total['rr'] / n:6.3f} "
          f"{total['recall'] / n:7.2f} {total['ndcg'] / n:8.3f}")


if __name__ == "__main__":
    main()
