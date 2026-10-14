# OnboardIQ — deployment runbook (dev)

Target topology: Container Apps (API, scale-to-zero) · Azure SQL serverless · Blob Storage · Document Intelligence · shared Azure OpenAI your Azure OpenAI resource · Key Vault · App Insights · Static Web App (review queue) · daily OFAC refresh job. AI Search is off by default.

## 0. Prerequisites
- Resource providers registered on the subscription (once): `Microsoft.ContainerRegistry Microsoft.App Microsoft.OperationalInsights Microsoft.Insights Microsoft.Sql Microsoft.Storage Microsoft.KeyVault Microsoft.CognitiveServices Microsoft.Web` (`az provider register --namespace <ns>`).
- `az login` into the your subscription (PAYG), `jq`, Docker not needed (ACR builds the image).
- `text-embedding-3-small` and `gpt-4.1-mini` deployed in your Azure OpenAI resource.

## 1. Entra ID app registrations (one-time)
Two app registrations:

**OnboardIQ-API** (the backend)
- Expose an API → Application ID URI `api://<client-id>`, add scope `access_as_user` (admin consent).
- App roles (Users/Groups + Applications where noted): `Admin`, `BsaOfficer`, `KycAnalyst`, `Auditor`, `Service` (Applications — for an upstream portal or integration using the client-credential flow).
- Assign yourself `Admin` (Enterprise applications → Users and groups). Assign a test user `KycAnalyst` and another `Auditor`.

**OnboardIQ-Review** (the SPA)
- Platform: Single-page application, redirect URIs `http://localhost:5180` and later the SWA host. No trailing slash (AADSTS50011 otherwise).
- API permissions: `OnboardIQ-API / access_as_user` (delegated), grant admin consent.

Note the tenant id, API client id, SPA client id.

## 2. Deploy the platform
```bash
cd infra
RG=rg-onboardiq-dev ENTRA_API_CLIENT_ID=<api client id> ./deploy.sh
```
What it does: builds the API image in ACR, creates a user-assigned **image-pull identity** with `AcrPull` on the registry (before the Bicep deployment, so the first revision can pull — the app's system-assigned identity only exists after the app is created), deploys `main.bicep` with `params.dev.json`, grants the API's system-assigned identity `Cognitive Services OpenAI User` on your Azure OpenAI resource (cross-RG), stores the SQL admin password in Key Vault, prints next steps.

Then, once:
1. **SQL**: open `sql-grant-managed-identity.sql`, put the API Container App name in, run it against the `onboardiq` database as yourself (you are the Entra SQL admin). This lets the app connect passwordless: it gets an Entra token from its managed identity and passes it to the ODBC driver (`app/db.py` `_use_entra_token`), so no SQL password is ever in config.
2. **OFAC lists**: `az containerapp job start -n caj-onboardiq-dev-ofac -g rg-onboardiq-dev` (downloads sdn/alt/cons_prim/cons_alt to the shared `ofac` file share; runs daily at 06:00 UTC afterwards).
3. **Restart the API** so Alembic migrations run against SQL and the screening lists load. Check `GET /health` → `screening_provider: ofac`, `screening_entries` in the tens of thousands, `storage: azure-blob`, `tracing: true`.

## 3. Review queue on Static Web Apps
```bash
cd frontend
cp .env.example .env.production   # VITE_AUTH_MODE=entra, VITE_API_BASE=https://<api fqdn>, tenant/SPA/API client ids
npm run build
az staticwebapp create -n swa-onboardiq-review -g rg-onboardiq-dev --sku Free
swa deploy ./dist --deployment-token "$(az staticwebapp secrets list -n swa-onboardiq-review -g rg-onboardiq-dev --query properties.apiKey -o tsv)"
```
Then add the SWA origin (`https://<host>.azurestaticapps.net`) to `corsOrigins` in `params.dev.json` and to the SPA redirect URIs, and redeploy (`./deploy.sh` again; Bicep is idempotent).

## 4. Verify
- Sign in to the review queue → header shows your name and roles from the token.
- `POST /cases` with a golden case using a bearer token (`az account get-access-token --resource api://<api client id>` works for a user with a role), `POST /cases/{id}/run` → 202, poll `GET /cases/{id}`.
- App Insights → Transaction search: spans `agent.run` → `tool.*` → `llm.chat` with token attributes.
- `GET /metrics` (Prometheus) if you point Azure Managed Grafana or a local Grafana at it.

## 5. Operating notes
- **Secrets**: none in config. Managed identity for OpenAI, Document Intelligence (`disableLocalAuth`), Blob, SQL. The SQL admin password sits in Key Vault for break-glass only.
- **Scaling**: min 0 / max 2 replicas in dev; agent runs are background tasks inside the API replica, so set `minReplicas: 1` in prod (a scale-to-zero replica dying mid-run leaves the case in `processing` — the `/run` endpoint rejects a second run until an operator resets it; a stuck-run sweeper is on the drop-3 list).
- **Migrations**: run at startup (`alembic upgrade head`) — Alembic takes a lock, safe with 2 workers. Schema changes = new revision via `alembic revision --autogenerate` locally (CI checks `alembic check`).
- **Data retention**: blob soft-delete 30 days; audit events are append-only. Purging a case is intentionally not an API.
- **Cost**: ~$10–20/month idle (SQL serverless auto-pause 60 min, Container Apps free grant, Log Analytics) plus pennies per case. Enabling `deploySearch` adds ~$75/month.

## 6. Rollback
`az containerapp revision list` → `az containerapp ingress traffic set --revision-weight <previous>=100`. Migrations are forward-only; keep them additive.
