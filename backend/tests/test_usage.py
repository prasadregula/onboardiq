"""Model usage accounting: every chat call inside a run is booked (loop + rate_risk grounding), cost is estimated."""
import json

from app import llm as llm_mod
from app.agent.orchestrator import run_case
from app.config import get_settings
from app.db import session_scope
from app.llm import ChatResult, estimate_cost_usd, usage_scope
from app.schemas import ApplicationIn
from app.services import create_case
from tests.conftest import golden


class CountingLLM(llm_mod.BaseLLM):
    """Follows the checklist; reports a versioned model name and cached tokens like Azure does."""
    name = "fake"
    chat_deployment = "gpt-4.1-mini"

    def __init__(self):
        self.plan = ["extract_document", "check_consistency", "screen_parties", "verify_address", "rate_risk", "record_decision"]

    def _chat(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0):
        if json_mode:
            return ChatResult(content=json.dumps({"rating": "low", "score": 0, "factors": [], "rationale": "ok"}),
                              tokens_in=300, tokens_out=40, tokens_cached=0, model="gpt-4.1-mini-2025-04-14")
        name = self.plan.pop(0)
        args = {"recommendation": "auto_approve", "rationale": "all good"} if name == "record_decision" else {}
        raw = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return ChatResult(content=None, tool_calls=[{"id": "c1", "name": name, "arguments": args}], raw_message=raw,
                          tokens_in=1000, tokens_out=20, tokens_cached=800, model="gpt-4.1-mini-2025-04-14")

    def _embed(self, texts):
        return llm_mod.MockLLM()._embed(texts)


def test_usage_scope_nests_and_books_every_call():
    fake = CountingLLM()
    with usage_scope() as outer:
        fake.chat([{"role": "user", "content": "x"}])
        with usage_scope() as inner:
            fake.chat([{"role": "user", "content": "y"}], json_mode=True)
            fake.embed(["z"])
    assert inner.llm_calls == 1 and inner.embed_calls == 1 and inner.tokens_in == 300
    assert outer.llm_calls == 2 and outer.embed_calls == 1 and outer.tokens_in == 1300 and outer.tokens_cached == 800
    assert outer.model == "gpt-4.1-mini-2025-04-14"
    # outside any scope nothing is recorded, and nothing breaks
    fake.chat([{"role": "user", "content": "x"}])
    assert outer.llm_calls == 2


def test_estimate_cost_prefix_match_and_cached_discount():
    prices = {"gpt-4.1-mini": [0.40, 1.60, 0.10], "gpt-4.1": [2.0, 8.0, 0.5]}
    # longest prefix wins: the versioned mini model must price as mini, not as gpt-4.1
    c = estimate_cost_usd("gpt-4.1-mini-2025-04-14", tokens_in=1_000_000, tokens_out=0, prices=prices)
    assert c == 0.40
    c = estimate_cost_usd("gpt-4.1-2025-04-14", tokens_in=1_000_000, tokens_out=0, prices=prices)
    assert c == 2.0
    # cached input billed at the cached rate
    c = estimate_cost_usd("gpt-4.1-mini", tokens_in=1_000_000, tokens_out=1_000_000, tokens_cached=500_000, prices=prices)
    assert c == round(0.5 * 0.40 + 0.5 * 0.10 + 1.60, 6)
    assert estimate_cost_usd("qwen2.5:14b", 10_000, 1_000, prices=prices) == 0.0
    assert estimate_cost_usd(None, 10, 10, prices=prices) == 0.0


def test_run_books_loop_and_grounding_calls_onto_case_run(monkeypatch):
    s = get_settings()
    s.orchestrator_mode = "llm"
    s.azure_openai_endpoint = "https://fake.openai.azure.com/"
    monkeypatch.setattr(llm_mod, "_llm", CountingLLM())
    try:
        with session_scope() as session:
            case = create_case(session, ApplicationIn.model_validate(golden("G01")))
            run = run_case(session, case, "llm")
            assert run.status == "finished"
            assert run.llm_calls == 7  # 6 loop steps + 1 rate_risk grounding call
            assert run.tokens_in == 6 * 1000 + 300 and run.tokens_out == 6 * 20 + 40
            assert run.tokens_cached == 6 * 800
            assert run.model == "gpt-4.1-mini-2025-04-14"  # provider's real model version, not just the deployment
            assert run.cost_usd == estimate_cost_usd(run.model, run.tokens_in, run.tokens_out, run.tokens_cached)
            assert run.cost_usd > 0
            from sqlmodel import select

            from app.models import AuditEvent
            fin = [e for e in session.exec(select(AuditEvent).where(AuditEvent.case_id == case.id)).all()
                   if e.event_type == "run_finished"][0]
            assert fin.payload["llm_calls"] == 7 and fin.payload["cost_usd"] == run.cost_usd and "duration_ms" in fin.payload
    finally:
        s.orchestrator_mode = "scripted"
        s.azure_openai_endpoint = None
        llm_mod.reset_llm()


def test_scripted_run_has_zero_usage():
    with session_scope() as session:
        case = create_case(session, ApplicationIn.model_validate(golden("G01")))
        run = run_case(session, case, "scripted")
        assert run.llm_calls == 0 and run.tokens_in == 0 and run.cost_usd == 0.0
