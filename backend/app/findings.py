"""Finding = a typed, coded observation produced by a tool. Gates and golden cases reference codes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "critical"]


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    source_tool: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    fixable_by_customer: bool = False  # drives request_more vs escalate
    missing_item: str | None = None  # human-readable item to ask the customer for


# Catalogue (documented in ARCHITECTURE.md §5.2 / §5.4)
CRITICAL_DATA_CODES = {"DOB_MISMATCH", "ID_NUMBER_MISMATCH", "UBO_OVER_100", "DOC_UNREADABLE"}
CUSTOMER_FIXABLE_CODES = {
    "REQUIRED_DOC_MISSING", "DOC_LOW_CONFIDENCE", "DOC_EXPIRED", "UBO_COVERAGE_GAP", "UBO_MISSING_ID",
    "ADDRESS_MISMATCH", "ADDRESS_UNVERIFIED", "NAME_MISMATCH", "CONTROL_PERSON_MISSING", "ADDRESS_PO_BOX",
}
