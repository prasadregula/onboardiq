"""Persistence models (SQLModel). Same models run on SQLite (dev) and Azure SQL (cloud)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column, DateTime, Text
from sqlalchemy.dialects import mssql
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timestamps stored as naive UTC, returned as tz-aware UTC - identical on SQLite and Azure SQL.

    SQL Server's DATETIME rejects ISO strings with a "+00:00" offset ("Conversion failed when converting date and/or
    time") and rounds to ~3 ms, which would break the audit hash chain (it hashes microseconds). So: strip the offset
    on write, re-attach UTC on read, and use DATETIME2(6) on SQL Server."""
    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(mssql.DATETIME2(precision=6) if dialect.name == "mssql" else DateTime())

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class CaseStatus(str, Enum):
    intake = "intake"
    processing = "processing"
    approved = "approved"
    awaiting_customer = "awaiting_customer"
    needs_review = "needs_review"
    escalated = "escalated"
    rejected = "rejected"


class Outcome(str, Enum):
    auto_approve = "auto_approve"
    request_more = "request_more"
    escalate = "escalate"
    needs_review = "needs_review"


class ApplicantType(str, Enum):
    individual = "individual"
    business = "business"


class Case(SQLModel, table=True):
    __tablename__ = "cases"
    id: str = Field(default_factory=lambda: new_id("case"), primary_key=True, max_length=32)
    external_ref: Optional[str] = Field(default=None, index=True, max_length=128)
    applicant_type: ApplicantType
    status: CaseStatus = Field(default=CaseStatus.intake, index=True, max_length=32)
    # Declared application data (name, dob, address, tin, entity details, product, expected activity...)
    declared: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    outcome: Optional[Outcome] = None
    risk_rating: Optional[str] = None
    risk_score: Optional[int] = None
    decision_reason: Optional[str] = None
    missing_items: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Party(SQLModel, table=True):
    """A natural person or entity attached to a case: applicant, beneficial owner, controller, signer."""
    __tablename__ = "parties"
    id: str = Field(default_factory=lambda: new_id("party"), primary_key=True, max_length=32)
    case_id: str = Field(index=True, foreign_key="cases.id", max_length=32)
    role: str  # applicant | beneficial_owner | controller | signer | entity
    name: str
    dob: Optional[str] = None  # ISO date
    nationality: Optional[str] = None
    country_of_residence: Optional[str] = None
    ownership_pct: Optional[float] = None
    is_control_person: bool = False
    id_document_id: Optional[str] = Field(default=None, max_length=32)
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Document(SQLModel, table=True):
    __tablename__ = "documents"
    id: str = Field(default_factory=lambda: new_id("doc"), primary_key=True, max_length=32)
    case_id: str = Field(index=True, foreign_key="cases.id", max_length=32)
    party_id: Optional[str] = Field(default=None, max_length=32)
    doc_type: str = Field(max_length=64)  # passport | drivers_license | utility_bill | bank_statement | articles_of_incorporation | ownership_declaration | ...
    filename: str
    blob_uri: Optional[str] = None
    content_type: Optional[str] = None
    # Mock-mode fixture: what the extractor should "find" in this document
    mock_extraction: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    extracted: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    confidence: Optional[float] = None
    extracted_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class CaseRun(SQLModel, table=True):
    __tablename__ = "case_runs"
    id: str = Field(default_factory=lambda: new_id("run"), primary_key=True, max_length=32)
    case_id: str = Field(index=True, foreign_key="cases.id", max_length=32)
    mode: str
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    status: str = "running"  # running | finished | failed
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    llm_calls: int = 0
    llm_latency_ms: int = 0  # wall time spent inside model calls
    cost_usd: float = 0.0  # list-price estimate (settings.llm_prices); 0 for local/mock
    findings: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    risk: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    tool_results: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    model_recommendation: Optional[str] = None
    model_rationale: Optional[str] = Field(default=None, sa_column=Column(Text))
    outcome: Optional[Outcome] = None
    gate: Optional[str] = None
    started_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    finished_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    error: Optional[str] = None


class ReviewTask(SQLModel, table=True):
    __tablename__ = "review_tasks"
    id: str = Field(default_factory=lambda: new_id("rt"), primary_key=True, max_length=32)
    case_id: str = Field(index=True, foreign_key="cases.id", max_length=32)
    run_id: Optional[str] = Field(default=None, max_length=32)
    priority: str = Field(max_length=32)  # sanctions | pep | high_risk | possible_match | medium_risk | other
    reason_codes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="open", index=True, max_length=16)  # open | claimed | done
    assigned_to: Optional[str] = None
    due_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    analyst_action: Optional[str] = None  # approve | reject | request_more | escalate_to_bsa
    analyst_notes: Optional[str] = Field(default=None, sa_column=Column(Text))
    disposition: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    resolved_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    case_id: str = Field(index=True, foreign_key="cases.id", max_length=32)
    seq: int
    ts: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    actor: str
    event_type: str
    run_id: Optional[str] = Field(default=None, max_length=32, index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    prev_hash: str = Field(max_length=64)
    hash: str = Field(max_length=64)


class AgentControl(SQLModel, table=True):
    """Platform-wide agent controls (w4-3). One row (id=1). Read before every run and every tool call, so a pause
    reaches every replica on its next tool call - no restart, no cache to wait for."""
    __tablename__ = "agent_controls"
    id: int = Field(default=1, primary_key=True)
    paused: bool = False
    reason: Optional[str] = Field(default=None, max_length=500)
    changed_by: Optional[str] = Field(default=None, max_length=200)
    changed_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)


class ControlEvent(SQLModel, table=True):
    """Who paused or resumed the agents, when and why. Platform-level, so it lives outside the per-case audit chain."""
    __tablename__ = "control_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    actor: str = Field(max_length=200)
    action: str = Field(max_length=32)  # pause | resume | demo_reset
    reason: Optional[str] = Field(default=None, max_length=500)
