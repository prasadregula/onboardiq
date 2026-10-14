"""w3-1: each agent role acts under its own identity; a broken identity breaks only its own agent."""
from sqlmodel import select

from app import identity
from app.agent.orchestrator import run_case
from app.agent.tools import REGISTRY
from app.db import session_scope
from app.models import AuditEvent, Outcome
from app.schemas import ApplicationIn
from app.services import create_case
from tests.conftest import golden


def test_tools_map_to_distinct_roles():
    assert identity.role_for_tool("extract_document") == "extraction"
    assert identity.role_for_tool("screen_parties") == "screening"
    assert identity.role_for_tool("rate_risk") == "risk"
    assert identity.role_for_tool("record_decision") == "decision"
    assert set(REGISTRY) <= set(identity.TOOL_ROLE), "every registered tool must have an explicit identity"


def test_acting_as_nests_and_restores():
    assert identity.current_role() == "api"
    with identity.acting_as("orchestrator"):
        with identity.acting_as("risk"):
            assert identity.current_role() == "risk"
        assert identity.current_role() == "orchestrator"
    assert identity.current_role() == "api"


def test_dedicated_client_id_selects_that_managed_identity(monkeypatch):
    from azure.identity import ManagedIdentityCredential
    identity.reset_for_tests()
    monkeypatch.setenv("ONBOARDIQ_IDENTITY_RISK", "11111111-2222-3333-4444-555555555555")
    try:
        cred = identity.credential_for("risk")
        assert isinstance(cred, ManagedIdentityCredential)
        assert identity.identity_label("risk") == "risk:11111111"
        assert identity.configured()["risk"] is True and identity.configured()["extraction"] is False
    finally:
        identity.reset_for_tests()


def test_tool_runs_under_its_role_and_audit_records_the_identity(monkeypatch):
    seen = {}
    tool = REGISTRY["screen_parties"]
    orig = tool.fn

    def spy(ctx, args):
        seen["role"] = identity.current_role()
        return orig(ctx, args)
    monkeypatch.setattr(tool, "fn", spy)
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G01")))
        run_case(s, case, "scripted")
        calls = [e.payload for e in s.exec(select(AuditEvent).where(AuditEvent.case_id == case.id,
                                                                    AuditEvent.event_type == "tool_call")).all()]
    assert seen["role"] == "screening"
    by_tool = {c["tool"]: c for c in calls}
    assert by_tool["rate_risk"]["agent_role"] == "risk" and by_tool["rate_risk"]["identity"].startswith("risk:")
    assert by_tool["extract_document"]["agent_role"] == "extraction"


def test_revoked_identity_breaks_only_its_agent(monkeypatch):
    """Simulates removing the risk identity's Azure role: rate_risk fails, every other agent still runs, and the
    policy engine refuses to approve without the risk check (needs_review), instead of crashing the whole run."""
    from azure.core.exceptions import ClientAuthenticationError

    def denied(ctx, args):
        raise ClientAuthenticationError("403 PermissionDenied for identity risk")
    monkeypatch.setattr(REGISTRY["rate_risk"], "fn", denied)
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G01")))  # would otherwise auto-approve
        run = run_case(s, case, "scripted")
        events = s.exec(select(AuditEvent).where(AuditEvent.case_id == case.id)).all()
    errors = [e.payload for e in events if e.event_type == "tool_error"]
    assert [e["tool"] for e in errors] == ["rate_risk"] and errors[0]["agent_role"] == "risk"
    assert "ClientAuthenticationError" in errors[0]["error"]
    ok_tools = {e.payload["tool"] for e in events if e.event_type == "tool_call"}
    assert {"extract_document", "screen_parties", "check_consistency"} <= ok_tools
    assert run.outcome == Outcome.needs_review and run.gate.startswith("GATE_CHECKS_INCOMPLETE")
