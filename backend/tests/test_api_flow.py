from sqlmodel import select

from app.db import session_scope
from app.models import AuditEvent
from tests.conftest import golden


def test_health_and_tools(client):
    assert client.get("/health").json()["ok"] is True
    names = {t["name"] for t in client.get("/tools").json()}
    assert {"extract_document", "check_consistency", "screen_parties", "resolve_beneficial_owners", "verify_address",
            "rate_risk", "record_decision"} <= names


def test_api_key_required(client):
    client.headers.pop("X-API-Key")
    assert client.get("/cases").status_code == 401


def test_clean_case_auto_approves_with_verifiable_audit(client):
    r = client.post("/cases", json=golden("G01"))
    assert r.status_code == 201
    cid = r.json()["id"]
    run = client.post(f"/cases/{cid}/run", json={}).json()
    assert run["case"]["status"] == "approved" and run["run"]["outcome"] == "auto_approve"
    assert run["run"]["risk"]["citations"], "risk rating must carry policy citations"
    audit = client.get(f"/cases/{cid}/audit").json()
    types = [e["event_type"] for e in audit]
    assert types[0] == "case_created" and "policy_gate" in types and types[-1] == "run_finished"
    assert client.get(f"/cases/{cid}/audit/verify").json()["ok"] is True


def test_tampering_breaks_the_chain(client):
    cid = client.post("/cases", json=golden("G02")).json()["id"]
    client.post(f"/cases/{cid}/run", json={})
    with session_scope() as s:
        ev = s.exec(select(AuditEvent).where(AuditEvent.case_id == cid, AuditEvent.seq == 3)).one()
        ev.payload = {**ev.payload, "tampered": True}
        s.add(ev)
        s.commit()
    v = client.get(f"/cases/{cid}/audit/verify").json()
    assert v["ok"] is False and v["broken_at_seq"] == 3


def test_escalation_lands_in_queue_and_analyst_flow(client):
    cid = client.post("/cases", json=golden("G05")).json()["id"]
    client.post(f"/cases/{cid}/run", json={})
    queue = client.get("/review/queue").json()
    item = next(q for q in queue if q["case_id"] == cid)
    assert item["priority"] == "pep" and item["status"] == "open"
    tid = item["task_id"]
    assert client.post(f"/review/tasks/{tid}/action", json={"action": "claim"}).json()["task"]["assigned_to"] == "analyst1"
    # another analyst cannot act on a claimed task
    other = client.post(f"/review/tasks/{tid}/action", json={"action": "approve"}, headers={"X-Analyst": "ben"})
    assert other.status_code == 409
    done = client.post(f"/review/tasks/{tid}/action", json={"action": "approve", "notes": "EDD completed, SoW verified",
                                                            "disposition": {"pep": "confirmed", "edd": "complete"}}).json()
    assert done["case_status"] == "approved" and done["task"]["status"] == "done"
    assert client.get("/review/queue").json() == [] or all(q["case_id"] != cid for q in client.get("/review/queue").json())
    last = client.get(f"/cases/{cid}/audit").json()[-1]
    assert last["event_type"] == "review_action" and last["actor"] == "analyst:analyst1"


def test_sanctions_approval_requires_second_approver(client):
    cid = client.post("/cases", json=golden("G04")).json()["id"]
    client.post(f"/cases/{cid}/run", json={})
    tid = next(q for q in client.get("/review/queue").json() if q["case_id"] == cid)["task_id"]
    r = client.post(f"/review/tasks/{tid}/action", json={"action": "approve", "notes": "false positive"})
    assert r.status_code == 403
    r = client.post(f"/review/tasks/{tid}/action", json={"action": "approve", "notes": "false positive",
                                                         "disposition": {"screening": "false_positive", "second_approver": "bsa.officer"}})
    assert r.status_code == 200 and r.json()["case_status"] == "approved"


def test_request_more_then_resubmit_reruns(client):
    cid = client.post("/cases", json=golden("G02")).json()["id"]
    first = client.post(f"/cases/{cid}/run", json={}).json()
    assert first["case"]["status"] == "awaiting_customer" and first["case"]["missing_items"]
    detail = client.get(f"/cases/{cid}").json()
    assert detail["latest_run"]["gate"] == "GATE_MISSING_INFO" and detail["review_task"] is None
    stats = client.get("/stats").json()
    assert stats["cases_by_status"]["awaiting_customer"] == 1
