"""Exercises the llm-mode orchestration loop with a fake tool-calling model (no Azure needed)."""
import json

from app import llm as llm_mod
from app.agent.orchestrator import run_llm
from app.agent.tools import RunContext
from app.config import get_settings
from app.db import session_scope
from app.llm import ChatResult, usage_scope
from app.models import CaseRun
from app.schemas import ApplicationIn
from app.services import create_case
from tests.conftest import golden


class FakeAgentLLM(llm_mod.BaseLLM):
    """Misbehaves on purpose: tries record_decision first (must be rejected), then follows the checklist."""
    name = "fake"

    def __init__(self):
        self.step = 0
        self.plan = ["record_decision", "extract_document", "check_consistency", "screen_parties", "verify_address",
                     "rate_risk", "record_decision"]

    def _chat(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0):
        if json_mode:  # rate_risk grounding call: model tries to LOWER the rating - must be ignored
            return ChatResult(content=json.dumps({"rating": "low", "score": 0, "factors": [], "rationale": "looks fine"}),
                              tokens_in=10, tokens_out=5, model="fake")
        name = self.plan[self.step]
        self.step += 1
        args = {"recommendation": "auto_approve", "rationale": "all good"} if name == "record_decision" else {}
        call = {"id": f"call_{self.step}", "name": name, "arguments": args}
        raw = {"role": "assistant", "content": None,
               "tool_calls": [{"id": call["id"], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
        return ChatResult(content=None, tool_calls=[call], raw_message=raw, tokens_in=100, tokens_out=20, model="fake")

    def _embed(self, texts):
        return llm_mod.MockLLM()._embed(texts)


def test_llm_loop_rejects_premature_decision_and_policy_overrides_model(monkeypatch):
    s = get_settings()
    s.orchestrator_mode = "llm"
    s.azure_openai_endpoint = "https://fake.openai.azure.com/"
    fake = FakeAgentLLM()
    monkeypatch.setattr(llm_mod, "_llm", fake)
    try:
        with session_scope() as session:
            case = create_case(session, ApplicationIn.model_validate(golden("G12")))  # medium risk case
            run = CaseRun(case_id=case.id, mode="llm")
            session.add(run)
            session.flush()
            ctx = RunContext(session=session, case=case, run=run, settings=s)
            with usage_scope() as usage:  # run_case() opens this scope; here we call run_llm directly
                decision = run_llm(ctx)
            # the fake model recommended auto_approve and rated low; policy floor says medium -> needs_review
            assert decision["outcome"] == "needs_review" and decision["gate"] == "GATE_MEDIUM_RISK"
            assert decision["model_recommendation"] == "auto_approve" and not decision["model_tightened"]
            assert ctx.results["rate_risk"]["rating"] == "medium" and ctx.results["rate_risk"]["model_used"] == "fake"
            assert run.tool_calls == 6 and usage.llm_calls == 8 and usage.tokens_in > 0  # 7 loop steps + grounding
            from sqlmodel import select

            from app.models import AuditEvent
            errs = [e for e in session.exec(select(AuditEvent).where(AuditEvent.case_id == case.id)).all() if e.event_type == "tool_error"]
            assert len(errs) == 1 and "required checks not run" in errs[0].payload["error"]
    finally:
        s.orchestrator_mode = "scripted"
        s.azure_openai_endpoint = None
        llm_mod.reset_llm()
