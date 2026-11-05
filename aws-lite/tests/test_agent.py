"""OnboardIQ-lite, tested against simulated AWS (moto: DynamoDB, STS, Step Functions) and a scripted Bedrock.

Run: pip install "moto[dynamodb,stepfunctions,sts]" pytest && pytest -q aws-lite/tests
"""
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                  GUARDRAIL_ID="gr-test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
import agent

ASL = (Path(__file__).resolve().parents[1] / "review.asl.json").read_text()


class FakeBedrock:
    """Plays one agent role. Enforces the same permissions the IAM roles grant."""

    def __init__(self, role, log):
        self.role, self.log, self.turn = role, log, 0

    def _deny(self, op):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": f"{self.role} may not {op}"}}, op)

    def apply_guardrail(self, **kw):
        if self.role != "extraction":
            self._deny("ApplyGuardrail")
        text = kw["content"][0]["text"]["text"]
        hit = "ignore your previous instructions" in text.lower()
        return {"action": "GUARDRAIL_INTERVENED" if hit else "NONE",
                "assessments": [{"contentPolicy": {"filters": [{"type": "PROMPT_ATTACK", "detected": hit,
                                                                "action": "BLOCKED" if hit else "NONE"}]}}]}

    def converse(self, **kw):
        if self.role not in ("orchestrator", "risk"):
            self._deny("InvokeModel")
        self.log.append({"role": self.role, "messages": json.dumps(kw["messages"], default=str)})
        usage = {"inputTokens": 900, "outputTokens": 60}
        if self.role == "risk":
            return {"output": {"message": {"role": "assistant", "content": [
                {"text": "<thinking>x</thinking>Rating follows the policy bands."}]}}, "usage": usage, "stopReason": "end_turn"}
        case_id = kw["messages"][0]["content"][0]["text"].split()[-1].rstrip(".")
        self.turn += 1
        plan = {1: [("record_decision", {"recommendation": "auto_approve", "rationale": "looks fine"}),  # premature
                    ("extract_document", {})],
                2: [("check_consistency", {}), ("screen_parties", {}), ("rate_risk", {})],
                3: [("record_decision", {"recommendation": "auto_approve", "rationale": "All checks passed."})]}
        calls = plan.get(self.turn, plan[3])
        return {"output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": f"t{self.turn}{i}", "name": n, "input": {"case_id": case_id, **a}}}
            for i, (n, a) in enumerate(calls)]}}, "usage": usage, "stopReason": "tool_use"}


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(TableName="onboardiq-audit", AttributeDefinitions=[
            {"AttributeName": "case_id", "AttributeType": "S"}, {"AttributeName": "seq", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "case_id", "KeyType": "HASH"}, {"AttributeName": "seq", "KeyType": "RANGE"}],
            ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1})
        ddb.create_table(TableName="onboardiq-control", AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                         ProvisionedThroughput={"ReadCapacityUnits": 1, "WriteCapacityUnits": 1})
        ddb.put_item(TableName="onboardiq-control", Item={"id": {"S": "agents"}, "paused": {"BOOL": False}})
        sm = boto3.client("stepfunctions", region_name="us-east-1").create_state_machine(
            name="onboardiq-review", definition=ASL.replace("__FUNCTION_ARN__", "arn:aws:lambda:us-east-1:123456789012:function:f"),
            roleArn="arn:aws:iam::123456789012:role/onboardiq-lite-sfn")["stateMachineArn"]
        monkeypatch.setattr(agent, "STATE_MACHINE_ARN", sm)
        agent._clients = agent.Clients()
        log, fakes = [], {}
        monkeypatch.setattr(agent.Clients, "bedrock_as",
                            lambda self, role: fakes.setdefault(role, FakeBedrock(role, log)))
        yield {"log": log, "ddb": ddb, "sm": sm}
        agent._clients = None


@pytest.mark.parametrize("case_id,outcome,gate", [
    ("G01", "auto_approve", "GATE_STP"),
    ("G04", "escalate", "GATE_SANCTIONS_CONFIRMED"),
    ("G07", "needs_review", "GATE_HIGH_RISK"),
    ("G13", "escalate", "GATE_DOCUMENT_INJECTION"),
])
def test_golden_cases(aws, case_id, outcome, gate):
    r = agent.handler({"action": "run_case", "case_id": case_id})
    assert (r["outcome"], r["gate"]) == (outcome, gate), r
    assert r["model_recommendation"] == "auto_approve"          # the model always said approve: gates only tighten
    assert set(r["checks_run"]) == set(agent.REQUIRED)
    assert r["review_workflow"].startswith("arn:aws:states")
    events = aws["ddb"].query(TableName="onboardiq-audit", KeyConditionExpression="case_id = :c",
                              ExpressionAttributeValues={":c": {"S": case_id}})["Items"]
    names = [e["event"]["S"] for e in events]
    assert "tool_denied" in names                              # the premature decision was refused and audited
    denied = next(json.loads(e["payload"]["S"]) for e in events if e["event"]["S"] == "tool_denied")
    assert "required checks not run" in denied["reason"]
    actors = {json.loads(e["payload"]["S"]).get("tool"): e["actor"]["S"] for e in events if e["event"]["S"] == "tool_result"}
    assert actors["screen_parties"].startswith("screening agent (role onboardiq-screening)")
    assert actors["extract_document"].startswith("extraction agent")
    assert agent.verify_audit(case_id)["chain_valid"]


def test_model_never_sees_document_text_names_or_the_injection(aws):
    agent.handler({"action": "run_case", "case_id": "G13"})
    seen = " ".join(c["messages"] for c in aws["log"])
    assert "Ignore your previous" not in seen and "Daniel Okafor" not in seen and "Wilshire" not in seen
    assert "M5566778" not in seen and "***6778" in seen
    assert "[withheld" in seen


def test_tampering_breaks_the_chain(aws):
    agent.handler({"action": "run_case", "case_id": "G01"})
    aws["ddb"].update_item(TableName="onboardiq-audit", Key={"case_id": {"S": "G01"}, "seq": {"N": "3"}},
                           UpdateExpression="SET actor = :a", ExpressionAttributeValues={":a": {"S": "someone else"}})
    v = agent.verify_audit("G01")
    assert not v["chain_valid"] and v["first_broken_seq"] == 3


def test_kill_switch_refuses_runs(aws):
    aws["ddb"].put_item(TableName="onboardiq-control", Item={"id": {"S": "agents"}, "paused": {"BOOL": True}})
    r = agent.handler({"action": "run_case", "case_id": "G01"})
    assert (r["outcome"], r["gate"], r["turns"]) == ("needs_review", "KILL_SWITCH", 0)
    assert aws["log"] == []                                     # no model call at all


def test_budget_stops_a_runaway_run(aws, monkeypatch):
    monkeypatch.setattr(agent, "MAX_TURNS", 1)
    r = agent.handler({"action": "run_case", "case_id": "G01"})
    assert (r["outcome"], r["gate"]) == ("needs_review", "RUN_BUDGET")


def test_policy_point():
    with pytest.raises(agent.PolicyDenied):
        agent.enforce("G01", "transfer_funds", {"case_id": "G01"})
    with pytest.raises(agent.PolicyDenied):
        agent.enforce("G01", "screen_parties", {"case_id": "G04"})       # another case's id
    with pytest.raises(agent.PolicyDenied):
        agent.enforce("G01", "screen_parties", {"case_id": "G01", "name": "x"})
    with pytest.raises(agent.PolicyDenied):
        agent.enforce("G01", "record_decision", {"case_id": "G01", "recommendation": "approve", "rationale": ""})
    with pytest.raises(agent.PolicyDenied):
        agent.enforce("G01", "record_decision", {"case_id": "G01", "recommendation": "escalate",
                                                 "rationale": "see http://x"})


def test_review_flow(aws, monkeypatch):
    agent.handler({"action": "run_case", "case_id": "G07"})
    assert agent.handler({"action": "register_review", "case_id": "G07", "run_id": "r1", "outcome": "needs_review",
                          "gate": "GATE_HIGH_RISK", "taskToken": "tok-123"})["registered"]
    sent = {}
    monkeypatch.setattr(agent.clients().sfn, "send_task_success", lambda **kw: sent.update(kw))
    r = agent.handler({"action": "analyst_decision", "case_id": "G07", "decision": "approve", "analyst": "prasad"})
    assert r["decision"] == "approve" and sent["taskToken"] == "tok-123"
    assert json.loads(sent["output"]) == {"decision": "approve", "analyst": "prasad"}
    assert "error" in agent.handler({"action": "analyst_decision", "case_id": "G07", "decision": "approve"})  # once only
    assert agent.verify_audit("G07")["chain_valid"]


def test_identity_check(aws):
    out = agent.handler({"action": "identity_check"})
    assert out["orchestrator"] == out["risk"] == "model ALLOWED"
    assert out["screening"].startswith("model DENIED") and out["extraction"].startswith("model DENIED")


def test_state_machine_definition_is_valid_json_with_one_placeholder():
    d = json.loads(ASL)
    assert d["StartAt"] == "Route" and ASL.count("__FUNCTION_ARN__") == 1
    assert d["States"]["AwaitAnalyst"]["Resource"].endswith("waitForTaskToken")
    assert d["States"]["AwaitAnalyst"]["Parameters"]["Payload"]["taskToken.$"] == "$$.Task.Token"
