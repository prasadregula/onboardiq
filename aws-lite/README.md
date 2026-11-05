# OnboardIQ-lite on AWS

The OnboardIQ agent controls rebuilt on serverless AWS, as a cross-cloud comparison with the Azure deployment.
Fictional data only. Nothing here costs anything while idle.

| Control | Azure (main app) | AWS (this folder) |
|---|---|---|
| Model | Azure OpenAI gpt-4.1-mini behind API Management | Bedrock Nova Micro (Converse API, tool use) |
| One identity per agent | User-assigned managed identities + RBAC | IAM role per agent, assumed per tool; the function's own role has **no** model access |
| Least privilege on the model | Only the gateway identity may call Azure OpenAI | `bedrock:InvokeModel` on **one model ARN**, orchestrator and risk roles only |
| Document injection screening | Prompt Shields + pattern scan (extraction identity) | Bedrock Guardrails `ApplyGuardrail`, prompt attack (extraction role) |
| What the model sees | Typed fields, identifiers masked, no document text | Same: names and addresses masked, document text never sent |
| Policy enforcement point | `policy_point.py` | `enforce()`: allow-listed tools, case-scoped `case_id`, bounded rationale |
| Required checks + gates | `record_decision` precondition, fixed gates; model only tightens | Same logic |
| Kill switch, run budgets | SQL flag, per-run limits | DynamoDB item read before every model turn; turn and token limits |
| Tamper-evident audit | Hash chain in Azure SQL | Hash chain in DynamoDB (`case_id` + `seq`) |
| Human review | Review queue UI | Step Functions waits for an analyst (`waitForTaskToken`) |
| Who did what | Agent identity on every audit event | Role session (`kyc-<role>-agent`) in CloudTrail and the audit chain |

## Run it (AWS CloudShell, us-east-1)

```bash
# upload aws-lite.zip via CloudShell "Actions > Upload file", then:
unzip -o aws-lite.zip && cd aws-lite && chmod +x *.sh
./deploy.sh            # about 2 minutes; ends with an identity check
./oiq.sh run           # G01 G04 G07 G13
./oiq.sh waiting       # cases parked for an analyst
./oiq.sh approve G07   # resume the review workflow
./oiq.sh audit G13     # the hash-chained audit
./oiq.sh verify G13
./oiq.sh pause && ./oiq.sh run G01 && ./oiq.sh resume    # kill switch
./cleanup.sh           # remove everything (add --all to delete the guardrail too)
```

Expected outcomes: G01 `auto_approve` (GATE_STP), G04 `escalate` (confirmed sanctions match, name + date of birth),
G07 `needs_review` (high-risk jurisdiction), G13 `escalate` (GATE_DOCUMENT_INJECTION: the poisoned bill is withheld
from the model). The model's own recommendation can make an outcome stricter, never looser.

## Cost

| Service | Charge |
|---|---|
| Lambda, Step Functions (Standard), DynamoDB (provisioned 1/1), CloudWatch Logs (7-day retention), IAM | Inside the always-free monthly allowances |
| Bedrock Nova Micro | About US$0.0002 per case |
| Bedrock Guardrails | A fraction of a cent per document |
| While idle | Nothing. Waiting Step Functions executions cost nothing |

Never added: OpenSearch Serverless, NAT gateways, containers, databases with an hourly charge.

## Tests

`pip install "moto[dynamodb,stepfunctions,sts]" pytest && pytest -q aws-lite/tests` runs every golden case,
the policy point, the kill switch, the budget, the review flow and audit tampering against simulated AWS.
