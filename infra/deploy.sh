#!/usr/bin/env bash
# Deploys OnboardIQ. Prereqs: az login, jq (the image is built in ACR - no local Docker needed).
# Environment-specific values live OUTSIDE the repo:
#   infra/.deploy.env      OPENAI_RG, OPENAI_NAME, ENTRA_API_CLIENT_ID (and optionally RG, LOC)   - gitignored
#   infra/params.dev.json  copy of params.example.json with your endpoint and CORS origin       - gitignored
#   ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
[ -f .deploy.env ] && . ./.deploy.env
RG=${RG:-rg-onboardiq-dev}
LOC=${LOC:-westus2}
ACR=${ACR:-acronboardiq${RG##*-}}          # set ACR=<existing acr name> to reuse
: "${OPENAI_RG:?set OPENAI_RG (resource group of your Azure OpenAI resource) in infra/.deploy.env}"
: "${OPENAI_NAME:?set OPENAI_NAME (your Azure OpenAI resource name) in infra/.deploy.env}"
PARAMS=${PARAMS:-params.dev.json}
[ -f "$PARAMS" ] || { echo "Missing $PARAMS - copy params.example.json to $PARAMS and fill in your values" >&2; exit 1; }
TAG=${TAG:-$(date +%Y%m%d%H%M)}
: "${ENTRA_API_CLIENT_ID:?set ENTRA_API_CLIENT_ID to the OnboardIQ-API app registration client id}"
SQL_PASSWORD=${SQL_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)Aa1!}
ME_OID=$(az ad signed-in-user show --query id -o tsv)
ME_UPN=$(az ad signed-in-user show --query userPrincipalName -o tsv)

# Retry a command (new identities take 30-120 s to replicate in Entra; new RBAC grants take ~1 min to apply).
retry() { local n=0; until "$@"; do n=$((n+1)); [ $n -ge 12 ] && { echo "Gave up after $n tries: $*" >&2; return 1; }
  echo "  ...not ready yet, retrying in 15s ($n/12)"; sleep 15; done; }
# Idempotent role assignment for a service principal, with retry for replication lag.
grant_sp() { local oid=$1 role=$2 scope=$3
  if [ -n "$(az role assignment list --assignee "$oid" --role "$role" --scope "$scope" --query '[0].id' -o tsv 2>/dev/null)" ]; then
    echo "  '$role' already assigned"; return 0; fi
  retry az role assignment create --assignee-object-id "$oid" --assignee-principal-type ServicePrincipal \
    --role "$role" --scope "$scope" -o none; }

az group create -n "$RG" -l "$LOC" -o none
az acr show -n "$ACR" -g "$RG" -o none 2>/dev/null || az acr create -n "$ACR" -g "$RG" --sku Basic -o none
az acr build -r "$ACR" -t "onboardiq-api:$TAG" -t onboardiq-api:latest ../backend
IMAGE="$ACR.azurecr.io/onboardiq-api:$TAG"
ACR_ID=$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)

# Image-pull identity: created and granted AcrPull BEFORE the Bicep deployment, so the first container revision can
# pull from the private registry (a system-assigned identity only exists after the app is created - chicken and egg).
PULL_ID_NAME=${PULL_ID_NAME:-id-onboardiq-${RG##*-}-pull}
az identity show -n "$PULL_ID_NAME" -g "$RG" -o none 2>/dev/null || az identity create -n "$PULL_ID_NAME" -g "$RG" -l "$LOC" -o none
PULL_ID=$(az identity show -n "$PULL_ID_NAME" -g "$RG" --query id -o tsv)
PULL_PRINCIPAL=$(az identity show -n "$PULL_ID_NAME" -g "$RG" --query principalId -o tsv)
echo "Waiting for the pull identity to appear in Entra..."
retry az ad sp show --id "$PULL_PRINCIPAL" -o none
if [ -z "$(az role assignment list --assignee "$PULL_PRINCIPAL" --role AcrPull --scope "$ACR_ID" --query '[0].id' -o tsv)" ]; then
  grant_sp "$PULL_PRINCIPAL" AcrPull "$ACR_ID"
  echo "Waiting 60s for the AcrPull role assignment to propagate..."
  sleep 60
fi
OPENAI_ID=$(az cognitiveservices account show -n "$OPENAI_NAME" -g "$OPENAI_RG" --query id -o tsv)

az deployment group create -g "$RG" -f main.bicep -p "@$PARAMS" \
  -p apiImage="$IMAGE" acrLoginServer="$ACR.azurecr.io" pullIdentityId="$PULL_ID" \
     openAiResourceId="$OPENAI_ID" entraApiClientId="$ENTRA_API_CLIENT_ID" \
     sqlAdminPassword="$SQL_PASSWORD" sqlAadAdminLogin="$ME_UPN" sqlAadAdminObjectId="$ME_OID" \
  --query properties.outputs -o json > outputs.json
cat outputs.json

PRINCIPAL=$(jq -r .apiPrincipalId.value outputs.json)
API_NAME=$(jq -r .apiPrincipalName.value outputs.json)
KV=$(jq -r .keyVault.value outputs.json)

# Cross-RG: ONLY the orchestrator and risk agent identities may call the shared Azure OpenAI (w3-1 least privilege)
ORCH_PRINCIPAL=$(jq -r .orchestratorPrincipalId.value outputs.json)
RISK_PRINCIPAL=$(jq -r .riskPrincipalId.value outputs.json)
DI_ID=$(jq -r .docIntelId.value outputs.json)
echo "Granting Azure OpenAI access to the orchestrator and risk agent identities..."
grant_sp "$ORCH_PRINCIPAL" "Cognitive Services OpenAI User" "$OPENAI_ID"
grant_sp "$RISK_PRINCIPAL" "Cognitive Services OpenAI User" "$OPENAI_ID"
# Revoke what the API's own identity no longer needs (earlier drops granted it OpenAI + Document Intelligence)
for pair in "Cognitive Services OpenAI User|$OPENAI_ID" "Cognitive Services User|$DI_ID"; do
  role=${pair%%|*}; scope=${pair#*|}
  if [ -n "$(az role assignment list --assignee "$PRINCIPAL" --role "$role" --scope "$scope" --query '[0].id' -o tsv)" ]; then
    az role assignment delete --assignee "$PRINCIPAL" --role "$role" --scope "$scope" -o none
    echo "  revoked '$role' from the API identity (now held only by the agent that needs it)"
  fi
done
# On-behalf-of (w3-3): let the OnboardIQ-API app registration trust the obo managed identity (secretless OBO).
OBO_PRINCIPAL=$(jq -r .oboPrincipalId.value outputs.json)
TENANT_ID=$(az account show --query tenantId -o tsv)
if [ -z "$(az ad app federated-credential list --id "$ENTRA_API_CLIENT_ID" --query "[?name=='onboardiq-obo-mi'].name" -o tsv 2>/dev/null)" ]; then
  az ad app federated-credential create --id "$ENTRA_API_CLIENT_ID" --parameters "{\"name\":\"onboardiq-obo-mi\",
    \"issuer\":\"https://login.microsoftonline.com/$TENANT_ID/v2.0\",\"subject\":\"$OBO_PRINCIPAL\",
    \"audiences\":[\"api://AzureADTokenExchange\"],\"description\":\"OnboardIQ on-behalf-of via managed identity\"}" -o none \
    && echo "Federated credential 'onboardiq-obo-mi' added to the OnboardIQ-API app registration" \
    || echo "WARN: could not add the federated credential (need Application Administrator or app owner) - OBO demo disabled"
fi
# (image pulls use the user-assigned pull identity configured in main.bicep)
# Keep the SQL admin password in Key Vault (only needed for break-glass; the app uses managed identity).
# The vault uses RBAC, so the deploying user needs a data-plane role before writing a secret.
KV_ID=$(az keyvault show -n "$KV" -g "$RG" --query id -o tsv)
if [ -z "$(az role assignment list --assignee "$ME_OID" --role "Key Vault Secrets Officer" --scope "$KV_ID" --query '[0].id' -o tsv)" ]; then
  az role assignment create --assignee-object-id "$ME_OID" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$KV_ID" -o none
  echo "Granted you Key Vault Secrets Officer on $KV (takes ~1 min to apply)"
fi
retry az keyvault secret set --vault-name "$KV" -n sql-admin-password --value "$SQL_PASSWORD" -o none
echo "SQL admin password stored in Key Vault secret 'sql-admin-password'"
echo
echo "API:            $(jq -r .apiUrl.value outputs.json)"
echo "API identity:   $API_NAME ($PRINCIPAL)"
echo "SQL server:     $(jq -r .sqlServerFqdn.value outputs.json)"
echo
echo "NEXT:"
echo " 1. Grant the managed identity in SQL: edit sql-grant-managed-identity.sql with '$API_NAME' and run it as $ME_UPN"
echo " 2. Seed the OFAC lists once:  az containerapp job start -n caj-onboardiq-dev-ofac -g $RG"
echo " 3. Restart the API so migrations + screening load:  az containerapp revision restart -n $API_NAME -g $RG --revision \$(az containerapp show -n $API_NAME -g $RG --query properties.latestRevisionName -o tsv)"
echo " 4. Deploy the review queue (frontend/README-SWA.md) and add its origin to corsOrigins in $PARAMS, then redeploy."
