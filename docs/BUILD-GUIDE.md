# OnboardIQ — complete build guide (from zero to running, then to Azure)

This is the single walkthrough. Follow it top to bottom on Linux, macOS, or Windows with WSL. Every step says what you should see before moving on. Ports: **API 8080, UI 5180**.

Phases:
- **A. Local, no cloud** (30 min) — the whole platform running with mock providers, tests and eval green, review queue in the browser.
- **B. Real model** — Azure OpenAI in your tenant (or local Ollama).
- **C. Real data** — OFAC sanctions lists, Document Intelligence.
- **D. Azure deployment** — Entra ID sign-in, Container Apps, SQL, Blob, App Insights.
- **E. Optional** — CI.

---

## A. Local build (no Azure needed)

### A1. Prerequisites
```bash
python3 --version    # 3.11 or 3.12
node --version       # 20 or 22
npm --version
git --version        # optional
```
If Python is missing: `sudo apt install python3.12 python3.12-venv`. If Node is missing: install via nvm.

### A2. Extract the project
```bash
mkdir -p ~/ai-projects && cd ~/ai-projects
unzip ~/Downloads/onboardiq.zip            # creates ~/ai-projects/onboardiq
cd onboardiq && ls
# docs  backend  frontend  infra  README.md  .github
```

### A3. Backend: environment and dependencies
```bash
cd ~/ai-projects/onboardiq/backend
python3 -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```
Expected: installs FastAPI, SQLModel, openai, azure-* SDKs, rapidfuzz, PyJWT, alembic, opentelemetry, prometheus-client. On Linux `pyodbc` needs `unixodbc`: `sudo apt install unixodbc unixodbc-dev` if pip complains (it is only needed for Azure SQL; you can also delete the `pyodbc` line for local work).

### A4. Configuration
```bash
cp .env.example .env
```
Leave everything as-is for phase A. The defaults are: `ONBOARDIQ_AUTH_MODE=apikey`, `ONBOARDIQ_ORCHESTRATOR_MODE=scripted`, `ONBOARDIQ_LLM_PROVIDER=auto`, mock extraction, mock screening, SQLite, local blob folder.

### A5. Run the tests
```bash
python -m pytest -q
```
Expected: `30 passed`. This covers policy gates, screening, OFAC CSV parsing, hash-chain tamper detection, the API flow, Entra JWT validation (with a locally generated key), async runs, uploads, and the LLM loop with a fake tool-calling model. If anything fails here, stop and check Python version / dependency install.

### A6. Run the evaluation harness
```bash
python -m eval.run_eval
```
Expected: 12 lines each starting `PASS`, then `12/12 passed, outcome accuracy 100%` and a report path under `eval/reports/`. Open the `.md` report — this is the baseline you compare every model or policy change against.

### A7. Seed the dev database and start the API
```bash
python seed.py
```
Expected: 12 lines `case_xxx  G01_... -> auto_approve (GATE_STP)` etc.
```bash
uvicorn app.main:app --reload --port 8080
```
Expected in the log: `startup complete: screening=mock-demo-list entries=9 llm=auto`. Leave this terminal running.

Check from a second terminal:
```bash
curl -s localhost:8080/health | python3 -m json.tool
# "ok": true, "auth_mode": "apikey", "orchestrator_mode": "scripted", "llm": "mock", "storage": "local"
curl -s -H 'X-API-Key: dev-key' localhost:8080/review/queue | python3 -m json.tool | head -30
```
Swagger UI: http://localhost:8080/docs (click **Authorize**, enter `dev-key` as the API key if you want to try calls there).

### A8. Frontend
```bash
cd ~/ai-projects/onboardiq/frontend
npm install
cp .env.example .env               # VITE_AUTH_MODE=apikey, VITE_ANALYST=analyst1, VITE_ROLE=KycAnalyst
npm run dev
```
Expected: `Local: http://localhost:5180/`. Open it. Header shows `API · scripted · mock · mock-demo-list` and `analyst1 · KycAnalyst`. The queue has 7 review tasks (sanctions, pep, high risk, identity, 2× possible match, medium risk); "All cases" shows 12.

### A9. Walk through a case (this is the demo)
1. Click **Claim** on *Delphine Okonkwo-Baptiste* (pep). It becomes claimed by analyst1.
2. Open the case. Tabs: **Findings** (SCREENING_PEP_MATCH, RISK_HIGH), **Screening** (the PEP hit with score and reasons), **Documents** (extracted fields + cross-document comparisons), **Risk & policy** (High, factors with policy references, RAG citations), **Agent timeline** (every tool call), **Audit** (hash-chained, "audit chain verified").
3. In the action panel: notes "EDD complete, source of wealth verified", disposition *True positive*, **Approve**. Case becomes `approved`, the last audit event is `review_action` by `analyst:analyst1`.
4. Try the sanctions case (*Viktor A. Sokolov-Marin*): approving as KycAnalyst requires a second approver; set `VITE_ROLE=BsaOfficer` in `frontend/.env`, restart `npm run dev`, and approve — you become the implicit second approver (visible in the audit payload).
5. Try `VITE_ROLE=Auditor`: the queue loses its Claim buttons and the case page says read-only. The API enforces this too (`POST /cases` → 403).

### A10. Create a case yourself
```bash
cd ~/ai-projects/onboardiq/backend
K='X-API-Key: dev-key'
jq .application eval/golden/G08_business_ubo_gap_request_more.json \
 | curl -s -H "$K" -H 'Content-Type: application/json' localhost:8080/cases -X POST -d @- | tee /tmp/case.json
CID=$(jq -r .id /tmp/case.json)
curl -s -i -H "$K" -H 'Content-Type: application/json' localhost:8080/cases/$CID/run -X POST -d '{}' | head -1   # HTTP/1.1 202 Accepted
sleep 2; curl -s -H "$K" localhost:8080/cases/$CID | jq '{status:.case.status, missing:.case.missing_items}'
```
Expected: `awaiting_customer` with two missing items (remaining 60% ownership, ID for Angela Park). Upload a document to it:
```bash
curl -s -H "$K" -F doc_type=drivers_license -F file=@/path/to/any.pdf localhost:8080/cases/$CID/documents
```
Then re-run (`POST .../run`) — in mock mode a real file has no fixture so it yields `DOC_UNREADABLE`; that is expected until phase C.

Phase A done: you have the full system running locally.

---

## B. Real model

### B1 (recommended). Azure OpenAI in your tenant
Prerequisite: in the Foundry resource your Azure OpenAI resource, deployments `gpt-4.1-mini` (exists) and `text-embedding-3-small` (deploy if missing: Foundry portal → Deployments → Deploy model).

```bash
cd ~/ai-projects/onboardiq/backend
az login                                   # your tenant; your account already has Cognitive Services OpenAI User
```
Edit `.env`:
```
ONBOARDIQ_ORCHESTRATOR_MODE=llm
ONBOARDIQ_LLM_PROVIDER=azure
ONBOARDIQ_AZURE_OPENAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com/
# leave ONBOARDIQ_AZURE_OPENAI_API_KEY empty -> DefaultAzureCredential (az login)
```
Then:
```bash
rm -f data/policy_index.json               # re-embed the policies with text-embedding-3-small
python -m eval.run_eval --mode llm         # ~1-3 s per case, < $0.50 total
```
Expected: 12/12 again, report now shows `model gpt-4.1-mini`, tokens per case, and — new — the model's own rationale in `run.model_rationale`. Compare with the scripted report: same outcomes, different rationale text, occasional extra tool retries. Restart uvicorn and the UI header shows `llm · azure-openai · gpt-4.1-mini`.

If you see `401`/`403` from Azure OpenAI: the resource needs your user in **Cognitive Services OpenAI User** (IAM on your Azure OpenAI resource), or set the API key temporarily.

### B2 (alternative). Local Ollama
```bash
ollama pull qwen2.5:14b && ollama pull nomic-embed-text
```
`.env`: `ONBOARDIQ_ORCHESTRATOR_MODE=llm`, `ONBOARDIQ_LLM_PROVIDER=ollama`. `rm -f data/policy_index.json`, run the eval. Expect a few `tool_error` events from the 14B model calling `record_decision` early; the checklist rejects it and outcomes stay correct.

---

## C. Real data

### C1. OFAC sanctions lists
```bash
cd ~/ai-projects/onboardiq/backend
python -m scripts.refresh_ofac             # downloads sdn.csv, alt.csv, cons_prim.csv, cons_alt.csv to data/ofac/
```
`.env`: `ONBOARDIQ_SCREENING_PROVIDER=ofac`. Restart uvicorn → log `OFAC lists loaded: NNNNN entries`. `/health` shows `screening_provider: ofac`. The demo PEP entries stay in (OFAC has no PEP data) so the PEP gate still works.
If the download fails (Treasury changed URLs before): download the four CSVs manually from https://sanctionslist.ofac.treas.gov/ into `data/ofac/` with those exact names.

### C2. Azure Document Intelligence
Portal → Create resource → *Document Intelligence* (F0 free tier is fine to start) in `<openai-resource-group>` or a new `rg-onboardiq-dev`. Under IAM give yourself **Cognitive Services User**.
`.env`:
```
ONBOARDIQ_EXTRACTION_PROVIDER=azure
ONBOARDIQ_DOC_INTEL_ENDPOINT=https://<name>.cognitiveservices.azure.com/
```
Restart, create a case (A10), upload a real driver's licence image as `drivers_license` with `-F party_id=<applicant party id>` (from `GET /cases/{id}` → parties), run it. The Documents tab shows `azure-document-intelligence / prebuilt-idDocument` with the real extracted fields.

---

## D. Azure deployment 

Full detail is in `docs/DEPLOY.md`; this is the order.

1. **Entra app registrations** (once): `OnboardIQ-API` (expose scope `access_as_user`, app roles Admin/BsaOfficer/KycAnalyst/Auditor/Service, assign yourself Admin) and `OnboardIQ-Review` (SPA, redirect `http://localhost:5180`, permission to the API scope, admin consent).
2. **Deploy**: `cd infra && RG=rg-onboardiq-dev ENTRA_API_CLIENT_ID=<api client id> ./deploy.sh`. Creates ACR + image, Container Apps env + API + daily OFAC job, SQL serverless, Storage (blob + file share), Document Intelligence, Key Vault, App Insights; grants the API identity on your Azure OpenAI resource.
3. **Once after deploy**: run `infra/sql-grant-managed-identity.sql` as yourself against the `onboardiq` DB; start the OFAC job (`az containerapp job start -n caj-onboardiq-dev-ofac -g rg-onboardiq-dev`); restart the API. `GET https://<api>/health` → `auth_mode: entra`, `storage: azure-blob`, `screening_provider: ofac`, `tracing: true`.
4. **Test with a real token**: `TOKEN=$(az account get-access-token --resource api://<api client id> --query accessToken -o tsv)` then `curl -H "Authorization: Bearer $TOKEN" https://<api>/me` → your UPN and roles.
5. **Review queue locally against Azure**: `frontend/.env` → `VITE_AUTH_MODE=entra`, `VITE_API_BASE=https://<api fqdn>`, tenant/SPA/API client ids. `npm run dev` → Microsoft sign-in → queue.
6. **Review queue on Static Web Apps**: `npm run build`, `swa deploy ./dist`, add the SWA origin to `corsOrigins` in `infra/params.dev.json` and to the SPA redirect URIs, run `deploy.sh` again.
7. **Seed cloud data**: with `ONBOARDIQ_AUTH_MODE` on the API being entra, run `seed.py` against the cloud DB is not possible from a laptop; instead post golden cases with the bearer token (A10 with `-H "Authorization: Bearer $TOKEN"` instead of the API key).

Cost at rest: ~$10–20/month (SQL auto-pauses, Container App scales to zero). Per case: pennies.

---

## E. Optional

- **CI**: push the repo to GitHub; `.github/workflows/ci.yml` runs lint, tests, the scripted eval gate, `alembic check`, frontend build, a Docker smoke test and `bicep build` on every push.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `30 passed` but `run_eval` fails on G10 | you edited `cdd_edd_risk_rating.md`; delete `data/policy_index.json` and re-run |
| UI header says `API offline` | API not on 8080, or `frontend/.env` missing (`cp .env.example .env`) |
| UI shows `Signed in as analyst` | `frontend/.env` missing `VITE_ANALYST` — copy the example again, restart Vite |
| `409 a run is already in progress` | the case is stuck in `processing` after a crash; `sqlite3 data/onboardiq.db "update cases set status='intake' where id='case_...'"` |
| `attempt to write a readonly database` | you deleted the SQLite file while uvicorn was running; restart uvicorn |
| Azure OpenAI 401 | missing **Cognitive Services OpenAI User** role, or endpoint has `/openai/v1` suffix (strip it) |
| AADSTS50011 in the browser | SPA redirect URI must be under the *Single-page application* platform, exact origin, no trailing slash |
| CORS "Failed to fetch" against Azure | add the UI origin to `corsOrigins` (no `*`), redeploy |
| `pyodbc` install error on Linux | `sudo apt install unixodbc unixodbc-dev`, or remove the `pyodbc` line if not using Azure SQL |
