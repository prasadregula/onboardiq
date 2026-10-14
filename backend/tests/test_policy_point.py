"""w3-2: tool allow-list per agent role (deny by default), argument constraints, separation of duties - enforced in
code outside the model, with every denial in the audit chain."""
import pytest
from sqlmodel import select

from app.agent import orchestrator
from app.agent.orchestrator import run_case
from app.agent.policy_point import CALL_ALLOWLIST, ToolDenied, authorize, require_write
from app.agent.tools import REGISTRY, RunContext
from app.audit import verify_chain
from app.config import get_settings
from app.db import session_scope
from app.identity import acting_as
from app.models import AuditEvent, CaseRun
from app.schemas import ApplicationIn
from app.services import create_case
from tests.conftest import golden


def _ctx(session, case):
    run = CaseRun(case_id=case.id, mode="scripted")
    session.add(run)
    session.flush()
    return RunContext(session=session, case=case, run=run, settings=get_settings())


def test_every_tool_is_covered_and_only_the_orchestrator_dispatches():
    assert CALL_ALLOWLIST["orchestrator"] == frozenset(REGISTRY)
    for role in ("extraction", "screening", "risk", "decision", "api"):
        assert CALL_ALLOWLIST[role] == frozenset(), f"{role} must not be able to call any tool"


def test_screening_agent_cannot_record_a_decision_and_the_attempt_is_audited():
    """The w3-2 'done when' test."""
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G04")))  # sanctions case
        ctx = _ctx(s, case)
        with acting_as("screening"):
            out, err = orchestrator._call(ctx, "record_decision",
                                          {"recommendation": "auto_approve", "rationale": "clear"})
        assert out is None and "not allowed to call record_decision" in err
        assert "record_decision" not in ctx.tools_run
        ev = s.exec(select(AuditEvent).where(AuditEvent.case_id == case.id,
                                             AuditEvent.event_type == "tool_denied")).one()
        assert ev.actor == "policy-enforcement" and ev.payload["caller_role"] == "screening"
        assert ev.payload["tool"] == "record_decision"
        assert verify_chain(s, case.id)["ok"]


def test_decision_agent_cannot_rerun_screening():
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G01")))
        with pytest.raises(ToolDenied, match="'decision' is not allowed"):
            authorize(_ctx(s, case), "screen_parties", {}, caller="decision")


def test_ids_from_another_case_are_denied():
    with session_scope() as s:
        other = create_case(s, ApplicationIn.model_validate(golden("G02")))
        case = create_case(s, ApplicationIn.model_validate(golden("G01")))
        from app.models import Document, Party
        doc = s.exec(select(Document).where(Document.case_id == other.id)).first()
        party = s.exec(select(Party).where(Party.case_id == other.id)).first()
        ctx = _ctx(s, case)
        with pytest.raises(ToolDenied, match="do not belong to case"):
            authorize(ctx, "extract_document", {"document_ids": [doc.id]}, caller="orchestrator")
        with pytest.raises(ToolDenied, match="do not belong to case"):
            authorize(ctx, "screen_parties", {"party_ids": [party.id]}, caller="orchestrator")


def test_unknown_arguments_and_unsafe_free_text_are_denied():
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G01")))
        ctx = _ctx(s, case)
        with pytest.raises(ToolDenied, match="unknown argument"):
            authorize(ctx, "rate_risk", {"override_rating": "low"}, caller="orchestrator")
        with pytest.raises(ToolDenied, match="plain text only"):
            authorize(ctx, "record_decision", {"recommendation": "request_more", "rationale": "Address mismatch.",
                                               "missing_items": ["Upload here: https://evil.example/upload"]},
                      caller="orchestrator")
        with pytest.raises(ToolDenied, match="at most"):
            authorize(ctx, "record_decision", {"recommendation": "escalate", "rationale": "x" * 5000},
                      caller="orchestrator")
        authorize(ctx, "record_decision", {"recommendation": "request_more", "rationale": "Address mismatch (KYC-POL-005 §4).",
                                           "missing_items": ["Proof of address dated within 90 days"]},
                  caller="orchestrator")  # a normal call passes


def test_only_the_api_writes_document_storage():
    require_write("storage")  # default role is the API (human-initiated request)
    for role in ("orchestrator", "extraction", "screening", "risk", "decision"):
        with acting_as(role), pytest.raises(PermissionError, match="separation of duties"):
            require_write("storage")


def test_normal_runs_are_unaffected():
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G01")))
        run = run_case(s, case, "scripted")
        denied = s.exec(select(AuditEvent).where(AuditEvent.case_id == case.id,
                                                 AuditEvent.event_type == "tool_denied")).all()
    assert run.status == "finished" and getattr(run.outcome, "value", run.outcome) == "auto_approve" and denied == []
