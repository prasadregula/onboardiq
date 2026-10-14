"""w3-3: an analyst-triggered run records BOTH the agent identity and the analyst identity in the audit chain;
OBO exchange wiring; T-17 dev-auth guard."""
import pytest
from sqlmodel import select

from app.audit import verify_chain
from app.config import get_settings
from app.db import session_scope
from app.models import AuditEvent
from tests.conftest import golden


def _events(case_id):
    with session_scope() as s:
        return s.exec(select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq)).all()


def test_analyst_triggered_rerun_carries_agent_and_analyst_identity(client):
    """The w3-3 'done when' test."""
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    r = client.post(f"/cases/{case_id}/run", json={"sync": True}, headers={"X-Analyst": "maria.analyst@bank.test"})
    assert r.status_code == 200, r.text
    events = _events(case_id)
    started = next(e for e in events if e.event_type == "run_started")
    assert started.payload["initiated_by"]["subject"] == "maria.analyst@bank.test"
    assert started.payload["initiated_by"]["kind"] == "user"
    tool_calls = [e.payload for e in events if e.event_type == "tool_call"]
    assert tool_calls, "expected tool events"
    for tc in tool_calls:  # every agent action: WHICH identity acted and FOR WHOM
        assert tc["identity"].startswith(tc["agent_role"] + ":")
        assert tc["on_behalf_of"]["subject"] == "maria.analyst@bank.test"
    risk = next(tc for tc in tool_calls if tc["tool"] == "rate_risk")
    assert risk["agent_role"] == "risk"
    with session_scope() as s:
        assert verify_chain(s, case_id)["ok"]


def test_async_run_keeps_the_analyst_across_the_background_hop(client):
    s = get_settings()
    s.run_async = True
    try:
        case_id = client.post("/cases", json=golden("G02")).json()["id"]
        r = client.post(f"/cases/{case_id}/run", json={"sync": False}, headers={"X-Analyst": "omar.reviewer@bank.test"})
        assert r.status_code == 202
    finally:
        s.run_async = False
    events = _events(case_id)  # TestClient runs background tasks before returning
    requested = next(e for e in events if e.event_type == "run_requested")
    assert requested.payload["requested_by"]["subject"] == "omar.reviewer@bank.test"
    assert all(e.payload["on_behalf_of"]["subject"] == "omar.reviewer@bank.test"
               for e in events if e.event_type == "tool_call")


def test_scripts_and_jobs_are_recorded_as_system_principals():
    from app.schemas import ApplicationIn
    from app.services import create_case, process_case
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(golden("G01")), actor="seed")
        process_case(s, case, "scripted", actor="seed")
        s.commit()
        cid = case.id
    started = next(e for e in _events(cid) if e.event_type == "run_started")
    assert started.payload["initiated_by"] == {"subject": "seed", "name": "seed", "kind": "system", "oid": None,
                                               "tid": None, "roles": [], "token_id": None}


def test_obo_uses_a_managed_identity_assertion_not_a_secret(monkeypatch):
    import azure.identity as ai

    from app import obo
    s = get_settings()
    s.obo_client_id, s.obo_assertion_identity, s.entra_tenant_id = "api-app-id", "uami-client-id", "tenant-id"
    seen = {}

    class FakeMI:
        def __init__(self, client_id):
            seen["mi"] = client_id

        def get_token(self, scope):
            seen["mi_scope"] = scope
            return type("T", (), {"token": "mi-assertion"})()

    class FakeOBO:
        def __init__(self, tenant_id, client_id, client_assertion_func, user_assertion):
            seen.update(tenant=tenant_id, client=client_id, user_assertion=user_assertion,
                        assertion=client_assertion_func())
    monkeypatch.setattr(ai, "ManagedIdentityCredential", FakeMI)
    monkeypatch.setattr(ai, "OnBehalfOfCredential", FakeOBO)
    try:
        obo.obo_credential("analyst-token")
    finally:
        s.obo_client_id = s.obo_assertion_identity = s.entra_tenant_id = None
    assert seen == {"mi": "uami-client-id", "mi_scope": "api://AzureADTokenExchange/.default", "tenant": "tenant-id",
                    "client": "api-app-id", "user_assertion": "analyst-token", "assertion": "mi-assertion"}


def test_obo_not_configured_is_explicit():
    from app.obo import OboNotConfigured, obo_credential
    with pytest.raises(OboNotConfigured):
        obo_credential("t")


def test_t17_apikey_auth_refused_outside_dev():
    from fastapi.testclient import TestClient

    from app.main import app
    s = get_settings()
    s.env = "prod"
    try:
        with pytest.raises(RuntimeError, match="not allowed"):
            with TestClient(app):
                pass
    finally:
        s.env = "dev"
