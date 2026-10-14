"""check_consistency — required-document coverage and cross-document / declared-data agreement (pure Python)."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel
from rapidfuzz import fuzz

from ...findings import Finding
from ...models import ApplicantType, Document
from ...providers.extraction import ID_DOC_TYPES
from ...providers.screening import normalize_name
from .base import RunContext, Tool, ToolError, register

TOOL = "check_consistency"
PROOF_OF_ADDRESS = {"utility_bill", "bank_statement", "lease", "tax_bill"}
ENTITY_DOCS = {"articles_of_incorporation", "partnership_agreement", "trust_instrument"}


class ConsistencyInput(BaseModel):
    pass


class Comparison(BaseModel):
    field: str
    subject: str
    a_source: str
    a_value: Any
    b_source: str
    b_value: Any
    agrees: bool
    similarity: float | None = None


class ConsistencyOutput(BaseModel):
    required_documents_present: bool
    comparisons: list[Comparison]
    findings: list[Finding]


def _norm_addr(a: str | None) -> str:
    a = (a or "").lower()
    a = re.sub(r"\b(street|st|avenue|ave|road|rd|drive|dr|suite|ste|apt|unit|boulevard|blvd|lane|ln)\b\.?", " ", a)
    return re.sub(r"[^a-z0-9]+", " ", a).strip()


def _names_agree(a: str, b: str) -> tuple[bool, float]:
    sim = fuzz.token_set_ratio(normalize_name(a), normalize_name(b)) / 100
    return sim >= 0.85, round(sim, 3)


def _norm_id(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def check_consistency(ctx: RunContext, _: ConsistencyInput) -> ConsistencyOutput:
    if "extract_document" not in ctx.tools_run:
        raise ToolError("extract_document must run before check_consistency")
    case, declared = ctx.case, ctx.case.declared
    docs, parties = ctx.documents(), ctx.parties()
    by_type: dict[str, list[Document]] = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(d)
    findings: list[Finding] = []
    comps: list[Comparison] = []

    def missing(item: str, why: str):
        findings.append(Finding(code="REQUIRED_DOC_MISSING", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                message=f"Required document missing: {item} ({why})", missing_item=item))

    # --- required documents (KYC-POL-005 §1)
    applicant = next((p for p in parties if p.role == "applicant"), None)
    if case.applicant_type == ApplicantType.individual:
        if not any(t in by_type for t in ID_DOC_TYPES):
            missing("Government-issued photo ID", "KYC-POL-005 §1")
        if not any(t in by_type for t in PROOF_OF_ADDRESS):
            missing("Proof of address (utility bill or bank statement dated within 90 days)", "KYC-POL-005 §1")
    else:
        if not any(t in by_type for t in ENTITY_DOCS):
            missing("Formation document (articles of incorporation/organization)", "KYC-POL-005 §1")
        if "ein_letter" not in by_type:
            missing("EIN confirmation letter", "KYC-POL-005 §1")
        if "ownership_declaration" not in by_type:
            missing("Beneficial ownership declaration", "KYC-POL-005 §1")
    required_ok = not findings

    # --- per-party ID document vs declared party data
    for p in parties:
        if p.role == "entity":
            continue
        iddoc = next((d for d in docs if d.party_id == p.id and d.doc_type in ID_DOC_TYPES and d.extracted), None)
        if not iddoc:
            continue
        f = iddoc.extracted.get("fields", {})
        doc_name = f.get("full_name") or " ".join(x for x in [f.get("first_name"), f.get("last_name")] if x)
        if doc_name:
            ok, sim = _names_agree(p.name, doc_name)
            comps.append(Comparison(field="name", subject=p.name, a_source="declared", a_value=p.name,
                                    b_source=iddoc.doc_type, b_value=doc_name, agrees=ok, similarity=sim))
            if not ok:
                findings.append(Finding(code="NAME_MISMATCH", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                        message=f"Declared name '{p.name}' does not match ID document name '{doc_name}' (similarity {sim:.2f})",
                                        evidence={"party_id": p.id, "document_id": iddoc.id, "similarity": sim},
                                        missing_item=f"Explanation or supporting document for name difference for {p.name}"))
        if p.dob and f.get("dob"):
            ok = str(f["dob"])[:10] == p.dob[:10]
            comps.append(Comparison(field="dob", subject=p.name, a_source="declared", a_value=p.dob,
                                    b_source=iddoc.doc_type, b_value=f["dob"], agrees=ok))
            if not ok:
                findings.append(Finding(code="DOB_MISMATCH", severity="critical", source_tool=TOOL,
                                        message=f"Date of birth on {iddoc.doc_type} ({f['dob']}) conflicts with declared DOB ({p.dob}) for {p.name} — possible identity fraud (KYC-POL-005 §5)",
                                        evidence={"party_id": p.id, "document_id": iddoc.id}))
        declared_id = (p.extra or {}).get("id_number") or (declared.get("id_number") if p.role == "applicant" else None)
        if declared_id and f.get("id_number"):
            ok = _norm_id(declared_id) == _norm_id(f["id_number"])
            comps.append(Comparison(field="id_number", subject=p.name, a_source="declared", a_value="***" + str(declared_id)[-4:],
                                    b_source=iddoc.doc_type, b_value="***" + str(f["id_number"])[-4:], agrees=ok))
            if not ok:
                findings.append(Finding(code="ID_NUMBER_MISMATCH", severity="critical", source_tool=TOOL,
                                        message=f"ID number on {iddoc.doc_type} does not match the declared ID number for {p.name} (KYC-POL-005 §5)",
                                        evidence={"party_id": p.id, "document_id": iddoc.id}))

    # --- declared address vs proof-of-address vs ID address (applicant only)
    decl_addr = declared.get("address")
    poa = next((d for d in docs if d.doc_type in PROOF_OF_ADDRESS and d.extracted), None)
    if decl_addr and poa:
        pa = poa.extracted.get("fields", {}).get("address")
        if pa:
            sim = fuzz.token_set_ratio(_norm_addr(decl_addr), _norm_addr(pa)) / 100
            ok = sim >= 0.80
            comps.append(Comparison(field="address", subject=applicant.name if applicant else case.id, a_source="declared",
                                    a_value=decl_addr, b_source=poa.doc_type, b_value=pa, agrees=ok, similarity=round(sim, 3)))
            if not ok:
                findings.append(Finding(code="ADDRESS_MISMATCH", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                        message=f"Declared address does not match the address on the {poa.doc_type.replace('_', ' ')} (similarity {sim:.2f}) (KYC-POL-005 §5)",
                                        evidence={"document_id": poa.id, "similarity": round(sim, 3)},
                                        missing_item="Proof of address showing the declared residential address"))
        poa_name = poa.extracted.get("fields", {}).get("full_name")
        if poa_name and applicant:
            ok, sim = _names_agree(applicant.name, poa_name)
            comps.append(Comparison(field="name", subject=applicant.name, a_source="declared", a_value=applicant.name,
                                    b_source=poa.doc_type, b_value=poa_name, agrees=ok, similarity=sim))
            if not ok:
                findings.append(Finding(code="NAME_MISMATCH", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                        message=f"Proof of address is in the name '{poa_name}', not the applicant '{applicant.name}'",
                                        evidence={"document_id": poa.id, "similarity": sim},
                                        missing_item="Proof of address in the applicant's own name"))

    # --- entity name: declared vs formation doc
    if case.applicant_type == ApplicantType.business:
        ent = next((d for d in docs if d.doc_type in ENTITY_DOCS and d.extracted), None)
        ename = ent.extracted.get("fields", {}).get("entity_name") if ent else None
        if ename and declared.get("entity_name"):
            ok, sim = _names_agree(declared["entity_name"], ename)
            comps.append(Comparison(field="entity_name", subject=declared["entity_name"], a_source="declared",
                                    a_value=declared["entity_name"], b_source=ent.doc_type, b_value=ename, agrees=ok, similarity=sim))
            if not ok:
                findings.append(Finding(code="NAME_MISMATCH", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                        message=f"Declared entity name '{declared['entity_name']}' differs from formation document '{ename}'",
                                        evidence={"document_id": ent.id, "similarity": sim},
                                        missing_item="Formation document matching the declared legal entity name"))
    return ConsistencyOutput(required_documents_present=required_ok, comparisons=comps, findings=findings)


register(Tool(
    name=TOOL,
    description="Verify that all required documents are present for the applicant type and that names, dates of birth, "
                "ID numbers and addresses agree across the declared application, identity documents, proof of address and "
                "formation documents. Requires extract_document first.",
    input_model=ConsistencyInput, output_model=ConsistencyOutput, fn=check_consistency,
))
