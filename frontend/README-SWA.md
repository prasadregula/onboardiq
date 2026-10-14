# Deploying the review queue to Azure Static Web Apps

The review queue is a static React build. Vite bakes `VITE_*` values in at **build time**, so the API URL and
Entra IDs must be in `frontend/.env.production` before `npm run build` (none of them are secrets).

1. Create the site once and note its hostname:
   `az staticwebapp create -n swa-onboardiq-dev -g rg-onboardiq-dev -l westus2 --sku Free`
   `az staticwebapp show -n swa-onboardiq-dev -g rg-onboardiq-dev --query defaultHostname -o tsv`
2. Entra: add `https://<hostname>` (no trailing slash) as a **Single-page application** redirect URI on the
   OnboardIQ-Review app registration (not "Web" - that gives AADSTS9002326). Assign users an app role on the
   OnboardIQ-API enterprise app.
3. CORS: put `https://<hostname>` in `corsOrigins` in `infra/params.dev.json` (exact origin, no `*`, no trailing
   slash) and redeploy - or patch live with `az containerapp ingress cors update` + `ONBOARDIQ_CORS_ORIGINS`.
4. Create `.env.production` (use printf - heredocs often break when pasted):
   ```
   VITE_API_BASE=https://<api fqdn from deploy.sh>      # no /api suffix - that prefix exists only in the dev proxy
   VITE_AUTH_MODE=entra
   VITE_TENANT_ID=<tenant guid>
   VITE_CLIENT_ID=<OnboardIQ-Review client id>
   VITE_API_CLIENT_ID=<OnboardIQ-API client id>
   ```
   A `.env.local` overrides `.env.production` - park it during production builds.
5. Build and verify the values were baked in:
   `rm -rf dist && npm run build && grep -q azurecontainerapps dist/assets/*.js && echo BUILD-OK`
6. Deploy:
   `npx @azure/static-web-apps-cli deploy ./dist --deployment-token "$(az staticwebapp secrets list -n swa-onboardiq-dev -g rg-onboardiq-dev --query properties.apiKey -o tsv)" --env production`

`public/staticwebapp.config.json` adds the SPA fallback for react-router. Symptom guide:
`Unexpected token '<' ... not valid JSON` = the build has no API URL (calls hit the SPA's index.html);
`AADSTS50011` = redirect URI missing; endless "Loading" = API not responding (check replicas / console logs).
