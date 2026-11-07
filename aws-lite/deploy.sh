#!/usr/bin/env bash
# OnboardIQ-lite on AWS: one-shot, idempotent deploy. Run from AWS CloudShell (no access keys needed):
#   cd aws-lite && ./deploy.sh
# Safe to re-run: existing resources are reused and updated, never duplicated.
#
# Everything here is free or pay-per-use with nothing billed while idle:
#   Lambda (always-free 1M requests/month), DynamoDB provisioned 1 RCU/1 WCU per table (always-free 25),
#   Step Functions Standard (always-free 4,000 transitions/month; waiting costs nothing), CloudWatch Logs (7-day
#   retention), IAM (free). Pay-per-use: Bedrock Nova Micro and Guardrails, fractions of a cent per case.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PAGER=""
REGION=${REGION:-us-east-1}
MODEL_ID=${MODEL_ID:-amazon.nova-micro-v1:0}
FN=onboardiq-lite-agent
SM=onboardiq-review
AUDIT=onboardiq-audit
CONTROL=onboardiq-control
GUARDRAIL_NAME=onboardiq-docs
EXEC_ROLE=onboardiq-lite-exec
SFN_ROLE=onboardiq-lite-sfn
AGENTS=(orchestrator extraction risk screening)

ACCT=$(aws sts get-caller-identity --query Account --output text)
FN_ARN="arn:aws:lambda:$REGION:$ACCT:function:$FN"
SM_ARN="arn:aws:states:$REGION:$ACCT:stateMachine:$SM"
MODEL_ARN="arn:aws:bedrock:$REGION::foundation-model/$MODEL_ID"
AUDIT_ARN="arn:aws:dynamodb:$REGION:$ACCT:table/$AUDIT"
CONTROL_ARN="arn:aws:dynamodb:$REGION:$ACCT:table/$CONTROL"
mkdir -p build
step() { echo; echo "== $*"; }
retry() { local n=0; until "$@" >/dev/null 2>build/last-error.txt; do n=$((n+1));
  if [ $n -ge 10 ]; then echo "FAILED after $n tries: $*" >&2; cat build/last-error.txt >&2; return 1; fi
  echo "   (waiting for IAM to propagate, retry $n/10)"; sleep 8; done; }

# ------------------------------------------------------------------------------------------- 1. guardrail
step "1/7 Bedrock guardrail ($GUARDRAIL_NAME)"
GR_ID=$(aws bedrock list-guardrails --region "$REGION" --query "guardrails[?name=='$GUARDRAIL_NAME'].id | [0]" --output text)
PII='{"piiEntitiesConfig":[
  {"type":"NAME","action":"ANONYMIZE","inputAction":"ANONYMIZE","inputEnabled":true,"outputAction":"ANONYMIZE","outputEnabled":true},
  {"type":"ADDRESS","action":"ANONYMIZE","inputAction":"ANONYMIZE","inputEnabled":true,"outputAction":"ANONYMIZE","outputEnabled":true}]}'
if [ -z "$GR_ID" ] || [ "$GR_ID" = "None" ]; then
  GR_ID=$(aws bedrock create-guardrail --region "$REGION" --name "$GUARDRAIL_NAME" \
    --blocked-input-messaging "Blocked: possible instructions in a document." --blocked-outputs-messaging "Blocked." \
    --content-policy-config '{"filtersConfig":[{"type":"PROMPT_ATTACK","inputStrength":"HIGH","outputStrength":"NONE"}]}' \
    --sensitive-information-policy-config "$PII" --query guardrailId --output text)
  echo "   created $GR_ID"
else
  echo "   reusing $GR_ID"
fi
GR_ARN="arn:aws:bedrock:$REGION:$ACCT:guardrail/$GR_ID"

# ------------------------------------------------------------------------------------------- 2. tables
step "2/7 DynamoDB tables (provisioned 1/1: inside the always-free allowance)"
make_table() { local t=$1 key=$2; shift 2
  if aws dynamodb describe-table --region "$REGION" --table-name "$t" >/dev/null 2>&1; then echo "   reusing $t"; return; fi
  aws dynamodb create-table --region "$REGION" --table-name "$t" "$@" \
    --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1 --table-class STANDARD >/dev/null
  aws dynamodb wait table-exists --region "$REGION" --table-name "$t"; echo "   created $t (key: $key)"; }
make_table "$AUDIT" "case_id + seq" \
  --attribute-definitions AttributeName=case_id,AttributeType=S AttributeName=seq,AttributeType=N \
  --key-schema AttributeName=case_id,KeyType=HASH AttributeName=seq,KeyType=RANGE
make_table "$CONTROL" "id" \
  --attribute-definitions AttributeName=id,AttributeType=S --key-schema AttributeName=id,KeyType=HASH
aws dynamodb put-item --region "$REGION" --table-name "$CONTROL" \
  --item '{"id":{"S":"agents"},"paused":{"BOOL":false}}' --condition-expression "attribute_not_exists(#i)" --expression-attribute-names '{"#i":"id"}'  \
  >/dev/null 2>&1 && echo "   kill switch item created (paused=false)" || echo "   kill switch item already present"

# ------------------------------------------------------------------------------------------- 3. roles
step "3/7 IAM roles (function role has NO model access; each agent role has only its job)"
ensure_role() { local name=$1 trust=$2 desc=$3
  if aws iam get-role --role-name "$name" >/dev/null 2>&1; then
    retry aws iam update-assume-role-policy --role-name "$name" --policy-document "$trust"; echo "   reusing $name"
  else
    retry aws iam create-role --role-name "$name" --assume-role-policy-document "$trust" --description "$desc"
    aws iam wait role-exists --role-name "$name"; echo "   created $name"
  fi; }
svc_trust() { echo "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$1\"},\"Action\":\"sts:AssumeRole\"}]}"; }

ensure_role "$EXEC_ROLE" "$(svc_trust lambda.amazonaws.com)" "OnboardIQ-lite function: logs, tables, review workflow, assume agent roles"
AGENT_ARNS=$(printf '"arn:aws:iam::%s:role/onboardiq-%s",' "$ACCT" "${AGENTS[@]}"); AGENT_ARNS="[${AGENT_ARNS%,}]"
retry aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name onboardiq-lite --policy-document "{
 \"Version\":\"2012-10-17\",\"Statement\":[
  {\"Sid\":\"OwnLogsOnly\",\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:$REGION:$ACCT:log-group:/aws/lambda/$FN:*\"},
  {\"Sid\":\"BecomeAnAgent\",\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Resource\":$AGENT_ARNS},
  {\"Sid\":\"AuditAndControl\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:Query\",\"dynamodb:DeleteItem\"],\"Resource\":[\"$AUDIT_ARN\",\"$CONTROL_ARN\"]},
  {\"Sid\":\"StartReview\",\"Effect\":\"Allow\",\"Action\":\"states:StartExecution\",\"Resource\":\"$SM_ARN\"},
  {\"Sid\":\"ResumeReview\",\"Effect\":\"Allow\",\"Action\":[\"states:SendTaskSuccess\",\"states:SendTaskFailure\"],\"Resource\":\"*\"}]}"
EXEC_ARN="arn:aws:iam::$ACCT:role/$EXEC_ROLE"

AGENT_TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"$EXEC_ARN\"},\"Action\":\"sts:AssumeRole\"}]}"
INVOKE_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"bedrock:InvokeModel\",\"bedrock:InvokeModelWithResponseStream\"],\"Resource\":\"$MODEL_ARN\"}]}"
GUARD_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"bedrock:ApplyGuardrail\",\"Resource\":\"$GR_ARN\"}]}"
for a in "${AGENTS[@]}"; do
  ensure_role "onboardiq-$a" "$AGENT_TRUST" "OnboardIQ $a agent: assumed only by the OnboardIQ-lite function"
  for p in $(aws iam list-role-policies --role-name "onboardiq-$a" --query PolicyNames --output text); do
    aws iam delete-role-policy --role-name "onboardiq-$a" --policy-name "$p"   # start from a clean slate each deploy
  done
done
retry aws iam put-role-policy --role-name onboardiq-orchestrator --policy-name invoke-one-model --policy-document "$INVOKE_POLICY"
retry aws iam put-role-policy --role-name onboardiq-risk --policy-name invoke-one-model --policy-document "$INVOKE_POLICY"
retry aws iam put-role-policy --role-name onboardiq-extraction --policy-name apply-guardrail --policy-document "$GUARD_POLICY"
echo "   orchestrator, risk: one model ($MODEL_ID) · extraction: the guardrail only · screening: no AWS permissions"

ensure_role "$SFN_ROLE" "$(svc_trust states.amazonaws.com)" "OnboardIQ-lite review workflow: may call the agent function only"
retry aws iam put-role-policy --role-name "$SFN_ROLE" --policy-name invoke-agent --policy-document "{
 \"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":[\"$FN_ARN\",\"$FN_ARN:*\"]}]}"

# ------------------------------------------------------------------------------------------- 4. logs
step "4/7 Log group (7-day retention, so logs never accumulate)"
aws logs create-log-group --region "$REGION" --log-group-name "/aws/lambda/$FN" >/dev/null 2>&1 || true
aws logs put-retention-policy --region "$REGION" --log-group-name "/aws/lambda/$FN" --retention-in-days 7
echo "   /aws/lambda/$FN"

# ------------------------------------------------------------------------------------------- 5. function
step "5/7 Lambda function $FN"
python3 - <<'PY'
import zipfile, pathlib
z = zipfile.ZipFile("build/agent.zip", "w", zipfile.ZIP_DEFLATED)
for f in ("agent.py", "cases.json"):
    z.write(pathlib.Path("lambda") / f, f)
z.close()
PY
cat > build/env.json <<EOF
{"Variables":{"REGION":"$REGION","MODEL_ID":"$MODEL_ID","GUARDRAIL_ID":"$GR_ID","STATE_MACHINE_ARN":"$SM_ARN",
 "AUDIT_TABLE":"$AUDIT","CONTROL_TABLE":"$CONTROL","ROLE_PREFIX":"onboardiq-","MAX_TURNS":"10","MAX_TOKENS":"40000"}}
EOF
if aws lambda get-function --region "$REGION" --function-name "$FN" >/dev/null 2>&1; then
  aws lambda update-function-code --region "$REGION" --function-name "$FN" --zip-file fileb://build/agent.zip >/dev/null
  aws lambda wait function-updated-v2 --region "$REGION" --function-name "$FN"
  aws lambda update-function-configuration --region "$REGION" --function-name "$FN" --role "$EXEC_ARN" \
    --environment file://build/env.json --timeout 120 --memory-size 256 >/dev/null
  aws lambda wait function-updated-v2 --region "$REGION" --function-name "$FN"; echo "   updated"
else
  retry aws lambda create-function --region "$REGION" --function-name "$FN" --runtime python3.12 \
    --handler agent.handler --role "$EXEC_ARN" --zip-file fileb://build/agent.zip --timeout 120 --memory-size 256 \
    --environment file://build/env.json --description "OnboardIQ-lite KYC agent (fictional data only)"
  aws lambda wait function-active-v2 --region "$REGION" --function-name "$FN"; echo "   created"
fi

# ------------------------------------------------------------------------------------------- 6. workflow
step "6/7 Step Functions state machine $SM (analyst review)"
sed "s|__FUNCTION_ARN__|$FN_ARN|" review.asl.json > build/review.asl.json
SFN_ROLE_ARN="arn:aws:iam::$ACCT:role/$SFN_ROLE"
if aws stepfunctions describe-state-machine --region "$REGION" --state-machine-arn "$SM_ARN" >/dev/null 2>&1; then
  retry aws stepfunctions update-state-machine --region "$REGION" --state-machine-arn "$SM_ARN" \
    --definition file://build/review.asl.json --role-arn "$SFN_ROLE_ARN"; echo "   updated"
else
  retry aws stepfunctions create-state-machine --region "$REGION" --name "$SM" --type STANDARD \
    --definition file://build/review.asl.json --role-arn "$SFN_ROLE_ARN"; echo "   created"
fi

# ------------------------------------------------------------------------------------------- 7. check
step "7/7 Identity check (each agent role tries the model; costs a fraction of a cent)"
echo "   waiting 20 s for new IAM permissions to reach every AWS region endpoint..."; sleep 20
aws lambda invoke --region "$REGION" --function-name "$FN" --cli-binary-format raw-in-base64-out \
  --payload '{"action":"identity_check"}' build/identity.json >/dev/null
python3 -c "import json; [print(f'   {k:13} {v}') for k, v in json.load(open('build/identity.json')).items()]"

echo
echo "DONE. Expected above: orchestrator and risk ALLOWED, extraction and screening DENIED."
echo "Next:  ./oiq.sh run            (runs G01 G04 G07 G13)"
echo "Cost:  nothing while idle; about US\$0.001 per case. Remove everything with ./cleanup.sh"
