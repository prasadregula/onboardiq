"""screen_parties — sanctions / PEP / adverse-media screening of every party and the entity."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ...findings import Finding
from ...providers.screening import get_screening_provider
from .base import RunContext, Tool, register

TOOL = "screen_parties"


class ScreenInput(BaseModel):
    party_ids: list[str] | None = Field(default=None, description="Omit to screen every party on the case")


class ScreeningHit(BaseModel):
    party_id: str
    party_name: str
    party_role: str
    matched_name: str
    list_name: str
    hit_type: str
    score: float
    program: str | None
    entry_id: str | None
    reasons: list[str]
    classification: str  # confirmed | possible


class ScreenOutput(BaseModel):
    parties_screened: int
    provider: str
    hits: list[ScreeningHit]
    findings: list[Finding]


def screen_parties(ctx: RunContext, args: ScreenInput) -> ScreenOutput:
    provider = get_screening_provider()
    s = ctx.settings
    parties = ctx.parties()
    if args.party_ids:
        parties = [p for p in parties if p.id in args.party_ids]
    hits: list[ScreeningHit] = []
    findings: list[Finding] = []
    for p in parties:
        for h in provider.screen(p.name, p.dob, p.country_of_residence or p.nationality):
            corroborated = any(r in ("dob match", "country match") for r in h.reasons)
            cls = "confirmed" if h.score >= s.screening_match_threshold and (
                corroborated or not s.screening_require_corroboration) else "possible"
            hits.append(ScreeningHit(party_id=p.id, party_name=p.name, party_role=p.role, matched_name=h.matched_name,
                                     list_name=h.list_name, hit_type=h.hit_type, score=h.score, program=h.program,
                                     entry_id=h.entry_id, reasons=h.reasons, classification=cls))
            ev = {"party_id": p.id, "list": h.list_name, "matched": h.matched_name, "score": h.score, "entry_id": h.entry_id}
            if h.hit_type == "sanctions" and cls == "confirmed":
                findings.append(Finding(code="SCREENING_SANCTIONS_MATCH", severity="critical", source_tool=TOOL, evidence=ev,
                                        message=f"{p.role} '{p.name}' matches {h.list_name} entry '{h.matched_name}' (score {h.score:.2f}, {h.program}) — KYC-POL-004 §4"))
            elif h.hit_type == "sanctions":
                findings.append(Finding(code="SCREENING_SANCTIONS_POSSIBLE", severity="warning", source_tool=TOOL, evidence=ev,
                                        message=f"Possible {h.list_name} match for {p.role} '{p.name}' → '{h.matched_name}' (score {h.score:.2f}); analyst disposition required — KYC-POL-004 §3"))
            elif h.hit_type == "pep" and cls == "confirmed":
                findings.append(Finding(code="SCREENING_PEP_MATCH", severity="critical", source_tool=TOOL, evidence=ev,
                                        message=f"{p.role} '{p.name}' is a politically exposed person ({h.program}, score {h.score:.2f}) — EDD required, KYC-POL-004 §5"))
            elif h.hit_type == "pep":
                findings.append(Finding(code="SCREENING_PEP_POSSIBLE", severity="warning", source_tool=TOOL, evidence=ev,
                                        message=f"Possible PEP match for {p.role} '{p.name}' → '{h.matched_name}' (score {h.score:.2f})"))
            elif h.hit_type == "adverse_media":
                findings.append(Finding(code="SCREENING_ADVERSE_MEDIA", severity="warning", source_tool=TOOL, evidence=ev,
                                        message=f"Adverse media for {p.role} '{p.name}': {h.program} (score {h.score:.2f}) — KYC-POL-004 §6"))
    return ScreenOutput(parties_screened=len(parties), provider=provider.name, hits=hits, findings=findings)


register(Tool(
    name=TOOL,
    description="Screen the applicant, the legal entity, every beneficial owner, control person and signer against "
                "sanctions lists (OFAC SDN, EU, UN), the PEP database and adverse media. Returns scored hits classified "
                "as confirmed (>=0.90) or possible (0.75-0.89).",
    input_model=ScreenInput, output_model=ScreenOutput, fn=screen_parties,
))
