"""API request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import ApplicantType


class PartyIn(BaseModel):
    role: Literal["applicant", "beneficial_owner", "controller", "signer", "entity"]
    name: str
    dob: str | None = None
    nationality: str | None = None
    country_of_residence: str | None = None
    ownership_pct: float | None = None
    is_control_person: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)
    key: str | None = Field(default=None, description="Client-side key so documents can reference this party")


class DocumentIn(BaseModel):
    doc_type: str
    filename: str
    party_key: str | None = None
    mock_extraction: dict[str, Any] | None = Field(default=None, description="Fixture used when the extraction provider is mock")


class ApplicationIn(BaseModel):
    external_ref: str | None = None
    applicant_type: ApplicantType
    declared: dict[str, Any] = Field(default_factory=dict)
    parties: list[PartyIn]
    documents: list[DocumentIn] = Field(default_factory=list)


class RunRequest(BaseModel):
    mode: Literal["scripted", "llm"] | None = None
    sync: bool | None = Field(default=None, description="Force synchronous run (default from ONBOARDIQ_RUN_ASYNC)")


class ReviewActionIn(BaseModel):
    action: Literal["claim", "approve", "reject", "request_more", "escalate_to_bsa", "reassign", "release"]
    notes: str | None = None
    message_to_customer: str | None = None
    assign_to: str | None = None
    disposition: dict[str, Any] | None = Field(default=None, description="e.g. {\"screening\": \"false_positive\"}")


class CaseSummary(BaseModel):
    id: str
    external_ref: str | None
    applicant_type: str
    applicant_name: str | None
    status: str
    outcome: str | None
    risk_rating: str | None
    risk_score: int | None
    created_at: datetime
    updated_at: datetime


class QueueItem(BaseModel):
    task_id: str
    case_id: str
    applicant_name: str | None
    applicant_type: str
    priority: str
    reason_codes: list[str]
    status: str
    assigned_to: str | None
    outcome: str | None
    risk_rating: str | None
    created_at: datetime
    due_at: datetime | None
