#!/usr/bin/env bash
# Everyday commands for OnboardIQ-lite (run from CloudShell after ./deploy.sh):
#   ./oiq.sh run [G01 G04 G07 G13]   run the agent on golden cases (default: all four)
#   ./oiq.sh waiting                 cases parked for an analyst
#   ./oiq.sh approve G07 | reject G07   analyst decision; resumes the review workflow
#   ./oiq.sh audit G13               print a case's audit chain
#   ./oiq.sh verify G13              recompute the hash chain
#   ./oiq.sh pause | resume | status kill switch
#   ./oiq.sh identities              which agent roles may call the model
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PAGER=""
REGION=${REGION:-us-east-1}
FN=onboardiq-lite-agent
mkdir -p build
invoke() {  # $1 = JSON payload; prints the function's JSON result
  local meta
  meta=$(aws lambda invoke --region "$REGION" --function-name "$FN" --cli-binary-format raw-in-base64-out \
    --cli-read-timeout 180 --payload "$1" build/out.json --query FunctionError --output text)
  if [ "$meta" != "None" ] && [ -n "$meta" ]; then
    echo "The function raised an error:"; cat build/out.json; echo
    echo "Details: CloudWatch -> Log groups -> /aws/lambda/$FN"; return 1
  fi
  cat build/out.json; }
pretty() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))"; }

case "${1:-help}" in
  run)
    shift; for c in "${@:-G01 G04 G07 G13}"; do for id in $c; do
      echo "== $id"; invoke "{\"action\":\"run_case\",\"case_id\":\"$id\"}" | python3 -c "
import json,sys; r=json.load(sys.stdin)
if 'error' in r: print('  ', r['error']); sys.exit()
print(f\"   outcome {r['outcome']:13} gate {r['gate']}\")
print(f\"   model recommended {r['model_recommendation']} · checks {', '.join(r['checks_run'])}\")
print(f\"   {r['turns']} model turns · {r['tokens']['in']}+{r['tokens']['out']} tokens · about US\${r['est_cost_usd']:.5f}\")
print('   review workflow:', 'finished (auto-approved)' if r['outcome']=='auto_approve' else 'waiting for an analyst -> ./oiq.sh approve|reject ' + r['case_id'])"
    done; done ;;
  waiting)
    aws dynamodb scan --region "$REGION" --table-name onboardiq-control --filter-expression "begins_with(#i, :r)" --expression-attribute-names '{"#i":"id"}' \
      --expression-attribute-values '{":r":{"S":"review#"}}' \
      --query "Items[].{case:id.S, outcome:outcome.S, gate:gate.S, since:since.S}" --output table ;;
  approve|reject)
    [ -n "${2:-}" ] || { echo "usage: ./oiq.sh $1 <case_id>"; exit 1; }
    invoke "{\"action\":\"analyst_decision\",\"case_id\":\"$2\",\"decision\":\"$1\",\"analyst\":\"${ANALYST:-prasad}\"}" | pretty ;;
  audit)
    [ -n "${2:-}" ] || { echo "usage: ./oiq.sh audit <case_id>"; exit 1; }
    aws dynamodb query --region "$REGION" --table-name onboardiq-audit --key-condition-expression "case_id = :c" \
      --expression-attribute-values "{\":c\":{\"S\":\"$2\"}}" --output json | python3 -c "
import json,sys
for it in json.load(sys.stdin)['Items']:
    p=json.loads(it['payload']['S']); extra=p.get('tool') or p.get('outcome') or p.get('reason') or ''
    print(f\"{it['seq']['N']:>3} {it['ts']['S'][11:23]} {it['event']['S']:18} {extra!s:24} {it['actor']['S'][:60]}  {it['hash']['S'][:10]}\")" ;;
  verify)
    [ -n "${2:-}" ] || { echo "usage: ./oiq.sh verify <case_id>"; exit 1; }
    invoke "{\"action\":\"verify_audit\",\"case_id\":\"$2\"}" | pretty ;;
  pause|resume)
    v=$([ "$1" = pause ] && echo true || echo false)
    aws dynamodb put-item --region "$REGION" --table-name onboardiq-control \
      --item "{\"id\":{\"S\":\"agents\"},\"paused\":{\"BOOL\":$v},\"changed_by\":{\"S\":\"${ANALYST:-prasad}\"}}"
    echo "agents paused = $v" ;;
  status)
    aws dynamodb get-item --region "$REGION" --table-name onboardiq-control --key '{"id":{"S":"agents"}}' \
      --query "Item.paused.BOOL" --output text | sed 's/^/agents paused = /' ;;
  identities)
    invoke '{"action":"identity_check"}' | pretty ;;
  *) sed -n '2,10p' "$0" ;;
esac
