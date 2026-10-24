#!/usr/bin/env bash
# Warm the API before a live demo so nobody waits for a cold start (container + serverless SQL resume take ~1 minute),
# then go back to scale-to-zero.
#   ./demo-warm.sh          warm for 60 minutes, then cool down automatically
#   ./demo-warm.sh 90       warm for 90 minutes
#   ./demo-warm.sh --cool   scale back to zero now (always run this when the demo is over)
# Warm costs roughly US$0.30-0.50 an hour (one replica + SQL awake). If the laptop sleeps, the automatic cool-down
# does not run - check with ./cost-check.sh afterwards. A redeploy (./deploy.sh) also resets minReplicas to 0.
set -euo pipefail
export AZURE_CORE_ONLY_SHOW_ERRORS=true
cd "$(dirname "$0")"
[ -f .deploy.env ] && . ./.deploy.env
RG=${RG:-rg-onboardiq-dev}
APP=$(az containerapp list -g "$RG" --query "[0].name" -o tsv)
URL="https://$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"

cool() { az containerapp update -n "$APP" -g "$RG" --min-replicas 0 -o none && \
         echo "Cooled down: minReplicas=0. The API scales to zero; SQL pauses about an hour after the last request."; }
if [ "${1:-}" = "--cool" ]; then cool; exit 0; fi

MINUTES=${1:-60}
echo "Keeping one replica running for $MINUTES minutes..."
az containerapp update -n "$APP" -g "$RG" --min-replicas 1 -o none
echo "Waiting for $URL to answer (startup runs database migrations, which also wakes SQL)..."
for _ in $(seq 1 36); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL/health" || true)" = "200" ] && break
  sleep 5
done
curl -s --max-time 20 "$URL/health" | jq '{ok, llm_ready, screening_entries, agent_identities}'
nohup bash -c "sleep $((MINUTES * 60)); az containerapp update -n '$APP' -g '$RG' --min-replicas 0 -o none" >/dev/null 2>&1 &
echo
echo "Warm. Automatic cool-down in $MINUTES minutes (only if this machine stays awake)."
echo "When the demo is over:  ./demo-warm.sh --cool"
