"""Retrieval quality and access trimming for the policy search.

    python -m eval.retrieval_eval            # local store (CI gate)
    GET /admin/rag/eval                      # the deployed store (Azure AI Search), from the review queue

Metrics over eval/retrieval_golden.json:
- hit rate@k: share of queries where at least one expected section is in the top k
- recall@k:   share of all expected sections found in the top k
- MRR:        mean of 1 / rank of the first expected section (0 if none)
- access leaks: sections returned to a caller who may not read them. Must be 0: a leak fails the gate outright.
Retrieval is scored separately from answers: a wrong answer from the right evidence is a model problem, a wrong
answer from the wrong evidence is a retrieval problem, and they are fixed differently.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

GOLDEN = Path(__file__).parent / "retrieval_golden.json"


def _key(ref_or_section: str) -> str:
    m = re.search(r"(KYC-POL-\d+)\s*§\s*(\d+)", ref_or_section or "")
    return f"{m.group(1)} §{m.group(2)}" if m else ""


def evaluate(store, golden: dict | None = None) -> dict:
    g = golden or json.loads(GOLDEN.read_text())
    k = g.get("k", 5)
    rows, hits, rr, found, expected_total, leaks = [], 0, 0.0, 0, 0, 0
    scored = [q for q in g["queries"] if q.get("expect")]
    for q in g["queries"]:
        got = [_key(c.ref()) for c in store.search(q["q"], top_k=k, roles=q["roles"])]
        exp, bad = set(q.get("expect", [])), set(q.get("must_not", []))
        leaked = sorted(bad & set(got))
        leaks += len(leaked)
        first = next((i + 1 for i, r in enumerate(got) if r in exp), None)
        if exp:
            hits += first is not None
            rr += 1 / first if first else 0.0
            found += len(exp & set(got))
            expected_total += len(exp)
        rows.append({"q": q["q"], "roles": q["roles"], "expect": sorted(exp), "got": got, "first_hit_rank": first,
                     "leaked": leaked})
    n = len(scored) or 1
    summary = {"store": getattr(store, "name", type(store).__name__), "k": k, "queries": len(g["queries"]),
               "hit_rate": round(hits / n, 3), "recall": round(found / (expected_total or 1), 3),
               "mrr": round(rr / n, 3), "access_leaks": leaks}
    t = g.get("thresholds", {})
    summary["passed"] = (summary["hit_rate"] >= t.get("hit_rate", 0) and summary["mrr"] >= t.get("mrr", 0)
                         and leaks <= t.get("access_leaks", 0))
    return {"summary": summary, "thresholds": t, "rows": rows}


def main() -> int:
    from app.identity import acting_as
    from app.rag.store import get_policy_store
    with acting_as("risk"):
        report = evaluate(get_policy_store())
    for r in report["rows"]:
        mark = "LEAK" if r["leaked"] else ("hit " if r["first_hit_rank"] else ("miss" if r["expect"] else "--  "))
        print(f"{mark} @{r['first_hit_rank'] or '-'}  {','.join(r['roles']):<11} {r['q'][:70]}")
    s = report["summary"]
    print(f"\n{s['store']}: hit@{s['k']} {s['hit_rate']:.2f} · recall {s['recall']:.2f} · MRR {s['mrr']:.2f} · "
          f"access leaks {s['access_leaks']}  -> {'PASS' if s['passed'] else 'FAIL'} (thresholds {report['thresholds']})")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
