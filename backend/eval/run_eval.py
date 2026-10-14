"""Evaluation harness.

    python -m eval.run_eval [--mode scripted|llm] [--model DEPLOYMENT] [--only G04] [--db sqlite:///...]
                            [--compare eval/reports/<file>.json]

Runs every golden case through the real service code path (create_case -> process_case) against a throwaway
database, scores the results and writes eval/reports/<timestamp>.{md,json} plus eval/reports/latest-<mode>.json.
Exit code 1 if any must_pass case fails (or if a case did not run in the requested mode); 2 if the requested
mode is not available (e.g. --mode llm with no model configured).

Per case it records outcome/gate/risk checks, run latency, model calls, tokens in/out/cached and a list-price cost
estimate, so an llm run can be compared with the scripted baseline on correctness, speed and money.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from app.config import get_settings
from app.db import reset_engine_for_tests, session_scope
from app.models import ReviewTask
from app.schemas import ApplicationIn
from app.services import create_case, process_case

GOLDEN_DIR = Path(__file__).parent / "golden"
REPORT_DIR = Path(__file__).parent / "reports"


class ModeUnavailable(RuntimeError):
    pass


def load_cases(only: str | None) -> list[dict]:
    cases = [json.loads(p.read_text()) for p in sorted(GOLDEN_DIR.glob("*.json"))]
    return [c for c in cases if not only or only.lower() in c["id"].lower()]


def apply_model_override(model: str | None) -> None:
    """--model: point the chat client at another deployment (Azure) or model tag (Ollama) for a challenger run."""
    if not model:
        return
    from app.llm import reset_llm
    s = get_settings()
    s.azure_openai_chat_deployment = model
    s.ollama_chat_model = model
    reset_llm()


def resolve_provider(mode: str) -> dict:
    """What will actually answer the model calls, resolved the same way app.llm.get_llm does. Raises if --mode llm
    was asked for but no chat model is configured, instead of silently degrading to scripted (which run_case does)."""
    s = get_settings()
    if mode != "llm":
        return {"provider": None, "deployment": None}
    if not s.llm_ready:
        raise ModeUnavailable(
            "llm mode requested but no chat model is configured. Set ONBOARDIQ_LLM_PROVIDER=azure with "
            "ONBOARDIQ_AZURE_OPENAI_ENDPOINT (+ key or `az login`), or ONBOARDIQ_LLM_PROVIDER=ollama with Ollama running. "
            "Check with: python -m scripts.check_llm")
    prov = s.llm_provider
    if prov == "auto":
        prov = "azure" if s.azure_openai_endpoint else "ollama"
    dep = s.azure_openai_chat_deployment if prov == "azure" else s.ollama_chat_model
    return {"provider": prov, "deployment": dep}


def score_case(g: dict, case, run, task, requested_mode: str) -> dict:
    exp = g["expect"]
    codes = {f["code"] for f in run.findings}
    checks: dict[str, bool] = {}
    checks["outcome"] = (case.outcome.value if case.outcome else None) == exp["outcome"]
    if "gate" in exp:
        checks["gate"] = (run.gate or "").split("+")[0] == exp["gate"]
    if "risk_rating" in exp:
        checks["risk_rating"] = case.risk_rating == exp["risk_rating"]
    order = {"low": 0, "medium": 1, "high": 2}
    if "risk_rating_max" in exp:  # the model may tighten, but not past this (e.g. not High on a data error)
        checks["risk_rating_max"] = order.get(case.risk_rating or "low", 0) <= order[exp["risk_rating_max"]]
    for code in exp.get("must_have", []):
        checks[f"has:{code}"] = code in codes
    for code in exp.get("must_not_have", []):
        checks[f"not:{code}"] = code not in codes
    if "review_priority" in exp:
        checks["review_priority"] = (task.priority if task else None) == exp["review_priority"]
    for item in exp.get("missing_items_contains", []):
        checks[f"missing:{item}"] = any(item.lower() in m.lower() for m in case.missing_items)
    checks["run_finished"] = run.status == "finished"
    checks["ran_in_requested_mode"] = run.mode == requested_mode
    return {"id": g["id"], "description": g["description"], "must_pass": g.get("must_pass", False),
            "passed": all(checks.values()), "checks": checks,
            "actual": {"outcome": case.outcome.value if case.outcome else None, "gate": run.gate, "risk_rating": case.risk_rating,
                       "risk_score": case.risk_score, "finding_codes": sorted(codes), "missing_items": case.missing_items,
                       "review_priority": task.priority if task else None, "tool_calls": run.tool_calls,
                       "llm_calls": run.llm_calls, "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
                       "tokens_cached": run.tokens_cached, "tokens": run.tokens_in + run.tokens_out,
                       "llm_latency_ms": run.llm_latency_ms, "cost_usd": run.cost_usd,
                       "budget_exhausted": "AGENT_BUDGET_EXHAUSTED" in codes,
                       "mode": run.mode, "model": run.model, "error": run.error},
            "expected": exp}


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, round(p * (len(xs) - 1))))
    return xs[k]


def run(mode: str, only: str | None, db_url: str, model: str | None = None) -> dict:
    s = get_settings()
    s.orchestrator_mode = mode
    apply_model_override(model)
    prov = resolve_provider(mode)
    reset_engine_for_tests(db_url)
    results = []
    tool_errors_total = 0
    print(f"mode={mode} provider={prov['provider'] or 'none'} deployment={prov['deployment'] or '-'} prompt={s.prompt_version}")
    for g in load_cases(only):
        t0 = time.perf_counter()
        with session_scope() as session:
            case = create_case(session, ApplicationIn.model_validate(g["application"]), actor="eval")
            run_ = process_case(session, case, mode, actor="eval")
            task = session.exec(select(ReviewTask).where(ReviewTask.case_id == case.id)).first()
            from app.models import AuditEvent
            tool_errors = sum(1 for e in session.exec(select(AuditEvent).where(AuditEvent.case_id == case.id)).all()
                              if e.event_type == "tool_error")
            session.commit()
            r = score_case(g, case, run_, task, mode)
        r["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        r["actual"]["tool_errors"] = tool_errors
        tool_errors_total += tool_errors
        results.append(r)
        a = r["actual"]
        extra = (f" calls {a['llm_calls']:>2}  tok {a['tokens_in']:>6}/{a['tokens_out']:<5} ${a['cost_usd']:.4f}"
                 if mode == "llm" else "")
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<48} {str(a['outcome']):<14} {r['latency_ms']:>6} ms{extra}")
    conf: Counter = Counter((r["expected"]["outcome"], r["actual"]["outcome"]) for r in results)
    per_code: dict[str, dict] = defaultdict(lambda: {"tp": 0, "fn": 0, "fp": 0})
    for r in results:
        for k, ok in r["checks"].items():
            if k.startswith("has:"):
                per_code[k[4:]]["tp" if ok else "fn"] += 1
            elif k.startswith("not:") and not ok:
                per_code[k[4:]]["fp"] += 1
    n = len(results)
    lat = [r["latency_ms"] for r in results]
    total_cost = round(sum(r["actual"]["cost_usd"] for r in results), 6)
    summary = {"mode": mode, "provider": prov["provider"], "deployment": prov["deployment"],
               "cases": n, "passed": sum(r["passed"] for r in results),
               "outcome_accuracy": round(sum(r["checks"]["outcome"] for r in results) / n, 3) if n else 0,
               "must_pass_failures": [r["id"] for r in results if r["must_pass"] and not r["passed"]],
               "mode_mismatches": [r["id"] for r in results if not r["checks"]["ran_in_requested_mode"]],
               "confusion": [{"expected": e, "actual": a, "count": c} for (e, a), c in sorted(conf.items())],
               "finding_codes": dict(per_code),
               "avg_latency_ms": int(sum(lat) / n) if n else 0, "p50_latency_ms": _percentile(lat, 0.5),
               "p95_latency_ms": _percentile(lat, 0.95), "max_latency_ms": max(lat) if lat else 0,
               "llm_calls": sum(r["actual"]["llm_calls"] for r in results),
               "tokens_in": sum(r["actual"]["tokens_in"] for r in results),
               "tokens_out": sum(r["actual"]["tokens_out"] for r in results),
               "tokens_cached": sum(r["actual"]["tokens_cached"] for r in results),
               "total_tokens": sum(r["actual"]["tokens"] for r in results),
               "total_cost_usd": total_cost, "avg_cost_per_case_usd": round(total_cost / n, 6) if n else 0,
               "budget_exhausted_cases": [r["id"] for r in results if r["actual"]["budget_exhausted"]],
               "tool_errors": tool_errors_total,
               "model": next((r["actual"]["model"] for r in results if r["actual"]["model"]), None),
               "price_known": None,
               "prompt_version": s.prompt_version, "generated_at": datetime.now(timezone.utc).isoformat()}
    if mode == "llm":
        from app.llm import price_known
        summary["price_known"] = price_known(summary["model"]) or price_known(prov["deployment"])
    return {"summary": summary, "results": results}


def compare(report: dict, baseline: dict) -> dict:
    """Case-by-case diff against an earlier report (typically the scripted baseline)."""
    base = {r["id"]: r for r in baseline["results"]}
    rows = []
    for r in report["results"]:
        b = base.get(r["id"])
        if not b:
            continue
        rows.append({"id": r["id"], "expected": r["expected"]["outcome"],
                     "baseline_outcome": b["actual"]["outcome"], "outcome": r["actual"]["outcome"],
                     "baseline_gate": b["actual"]["gate"], "gate": r["actual"]["gate"],
                     "baseline_risk": b["actual"]["risk_rating"], "risk": r["actual"]["risk_rating"],
                     "baseline_passed": b["passed"], "passed": r["passed"],
                     "baseline_latency_ms": b["latency_ms"], "latency_ms": r["latency_ms"],
                     "cost_usd": r["actual"]["cost_usd"],
                     "changed": (b["actual"]["outcome"] != r["actual"]["outcome"]
                                 or (b["actual"]["gate"] or "").split("+")[0] != (r["actual"]["gate"] or "").split("+")[0]
                                 or b["actual"]["risk_rating"] != r["actual"]["risk_rating"]),
                     "regressed": b["passed"] and not r["passed"]})
    bs, rs = baseline["summary"], report["summary"]
    return {"baseline_mode": bs["mode"], "baseline_model": bs.get("model"), "baseline_generated_at": bs["generated_at"],
            "changed": [x["id"] for x in rows if x["changed"]], "regressed": [x["id"] for x in rows if x["regressed"]],
            "latency_ratio": round(rs["avg_latency_ms"] / bs["avg_latency_ms"], 1) if bs["avg_latency_ms"] else None,
            "rows": rows}


def write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    s = report["summary"]
    (REPORT_DIR / f"{stamp}.json").write_text(json.dumps(report, indent=2, default=str))
    shutil.copyfile(REPORT_DIR / f"{stamp}.json", REPORT_DIR / f"latest-{s['mode']}.json")
    model = s["model"] or "n/a"
    head = (f"**{s['passed']}/{s['cases']} cases passed · outcome accuracy {s['outcome_accuracy']:.0%} · "
            f"latency avg {s['avg_latency_ms']} / p95 {s['p95_latency_ms']} ms · prompt {s['prompt_version']}**")
    lines = [f"# OnboardIQ eval — {s['mode']} mode — {s['generated_at'][:19]}Z", "", head, ""]
    if s["mode"] == "llm":
        lines += [f"Model: `{model}` via {s['provider']} (deployment `{s['deployment']}`) · {s['llm_calls']} model calls · "
                  f"tokens in {s['tokens_in']:,} (cached {s['tokens_cached']:,}) / out {s['tokens_out']:,} · "
                  f"**cost ${s['total_cost_usd']:.4f} total, ${s['avg_cost_per_case_usd']:.4f} per case** (list price) · "
                  f"tool errors {s['tool_errors']} · budget fallbacks {len(s['budget_exhausted_cases'])}", ""]
        lines += ["| Case | Expected | Actual | Gate | Risk | Result | Latency | Calls | Tokens in/out (cached) | Cost | Failed checks |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in report["results"]:
            a = r["actual"]
            failed = ", ".join(k for k, ok in r["checks"].items() if not ok) or "—"
            lines.append(f"| {r['id']} | {r['expected']['outcome']} | {a['outcome']} | {a['gate']} | "
                         f"{a['risk_rating']} ({a['risk_score']}) | {'PASS' if r['passed'] else 'FAIL'} | {r['latency_ms']} ms | "
                         f"{a['llm_calls']} | {a['tokens_in']:,}/{a['tokens_out']:,} ({a['tokens_cached']:,}) | "
                         f"${a['cost_usd']:.4f} | {failed} |")
    else:
        lines += ["| Case | Expected | Actual | Gate | Risk | Result | Latency | Failed checks |", "|---|---|---|---|---|---|---|---|"]
        for r in report["results"]:
            a = r["actual"]
            failed = ", ".join(k for k, ok in r["checks"].items() if not ok) or "—"
            lines.append(f"| {r['id']} | {r['expected']['outcome']} | {a['outcome']} | {a['gate']} | "
                         f"{a['risk_rating']} ({a['risk_score']}) | {'PASS' if r['passed'] else 'FAIL'} | {r['latency_ms']} ms | {failed} |")
    lines += ["", "## Outcome confusion", "", "| expected | actual | count |", "|---|---|---|"]
    lines += [f"| {c['expected']} | {c['actual']} | {c['count']} |" for c in s["confusion"]]
    if report.get("comparison"):
        c = report["comparison"]
        lines += ["", f"## Compared with {c['baseline_mode']} baseline ({c['baseline_generated_at'][:19]}Z"
                      f"{', model ' + c['baseline_model'] if c['baseline_model'] else ''})", "",
                  f"Changed decisions: {', '.join(c['changed']) or 'none'} · regressions: {', '.join(c['regressed']) or 'none'}"
                  f" · latency ×{c['latency_ratio']}", "",
                  "| Case | Expected | Baseline → now | Gate baseline → now | Risk baseline → now | Latency baseline → now | Cost |",
                  "|---|---|---|---|---|---|---|"]
        for x in c["rows"]:
            mark = " ⚠" if x["changed"] else ""
            lines.append(f"| {x['id']} | {x['expected']} | {x['baseline_outcome']} → {x['outcome']}{mark} | "
                         f"{x['baseline_gate']} → {x['gate']} | {x['baseline_risk']} → {x['risk']} | "
                         f"{x['baseline_latency_ms']} → {x['latency_ms']} ms | ${x['cost_usd']:.4f} |")
    md = REPORT_DIR / f"{stamp}.md"
    md.write_text("\n".join(lines) + "\n")
    return md


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="scripted", choices=["scripted", "llm"])
    ap.add_argument("--only", default=None)
    ap.add_argument("--model", default=None, help="challenger deployment/model name (overrides the configured chat model)")
    ap.add_argument("--db", default="sqlite:///./data/eval.db")
    ap.add_argument("--compare", default=None, metavar="REPORT_JSON",
                    help="diff against an earlier report, e.g. eval/reports/latest-scripted.json")
    args = ap.parse_args()
    Path("./data").mkdir(exist_ok=True)
    if args.db.startswith("sqlite:///"):
        p = Path(args.db.removeprefix("sqlite:///"))
        if p.exists():
            p.unlink()
    try:
        report = run(args.mode, args.only, args.db, args.model)
    except ModeUnavailable as e:
        print(f"ERROR: {e}")
        return 2
    if args.compare:
        report["comparison"] = compare(report, json.loads(Path(args.compare).read_text()))
    path = write_report(report)
    s = report["summary"]
    cost = f", cost ${s['total_cost_usd']:.4f} (${s['avg_cost_per_case_usd']:.4f}/case)" if args.mode == "llm" else ""
    print(f"\n{s['passed']}/{s['cases']} passed, outcome accuracy {s['outcome_accuracy']:.0%}, "
          f"latency avg {s['avg_latency_ms']} ms p95 {s['p95_latency_ms']} ms{cost} -> {path}")
    if args.mode == "llm" and not s["price_known"]:
        print(f"NOTE: no price configured for {s['model']}; cost shows $0. Add it with ONBOARDIQ_LLM_PRICES "
              "(USD per 1M tokens [input, output, cached_input]) from the Azure pricing page.")
    if report.get("comparison"):
        c = report["comparison"]
        print(f"vs baseline: changed {c['changed'] or 'none'}, regressed {c['regressed'] or 'none'}, latency x{c['latency_ratio']}")
    if s["mode_mismatches"]:
        print("CASES DID NOT RUN IN REQUESTED MODE:", s["mode_mismatches"])
        return 1
    if s["must_pass_failures"]:
        print("MUST-PASS FAILURES:", s["must_pass_failures"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
