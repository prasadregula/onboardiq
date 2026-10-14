"""resolve_beneficial_owners — FinCEN CDD rule: 25% ownership prong + control prong, coverage, ID on file.

Ownership can be declared directly (Party.ownership_pct) or through intermediate entities described in
case.declared["ownership_tree"] = [{"owner": "<name>", "of": "<entity name>", "pct": 50}, ...]; indirect
percentages are multiplied through each tier (KYC-POL-003 §2).
"""
from __future__ import annotations

from pydantic import BaseModel

from ...findings import Finding
from ...models import ApplicantType
from ...providers.extraction import ID_DOC_TYPES
from .base import RunContext, Tool, register

TOOL = "resolve_beneficial_owners"


class UboInput(BaseModel):
    pass


class UboEntry(BaseModel):
    party_id: str | None
    name: str
    direct_pct: float
    effective_pct: float
    via: list[str]
    is_control_person: bool
    id_document_on_file: bool
    meets_ownership_prong: bool


class UboOutput(BaseModel):
    applicable: bool
    threshold_pct: float
    owners: list[UboEntry]
    declared_coverage_pct: float
    control_person: str | None
    findings: list[Finding]


def _effective(tree: list[dict], root: str, owner: str, _seen=None) -> tuple[float, list[str]]:
    """Effective ownership of `owner` in `root` through the declared tree (depth-first, multiplicative)."""
    _seen = _seen or set()
    total, via = 0.0, []
    for edge in tree:
        if edge["of"].lower() != root.lower():
            continue
        if edge["owner"].lower() == owner.lower():
            total += float(edge["pct"])
        elif edge["owner"].lower() not in _seen:
            sub, path = _effective(tree, edge["owner"], owner, _seen | {root.lower()})
            if sub:
                total += float(edge["pct"]) * sub / 100.0
                via.append(edge["owner"])
    return total, via


def resolve_beneficial_owners(ctx: RunContext, _: UboInput) -> UboOutput:
    s = ctx.settings
    case = ctx.case
    if case.applicant_type != ApplicantType.business:
        return UboOutput(applicable=False, threshold_pct=s.ubo_threshold_pct, owners=[], declared_coverage_pct=100.0,
                         control_person=None, findings=[])
    parties, docs = ctx.parties(), ctx.documents()
    entity_name = case.declared.get("entity_name", "")
    tree = case.declared.get("ownership_tree") or []
    id_by_party = {d.party_id for d in docs if d.doc_type in ID_DOC_TYPES and d.party_id}
    findings: list[Finding] = []
    owners: list[UboEntry] = []
    for p in parties:
        if p.role not in ("beneficial_owner", "controller", "applicant"):
            continue
        direct = float(p.ownership_pct or 0)
        eff, via = (_effective(tree, entity_name, p.name) if tree else (direct, []))
        if tree and direct and not eff:
            eff = direct
        meets = eff >= s.ubo_threshold_pct
        owners.append(UboEntry(party_id=p.id, name=p.name, direct_pct=direct, effective_pct=round(eff, 2), via=via,
                               is_control_person=p.is_control_person, id_document_on_file=p.id in id_by_party,
                               meets_ownership_prong=meets))
        if (meets or p.is_control_person) and p.id not in id_by_party:
            findings.append(Finding(code="UBO_MISSING_ID", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                    message=f"No identification document on file for {'control person' if p.is_control_person else 'beneficial owner'} {p.name} ({eff:.0f}%) — KYC-POL-003 §5",
                                    evidence={"party_id": p.id, "effective_pct": eff},
                                    missing_item=f"Government-issued photo ID for {p.name}"))
    coverage = round(sum(o.effective_pct for o in owners), 2)
    direct_sum = round(sum(float(e["pct"]) for e in tree if e["of"].lower() == entity_name.lower()), 2) if tree else \
        round(sum(o.direct_pct for o in owners), 2)
    if direct_sum > 100.0 + 0.01:
        findings.append(Finding(code="UBO_OVER_100", severity="critical", source_tool=TOOL,
                                message=f"Declared ownership of {entity_name} totals {direct_sum:.1f}% (>100%) — data integrity failure, KYC-POL-003 §2",
                                evidence={"declared_total_pct": direct_sum}))
    elif coverage < 75.0 and not case.declared.get("ownership_gap_explanation"):
        findings.append(Finding(code="UBO_COVERAGE_GAP", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                message=f"Identified natural-person ownership covers {coverage:.1f}% of {entity_name}; remaining interests must be declared or explained — KYC-POL-003 §4",
                                evidence={"coverage_pct": coverage},
                                missing_item=f"Beneficial owners (or explanation) for the remaining {100 - coverage:.0f}% of {entity_name}"))
    control = next((o.name for o in owners if o.is_control_person), None)
    if not control:
        findings.append(Finding(code="CONTROL_PERSON_MISSING", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                message=f"No control person declared for {entity_name} — exactly one is required, KYC-POL-003 §3",
                                missing_item=f"Control person (CEO/CFO/Managing Member) for {entity_name} with ID"))
    tiers = len({e["owner"] for e in tree if any(x["of"].lower() == e["owner"].lower() for x in tree)})
    if tiers >= 1 or case.declared.get("has_trust_or_nominee"):
        findings.append(Finding(code="OWNERSHIP_COMPLEX", severity="info", source_tool=TOOL,
                                message=f"Layered ownership structure ({tiers} intermediate tiers{', trust/nominee' if case.declared.get('has_trust_or_nominee') else ''}) — risk factor KYC-POL-002 §2",
                                evidence={"intermediate_tiers": tiers}))
    return UboOutput(applicable=True, threshold_pct=s.ubo_threshold_pct, owners=owners, declared_coverage_pct=coverage,
                     control_person=control, findings=findings)


register(Tool(
    name=TOOL,
    description="For legal-entity applicants, resolve beneficial owners under the 25% ownership prong (tracing through "
                "intermediate entities) and the control prong; report coverage, missing IDs, over-100% declarations and "
                "layered structures. Returns applicable=false for individuals.",
    input_model=UboInput, output_model=UboOutput, fn=resolve_beneficial_owners, business_only=True,
))
