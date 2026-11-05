#!/usr/bin/env bash
# Removes everything deploy.sh created. Keeps the guardrail (free while idle) unless you pass --all.
set -uo pipefail
export AWS_PAGER=""
REGION=${REGION:-us-east-1}
ACCT=$(aws sts get-caller-identity --query Account --output text)
SM_ARN="arn:aws:states:$REGION:$ACCT:stateMachine:onboardiq-review"
echo "== stopping open review workflows and deleting the state machine"
for e in $(aws stepfunctions list-executions --region "$REGION" --state-machine-arn "$SM_ARN" --status-filter RUNNING \
           --query "executions[].executionArn" --output text 2>/dev/null); do
  aws stepfunctions stop-execution --region "$REGION" --execution-arn "$e" >/dev/null && echo "   stopped $e"; done
aws stepfunctions delete-state-machine --region "$REGION" --state-machine-arn "$SM_ARN" 2>/dev/null && echo "   deleted"
echo "== function and logs"
aws lambda delete-function --region "$REGION" --function-name onboardiq-lite-agent >/dev/null 2>&1 && echo "   function deleted"
aws logs delete-log-group --region "$REGION" --log-group-name /aws/lambda/onboardiq-lite-agent 2>/dev/null && echo "   logs deleted"
echo "== tables"
for t in onboardiq-audit onboardiq-control; do
  aws dynamodb delete-table --region "$REGION" --table-name "$t" >/dev/null 2>&1 && echo "   $t deleted"; done
echo "== roles"
for r in onboardiq-lite-exec onboardiq-lite-sfn onboardiq-orchestrator onboardiq-extraction onboardiq-risk onboardiq-screening; do
  aws iam get-role --role-name "$r" >/dev/null 2>&1 || continue
  for p in $(aws iam list-role-policies --role-name "$r" --query PolicyNames --output text); do
    aws iam delete-role-policy --role-name "$r" --policy-name "$p"; done
  for p in $(aws iam list-attached-role-policies --role-name "$r" --query "AttachedPolicies[].PolicyArn" --output text); do
    aws iam detach-role-policy --role-name "$r" --policy-arn "$p"; done
  aws iam delete-role --role-name "$r" && echo "   $r deleted"
done
if [ "${1:-}" = "--all" ]; then
  GR=$(aws bedrock list-guardrails --region "$REGION" --query "guardrails[?name=='onboardiq-docs'].id | [0]" --output text)
  [ -n "$GR" ] && [ "$GR" != "None" ] && aws bedrock delete-guardrail --region "$REGION" --guardrail-identifier "$GR" && echo "== guardrail deleted"
fi
echo "Done. Nothing from OnboardIQ-lite is left running."
