"""extract_document / extract_all_documents — Azure Document Intelligence (or fixtures) → typed fields + findings."""
from __future__ import annotations

from datetime import date
from typing import Any

from dateutil import parser as dtparse
from pydantic import BaseModel, Field

from ...findings import Finding
from ...models import utcnow
from ...providers.extraction import ID_DOC_TYPES, get_extraction_provider
from ..untrusted import WITHHELD, ShieldUnavailable, document_text, heuristic_scan, prompt_shields
from .base import RunContext, Tool, register

TOOL = "extract_document"


class ExtractInput(BaseModel):
    document_ids: list[str] | None = Field(default=None, description="Documents to extract; omit for all documents on the case")


class ExtractedDoc(BaseModel):
    document_id: str
    doc_type: str
    party_id: str | None
    fields: dict[str, Any]
    confidence: float
    provider: str
    model: str


class ExtractOutput(BaseModel):
    documents: list[ExtractedDoc]
    findings: list[Finding]


def _parse_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return dtparse.parse(str(v)).date()
    except (ValueError, OverflowError):
        return None


def extract_documents(ctx: RunContext, args: ExtractInput) -> ExtractOutput:
    provider = get_extraction_provider()
    docs = ctx.documents()
    if args.document_ids:
        docs = [d for d in docs if d.id in args.document_ids]
    out, findings = [], []
    thr = ctx.settings.doc_confidence_threshold
    today = date.today()
    for d in docs:
        res = provider.extract(d)
        d.extracted, d.confidence, d.extracted_at = res, res["confidence"], utcnow()
        ctx.session.add(d)
        fields = res.get("fields", {})
        label = f"{d.doc_type.replace('_', ' ')} ({d.filename})"
        if res.get("error") or (not fields and res["confidence"] == 0):
            findings.append(Finding(code="DOC_UNREADABLE", severity="critical", source_tool=TOOL, fixable_by_customer=True,
                                    message=f"No data could be extracted from {label}: {res.get('error', 'empty result')}",
                                    evidence={"document_id": d.id}, missing_item=f"Clear, legible copy of {label}"))
        elif res["confidence"] < thr:
            findings.append(Finding(code="DOC_LOW_CONFIDENCE", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                    message=f"{label} extraction confidence {res['confidence']:.2f} is below the {thr:.2f} threshold (KYC-POL-005 §3)",
                                    evidence={"document_id": d.id, "confidence": res["confidence"]},
                                    missing_item=f"Clearer copy of {label}"))
        if d.doc_type in ID_DOC_TYPES:
            exp = _parse_date(fields.get("expiry"))
            if exp and exp < today:
                findings.append(Finding(code="DOC_EXPIRED", severity="warning", source_tool=TOOL, fixable_by_customer=True,
                                        message=f"{label} expired on {exp.isoformat()} (KYC-POL-005 §2)",
                                        evidence={"document_id": d.id, "expiry": exp.isoformat()},
                                        missing_item=f"Unexpired {d.doc_type.replace('_', ' ')}"))
        out.append(ExtractedDoc(document_id=d.id, doc_type=d.doc_type, party_id=d.party_id, fields=fields,
                                confidence=res["confidence"], provider=res["provider"], model=res["model"]))
    findings += _screen_for_injection(out)
    ctx.session.flush()
    return ExtractOutput(documents=out, findings=findings)


def _screen_for_injection(out: list[ExtractedDoc]) -> list[Finding]:
    """T-01: look for instructions hidden in document text before the model sees it (app/agent/untrusted.py).
    A flagged document's text is withheld from the model; the stored extraction stays intact for the analyst."""
    findings: list[Finding] = []
    reasons = {o.document_id: heuristic_scan(o.fields) for o in out}
    try:
        shields = prompt_shields([document_text(o.fields) for o in out])
    except ShieldUnavailable as e:
        shields = None
        findings.append(Finding(code="INJECTION_CHECK_UNAVAILABLE", severity="warning", source_tool=TOOL,
                                message=f"Documents could not be screened by Prompt Shields ({e}); the case cannot be "
                                        "approved automatically", evidence={"error": str(e)}))
    for o, hit in zip(out, shields or []):
        if hit:
            reasons[o.document_id].append("prompt_shields:document_attack")
    for o in out:
        if reasons[o.document_id]:
            findings.append(Finding(
                code="DOC_INJECTION_SUSPECTED", severity="critical", source_tool=TOOL,
                message=f"{o.doc_type.replace('_', ' ')} contains text that looks like instructions to the agent "
                        f"({', '.join(reasons[o.document_id])}). Its content was withheld from the model; possible "
                        "manipulation attempt (KYC-POL-005 §5)",
                evidence={"document_id": o.document_id, "detectors": reasons[o.document_id]}))
            o.fields = {k: (WITHHELD if isinstance(v, str) else v) for k, v in o.fields.items()}
    return findings


register(Tool(
    name=TOOL,
    description="Run OCR/field extraction on the case's uploaded documents (identity documents, proof of address, "
                "formation documents, ownership declarations). Returns typed fields and confidence per document. "
                "Call this first; other checks depend on extracted fields.",
    input_model=ExtractInput, output_model=ExtractOutput, fn=extract_documents,
))
