# OnboardIQ — Agentic KYC / Account-Onboarding Platform

**Architecture & Design Document · v0.2 (drop 2: enterprise hardening) · September 2026**
Author: Prasad Regula · Environment: personal Azure subscription (fictional data only) · Status: living document

---

## 1. Purpose and positioning

OnboardIQ is the back-office brain behind account onboarding. A customer (or a banker on their behalf) submits an application plus identity and entity documents; OnboardIQ runs the full KYC/CIP/CDD workflow as an **auditable agent** and lands on one of three outcomes:

| Outcome | Meaning | Who acts next |
|---|---|---|
| `auto_approve` | Every required check passed, low risk, policy allows straight-through processing | Nobody — account opens |
| `request_more` | A required document is missing, unreadable, inconsistent, or ownership is incomplete | Customer (via an upstream onboarding portal) |
| `escalate` | Screening hit, PEP, high risk, or the policy requires Enhanced Due Diligence | KYC analyst / BSA officer in the review queue |
| `needs_review` | Nothing blocks, but confidence or medium risk means a human should confirm | KYC analyst in the review queue |

The design goal is not "let the model decide". It is **let the model orchestrate typed tools, and let deterministic policy decide**. The model is good at sequencing, reading messy documents, and writing rationale grounded in policy text; it is not allowed to override a sanctions hit, invent a risk score, or approve anything the policy engine would not.

It is designed as the back office behind any customer-facing onboarding portal, and uses an Azure OpenAI resource with `gpt-4.1-mini` + `text-embedding-3-small` deployments.

## 2. Requirements captured

Functional:

1. Orchestrator agent with **typed tools**: document extraction (Azure Document Intelligence), cross-document consistency checks, sanctions/PEP screening, beneficial-ownership resolution (FinCEN CDD rule, 25% threshold + control prong), address verification.
2. **Policy-grounded risk rating** using RAG over the bank's KYC/CDD/EDD policies with citations.
3. Three-way decision (`auto_approve` / `request_more` / `escalate`) plus a **human-in-the-loop analyst review queue**.
4. **Full audit trail**: every tool call, every model message, every policy gate, every analyst action — tamper-evident.
5. **Evaluation harness** with golden cases so that prompt, model, or policy changes are measured, not guessed.

Non-functional:

- Runs **fully offline in mock mode** (no Azure calls) so the whole pipeline, tests, and eval run on a laptop or in CI; flips to real Azure services by configuration only.
- Deterministic where regulators expect determinism (screening thresholds, UBO math, decision gates); LLM only where judgment or language is needed.
- Every AI output that influences a decision is stored alongside the inputs it saw (prompt, retrieved policy chunks, model, temperature).
- No customer PII in logs; PII only in the case store and blob store.

## 3. High-level architecture

```
┌──────────────────────┐    ┌──────────────────────────────────────────────────────────┐
│  Onboarding portal   │    │  OnboardIQ API  (FastAPI, Python 3.11+, Container Apps)   │
│  (any front end,     │───▶│                                                          │
│   Service role)      │    │  /cases  /documents  /run  /review  /audit  /eval        │
└──────────────────────┘    │            │                                             │
                            │            ▼                                             │
┌──────────────────────┐    │  ┌──────────────────────┐   ┌────────────────────────┐   │
│  Analyst Review Queue│───▶│  │ Orchestrator Agent   │──▶│ Typed Tool Registry    │   │
│  (React, this repo)  │    │  │ gpt-4.1-mini         │   │ extract_document       │   │
└──────────────────────┘    │  │ tool-calling loop    │   │ check_consistency      │   │
                            │  │ + scripted fallback  │   │ screen_parties         │   │
                            │  └──────────┬───────────┘   │ resolve_beneficial_own.│   │
                            │             │               │ verify_address         │   │
                            │             ▼               │ rate_risk (RAG)        │   │
                            │  ┌──────────────────────┐   │ record_decision        │   │
                            │  │ Decision Policy      │   └───────────┬────────────┘   │
                            │  │ Engine (deterministic│               │                │
                            │  │ gates, never LLM)    │               ▼                │
                            │  └──────────┬───────────┘   ┌────────────────────────┐   │
                            │             │               │ Providers              │   │
                            │             ▼               │ Doc Intelligence | mock│   │
                            │  ┌──────────────────────┐   │ Screening list  | mock │   │
                            │  │ Audit Trail          │   │ Address verify  | mock │   │
                            │  │ (hash-chained)       │   │ Policy RAG store       │   │
                            │  └──────────────────────┘   └────────────────────────┘   │
                            └──────────────────────────────────────────────────────────┘
                                          │                          │
                                ┌─────────┴────────┐       ┌─────────┴─────────┐
                                │ Case DB          │       │ Azure OpenAI      │
                                │ SQLite (dev) /   │       │ <your-openai-resource>  │
                                │ Azure SQL (cloud)│       │ gpt-4.1-mini      │
                                │ Blob store (docs)│       │ text-embedding-3-s│
                                └──────────────────┘       └───────────────────┘
```

### 3.1 Components

| Component | Responsibility | Tech |
|---|---|---|
| **API** | Case lifecycle, document upload, run trigger, review actions, audit read, eval trigger | FastAPI + SQLModel + Pydantic v2 |
| **Orchestrator** | Runs a case: chooses and sequences tools, collects findings, asks for a decision | Azure OpenAI tool calling (`llm` mode) or a fixed canonical sequence (`scripted` mode) |
| **Tool registry** | Each tool = Pydantic input model + Pydantic output model + pure function. JSON schema is generated for the model from the input model. Outputs are validated before anything is stored | `app/agent/tools/*` |
| **Providers** | The external world behind each tool. Every provider has a `Mock*` twin driven by fixtures so the full flow runs offline | Document Intelligence SDK, screening list adapter, address adapter |
| **Decision policy engine** | Deterministic gates that convert findings + risk into the final outcome. The model's recommendation can only make the outcome *stricter*, never more lenient | `app/policy/decision_policy.py` |
| **Policy RAG** | KYC/CIP/CDD/EDD policies chunked by heading, embedded, hybrid (vector + keyword) retrieval with citations; each policy declares its readers (`<!-- access: ... -->`) and every query is filtered to the roles of the person the agent acts for; quality gated by `eval/retrieval_golden.json` (hit rate, recall, MRR, zero access leaks) | local JSON index (dev, CI) → Azure AI Search free tier, keyless, optional semantic re-ranking (cloud) |
| **Audit trail** | Append-only, hash-chained events per case | `AuditEvent` table, SHA-256 chain |
| **Review queue** | Analyst UI: queue, case detail (documents, findings, risk with citations, tool timeline, audit), actions | React 18 + Vite + TypeScript + Tailwind |
| **Eval harness** | Golden cases → run → score decisions/findings/risk → markdown + JSON report | `eval/run_eval.py` |

## 4. Domain model

```
Case ──1:n── Document            (uploaded file + extraction result + confidence)
Case ──1:n── Party               (applicant, beneficial owners, controllers, signers)
Case ──1:n── CaseRun             (one agent execution: mode, model, findings, risk, recommendation)
Case ──1:n── AuditEvent          (hash-chained; every tool call/result, model msg, gate, analyst action)
Case ──0:1── ReviewTask          (open when outcome is escalate / needs_review)
```

Key enumerations:

- `CaseStatus`: `intake → processing → {approved | awaiting_customer | needs_review | escalated} → {approved | rejected}` (analyst closes the last two).
- `Outcome`: `auto_approve | request_more | escalate | needs_review`.
- `RiskRating`: `low | medium | high` with a numeric score 0–100 and the factors that produced it.
- `FindingSeverity`: `info | warning | critical`. Findings carry a stable `code` (e.g. `SCREENING_SANCTIONS_MATCH`, `UBO_COVERAGE_GAP`, `DOB_MISMATCH`) so policy gates and golden cases can reference them.

## 5. The agent

### 5.1 Why typed tools

Each tool declares its inputs and outputs as Pydantic models. The model receives the JSON schema; its arguments are validated before execution; the tool's return value is validated before it is written to the run. A malformed argument is returned to the model as a tool error, not executed. This is what makes the audit trail replayable and the eval harness meaningful.

```python
class ScreenPartiesInput(BaseModel):
    case_id: str
    parties: list[str] | None = None   # default: all parties on the case

class ScreeningHit(BaseModel):
    party_name: str; list_name: str; matched_name: str
    score: float; hit_type: Literal["sanctions","pep","adverse_media"]

class ScreenPartiesOutput(BaseModel):
    hits: list[ScreeningHit]; parties_screened: int; findings: list[Finding]
```

### 5.2 Tool set (first drop)

| Tool | Provider | Deterministic? | Produces findings |
|---|---|---|---|
| `extract_document` | Azure Document Intelligence (`prebuilt-idDocument`, `prebuilt-read`, layout) / mock fixtures | Provider-dependent; confidence surfaced | `DOC_LOW_CONFIDENCE`, `DOC_EXPIRED`, `DOC_UNREADABLE`, `DOC_INJECTION_SUSPECTED`, `INJECTION_CHECK_UNAVAILABLE` (w4: pattern scan + Prompt Shields, `app/agent/untrusted.py`) |
| `check_consistency` | Pure Python | Yes | `NAME_MISMATCH`, `DOB_MISMATCH`, `ADDRESS_MISMATCH`, `ID_NUMBER_MISMATCH`, `REQUIRED_DOC_MISSING` |
| `screen_parties` | List adapter (bundled demo SDN/PEP subset; OFAC SDN CSV or vendor API pluggable) | Yes (fuzzy match with fixed thresholds) | `SCREENING_SANCTIONS_MATCH`, `SCREENING_SANCTIONS_POSSIBLE`, `SCREENING_PEP_MATCH` |
| `resolve_beneficial_owners` | Pure Python over declared ownership graph | Yes | `UBO_COVERAGE_GAP`, `UBO_MISSING_ID`, `UBO_OVER_100`, `CONTROL_PERSON_MISSING` |
| `verify_address` | Address adapter (mock; USPS/Smarty/Melissa pluggable) | Provider-dependent | `ADDRESS_UNVERIFIED`, `ADDRESS_PO_BOX`, `ADDRESS_HIGH_RISK_JURISDICTION` |
| `rate_risk` | Policy RAG + LLM (or deterministic scorer in mock mode) | Floor is deterministic; LLM may raise, never lower | `RISK_HIGH_JURISDICTION`, `RISK_CASH_INTENSIVE`, … |
| `record_decision` | Decision policy engine | Yes | — (final) |

### 5.3 Orchestration loop

```
system prompt: role, the required-checks checklist, "you MUST call record_decision last"
user: case summary (parties, documents, declared data — no raw images)
loop (max 12 steps):
    model → tool_calls
    for each: validate args → execute → validate output → audit(tool_call, tool_result)
    append tool results to the conversation
until record_decision called or budget exhausted
```

Guardrails around the loop:

- **Required-checks checklist**: `record_decision` is rejected (tool error) until extraction, consistency, screening, ownership (business only), address and risk have all run. The model cannot skip screening.
- **Budget**: max tool calls and max tokens per run; exhaustion produces `needs_review` with finding `AGENT_BUDGET_EXHAUSTED`, never silent approval.
- **Scripted fallback**: `ORCHESTRATOR_MODE=scripted` runs the canonical sequence without a model. Used for tests, the eval baseline, and as the degraded mode if Azure OpenAI is unavailable.
- **Idempotent tools**: tools read from the case store and return the same result for the same inputs, so a run can be replayed from the audit log.

### 5.4 Decision policy engine (deterministic gates)

Evaluated in order; first matching gate wins. The model's recommendation is consulted only in the last step and only to tighten.

1. Any `SCREENING_SANCTIONS_MATCH` (score ≥ 0.90) → `escalate` (`GATE_SANCTIONS`). Non-negotiable.
2. Any `SCREENING_PEP_MATCH` → `escalate` (`GATE_PEP_EDD`).
3. Risk rating `high` → `escalate` (`GATE_HIGH_RISK`).
4. *(w4)* `DOC_INJECTION_SUSPECTED` → `escalate` (`GATE_DOCUMENT_INJECTION`, priority `identity`), ranked after sanctions and PEP; `INJECTION_CHECK_UNAVAILABLE` (Prompt Shields configured but unreachable) → at least `needs_review` (`GATE_INJECTION_CHECK_UNAVAILABLE`), so unscreened documents never auto-approve.
5. Any critical data conflict (`DOB_MISMATCH`, `ID_NUMBER_MISMATCH`, `UBO_OVER_100`, `DOC_UNREADABLE`): `DOC_UNREADABLE` alone → `request_more` (a clearer copy fixes it); a government-document conflict → `escalate` (`GATE_CRITICAL_DATA`, priority `identity`) because KYC-POL-005 §5 treats it as possible identity fraud, not a customer request; combined with an open screening alert → `escalate` (`GATE_CRITICAL_DATA_WITH_SCREENING`).
5. Any `warning` finding that a customer can fix (`REQUIRED_DOC_MISSING`, `DOC_LOW_CONFIDENCE`, `UBO_COVERAGE_GAP`, `UBO_MISSING_ID`, `CONTROL_PERSON_MISSING`, `ADDRESS_MISMATCH`, `ADDRESS_UNVERIFIED`, `ADDRESS_PO_BOX`, `NAME_MISMATCH`, `DOC_EXPIRED`) → `request_more` (`GATE_MISSING_INFO`) with the list of missing items.
6. `SCREENING_SANCTIONS_POSSIBLE` (0.75 ≤ score < 0.90), `SCREENING_PEP_POSSIBLE` or `SCREENING_ADVERSE_MEDIA` → `needs_review` (`GATE_POSSIBLE_MATCH`) — a human disposes the alert.
7. Risk rating `medium` → `needs_review` (`GATE_MEDIUM_RISK`).
8. Otherwise, if all required checks passed and risk is `low` → `auto_approve`. If the model recommended something stricter, take the stricter outcome and record `MODEL_TIGHTENED`.

Every gate evaluation is audited with the finding codes it saw, so an analyst (or an examiner) can see exactly why a case went where it went.

## 6. Policy-grounded risk rating (RAG)

Corpus (first drop ships realistic sample policies, replace with the bank's own):

- `kyc_cip_policy.md` — Customer Identification Program: required identifiers, acceptable documents, verification methods
- `cdd_edd_risk_rating.md` — risk factors, weights, rating bands, EDD triggers
- `beneficial_ownership.md` — 25% ownership prong, control prong, exemptions, certification
- `sanctions_screening_procedure.md` — lists, match thresholds, alert disposition, escalation
- `document_standards.md` — expiry rules, confidence thresholds, acceptable proof of address

Pipeline: chunk by heading (≈300–600 tokens) → embed (`text-embedding-3-small`; deterministic hashing embedder in mock mode) → store. Retrieval is hybrid: cosine top-k ∪ keyword top-k, re-ranked, returning `{policy, section, text, score}` citations.

`rate_risk` builds a fact sheet from the findings and party data, retrieves the relevant policy sections, and asks the model for `{rating, score, factors[{name, weight, policy_citation}], rationale}` in JSON mode at temperature 0. The **deterministic scorer** computes a floor from the same fact sheet (jurisdiction, entity type, industry, product, screening results, document quality). Final rating = max(model, floor). In mock mode the floor is the rating. Citations are stored with the run and rendered in the review queue.

## 7. Human-in-the-loop review queue

- A `ReviewTask` is created whenever the outcome is `escalate` or `needs_review`, with priority (`sanctions` 4h > `pep` 24h > `high_risk` 48h > `identity` 24h > `possible_match` 24h > `medium_risk` 72h), SLA due date, and reason codes.
- Analysts **claim** a task, see everything the agent saw (documents + extractions, findings with evidence, screening hits with scores, UBO tree, risk factors with policy citations, full tool timeline) and take one action: `approve`, `reject`, `request_more` (with message to customer), `escalate_to_bsa`, or `reassign`. Optional four-eyes: `approve` on a `sanctions`-priority task requires a second analyst.
- Every action writes an audit event with actor, timestamp, previous status, new status, notes.
- Analyst dispositions of screening alerts (true/false positive) are stored and fed back as **golden cases** for the eval harness — the review queue is also the labelling tool.

Identity: first drop uses an API key + `X-Analyst` header for local dev. Cloud target is Entra ID app roles (`KycAnalyst`, `BsaOfficer`, `Auditor`, `Admin`) exactly like an earlier project; the middleware slot is already there.

## 8. Audit trail

`AuditEvent(case_id, seq, ts, actor, event_type, payload_json, prev_hash, hash)` where `hash = sha256(prev_hash + seq + ts + actor + event_type + canonical_json(payload))`. `GET /cases/{id}/audit/verify` recomputes the chain. Event types: `case_created`, `document_added`, `run_started`, `llm_message`, `tool_call`, `tool_result`, `tool_error`, `policy_gate`, `decision`, `review_task_created`, `review_action`, `run_finished`. Model messages store the prompt and completion (or a content hash + blob pointer when the prompt exceeds a size limit). Prompt template version, model deployment name and retrieval index version are on every `run_started` event.

## 9. Evaluation harness

`eval/golden/*.json`: each golden case = an application (parties, declared data, documents with mock extraction fixtures) + expected `outcome`, expected `finding_codes` (must-have and must-not-have), expected `risk_rating`, and optional expected `missing_items`.

`python -m eval.run_eval --mode scripted|llm` runs every golden case through the real API code path (in-process), then scores:

- outcome accuracy (exact) and a confusion matrix
- finding recall/precision by code
- risk-rating accuracy (and "never below floor")
- required-checks coverage (did every mandatory tool run)
- latency and token usage per case (llm mode)

Output: `eval/reports/<timestamp>.md` + `.json`. CI gate: outcome accuracy must be 100% on the `must_pass` subset in scripted mode; in llm mode the report is informational until enough cases exist. First drop ships 10 golden cases covering every gate.

## 10. Azure topology (your tenant, dev)

| Resource | Name (proposed) | Notes |
|---|---|---|
| Resource group | `rg-onboardiq-dev` | Separate from `<openai-resource-group>` |
| LLM | Azure OpenAI your Azure OpenAI resource **or** local Ollama (`qwen2.5:14b` + `nomic-embed-text`, `ONBOARDIQ_LLM_PROVIDER=ollama`) | `gpt-4.1-mini` (chat/tools), `text-embedding-3-small`; endpoint base `https://<your-openai-resource>.openai.azure.com/` — strip the `/openai/v1` suffix the Foundry portal shows |
| Document Intelligence | `di-onboardiq-dev` (S0) | `prebuilt-idDocument`, `prebuilt-read`, `prebuilt-layout` |
| Azure AI Search | `srch-onboardiq-dev` (Basic) | policy index; optional in dev (local index works) |
| Storage | `stonboardiqdev` | `documents` container (private, SSE, soft delete) |
| Azure SQL | `sql-onboardiq-dev / onboardiq` | serverless; SQLite in local dev |
| Container Apps | `ca-onboardiq-api` | managed identity → OpenAI (`Cognitive Services OpenAI User`), DI (`Cognitive Services User`), Storage (`Blob Data Contributor`), Search (`Index Data Reader`) |
| Static Web App | `swa-onboardiq-review` | review queue |
| Key Vault | `kv-onboardiq-dev` | screening vendor keys, API key |
| App Insights | `appi-onboardiq-dev` | traces incl. tool spans; Managed Grafana optional |

Secrets: none in code; managed identity first, key fallback for local. `infra/main.bicep` + `deploy.sh` create the above.

## 11. Security & compliance posture

Implemented in drop 2:

| Concern | Implementation |
|---|---|
| Identity | Microsoft Entra ID bearer tokens (RS256, JWKS cached with rollover handling), audience `api://OnboardIQ-API`, v1 and v2 issuers. Dev mode keeps an API key + `X-Role` header for local work and CI. |
| Authorization | App roles `Admin`, `BsaOfficer`, `KycAnalyst`, `Auditor`, `Service`. Readers vs case-writers vs reviewers vs admin are FastAPI dependencies on every route; the UI hides actions the role cannot take, the API enforces regardless. A `BsaOfficer` acting on a sanctions case is the implicit second approver; analysts must name one. |
| Identity in audit | Every analyst action is stamped with the token subject (UPN), never a header the client controls. |
| Secrets | None in config in Azure: managed identity for Azure OpenAI, Document Intelligence (`disableLocalAuth`), Blob, SQL (Entra access token from the managed identity); break-glass SQL password only in Key Vault. |
| Documents | Private Blob container, soft delete, served only through `GET /cases/{id}/documents/{doc}/content` behind a reader role — no SAS/public URLs. Upload limited to 25 MB and document media types. |
| Transport & headers | HTTPS-only ingress, `X-Request-Id` propagated and returned, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, CORS allow-list from config. |
| Resilience | Async runs (202 + poll) so long LLM runs do not hit HTTP timeouts; per-case processing lock; exponential-backoff retries on Azure OpenAI (429/5xx/connection) and Document Intelligence; budget fallback in the agent loop. |
| Observability | OpenTelemetry spans (`agent.run` → `tool.*` → `llm.chat`) exported to App Insights; Prometheus `/metrics` (cases by outcome/gate, tool latency, LLM tokens, findings by code, screening hits, review actions, HTTP latency); JSON logs keyed by request id, case id, run id — no PII fields. |
| Screening data | Real OFAC SDN + Consolidated lists with aliases, refreshed daily by a Container Apps job onto a shared file volume, hot-reloaded via `POST /admin/screening/refresh`. |
| Schema | Alembic migrations, bounded string columns (SQL Server compatible), run at startup; CI asserts migrations match the models. |

- PII lives only in the case DB and blob store; logs carry case ids and finding codes, never names or ID numbers.
- Model calls go to Azure OpenAI in the bank's tenant (no training on data, data-at-rest in region).
- Screening thresholds, risk weights and gates are configuration under version control — changes are reviewable and show up in eval diffs.
- The model never sees raw images; it sees typed extraction results. Prompt-injection surface is limited to extracted text fields, which are treated as data (the system prompt says so, and tool outputs are wrapped as JSON, not free text).
- Auditor role is read-only over cases, runs, and the audit chain.

## 12. Roadmap

| Drop | Content |
|---|---|
| 1 | Backend with all tools in mock + Azure modes, decision engine, audit chain, policy RAG, review queue UI, eval harness with 12 golden cases, Bicep |
| **2 (this)** | Enterprise hardening: Entra app-role auth (API + MSAL SPA), Azure SQL + Alembic, Blob storage, async runs + locks + retries, OpenTelemetry/App Insights + Prometheus, real OFAC ingestion with daily job, gated Bicep (SQL, KV, no Search by default), GitHub Actions CI with eval gate, deployment runbook |
| 3 | Onboarding-portal integration (`Service` role, client-credentials): `request_more` pushes a document request; portal uploads land in the case; re-run on new evidence. Vendor PEP/adverse-media adapter, USPS/Smarty address adapter, stuck-run sweeper |
| 4 | Ongoing monitoring: periodic re-screen of approved customers, trigger reviews on list updates |
| 5 | Eval at scale: analyst dispositions auto-become golden cases; prompt/model regression gate in CI; drift dashboard in Grafana |

## 13. Design decisions (ADR summary)

- **ADR-1 Typed tools over free-form agents.** Pydantic in/out on every tool; the model never executes code or SQL.
- **ADR-2 Deterministic decision gates.** LLM recommends; policy decides; LLM can only tighten.
- **ADR-3 Mock-first providers.** Every external dependency has a fixture-driven twin so the whole system runs offline and in CI.
- **ADR-4 Hash-chained audit.** Cheap, examiner-friendly tamper evidence without an external ledger.
- **ADR-5 Model is configuration.** One LLM interface (`chat` with tools/JSON, `embed`) with Azure OpenAI, local Ollama and mock implementations; the orchestrator, tools and eval never know which is behind it.
- **ADR-6 SQLite → Azure SQL via SQLModel.** Same models, connection-string switch; no ORM-specific SQL in the app.
