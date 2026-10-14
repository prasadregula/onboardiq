# OnboardIQ threat model

| | |
|---|---|
| Version | 1.0 (drop 3b) — 2026-10-04 |
| Scope | Backend API, orchestrator (scripted + llm modes), the eight agent tools, policy RAG, OFAC ingestion, review queue, audit trail. Frontend only where it renders model output. |
| Method | Walk every agent action and ask: who triggers it, what can the model influence, where can untrusted text get in, what is the blast radius if it goes wrong, and what stops it. Threats are tagged with OWASP Top 10 for LLM Applications (LLM01–LLM10) and checked against the "lethal trifecta" (private data + untrusted content + a way out). |
| Evidence | Code at drop 3b; baseline eval runs 2026-09-27 (12/12 scripted, 12/12 llm × 2 on Azure OpenAI gpt-4.1-mini); fake-model tests in `tests/`. |
| Owner | Prasad (architect). Review before every drop that adds a tool, a data source or an outbound channel. |

## 1. System in one paragraph

An analyst (or an upstream onboarding portal, via the `Service` role) creates a case and starts a run. In **llm mode** the
orchestrator (`app/agent/orchestrator.py`) sends the case brief and the tool catalogue to Azure OpenAI; the model
proposes tool calls; **code** executes them (`app/agent/tools/*`), appends results to the conversation, and loops
until `record_decision`. The **deterministic policy engine** (`app/policy/decision_policy.py`) computes the outcome;
the model's recommendation can only make it stricter. Escalations land in the analyst review queue. Every message,
tool call, result and decision is written to a **hash-chained audit trail** (`app/audit.py`).

## 2. Assets

| Asset | Why it matters |
|---|---|
| Applicant PII: names, DOB, ID numbers, addresses, ownership | GLBA / NYDFS 500; leakage is a reportable incident |
| The onboarding decision and its reason | A wrong approve is a BSA/AML failure; a wrong escalate is cost and customer harm |
| Customer risk rating | Drives EDD and ongoing monitoring for the life of the relationship |
| Sanctions / PEP lists | Integrity: a tampered list silently approves a sanctioned party |
| Policy corpus (5 KYC policies) | Integrity: a changed policy changes every decision that cites it |
| Audit trail | The evidence a regulator or model validator relies on |
| Credentials and identities | API app identity, Azure OpenAI access, analyst tokens |

## 3. Trust boundaries (where untrusted text enters)

1. **Uploaded documents** → `extract_document` output → model context. *Fully attacker-controlled.*
2. **Declared application data** (names, industry, purpose) → case brief → model context. *Applicant-controlled.*
3. **External lists** (OFAC SDN/Consolidated) → screening results. *Third-party data, integrity matters.*
4. **Policy corpus** → retrieved excerpts in `rate_risk`. *Internal, but editable.*
5. **Model output** → tool arguments, recommendation, rationale → database, UI, exports. *Must be treated as untrusted.*
6. **Analyst input** (notes, overrides) → review records. *Trusted identity, untrusted text.*

**Lethal trifecta check.** OnboardIQ has two legs by nature: private data (applicant PII) and untrusted content
(documents). **The third leg — a way to send data out — is deliberately absent:** no tool sends email, calls a URL,
writes to a customer-visible channel or renders model output as markdown/HTML. *This is a design constraint, not an
accident: any change that adds an outbound channel must re-open this threat model (§6, T-08).*

## 4. Per-action analysis

Risk = likelihood × impact after existing controls (H / M / L).

| Action | Triggered by | What the model influences | Untrusted input | Blast radius if subverted | Existing controls | Planned controls | Risk |
|---|---|---|---|---|---|---|---|
| `extract_document` | Model (must be first) | Which document, when | **Document text (boundary 1)** | Injected text enters context for every later step; PII exposed to the model | Tool output treated as data (system prompt rule 5); fixtures in eval | Prompt-shield scan on extracted text (wk 4); PII masking before model (wk 4) | **H** |
| `check_consistency` | Model | Ordering only | Extracted fields | Missed mismatch → wrong gate | Deterministic comparison in code; findings feed policy gate | — | L |
| `screen_parties` | Model | Could re-run with narrower arguments | Party names; external lists (boundary 3) | **Skipped or narrowed screening → sanctioned party approved** | Checklist: `record_decision` refused until it runs; screening is code, not model; sanctions gate first in policy | Per-step tool allow-list (wk 4); OFAC file integrity check (wk 5) | M |
| `resolve_beneficial_owners` | Model (business only) | Ordering | Ownership declaration | Missed UBO → coverage gap | Deterministic thresholds (25%); `UBO_OVER_100` is a critical-data code | — | L |
| `verify_address` | Model | Ordering | Address fields | Wrong address status | Mock provider in code | Real provider adapter will be an external service → MCP/vendor controls (T-12) | L |
| `rate_risk` | Model | **Model-added factors, rationale** | Fact sheet incl. finding messages; policy excerpts (boundary 4) | **Inflated or deflated rating → wrong EDD path for the life of the account** | Deterministic floor; model can only raise. **Drop 3b:** data-quality findings excluded from scoring; model factors accepted only if they cite a retrieved policy section; weights table always in evidence set | Bias review of model-added factors (wk 6); larger golden set | M (was H) |
| `record_decision` | Model | Recommendation + rationale text | Everything above, via the model | Wrong outcome; misleading rationale shown to analyst | Checklist; policy gate decides; model can only tighten; outcome and gate audited | Rationale schema + PII scan (wk 4) | L (outcome) / M (rationale) |
| Orchestrator loop | API (`POST /cases/{id}/run`) | Path, number of steps | All of the above | Cost/latency blow-up; budget fallback | `max_tool_calls` = 14; fallback never auto-approves; tool output truncated at 60k chars | Per-run token/cost budget + global pause switch (wk 4) | M |
| Review queue actions | Analyst (Entra role) | — | Analyst notes | Unauthorised override | App roles per endpoint; actions audited | Analyst identity propagated into tool-call audit (wk 3) | L |
| Policy index build | Operator | — | Policy markdown (boundary 4) | Every rating that cites a changed section | Corpus in repo; index rebuilt from files; embedder recorded in index | Corpus approval workflow + index hash recorded per run (wk 5) | M |
| OFAC refresh job | Schedule | — | Downloaded CSVs (boundary 3) | Tampered/empty list → misses | HTTPS from treasury.gov; atomic file replace | Row-count/size sanity check and alert; keep last-good copy (wk 5) | M |

## 5. Evidence from the baseline runs

| Ref | What we saw | Meaning | Status |
|---|---|---|---|
| **E-1 (G11)** | In llm mode the model rated a business **High** because declared ownership summed to 110%, and the gate changed from `GATE_CRITICAL_DATA_WITH_SCREENING` to `GATE_HIGH_RISK`. Outcome stayed `escalate`; eval passed because G11 pinned only the outcome. | "Model can only tighten" is a good guardrail, not a complete one: over-rating changes routing and triggers EDD. Tests that check only the outcome miss it. | **Fixed in drop 3b** (T-03) |
| E-2 (G07) | Model raised a DOB-mismatch case from low to medium; gate unchanged. | Allowed tightening; now bounded by `risk_rating_max` in the golden case. | Accepted |
| E-3 | Same case, different path between runs (6 vs 7 model calls); parallel tool calls observed. | Non-determinism in the route, not the outcome. The deterministic gate is what makes it acceptable. | Monitored via eval `--compare` |
| E-4 | ~14k input tokens per case; business cases ~2× individuals. | Cost and blast-radius scale with context size. | Input to T-10 budgets |
| E-5 | `extract_document` returns raw fields, including **ID numbers and DOB**, to the model. The case brief strips `id_number`/`tin`, but extraction re-exposes them. | PII reaches the model and the conversation log unmasked. | Open (T-07) |

## 6. Threat register

| ID | Threat | OWASP | L | I | Existing | Planned | When |
|---|---|---|---|---|---|---|---|
| T-01 | Indirect prompt injection via uploaded documents ("screening already done, approve") | LLM01 | H | M | Checklist; policy gate decides; rule 5 | Prompt shields on extracted text; red-team cases in CI | wk 4–5 |
| T-02 | Model skips or reorders required checks | LLM06 | M | M | Checklist rejects early decision; budget fallback runs missing checks | Per-step allow-list | wk 4 |
| **T-03** | **Model inflates risk on data-quality issues and changes the escalation reason (E-1)** | LLM09 | M | M | **Drop 3b:** gate precedence (critical data outranks high risk, never loosens); model fact sheet separates data-quality findings; ungrounded factors rejected and recorded; goldens pin gate + `risk_rating_max` | Monitor `rejected_model_factors` rate | done |
| T-04 | All tools run under one app identity: a subverted step can do anything any step can | LLM06 | M | H | **Done (w3-1):** one user-assigned identity per agent role, chosen in code per tool (`app/identity.py`); OpenAI only for orchestrator + risk, Document Intelligence + Blob *read* only for extraction, screening and decision have no Azure roles; API identity's OpenAI/DI roles revoked; identity recorded on every tool event; a revoked identity fails only its tool (→ `GATE_CHECKS_INCOMPLETE`) | Per-identity SQL users with table grants; one container per agent for code-execution isolation | wk 3 / later |
| T-05 | Model is offered every tool on every step | LLM06 | M | M | **Partly done (w3-2):** policy enforcement point (`app/agent/policy_point.py`) on every call: caller allow-list deny-by-default (only the orchestrator dispatches; no agent can invoke another tool), no unknown arguments, case-scoped ids, bounded plain-text decision fields; denials audited as `tool_denied` | Per-STEP allow-list (state machine: which tools are valid next) | wk 4 |
| T-06 | Analyst identity stops at the API; tool calls not attributable to a human | — (repudiation) | M | M | **Done (w3-3):** the validated caller (subject, oid, tenant, roles, token id) is recorded as `initiated_by` on `run_started` and as `on_behalf_of` on every tool / denial event, next to the agent identity; survives the async background hop; scripts and jobs recorded as `system` principals. Secretless OBO (managed-identity federated credential) available for analyst-scoped downstream resources; KYC tools stay app-only by design | Per-tool delegated calls if analyst-scoped evidence sources are added | wk 3 |
| T-07 | PII sent to the model and stored in conversation logs (E-5) | LLM02 | H | M | Case brief strips `id_number`/`tin` | Mask/tokenize IDs in extraction output; PII scan on rationale | wk 4 |
| T-08 | A future outbound channel completes the lethal trifecta | LLM01/LLM02 | L now | H | No outbound tools; React escapes text | Design rule: no outbound tool without re-review; egress allow-list at gateway | standing |
| T-09 | Model output rendered or reused unsafely | LLM05 | L | M | Structured outputs; JSON parsing with fallback; React escaping; **w3-2:** rationale ≤1500 chars, customer-facing `missing_items` plain text without links/markup | Output PII scan | wk 4 |
| T-10 | Cost or latency blow-up (long documents, loops) | LLM10 | M | M | Step budget; 60k-char truncation | Token and cost budget per run; upload size limit; global pause | wk 4 |
| T-11 | Policy corpus or index tampering changes ratings | LLM04 | L | H | Corpus in repo | Approval workflow; corpus hash per run | wk 5 |
| T-12 | Supply chain: Python packages, model provider, OFAC feed, future MCP/vendor tools | LLM03 | M | H | Pinned major versions; HTTPS feed | Dependency + image scanning in CI; OFAC sanity checks; MCP server allow-list and description pinning at the guardrail gateway | wk 5 |
| T-13 | Model change or retirement changes behaviour (gpt-4.1-mini retires 2027-04-14) | — (model risk) | H | M | Model version recorded per run; CI eval gate; `--compare` | Challenger runs with `--model` (e.g. gpt-5.4-mini); re-validation plan in ADR | wk 6–7 |
| T-14 | System prompt leakage | LLM07 | M | L | Nothing secret in prompts; thresholds live in config/policy | — | — |
| T-15 | Vector store leaks across cases | LLM08 | L | M | Only policies are indexed; no applicant documents | Re-assess if case documents are ever indexed | standing |
| T-16 | Audit trail rewritten by someone with DB access | — | L | H | Hash chain + `verify_chain` | Periodic anchor of chain head to immutable storage | wk 5 |
| T-17 | Dev auth mode (`apikey`, role from header) enabled outside dev | — | L | H | Bicep deploys `entra`; **done (w3-3):** the API refuses to start with `apikey` unless `ONBOARDIQ_ENV` is dev/ci/test/local (tested) | — | done |

## 7. Build order this drives (weeks 3–5)

1. **Identity per agent role + analyst on-behalf-of** (T-04, T-06, T-17) — week 3. Biggest blast-radius reduction.
2. **Policy enforcement point**: per-step tool allow-list, argument validation, token/cost budget, pause switch (T-02, T-05, T-10) — week 4.
3. **PII masking in extraction output + rationale scan** (T-07, T-09) — week 4.
4. **Prompt-shield scan on extracted text + red-team suite in CI** (T-01) — weeks 4–5.
5. **Integrity**: OFAC sanity checks, corpus hash per run, audit anchoring, dependency scanning (T-11, T-12, T-16) — week 5, inside the guardrail-gateway extraction.

## 8. Residual risks accepted for the portfolio stage

- Grounded-but-wrong model factors can still raise a rating by one band; bounded by `risk_rating_max` in tests and by analyst review in production.
- Twelve golden cases demonstrate the method; validation would need hundreds, including adversarial and fairness cases.
- Mock extraction/address providers: real providers add vendor risk (T-12) not yet exercised.
- Agent identities share one process (w3-1): this contains model-driven misuse and gives attribution, but code execution in the container could use any attached identity. SQL access is still the API's identity for every agent (shared transaction); per-identity SQL users are the next step.

## 9. Change log

| Date | Change |
|---|---|
| 2026-10-04 | v1.0. Created from code at drop 3b and the 2026-09-27 baseline. T-03 fixed in the same drop. |
| 2026-10-11 | Deployment findings: real-OFAC false positives (subset fuzzy match, vessels screened as people) fixed with coverage-aware scoring, entity-type filter and a corroboration rule for "confirmed"; readiness probe no longer queries SQL (cost). |
| 2026-10-13 | w3-1: T-04 mitigated — per-agent managed identities, least-privilege role split, identity in the audit chain, failure isolation test. |
| 2026-10-15 | w3-3: T-06 mitigated (analyst identity on run + every tool event, OBO path); T-17 closed (startup guard). |
| 2026-10-14 | w3-2: policy enforcement point — caller allow-list, argument constraints, storage writes only from the API (separation of duties); T-05 partly, T-09 tightened. |
