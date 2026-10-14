"""Typed tool registry. Every tool = Pydantic input + Pydantic output + pure function over a RunContext.

The model sees the JSON schema generated from the input model; arguments are validated before execution and
results are validated before being stored. That is what makes runs replayable and evaluable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from ...config import Settings
from ...findings import Finding
from ...models import Case, CaseRun, Document, Party


class ToolError(Exception):
    """Returned to the model as a tool error message; never crashes the run."""


@dataclass
class RunContext:
    session: Session
    case: Case
    run: CaseRun
    settings: Settings
    findings: list[Finding] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)  # tool name -> last output (dict)
    tools_run: set[str] = field(default_factory=set)
    tools_failed: set[str] = field(default_factory=set)  # attempted but errored (e.g. identity lost its role)

    def parties(self) -> list[Party]:
        return list(self.session.exec(select(Party).where(Party.case_id == self.case.id)).all())

    def documents(self) -> list[Document]:
        return list(self.session.exec(select(Document).where(Document.case_id == self.case.id)).all())

    def add_findings(self, fs: list[Finding]) -> None:
        seen = {(f.code, f.message) for f in self.findings}
        for f in fs:
            if (f.code, f.message) not in seen:
                self.findings.append(f)
                seen.add((f.code, f.message))

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.findings)


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    fn: Callable[[RunContext, BaseModel], BaseModel]
    required: bool = True  # part of the mandatory checklist before record_decision
    business_only: bool = False

    def run(self, ctx: RunContext, raw_args: dict[str, Any]) -> BaseModel:
        try:
            args = self.input_model.model_validate(raw_args or {})
        except ValidationError as e:
            raise ToolError(f"invalid arguments for {self.name}: {e.errors(include_url=False)}") from e
        from ...identity import acting_as, role_for_tool
        with acting_as(role_for_tool(self.name)):  # Azure/LLM calls inside the tool use this agent's identity
            out = self.fn(ctx, args)
        out = self.output_model.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
        ctx.results[self.name] = out.model_dump(mode="json")
        ctx.tools_run.add(self.name)
        fs = getattr(out, "findings", None)
        if fs:
            ctx.add_findings([Finding.model_validate(f) if not isinstance(f, Finding) else f for f in fs])
        return out

    def openai_spec(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": schema}}


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    REGISTRY[tool.name] = tool
    return tool


def openai_tool_specs() -> list[dict[str, Any]]:
    return [t.openai_spec() for t in REGISTRY.values()]


def required_tools(case: Case) -> list[str]:
    from ...models import ApplicantType
    return [n for n, t in REGISTRY.items() if t.required and (not t.business_only or case.applicant_type == ApplicantType.business)]
