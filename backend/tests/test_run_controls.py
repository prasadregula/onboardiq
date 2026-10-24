"""w4-3: the agent kill switch and per-run budgets.

The "done when" tests: flipping the pause flag stops a run at its next step (no decision, case goes to an analyst,
audited), new runs are refused while paused, and a run that exceeds a budget aborts cleanly with an audit entry."""
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app import control
from app.agent import orchestrator as orch
from app.config import get_settings
from app.db import session_scope
from app.models import AuditEvent, Case, ControlEvent, ReviewTask
from tests.conftest import golden

ADMIN = {"X-Role": "Admin", "X-Analyst": "admin1"}


def _events(case_id):
    with session_scope() as s:
        return s.exec(select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq)).all()


def _task(case_id):
    with session_scope() as s:
        return s.exec(select(ReviewTask).where(ReviewTask.case_id == case_id)).first()


def test_only_admin_can_pause(client):
    assert client.post("/admin/agents/pause", json={"reason": "test"}).status_code == 403  # default role: KycAnalyst
    r = client.post("/admin/agents/pause", json={"reason": "incident drill"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["paused"] is True
    view = client.get("/admin/agents").json()  # every reader can see the state (for the banner)
    assert view["paused"] and view["changed_by"] == "admin1" and view["reason"] == "incident drill"
    assert view["history"][0]["action"] == "pause"


def test_pause_requires_a_reason(client):
    assert client.post("/admin/agents/pause", json={"reason": ""}, headers=ADMIN).status_code == 422


def test_paused_platform_refuses_new_runs_and_audits_it(client):
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    client.post("/admin/agents/pause", json={"reason": "model provider incident"}, headers=ADMIN)
    r = client.post(f"/cases/{case_id}/run", json={"sync": True})
    assert r.status_code == 423 and "paused" in r.json()["detail"]
    refused = [e for e in _events(case_id) if e.event_type == "run_refused"]
    assert refused and refused[0].payload["code"] == control.KILL_SWITCH
    client.post("/admin/agents/resume", json={"reason": "provider recovered"}, headers=ADMIN)
    assert client.post(f"/cases/{case_id}/run", json={"sync": True}).status_code == 200


def test_pause_stops_a_running_case_at_its_next_step(client, monkeypatch):
    """The admin pauses while a run is in flight: the next tool call is not made, nothing is decided."""
    original = orch._audit_tool

    def pause_after_extraction(ctx, name, *a, **kw):
        original(ctx, name, *a, **kw)
        if name == "extract_document":
            control.set_paused(ctx.session, True, "admin1", "stop now")

    monkeypatch.setattr(orch, "_audit_tool", pause_after_extraction)
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    body = client.post(f"/cases/{case_id}/run", json={"sync": True}).json()
    assert body["run"]["status"] == "aborted" and body["run"]["error"].startswith(control.KILL_SWITCH)
    events = _events(case_id)
    aborted = next(e for e in events if e.event_type == "run_aborted")
    assert aborted.payload["code"] == control.KILL_SWITCH and aborted.payload["tools_run"] == ["extract_document"]
    assert not any(e.event_type == "decision" for e in events)
    assert [e.payload["tool"] for e in events if e.event_type == "tool_call"] == ["extract_document"]
    with session_scope() as s:
        assert s.get(Case, case_id).status.value == "needs_review"
    assert _task(case_id).reason_codes == [control.KILL_SWITCH]


def test_tool_call_budget_aborts_the_run(client):
    s = get_settings()
    s.run_max_tool_calls = 3
    try:
        case_id = client.post("/cases", json=golden("G01")).json()["id"]
        body = client.post(f"/cases/{case_id}/run", json={"sync": True}).json()
    finally:
        s.run_max_tool_calls = 30
    assert body["run"]["status"] == "aborted"
    aborted = next(e for e in _events(case_id) if e.event_type == "run_aborted")
    assert aborted.payload["code"] == control.BUDGET_TOOL_CALLS
    assert aborted.payload["details"]["max_tool_calls"] == 3
    assert _task(case_id).reason_codes == [control.BUDGET_TOOL_CALLS]


def test_run_started_records_the_budget(client):
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    client.post(f"/cases/{case_id}/run", json={"sync": True})
    started = next(e for e in _events(case_id) if e.event_type == "run_started")
    assert started.payload["budget"]["max_tool_calls"] == 30


@pytest.mark.parametrize("usage,code", [
    (SimpleNamespace(tokens_in=900, tokens_out=200, tokens_cached=0, model="gpt-4.1-mini"), control.BUDGET_TOKENS),
    (SimpleNamespace(tokens_in=10, tokens_out=10, tokens_cached=0, model="gpt-4.1"), None),
])
def test_token_budget(usage, code):
    budget = control.RunBudget(max_tool_calls=30, max_tokens=1000, max_cost_usd=1.0, max_seconds=60)
    with session_scope() as s:
        if code:
            with pytest.raises(control.RunAborted) as e:
                control.check(s, budget, usage)
            assert e.value.code == code
        else:
            control.check(s, budget, usage)


def test_cost_budget():
    budget = control.RunBudget(max_tool_calls=30, max_tokens=10_000_000, max_cost_usd=0.0001, max_seconds=60)
    usage = SimpleNamespace(tokens_in=500_000, tokens_out=100_000, tokens_cached=0, model="gpt-4.1-mini")
    with session_scope() as s, pytest.raises(control.RunAborted) as e:
        control.check(s, budget, usage)
    assert e.value.code == control.BUDGET_COST


def test_demo_reset_reloads_golden_cases(client):
    assert client.post("/admin/demo/reset", headers={"X-Role": "KycAnalyst"}).status_code == 403
    assert client.post("/admin/demo/reset", headers=ADMIN).status_code == 202  # TestClient runs the background task
    with session_scope() as s:
        refs = {c.external_ref for c in s.exec(select(Case)).all()}
        assert {"G01", "G12"} <= refs
        assert s.exec(select(ControlEvent).where(ControlEvent.action == "demo_reset")).first() is not None
    client.post("/admin/demo/reset", headers=ADMIN)  # running it twice replaces, never duplicates
    with session_scope() as s:
        assert len([c for c in s.exec(select(Case)).all() if c.external_ref == "G01"]) == 1


def test_demo_reset_is_disabled_outside_dev(client):
    s = get_settings()
    s.env = "prod"
    try:
        assert client.post("/admin/demo/reset", headers=ADMIN).status_code == 403
    finally:
        s.env = "dev"
