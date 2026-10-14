# Baseline run: llm mode against Azure OpenAI

Goal of this drop (roadmap week 1, Build task): prove the orchestrator works end to end against a real model and
record the numbers everything later is measured against — correctness versus the scripted baseline, latency, tokens
and cost per case. Do this before the security work (identity, policy enforcement, red team), so every later change has
a "before".

## What changed in the code (drop 3a)

| Area | Change |
|---|---|
| `app/llm.py` | `usage_scope()` books **every** model call made inside a run — orchestrator loop *and* the `rate_risk` grounding call — with calls, tokens in/out, **cached prompt tokens**, per-call latency and the provider's real model version (`gpt-4.1-mini-2025-04-14`, not just the deployment name). `estimate_cost_usd()` prices a run from `settings.llm_prices`. |
| `app/models.py` + migration `b7c1e4d2a9f0` | `CaseRun` gains `llm_calls`, `tokens_cached`, `llm_latency_ms`, `cost_usd`; `model` holds the real model version in llm mode. Exposed on `GET /cases/{id}` (`latest_run`) and `/runs/{id}`. |
| `app/agent/orchestrator.py` | Opens the usage scope around the run; `run_finished` audit event now carries calls, tokens, cached, latency, cost, model, duration. |
| `app/config.py` | `llm_prices` table (USD per 1M tokens `[input, output, cached_input]`, prefix-matched). Override with `ONBOARDIQ_LLM_PRICES` JSON after checking current Azure pricing. |
| `scripts/check_llm.py` | Connectivity smoke test: resolved provider, one chat call with a tool offered, one JSON-mode call, one embedding. Prints model version, latency, tokens, cost, and a hint on the usual failures (401/404/429/connection). |
| `eval/run_eval.py` | Refuses `--mode llm` when no model is configured (exit 2) instead of silently running scripted; fails a case that did not run in the requested mode; per-case calls/tokens/cached/cost/latency; p50/p95 latency; `latest-<mode>.json`; `--compare <report.json>` diff (changed decisions, regressions, latency ratio). |
| `tests/` | `test_usage.py`, `test_eval_harness.py` (+ `test_llm_loop.py` adjusted): 36 tests. |
| `pyproject.toml` | Ruff rule set pinned (`E4 E7 E9 F I`) so CI does not break when a newer ruff widens its defaults. |

## Run it

```bash
cd backend && source .venv/bin/activate
pip install -r requirements.txt            # no new dependencies, but re-run after pulling
python -m pytest -q                        # 36 passed
alembic upgrade head                       # only if you point at Azure SQL; SQLite dev DBs are created by create_all

# 1. scripted baseline (fresh report with the new columns)
python -m eval.run_eval --mode scripted    # 12/12 -> eval/reports/latest-scripted.json

# 2. point at Azure OpenAI (your tenant, Foundry resource <your-openai-resource>)
export ONBOARDIQ_ORCHESTRATOR_MODE=llm
export ONBOARDIQ_LLM_PROVIDER=azure
export ONBOARDIQ_AZURE_OPENAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com/   # no /openai/v1 suffix
export ONBOARDIQ_AZURE_OPENAI_API_KEY=...   # or omit and `az login` (needs Cognitive Services OpenAI User on the resource)
rm -f data/policy_index.json               # policy index must be re-embedded with text-embedding-3-small

# 3. connectivity check first - fix anything it flags before spending eval tokens
python -m scripts.check_llm

# 4. the baseline run
python -m eval.run_eval --mode llm --compare eval/reports/latest-scripted.json
```

Expected: `mode=llm provider=azure deployment=gpt-4.1-mini` on the first line, then 12 lines like

```
PASS  G04_individual_sanctions_match_escalate          escalate         2140 ms calls  7  tok   9120/310   $0.0041
```

and a summary with `cost $... (.../case)` plus `vs baseline: changed none, regressed none`. The `.md` report has the
per-case table and the comparison section. Run it **twice**: the second run should show `cached` tokens > 0 on most
calls (the system prompt and tool definitions repeat verbatim) and a lower cost — that is prompt caching working.

## Record these numbers (roadmap week 1 → week 6 cost work)

| Metric | Where | Scripted | LLM run 1 | LLM run 2 (cache warm) |
|---|---|---|---|---|
| Cases passed | summary `passed` | 12/12 | | |
| Changed decisions vs baseline | comparison `changed` | — | | |
| Model version | summary `model` | — | | |
| Model calls per case (avg) | `llm_calls` / 12 | 0 | | |
| Tokens in / out per case (avg) | `tokens_in`, `tokens_out` / 12 | 0 | | |
| Cached share of input | `tokens_cached / tokens_in` | — | | |
| Latency avg / p95 | `avg_latency_ms`, `p95_latency_ms` | ~30 ms | | |
| Cost per case (list) | `avg_cost_per_case_usd` | $0 | | |
| Tool errors (model called a tool it should not have) | `tool_errors` | 0 | | |
| Budget fallbacks | `budget_exhausted_cases` | — | | |

Then the two architect questions the numbers answer: what does a case cost, and what would 50,000 cases a month cost
(`avg_cost_per_case_usd × 50,000`)? Both go in the week-7 ADR on model choice.

## What to look at if a case changes decision

The policy engine decides, the model only recommends, so a changed *outcome* between scripted and llm means the model
produced different **findings** (e.g. it re-ran `screen_parties` with narrower arguments) or `rate_risk` grounding
raised the rating. Open the case's audit trail (`GET /cases/{id}/audit`): `tool_call` / `tool_result` events show what
the model asked for, `policy_gate` shows `model_recommendation` versus the gate's outcome and `model_tightened`.
`tool_error` events with "required checks not run" are the checklist rejecting a premature `record_decision` — expected
occasionally, and worth counting: it is the first data point for the week-2 threat model (how often does the model try
to skip a check?).

## Known limits of this drop

- Cost is a **list-price estimate** from a static table; PTU deployments, negotiated rates and regional pricing differ.
  The token counts are exact (from the API's `usage` object); price them however finance does.
- `latency_ms` in the eval is wall time for the whole case including tool execution and SQLite writes;
  `llm_latency_ms` on the run is the model's share.
- Live Azure paths were verified against the SDK contract and a fake provider here, not against your resource — that is
  the point of step 3.

## Challenger run: a newer model (drop 3b)

`gpt-4.1-mini` is deprecated and retires on 2027-04-14, so validate its successor with the same harness.

```bash
# deploy e.g. gpt-5.4-mini on the same Foundry resource, then:
export ONBOARDIQ_AZURE_OPENAI_API_VERSION=<version from the deployment page>
export ONBOARDIQ_LLM_PRICES='{"gpt-5.4-mini": [<input>, <output>, <cached_input>]}'   # from the Azure pricing page
python -m eval.run_eval --mode llm --model gpt-5.4-mini --compare eval/reports/latest-llm.json
```

Reasoning models are detected from the deployment name (`gpt-5*`, `gpt-6*`, `o1/o3/o4*`): `temperature` is not sent and
reasoning tokens are counted. Set `ONBOARDIQ_LLM_REASONING=yes` if your deployment name does not reveal the model.
Note: `latest-llm.json` is overwritten by every llm run, so copy your gpt-4.1-mini report aside first if you want to
keep it as the comparison baseline.
