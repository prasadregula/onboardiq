"""verify_address — deliverability, PO Box, jurisdiction risk for the declared address and each party's residence."""
from __future__ import annotations

from pydantic import BaseModel

from ...findings import Finding
from ...providers.address import get_address_provider
from .base import RunContext, Tool, register

TOOL = "verify_address"


class AddressInput(BaseModel):
    pass


class AddressCheck(BaseModel):
    subject: str
    input: str
    normalized: str
    deliverable: bool
    is_po_box: bool
    country: str
    jurisdiction_risk: str
    notes: list[str]


class AddressOutput(BaseModel):
    provider: str
    checks: list[AddressCheck]
    findings: list[Finding]


def verify_address(ctx: RunContext, _: AddressInput) -> AddressOutput:
    provider = get_address_provider()
    checks, findings = [], []
    subjects: list[tuple[str, str, str | None]] = []
    if ctx.case.declared.get("address"):
        subjects.append(("applicant address", ctx.case.declared["address"], ctx.case.declared.get("country")))
    for p in ctx.parties():
        addr = (p.extra or {}).get("address")
        if addr and p.role != "applicant":
            subjects.append((f"{p.role} {p.name}", addr, p.country_of_residence))
    for subject, addr, country in subjects:
        r = provider.verify(addr, country)
        checks.append(AddressCheck(subject=subject, input=r.input, normalized=r.normalized, deliverable=r.deliverable,
                                   is_po_box=r.is_po_box, country=r.country, jurisdiction_risk=r.jurisdiction_risk, notes=r.notes))
        if r.is_po_box:
            findings.append(Finding(code="ADDRESS_PO_BOX", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                    message=f"{subject} is a PO Box — a physical street address is required (KYC-POL-001 §2, KYC-POL-005 §4)",
                                    evidence={"address": r.normalized}, missing_item=f"Physical street address for {subject}"))
        elif not r.deliverable:
            findings.append(Finding(code="ADDRESS_UNVERIFIED", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                    message=f"{subject} could not be verified as a deliverable address: {'; '.join(r.notes) or 'no match'}",
                                    evidence={"address": r.normalized}, missing_item=f"Verifiable street address for {subject}"))
        if r.jurisdiction_risk == "high":
            findings.append(Finding(code="RISK_HIGH_JURISDICTION", severity="warning", source_tool=TOOL,
                                    message=f"{subject} is in a FATF high-risk jurisdiction ({r.country}) — KYC-POL-002 §2",
                                    evidence={"country": r.country}))
        elif r.jurisdiction_risk == "increased":
            findings.append(Finding(code="RISK_INCREASED_MONITORING_JURISDICTION", severity="info", source_tool=TOOL,
                                    message=f"{subject} is in a jurisdiction under increased monitoring ({r.country}) — KYC-POL-002 §2",
                                    evidence={"country": r.country}))
    if not subjects:
        findings.append(Finding(code="ADDRESS_UNVERIFIED", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                message="No address was declared on the application", missing_item="Residential/principal business street address"))
    return AddressOutput(provider=provider.name, checks=checks, findings=findings)


register(Tool(
    name=TOOL,
    description="Verify the declared applicant address and beneficial-owner residence addresses: deliverability, "
                "PO Box detection, and jurisdiction risk classification (FATF high-risk / increased monitoring).",
    input_model=AddressInput, output_model=AddressOutput, fn=verify_address,
))
