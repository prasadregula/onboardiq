"""Evidence pack: an auditor-readable record of one decision, including exactly what the model was shown."""
import json

import pytest

from app import llm as llm_mod
from app.config import get_settings
from app.db import session_scope
from app.models import AuditEvent
from tests.conftest import golden
from tests.test_pii_minimisation import RecordingLLM, _plan

AUDITOR = {"X-Role": "Auditor", "X-Analyst": "auditor1"}


@pytest.fixture
def llm_mode():
    s = get_settings()
    s.orchestrator_mode, s.azure_openai_endpoint = "llm", "https://fake.openai.azure.com/"
    yield
    s.orchestrator_mode, s.azure_openai_endpoint = "scripted", None
    llm_mod.reset_llm()


def _run(client, monkeypatch, gid):
    case_id = client.post("/cases", json=golden(gid)).json()["id"]
    monkeypatch.setattr(llm_mod, "_llm", RecordingLLM(_plan(case_id)))
    client.post(f"/cases/{case_id}/run", json={"sync": True})
    return case_id


def test_pack_records_what_the_model_saw_and_who_did_what(client, llm_mode, monkeypatch):
    case_id = _run(client, monkeypatch, "G13")
    r = client.get(f"/cases/{case_id}/evidence?format=json", headers=AUDITOR)
    assert r.status_code == 200
    p = r.json()
    assert p["case"]["outcome"] == "escalate" and p["run"]["gate"].startswith("GATE_DOCUMENT_INJECTION")
    assert p["audit"]["verified"] and p["exports"] and p["exports"][0]["actor"] == "user:auditor1"
    # every model request fingerprinted before it was sent
    assert p["model_requests"] and all(len(m["sha256"]) == 64 for m in p["model_requests"])
    assert {m["purpose"] for m in p["model_requests"]} == {"orchestration", "risk_grounding"}
    # the transcript is the masked view: tool results included, poisoned text withheld, no real names
    shown = json.dumps(p["transcript"])
    assert any(m["role"] == "tool" and m["tool"] == "extract_document" for m in p["transcript"])
    name = golden("G13")["parties"][0]["name"]
    assert "[withheld" in shown and "Ignore your previous" not in shown and name not in shown
    assert "PERSON_1" in shown
    # but the auditor's own record names the person
    assert p["parties"][0]["name"] == name
    # each step carries the agent identity and the person it acted for
    calls = [s for s in p["steps"] if s["event"] == "tool_call"]
    assert {s["agent_role"] for s in calls} >= {"extraction", "screening", "risk"}
    assert all(s["on_behalf_of"] for s in calls)
    assert p["controls"]["minimisation"]["values_known"] >= 3


def test_html_pack_is_readable_and_restricted(client, llm_mode, monkeypatch):
    case_id = _run(client, monkeypatch, "G04")
    assert client.get(f"/cases/{case_id}/evidence").status_code == 403          # a KYC analyst may not export
    r = client.get(f"/cases/{case_id}/evidence", headers=AUDITOR)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    h = r.text
    for heading in ("1. Decision", "2. Model, versions and controls", "6. Agent steps", "7. What the model was shown",
                    "9. Audit trail", "Audit chain verified", "GATE_SANCTIONS", "Contains personal data"):
        assert heading in h, heading
    assert "<script" not in h.lower()


def test_tampering_shows_in_the_pack(client):
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    client.post(f"/cases/{case_id}/run", json={"sync": True})
    with session_scope() as s:
        ev = s.exec(AuditEvent.__table__.select().where(AuditEvent.case_id == case_id)).first()
        s.exec(AuditEvent.__table__.update().where(AuditEvent.id == ev.id).values(actor="someone-else"))
        s.commit()
    h = client.get(f"/cases/{case_id}/evidence", headers=AUDITOR).text
    assert "AUDIT CHAIN BROKEN" in h


def test_scripted_run_pack_and_unknown_run(client):
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    client.post(f"/cases/{case_id}/run", json={"sync": True})
    p = client.get(f"/cases/{case_id}/evidence?format=json", headers=AUDITOR).json()
    assert p["run"]["mode"] == "scripted" and p["transcript"] == [] and p["case"]["outcome"] == "auto_approve"
    assert client.get(f"/cases/{case_id}/evidence?run_id=run_nope", headers=AUDITOR).status_code == 404
    assert client.get(f"/cases/{case_id}/evidence?format=pdf", headers=AUDITOR).status_code == 422
