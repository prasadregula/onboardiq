"""Policy enforcement point for tool calls (week 3, w3-2) - OWASP LLM06 "excessive agency".

Every tool call passes through `authorize()` BEFORE the tool runs. The checks are code, outside the model; nothing in a
prompt, a document or a tool result can change them.

1. Caller allow-list, deny by default. The caller is the agent role that is executing when the call is made
   (app/identity.py). Only the orchestrator may dispatch tools; a tool's own code (screening, extraction, risk,
   decision) can never invoke another tool - so the screening agent cannot record a decision, and the agent that
   decides cannot re-run screening to get a different answer.
2. Argument constraints beyond the JSON schema: no unknown arguments; ids must belong to THIS case (an id from another
   case is an injection or confusion signal); bounded, plain-text free-text fields - record_decision's rationale and
   the customer-facing missing_items cannot carry links or markup.
3. Separation of duties on side effects: see `require_write()` - only the API (human-initiated requests) writes to
   document storage; no agent role can.

A denial raises ToolDenied (a ToolError, so the model sees the reason and the run continues) and is written to the
hash-chained audit trail as a `tool_denied` event with caller, tool and reason.
"""
from __future__ import annotations

import re
from typing import Any

from ..identity import current_role
from .tools.base import REGISTRY, RunContext, ToolError

# caller role -> tools it may dispatch. Anything not listed is denied.
CALL_ALLOWLIST: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"extract_document", "check_consistency", "screen_parties", "resolve_beneficial_owners",
                               "verify_address", "rate_risk", "record_decision"}),
    "extraction": frozenset(),
    "screening": frozenset(),
    "risk": frozenset(),
    "decision": frozenset(),
    "api": frozenset(),  # HTTP handlers start runs; they never call tools directly
}

# side effects: which roles may write to document storage
STORAGE_WRITERS = frozenset({"api"})

MAX_RATIONALE = 1500
MAX_MISSING_ITEMS = 10
MAX_MISSING_ITEM_LEN = 200
_UNSAFE_TEXT = re.compile(r"(https?://|www\.|<[a-z/!]|\]\(|javascript:)", re.I)


class ToolDenied(ToolError):
    def __init__(self, caller: str, tool: str, reason: str):
        self.caller, self.tool, self.reason = caller, tool, reason
        super().__init__(f"denied by policy: {reason}")


def authorize(ctx: RunContext, tool_name: str, args: dict[str, Any] | None, caller: str | None = None) -> None:
    """Raise ToolDenied unless `caller` may run `tool_name` with `args` on this case."""
    caller = caller or current_role()
    args = args or {}
    if tool_name not in CALL_ALLOWLIST.get(caller, frozenset()):
        raise ToolDenied(caller, tool_name, f"agent role '{caller}' is not allowed to call {tool_name}")
    tool = REGISTRY.get(tool_name)
    if tool is None:
        raise ToolDenied(caller, tool_name, "unknown tool")
    allowed = set(tool.input_model.model_fields)
    if unknown := sorted(set(args) - allowed):
        raise ToolDenied(caller, tool_name, f"unknown argument(s) {unknown}")
    _check_ids(ctx, tool_name, args, caller)
    if tool_name == "record_decision":
        _check_decision_text(args, caller)


def _check_ids(ctx: RunContext, tool_name: str, args: dict, caller: str) -> None:
    for key, known in (("document_ids", {d.id for d in ctx.documents()}), ("party_ids", {p.id for p in ctx.parties()})):
        ids = args.get(key)
        if ids is None:
            continue
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ToolDenied(caller, tool_name, f"{key} must be a list of ids")
        if foreign := sorted(set(ids) - known):
            raise ToolDenied(caller, tool_name, f"{key} {foreign} do not belong to case {ctx.case.id}")


def _check_decision_text(args: dict, caller: str) -> None:
    rationale = args.get("rationale") or ""
    if not isinstance(rationale, str) or len(rationale) > MAX_RATIONALE:
        raise ToolDenied(caller, "record_decision", f"rationale must be text of at most {MAX_RATIONALE} characters")
    items = args.get("missing_items") or []
    if not isinstance(items, list) or len(items) > MAX_MISSING_ITEMS:
        raise ToolDenied(caller, "record_decision", f"missing_items must be a list of at most {MAX_MISSING_ITEMS}")
    for it in items:
        if not isinstance(it, str) or len(it) > MAX_MISSING_ITEM_LEN or _UNSAFE_TEXT.search(it):
            raise ToolDenied(caller, "record_decision",
                             "missing_items are sent to the customer: plain text only, no links or markup, "
                             f"max {MAX_MISSING_ITEM_LEN} characters each")
    if _UNSAFE_TEXT.search(rationale):
        raise ToolDenied(caller, "record_decision", "rationale must not contain links or markup")


def require_write(resource: str) -> None:
    """Separation of duties for side effects: raise PermissionError if the current role may not write `resource`."""
    role = current_role()
    if resource == "storage" and role not in STORAGE_WRITERS:
        raise PermissionError(f"agent role '{role}' may not write to document storage (separation of duties)")
