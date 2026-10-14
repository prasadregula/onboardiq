"""Drop 3b: gate precedence (G11), rate_risk grounding guard, reasoning-model parameters, eval --model."""
import json

from app import llm as llm_mod
from app.agent.orchestrator import run_case
from app.agent.tools.risk import DATA_QUALITY_CODES, model_fact_sheet
from app.config import get_settings
from app.db import session_scope
from app.findings import Finding
from app.llm import ChatResult, is_reasoning_model
from app.models import Outcome
from app.policy.decision_policy import decide
from app.schemas import ApplicationIn
from app.services import create_case
from tests.conftest import golden

ALL = ["extract_document", "check_consistency", "screen_parties", "verify_address", "rate_risk"]


def f(code, sev="critical"):
    return Finding(code=code, severity=sev, source_tool="test", message=code)


# ---- gate precedence ------------------------------------------------------------------------------------------
def test_critical_data_outranks_high_risk_and_keeps_rating_in_reason():
    d = decide([f("UBO_OVER_100"), f("SCREENING_ADVERSE_MEDIA", "warning"), f("RISK_HIGH")], "high", ALL, set(ALL))
    assert d.outcome == Outcome.escalate and d.gate == "GATE_CRITICAL_DATA_WITH_SCREENING"
    assert "RISK_HIGH" in d.triggering_codes and "also High" in d.reason
    d = decide([f("DOB_MISMATCH"), f("RISK_HIGH")], "high", ALL, set(ALL))
    assert d.gate == "GATE_CRITICAL_DATA" and d.review_priority == "identity"


def test_unreadable_document_never_loosens_a_high_risk_escalation():
    d = decide([f("DOC_UNREADABLE"), f("RISK_HIGH")], "high", ALL, set(ALL))
    assert d.outcome == Outcome.escalate and d.gate == "GATE_HIGH_RISK"
    d = decide([f("DOC_UNREADABLE")], "low", ALL, set(ALL))
    assert d.outcome == Outcome.request_more and d.gate == "GATE_MISSING_INFO"


def test_sanctions_and_pep_still_outrank_data_conflicts():
    assert decide([f("SCREENING_SANCTIONS_MATCH"), f("DOB_MISMATCH")], "high", ALL, set(ALL)).gate == "GATE_SANCTIONS"
    assert decide([f("SCREENING_PEP_MATCH"), f("UBO_OVER_100")], "high", ALL, set(ALL)).gate == "GATE_PEP_EDD"


# ---- rate_risk grounding --------------------------------------------------------------------------------------
def test_model_fact_sheet_separates_data_quality_findings():
    fs = {"applicant_type": "business", "finding_codes": ["UBO_OVER_100", "SCREENING_ADVERSE_MEDIA"],
          "findings": [{"code": "UBO_OVER_100", "severity": "critical", "message": "110%"},
                       {"code": "SCREENING_ADVERSE_MEDIA", "severity": "warning", "message": "media"}]}
    m = model_fact_sheet(fs)
    assert "finding_codes" not in m and "findings" not in m
    assert [x["code"] for x in m["risk_relevant_findings"]] == ["SCREENING_ADVERSE_MEDIA"]
    assert [x["code"] for x in m["data_quality_findings_not_for_scoring"]] == ["UBO_OVER_100"]
    assert "UBO_OVER_100" in DATA_QUALITY_CODES


class InflatingLLM(llm_mod.BaseLLM):
    """Follows the checklist; in rate_risk it tries to push G11 to High with one ungrounded and one grounded factor."""
    name = "fake"
    chat_deployment = "gpt-4.1-mini"

    def __init__(self, grounded_weight=10):
        self.plan = ["extract_document", "check_consistency", "screen_parties", "resolve_beneficial_owners",
                     "verify_address", "rate_risk", "record_decision"]
        self.w = grounded_weight
        self.seen_fact_sheet = None

    def _chat(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0):
        if json_mode:
            self.seen_fact_sheet = messages[1]["content"]
            return ChatResult(content=json.dumps({"rating": "high", "score": 90, "rationale": "ownership inconsistent", "factors": [
                {"name": "Ownership percentages inconsistent", "weight": 50, "policy_ref": "model judgement"},
                {"name": "Investment advisory with adverse media", "weight": self.w, "policy_ref": "KYC-POL-002 §2"}]}),
                tokens_in=100, tokens_out=20, model="fake")
        name = self.plan.pop(0)
        args = {"recommendation": "escalate", "rationale": "x"} if name == "record_decision" else {}
        raw = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return ChatResult(content=None, tool_calls=[{"id": "c", "name": name, "arguments": args}], raw_message=raw,
                          tokens_in=100, tokens_out=10, model="fake")

    def _embed(self, texts):
        return llm_mod.MockLLM()._embed(texts)


def test_g11_model_cannot_inflate_rating_or_change_the_reason(monkeypatch):
    s = get_settings()
    s.orchestrator_mode, s.azure_openai_endpoint = "llm", "https://fake.openai.azure.com/"
    fake = InflatingLLM(grounded_weight=10)
    monkeypatch.setattr(llm_mod, "_llm", fake)
    try:
        with session_scope() as session:
            case = create_case(session, ApplicationIn.model_validate(golden("G11")))
            run = run_case(session, case, "llm")
            risk = run.risk
            assert [r["name"] for r in risk["rejected_model_factors"]] == ["Ownership percentages inconsistent"]
            assert any(x["source"] == "model" and x["weight"] == 10 for x in risk["factors"])
            assert risk["score"] == risk["floor_score"] + 10 and risk["rating"] == "medium"  # 25 + 10 = 35, not 90/high
            assert run.gate.split("+")[0] == "GATE_CRITICAL_DATA_WITH_SCREENING" and run.outcome == Outcome.escalate
            assert "data_quality_findings_not_for_scoring" in fake.seen_fact_sheet
            assert "KYC-POL-002 §2" in json.dumps(risk["citations"], ensure_ascii=False)  # weights table always in the evidence set
    finally:
        s.orchestrator_mode, s.azure_openai_endpoint = "scripted", None
        llm_mod.reset_llm()


# ---- reasoning models -----------------------------------------------------------------------------------------
def test_reasoning_model_detection_and_override():
    assert is_reasoning_model("gpt-5.4-mini") and is_reasoning_model("o4-mini") and is_reasoning_model("gpt-6-astra")
    assert not is_reasoning_model("gpt-4.1-mini") and not is_reasoning_model("gpt-5-chat")
    assert is_reasoning_model("my-kyc-deployment", "yes") and not is_reasoning_model("gpt-5", "no")


def test_reasoning_request_omits_temperature(monkeypatch):
    sent = {}

    class Resp:
        class _U:
            prompt_tokens, completion_tokens = 10, 30
            prompt_tokens_details = type("P", (), {"cached_tokens": 4})()
            completion_tokens_details = type("C", (), {"reasoning_tokens": 20})()
        usage = _U()
        model = "gpt-5.4-mini-2026-03-17"
        choices = [type("Ch", (), {"message": type("M", (), {"content": "{}", "tool_calls": None,
                                                              "model_dump": lambda self, **k: {"role": "assistant"}})()})()]

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    sent.update(kw)
                    return Resp()

    s = get_settings()
    s.llm_reasoning_effort = "low"
    try:
        m = object.__new__(llm_mod.AzureOpenAILLM)
        m.client, m.chat_deployment = Client(), "gpt-5.4-mini"
        res = m._request([{"role": "user", "content": "x"}], json_mode=True)
        assert "temperature" not in sent and sent["reasoning_effort"] == "low"
        assert res.tokens_reasoning == 20 and res.tokens_cached == 4
        m.chat_deployment = "gpt-4.1-mini"
        sent.clear()
        m._request([{"role": "user", "content": "x"}])
        assert sent["temperature"] == 0.0 and "reasoning_effort" not in sent
    finally:
        s.llm_reasoning_effort = None


def test_eval_model_override_and_risk_ceiling():
    from eval import run_eval
    s = get_settings()
    before = (s.azure_openai_chat_deployment, s.ollama_chat_model)
    try:
        run_eval.apply_model_override("gpt-5.4-mini")
        assert s.azure_openai_chat_deployment == "gpt-5.4-mini" == s.ollama_chat_model
    finally:
        s.azure_openai_chat_deployment, s.ollama_chat_model = before
        llm_mod.reset_llm()
    from tests.conftest import GOLDEN
    g = json.loads(next(GOLDEN.glob("G11*.json")).read_text())
    assert g["expect"]["gate"] == "GATE_CRITICAL_DATA_WITH_SCREENING" and g["expect"]["risk_rating_max"] == "medium"
