#!/usr/bin/env bash
# Read-only cost check for the OnboardIQ dev environment. Safe to run any time: it only reads management data,
# so it does NOT wake the SQL database or start a container.
#   ./cost-check.sh           show status and month-to-date cost
#   ./cost-check.sh --sleep   also force the API back to zero replicas (if someone left it warm)
set -uo pipefail
export AZURE_CORE_ONLY_SHOW_ERRORS=true   # hide extension "behaviour altered" warnings
cd "$(dirname "$0")"
[ -f .deploy.env ] && . ./.deploy.env
RG=${RG:-rg-onboardiq-dev}
SUB_ID=$(az account show --query id -o tsv)
APP=$(az containerapp list -g "$RG" --query "[0].name" -o tsv)

echo "== Container App ($APP)"
MIN=$(az containerapp show -n "$APP" -g "$RG" --query properties.template.scale.minReplicas -o tsv)
REV=$(az containerapp show -n "$APP" -g "$RG" --query properties.latestRevisionName -o tsv)
RUNNING=$(az containerapp replica list -n "$APP" -g "$RG" --revision "$REV" --query "length(@)" -o tsv 2>/dev/null || echo 0)
echo "   minReplicas: $MIN   running replicas now: ${RUNNING:-0}"
[ "$MIN" != "0" ] && echo "   WARNING: minReplicas is $MIN - the API never scales to zero and bills 24x7"

echo "== Azure SQL"
SQL_SERVER=$(az sql server list -g "$RG" --query "[0].name" -o tsv)
az sql db show -g "$RG" -s "$SQL_SERVER" -n onboardiq --query "{status:status,autoPauseMinutes:autoPauseDelay}" -o table
echo "   (Online right after you used the app is normal; it should be Paused about an hour after the last request)"

echo "== Month-to-date cost for $RG, by service"
BODY='{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None",
  "aggregation":{"cost":{"name":"Cost","function":"Sum"}},"grouping":[{"type":"Dimension","name":"ServiceName"}]}}'
az rest --method post \
  --url "https://management.azure.com/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.CostManagement/query?api-version=2023-03-01" \
  --body "$BODY" -o json 2>/dev/null \
  | jq -r '.properties.rows | sort_by(-.[0]) | (map(.[0]) | add // 0) as $t
           | (.[] | "   \(.[1] | .[0:34] | . + " " * (34 - length))  \(.[0] * 100 | round / 100) \(.[2])"),
             "   ----------------------------------  ",
             "   TOTAL                               \($t * 100 | round / 100)"' \
  || echo "   (cost data unavailable - needs Cost Management Reader; it can also lag by up to 24 h)"

if [ "${1:-}" = "--sleep" ] && [ "$MIN" != "0" ]; then
  az containerapp update -n "$APP" -g "$RG" --min-replicas 0 -o none && echo "== Set minReplicas to 0"
fi
