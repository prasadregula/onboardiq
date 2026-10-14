# OnboardIQ — Architecture as deployed (Azure, dev)

*One-page view of what runs in Azure and how the AI makes decisions. Fictional data only (golden cases) until
threat T-07 (PII masking before the model) is closed. Deeper detail: [ARCHITECTURE.md](ARCHITECTURE.md),
[THREATMODEL.md](THREATMODEL.md), [DEPLOY.md](DEPLOY.md).*

## 1. What it is

An agentic KYC / account-onboarding platform for a regulated bank. An application (applicant, parties, documents)
goes in; the system investigates it, decides **auto-approve / request more information / needs review / escalate**,
and routes anything risky to a human analyst queue, with a tamper-evident audit trail of every step.

## 2. Deployed topology

```
Analyst browser ── Entra ID sign-in (PKCE, app roles: KycAnalyst | BsaOfficer | Auditor | Admin)
      │
      ▼
Review queue SPA ─────────── Azure Static Web Apps (React, MSAL)
      │  bearer token, audience = OnboardIQ-API
      ▼
OnboardIQ API ────────────── Azure Container Apps (FastAPI, 1 worker/replica, startup + liveness probes)
  │  system-assigned managed identity for everything below — no keys, no passwords in config
  ├─ Azure OpenAI ........... gpt-4.1-mini (agent) + text-embedding-3-small (policy RAG)
  ├─ Azure SQL (serverless) . cases, findings, runs, review tasks, hash-chained audit (Entra token auth)
  ├─ Blob Storage ........... uploaded documents
  ├─ Document Intelligence .. ID / document extraction
  ├─ Azure Files ............ OFAC lists + policy index, refreshed daily by a Container Apps Job
  └─ App Insights / Log Analytics . traces, token + cost per run, console logs
Image pull: separate user-assigned identity with AcrPull only.   Key Vault: break-glass SQL admin secret only.
```

## 3. How the AI works — "the model proposes, the code decides"

| Layer | What it does | Who is in charge |
|---|---|---|
| **Agent (LLM)** | Plans the investigation in a tool-calling loop (reason → call tool → read result → next step), writes the rationale | gpt-4.1-mini |
| **Tools** (7, typed I/O) | `extract_document`, `check_consistency`, `verify_address`, `screen_parties`, `resolve_beneficial_owners`, `rate_risk`, `record_decision` — produce the **facts** (findings with codes + evidence) | Deterministic code |
| **Policy RAG** | `rate_risk` retrieves bank policy sections; the model may add a risk factor **only if it cites a retrieved section** — uncited factors are rejected and logged | Code checks the model |
| **Decision gates** | Fixed priority: checks incomplete → sanctions → PEP → critical data conflict → high risk → unreadable doc → customer-fixable gaps → possible match → medium risk → straight-through | Deterministic code — the model can **only tighten**, never loosen |
| **Human in the loop** | Review queue with SLA, claim/disposition, role-based access | Analyst |
| **Audit** | Every event hash-chained (`sha256(prev ‖ seq ‖ ts ‖ actor ‖ event ‖ payload)`), verifiable per case | Code |

Screening: OFAC SDN + consolidated lists (~18k entries + aliases), coverage-aware fuzzy name matching, vessels and
aircraft excluded for customer parties; **confirmed** needs score ≥ 0.90 **and** a corroborating DOB or country,
otherwise **possible** → analyst disposition (KYC-POL-004 §3).

## 4. Results in the deployed environment

* 11/12 golden cases match expectations; the one deviation (G12) is a conservative over-escalation (medium → high),
  reviewed and accepted — the policy design guarantees deviations can only go the safe way.
* Local baseline (gpt-4.1-mini): ~7 s and ~US$0.0035 per case, ~14k input / 350 output tokens; cost, tokens and
  latency are recorded on every run.

## 5. Production lessons from the first deployment

| Symptom | Root cause | Fix / principle |
|---|---|---|
| Role assignment "principal not found" | New managed identity not yet replicated in Entra | Retry with backoff; create identities before they are needed |
| Key Vault `ForbiddenByRbac` | Control-plane ownership ≠ data-plane access | Grant the data-plane role explicitly (Secrets Officer) |
| ACR pull unauthorized on first revision | System identity exists only after the app does | Pre-created user-assigned pull identity |
| 404 after manual revision change | Single-revision mode routes to "latest" | Never deactivate by hand; let deploys switch |
| SPA called itself (`<!doctype` is not JSON) | Vite bakes `VITE_*` at build time; env file missing | Build-time config check before deploy |
| Container crash loop, no traceback | Alembic `fileConfig` replaced app logging | Keep app logging; log startup failures explicitly |
| Concurrent migrations | Several workers migrate at once | SQL application lock (`sp_getapplock`) |
| ODBC driver "file not found" | Floating base image moved OS release; autoremove dropped a dependency | Pin the OS release; `ldd` check fails the **build**, not the runtime |
| "Authentication with Integrated Security" | SQLAlchemy adds `Trusted_Connection` to a user-less URL | Inject an Entra access token (Microsoft's documented pattern) |
| Timestamp conversion error | `DATETIME` rejects offsets and loses µs (breaks audit hashes) | UTC type + `DATETIME2(6)`; test on the real engine |
| Fictional "Maria" = sanctioned vessel "MARIA" (1.00) | Subset-based fuzzy score + vessels screened as people | Coverage-aware score, entity-type filter, corroboration rule |

## 6. Known gaps (next)

T-07 PII minimisation before the model · per-agent identities / OBO · private endpoints instead of public SQL
firewall rules · migrations as a pre-deploy job · Azure SQL integration tests in CI · weak-AKA handling and a
vendor PEP/adverse-media feed.

---

### Interview talk track (90 seconds)

> "I built an agentic KYC platform on Azure for a regulated-bank setting. The LLM orchestrates the investigation
> through typed tools, but the facts come from deterministic code and the final decision from a policy engine — the
> model can tighten a decision, never loosen it. Risk factors the model adds must cite a retrieved policy section or
> they're rejected. Everything is passwordless with managed identities, and every step is hash-chained for audit.
> When I moved from the demo watchlist to the real OFAC lists, a clean applicant was flagged against a sanctioned
> vessel because the matcher scored subsets as perfect matches. I traced it from the analyst UI to the matcher and
> fixed it the way sanctions teams work: whole-name coverage, entity-type filtering, and a second identifier before
> anything is 'confirmed' — name-only hits go to an analyst. Golden-set accuracy in the cloud is 11/12, and the one
> deviation is a conservative over-escalation I traced and accepted."

**Likely follow-ups:** Why not let the model decide? (accountability and explainability: a
regulated decision needs testable, auditable rules and a human owner; deterministic gates give that, the model adds
speed and judgment within them) · How do you measure it?
(golden set with must-pass gates, per-run cost/latency/tokens, challenger runs on newer models) · What would you
change for production? (section 6).
