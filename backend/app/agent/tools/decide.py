"""record_decision — the terminal tool. Validates the checklist, applies the deterministic policy engine."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...policy.decision_policy import decide
from .base import RunContext, Tool, ToolError, register, required_tools

TOOL = "record_decision"


class DecisionInput(BaseModel):
    recommendation: Literal["auto_approve", "request_more", "escalate", "needs_review"] = Field(
        description="Your recommended outcome based on the findings")
    rationale: str = Field(description="2-4 sentences citing the findings and policy sections that drive the recommendation")
    missing_items: list[str] = Field(default_factory=list, description="Items to request from the customer, if any")


class DecisionOutput(BaseModel):
    outcome: str
    gate: str
    reason: str
    missing_items: list[str]
    triggering_codes: list[str]
    model_recommendation: str | None
    model_tightened: bool
    review_priority: str | None
    risk_rating: str | None
    risk_score: int | None


def record_decision(ctx: RunContext, args: DecisionInput) -> DecisionOutput:
    req = required_tools(ctx.case)
    # Skipped checks are rejected so the agent goes back and runs them. Checks that were ATTEMPTED but failed (an
    # outage, or an agent identity that lost its Azure role) don't block: the policy engine then routes the case to
    # an analyst via GATE_CHECKS_INCOMPLETE - it can never auto-approve with a check missing.
    not_run = [t for t in req if t not in ctx.tools_run and t not in ctx.tools_failed and t != TOOL]
    if not_run:
        raise ToolError(f"Cannot record a decision yet — required checks not run: {not_run}. Run them first.")
    risk = ctx.results.get("rate_risk") or {}
    d = decide(ctx.findings, risk.get("rating"), [t for t in req if t != TOOL], ctx.tools_run, args.recommendation)
    extra_missing = [m for m in args.missing_items if m not in d.missing_items] if d.outcome.value == "request_more" else []
    ctx.run.model_recommendation = args.recommendation
    ctx.run.model_rationale = args.rationale
    return DecisionOutput(outcome=d.outcome.value, gate=d.gate, reason=d.reason, missing_items=d.missing_items + extra_missing,
                          triggering_codes=d.triggering_codes, model_recommendation=d.model_recommendation,
                          model_tightened=d.model_tightened, review_priority=d.review_priority,
                          risk_rating=risk.get("rating"), risk_score=risk.get("score"))


register(Tool(
    name=TOOL,
    description="Record the final onboarding decision. MUST be the last call, after every required check has run. "
                "The deterministic policy engine validates the recommendation; it can make the outcome stricter but "
                "never more lenient than policy allows.",
    input_model=DecisionInput, output_model=DecisionOutput, fn=record_decision, required=False,
))
