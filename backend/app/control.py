"""Run controls (w4-3): the platform kill switch and per-run budgets.

Two different questions, both answered in code outside the model:
- Kill switch: "may ANY agent act right now?" A single row in the database, read before every run and before every
  tool call and model turn, so a pause reaches every replica within one step. Only the Admin role can flip it, and
  every change is recorded with who, when and why.
- Budgets: "has THIS run spent enough?" Hard caps on tool calls, model tokens, estimated cost and wall time. They are
  separate from the orchestrator's soft step budget (max_tool_calls), which finishes the checklist deterministically;
  a hard budget stops the run and hands the case to an analyst.

Either control raises RunAborted. The orchestrator records `run_aborted` in the case's audit chain, the run ends with
status "aborted", and the case goes to the analyst queue with the abort code as the reason. A stopped run never
produces a decision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlmodel import Session, select

from .config import get_settings
from .models import AgentControl, ControlEvent, utcnow

KILL_SWITCH = "KILL_SWITCH"
BUDGET_TOOL_CALLS = "BUDGET_TOOL_CALLS"
BUDGET_TOKENS = "BUDGET_TOKENS"
BUDGET_COST = "BUDGET_COST"
BUDGET_TIME = "BUDGET_TIME"


class RunAborted(Exception):
    """A control stopped the run. `code` is machine-readable and becomes the review task's reason code."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details or {}


class AgentsPaused(Exception):
    def __init__(self, state: AgentControl):
        super().__init__(f"Agents are paused by {state.changed_by}: {state.reason or 'no reason given'}")
        self.state = state


# ------------------------------------------------------------------------------------------------------ kill switch
def get_state(session: Session) -> AgentControl:
    row = session.get(AgentControl, 1)
    return row or AgentControl(id=1, paused=False)


def set_paused(session: Session, paused: bool, actor: str, reason: str | None) -> AgentControl:
    row = session.get(AgentControl, 1) or AgentControl(id=1)
    row.paused, row.reason, row.changed_by, row.changed_at = paused, (reason or "")[:500] or None, actor[:200], utcnow()
    session.add(row)
    session.add(ControlEvent(actor=actor[:200], action="pause" if paused else "resume", reason=row.reason))
    session.flush()
    return row


def record_event(session: Session, actor: str, action: str, reason: str | None = None) -> None:
    session.add(ControlEvent(actor=actor[:200], action=action, reason=(reason or "")[:500] or None))
    session.flush()


def history(session: Session, limit: int = 10) -> list[ControlEvent]:
    return list(session.exec(select(ControlEvent).order_by(ControlEvent.id.desc()).limit(limit)).all())  # type: ignore[union-attr]


def ensure_not_paused(session: Session) -> None:
    state = session.get(AgentControl, 1)
    if state is not None and state.paused:
        raise AgentsPaused(state)


# ---------------------------------------------------------------------------------------------------------- budgets
@dataclass
class RunBudget:
    max_tool_calls: int
    max_tokens: int
    max_cost_usd: float
    max_seconds: int
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0  # every attempt, including denied and failed ones: an agent looping on errors still spends

    @classmethod
    def from_settings(cls) -> "RunBudget":
        s = get_settings()
        return cls(s.run_max_tool_calls, s.run_max_tokens, s.run_max_cost_usd, s.run_max_seconds)

    def limits(self) -> dict:
        return {"max_tool_calls": self.max_tool_calls, "max_tokens": self.max_tokens,
                "max_cost_usd": self.max_cost_usd, "max_seconds": self.max_seconds}


def check(session: Session, budget: RunBudget | None, usage=None, model: str | None = None) -> None:
    """Called before every tool call and every model turn. Raises RunAborted if the run must stop."""
    from .llm import estimate_cost_usd

    try:
        ensure_not_paused(session)
    except AgentsPaused as p:
        raise RunAborted(KILL_SWITCH, f"agents paused by {p.state.changed_by}: {p.state.reason or 'no reason given'}",
                         {"paused_by": p.state.changed_by, "reason": p.state.reason}) from None
    if budget is None:
        return
    spent = {"tool_calls": budget.tool_calls, "seconds": round(time.monotonic() - budget.started, 1)}
    if budget.tool_calls >= budget.max_tool_calls:
        raise RunAborted(BUDGET_TOOL_CALLS, f"{budget.tool_calls} tool calls reached the limit of {budget.max_tool_calls}",
                         spent | budget.limits())
    if spent["seconds"] > budget.max_seconds:
        raise RunAborted(BUDGET_TIME, f"run time {spent['seconds']} s exceeded {budget.max_seconds} s", spent | budget.limits())
    if usage is not None:
        tokens = usage.tokens_in + usage.tokens_out
        cost = estimate_cost_usd(model or usage.model or "", usage.tokens_in, usage.tokens_out, usage.tokens_cached)
        spent |= {"tokens": tokens, "cost_usd": round(cost, 5)}
        if tokens > budget.max_tokens:
            raise RunAborted(BUDGET_TOKENS, f"{tokens} model tokens exceeded {budget.max_tokens}", spent | budget.limits())
        if cost > budget.max_cost_usd:
            raise RunAborted(BUDGET_COST, f"estimated cost ${cost:.4f} exceeded ${budget.max_cost_usd:.4f}",
                             spent | budget.limits())
