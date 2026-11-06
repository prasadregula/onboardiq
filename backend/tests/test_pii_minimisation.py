"""T-07: no personal data reaches the model, across every golden case; analysts still see real names; free text is
scanned by Azure AI Language when configured and withheld (not sent) if that service is down."""
import json

import httpx
import pytest

from app import identity
from app import llm as llm_mod
from app.agent import pii as pii_mod
from app.agent.pii import WITHHELD, Pseudonymizer
from app.agent.tools.base import required_tools
from app.config import get_settings
from app.db import session_scope
from app.llm import ChatResult
from app.models import Case, CaseRun
from tests.conftest import GOLDEN, golden

ORDER = ["extract_document", "check_consistency", "screen_parties", "resolve_beneficial_owners", "verify_address",
         "rate_risk"]


def _plan(case_id: str) -> list[str]:
    with session_scope() as s:
        req = set(required_tools(s.get(Case, case_id)))
    return [t for t in ORDER if t in req] + ["record_decision"]


class RecordingLLM(llm_mod.BaseLLM):
    """Follows the checklist for whatever case it is given and records every message it receives."""
    name = "recording"

    def __init__(self, plan):
        self.plan, self.step, self.seen = plan, 0, []

    def _chat(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0):
        self.seen.append(json.dumps(messages, default=str))
        if json_mode:
            return ChatResult(content=json.dumps({"rating": "low", "score": 0, "factors": [],
                                                  "rationale": "PERSON_1 has no risk factors."}), model="rec")
        name = self.plan[min(self.step, len(self.plan) - 1)]
        self.step += 1
        args = {"recommendation": "needs_review", "rationale": "PERSON_1 lives at ADDRESS_1; see findings."} \
            if name == "record_decision" else {}
        call = {"id": f"c{self.step}", "name": name, "arguments": args}
        raw = {"role": "assistant", "content": None, "tool_calls": [
            {"id": call["id"], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return ChatResult(content=None, tool_calls=[call], raw_message=raw, tokens_in=10, tokens_out=5, model="rec")

    def _embed(self, texts):
        return llm_mod.MockLLM()._embed(texts)


def _personal_values(app: dict) -> set[str]:
    vals = set()
    for p in app.get("parties", []):
        vals |= {p.get("name"), p.get("dob")}
    d = app.get("declared", {})
    vals |= {d.get("address"), d.get("entity_name")}
    for doc in app.get("documents", []):
        f = (doc.get("mock_extraction") or {}).get("fields", {})
        vals |= {f.get(k) for k in ("full_name", "dob", "address", "entity_name")}
    return {v for v in vals if isinstance(v, str) and len(v) >= 4}


@pytest.fixture
def llm_mode(monkeypatch):
    s = get_settings()
    s.orchestrator_mode, s.azure_openai_endpoint = "llm", "https://fake.openai.azure.com/"
    yield s
    s.orchestrator_mode, s.azure_openai_endpoint, s.pii_endpoint = "scripted", None, None
    llm_mod.reset_llm()


@pytest.mark.parametrize("path", sorted(GOLDEN.glob("G*.json")), ids=lambda p: p.stem[:3])
def test_no_personal_data_reaches_the_model(client, llm_mode, monkeypatch, path):
    app = json.loads(path.read_text())["application"]
    case_id = client.post("/cases", json=app).json()["id"]
    plan = _plan(case_id)
    rec = RecordingLLM(plan)
    monkeypatch.setattr(llm_mod, "_llm", rec)
    body = client.post(f"/cases/{case_id}/run", json={"sync": True}).json()
    assert body["run"]["status"] == "finished", body["run"]
    sent = "\n".join(rec.seen)
    leaked = sorted(v for v in _personal_values(app) if v.lower() in sent.lower())
    assert not leaked, f"{path.stem}: sent to the model: {leaked}"
    assert "PERSON_1" in sent  # the model still gets something to reason with
    with session_scope() as s:
        run = s.get(CaseRun, body["run"]["id"])
        names = [p["name"] for p in app["parties"]]
        assert "PERSON_1" not in run.model_rationale and "ADDRESS_1" not in run.model_rationale  # re-identified
        assert any(n in run.model_rationale for n in names)


def test_tokens_are_consistent_and_variants_get_their_own_token():
    p = Pseudonymizer(scan_free_text=False)
    p.token("PERSON", "Maria Elena Castillo")
    out = p.minimise({"documents": [{"fields": {"full_name": "Maria E. Castillo", "dob": "1988-02-11",
                                                "address": "1420 Wilshire Blvd", "statement_date": "2026-08-20"}}],
                      "note": "Name on bill differs from maria elena castillo", "factors": [{"name": "PEP"}]})
    f = out["documents"][0]["fields"]
    assert f["full_name"] == "PERSON_2" and f["dob"] == "DOB_1" and f["address"] == "ADDRESS_1"
    assert f["statement_date"] == "2026-08-20"                       # not personal: untouched
    assert out["note"] == "Name on bill differs from PERSON_1"       # known value found inside free text
    assert out["factors"][0]["name"] == "PEP"                        # a risk factor's name is not a person
    assert p.reidentify("PERSON_1 vs PERSON_2 at ADDRESS_1") == "Maria Elena Castillo vs Maria E. Castillo at 1420 Wilshire Blvd"


def _language(monkeypatch, *, fail=False):
    s = get_settings()
    s.pii_endpoint = "https://lang.example.cognitiveservices.azure.com/"
    monkeypatch.setattr(identity, "token_provider", lambda scope: (lambda: "token"))
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"])
        if fail:
            raise httpx.ConnectError("unreachable")
        assert url.endswith("/language/:analyze-text") and kw["headers"]["Authorization"] == "Bearer token"
        docs = kw["json"]["analysisInput"]["documents"]
        assert len(docs) <= 5
        res = []
        for d in docs:
            ents = [{"text": "555-201-7788", "category": "PhoneNumber", "confidenceScore": 0.9}] if "555-201-7788" in d["text"] else []
            ents += [{"text": "Priya Raman", "category": "Person", "confidenceScore": 0.95}] if "Priya Raman" in d["text"] else []
            ents += [{"text": "2026", "category": "DateTime", "confidenceScore": 0.9}] if "2026" in d["text"] else []
            res.append({"id": d["id"], "entities": ents, "warnings": []})
        return httpx.Response(200, json={"results": {"documents": res, "errors": []}}, request=httpx.Request("POST", url))
    monkeypatch.setattr(pii_mod.httpx, "post", fake_post)
    return calls


def test_language_service_masks_names_hidden_in_free_text(monkeypatch):
    calls = _language(monkeypatch)
    try:
        p = Pseudonymizer()
        out = p.minimise({"fields": {"account_notes": "Call Priya Raman on 555-201-7788 before March 2026 renewal.",
                                     "doc_type": "utility_bill", "short": "ok"}})
    finally:
        get_settings().pii_endpoint = None
    assert out["fields"]["account_notes"] == "Call PERSON_1 on PHONE_1 before March 2026 renewal."  # dates kept
    assert out["fields"]["doc_type"] == "utility_bill" and len(calls) == 1
    assert p.stats["entities_found"] == 2 and p.stats["service"] == "azure-ai-language"


def test_language_outage_withholds_free_text(monkeypatch):
    _language(monkeypatch, fail=True)
    try:
        p = Pseudonymizer()
        out = p.minimise({"fields": {"account_notes": "Call Priya Raman on 555-201-7788 before renewal.",
                                     "dob": "1990-01-01"}})
    finally:
        get_settings().pii_endpoint = None
    assert out["fields"]["account_notes"] == WITHHELD and out["fields"]["dob"] == "DOB_1"  # fail closed
    assert p.stats["texts_withheld"] == 1


def test_minimisation_is_recorded_in_the_audit(client, llm_mode, monkeypatch):
    case_id = client.post("/cases", json=golden("G01")).json()["id"]
    plan = _plan(case_id)
    monkeypatch.setattr(llm_mod, "_llm", RecordingLLM(plan))
    client.post(f"/cases/{case_id}/run", json={"sync": True})
    events = client.get(f"/cases/{case_id}/audit").json()
    ev = next(e for e in (events if isinstance(events, list) else events["events"]) if e["event_type"] == "model_view_minimised")
    assert ev["payload"]["values_known"] >= 3 and ev["payload"]["tokens_replaced"] > 0
    assert "Maria" not in json.dumps(ev["payload"])  # counts only, never values


def test_health_reports_pii_masking(client):
    assert client.get("/health").json()["pii_masking"] == "pseudonymise"
