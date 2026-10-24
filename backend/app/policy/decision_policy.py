"""Deterministic decision gates (ARCHITECTURE.md §5.4). The model recommends; policy decides.

Gates are evaluated in order; the first match wins. The model's recommendation may only TIGHTEN the outcome.
Order: checks incomplete > sanctions > PEP > critical data conflict > high risk > unreadable document >
customer-fixable gaps > possible match > medium risk > straight-through.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..findings import CRITICAL_DATA_CODES, CUSTOMER_FIXABLE_CODES, Finding
from ..models import Outcome

STRICTNESS = {Outcome.auto_approve: 0, Outcome.needs_review: 1, Outcome.request_more: 2, Outcome.escalate: 3}
PRIORITY_FOR_GATE = {"GATE_SANCTIONS": "sanctions", "GATE_PEP_EDD": "pep", "GATE_HIGH_RISK": "high_risk",
                     "GATE_POSSIBLE_MATCH": "possible_match", "GATE_MEDIUM_RISK": "medium_risk",
                     "GATE_CRITICAL_DATA_WITH_SCREENING": "possible_match", "GATE_CRITICAL_DATA": "identity", "GATE_DOCUMENT_INJECTION": "identity",
                     "GATE_INJECTION_CHECK_UNAVAILABLE": "other", "GATE_AGENT_BUDGET": "other",
                     "MODEL_TIGHTENED": "other", "GATE_CHECKS_INCOMPLETE": "other"}


@dataclass
class Decision:
    outcome: Outcome
    gate: str
    reason: str
    missing_items: list[str] = field(default_factory=list)
    triggering_codes: list[str] = field(default_factory=list)
    model_recommendation: str | None = None
    model_tightened: bool = False
    review_priority: str | None = None


def decide(findings: list[Finding], risk_rating: str | None, required_tools: list[str], tools_run: set[str],
           model_recommendation: str | None = None) -> Decision:
    codes = {f.code for f in findings}
    missing = sorted({f.missing_item for f in findings if f.missing_item})

    def sel(*cs: str) -> list[str]:
        return sorted(codes & set(cs))

    d: Decision
    incomplete = [t for t in required_tools if t not in tools_run]
    if incomplete:
        d = Decision(Outcome.needs_review, "GATE_CHECKS_INCOMPLETE",
                     f"Required checks did not complete: {', '.join(incomplete)}; a human must review.", missing, [])
    elif "SCREENING_SANCTIONS_MATCH" in codes:
        d = Decision(Outcome.escalate, "GATE_SANCTIONS",
                     "Confirmed sanctions-list match. No approval or customer contact permitted; escalated to BSA (KYC-POL-004 §4).",
                     [], sel("SCREENING_SANCTIONS_MATCH"))
    elif "SCREENING_PEP_MATCH" in codes:
        d = Decision(Outcome.escalate, "GATE_PEP_EDD",
                     "Politically exposed person identified; Enhanced Due Diligence and senior approval required (KYC-POL-004 §5, KYC-POL-002 §4).",
                     missing, sel("SCREENING_PEP_MATCH"))
    elif "DOC_INJECTION_SUSPECTED" in codes:
        d = Decision(Outcome.escalate, "GATE_DOCUMENT_INJECTION",
                     "A document contains text that looks like instructions to the agent: a possible manipulation "
                     "attempt. Its content was withheld from the model; analyst escalation (KYC-POL-005 §5).",
                     [], sel("DOC_INJECTION_SUSPECTED"))
    elif (crit := sorted(codes & CRITICAL_DATA_CODES)) and crit != ["DOC_UNREADABLE"]:
        # Data-integrity conflicts outrank the risk-rating gate: the root cause (possible fraud or a data error) drives
        # routing, and a rating computed from conflicting data is not trustworthy yet. Both outcomes escalate, so this
        # ordering never loosens a decision. The High rating, if any, is carried in the reason and triggering codes.
        also_high = " Customer risk rating is also High; EDD applies once the data is resolved." if risk_rating == "high" else ""
        risk_codes = sel("RISK_HIGH") if risk_rating == "high" else []
        if "SCREENING_SANCTIONS_POSSIBLE" in codes or "SCREENING_ADVERSE_MEDIA" in codes:
            d = Decision(Outcome.escalate, "GATE_CRITICAL_DATA_WITH_SCREENING",
                         f"Critical data conflict ({', '.join(crit)}) together with an open screening alert — analyst escalation (KYC-POL-001 §5).{also_high}",
                         missing, crit + sel("SCREENING_SANCTIONS_POSSIBLE", "SCREENING_ADVERSE_MEDIA") + risk_codes)
        else:
            d = Decision(Outcome.escalate, "GATE_CRITICAL_DATA",
                         f"Government-document conflict ({', '.join(crit)}) indicates possible identity fraud — analyst escalation rather than customer request (KYC-POL-005 §5).{also_high}",
                         missing, crit + risk_codes)
    elif risk_rating == "high":
        d = Decision(Outcome.escalate, "GATE_HIGH_RISK",
                     "Customer risk rating is High; EDD and analyst approval required (KYC-POL-002 §4).", missing, sel("RISK_HIGH"))
    elif "DOC_UNREADABLE" in codes:
        d = Decision(Outcome.request_more, "GATE_MISSING_INFO",
                     "A document could not be read; a clearer copy is required (KYC-POL-005 §3).", missing, ["DOC_UNREADABLE"])
    elif codes & CUSTOMER_FIXABLE_CODES:
        d = Decision(Outcome.request_more, "GATE_MISSING_INFO",
                     f"Verification gaps the customer can resolve: {', '.join(sorted(codes & CUSTOMER_FIXABLE_CODES))} (KYC-POL-001 §5).",
                     missing, sorted(codes & CUSTOMER_FIXABLE_CODES))
    elif "INJECTION_CHECK_UNAVAILABLE" in codes:  # fail closed: unscreened documents never auto-approve
        d = Decision(Outcome.needs_review, "GATE_INJECTION_CHECK_UNAVAILABLE",
                     "Documents could not be screened for injected instructions; analyst review required.", [],
                     sel("INJECTION_CHECK_UNAVAILABLE"))
    elif codes & {"SCREENING_SANCTIONS_POSSIBLE", "SCREENING_PEP_POSSIBLE", "SCREENING_ADVERSE_MEDIA"}:
        d = Decision(Outcome.needs_review, "GATE_POSSIBLE_MATCH",
                     "Possible screening match must be dispositioned by a KYC analyst before opening (KYC-POL-004 §3/§6).",
                     [], sel("SCREENING_SANCTIONS_POSSIBLE", "SCREENING_PEP_POSSIBLE", "SCREENING_ADVERSE_MEDIA"))
    elif risk_rating == "medium":
        d = Decision(Outcome.needs_review, "GATE_MEDIUM_RISK",
                     "Medium risk rating requires analyst confirmation (KYC-POL-002 §5).", [], sel("RISK_MEDIUM"))
    elif risk_rating == "low":
        d = Decision(Outcome.auto_approve, "GATE_STP",
                     "All required checks passed, no open alerts, low risk — straight-through approval (KYC-POL-001 §6, KYC-POL-002 §6).")
    else:
        d = Decision(Outcome.needs_review, "GATE_NO_RATING", "No risk rating available; analyst review required.")

    d.model_recommendation = model_recommendation
    if model_recommendation:
        try:
            rec = Outcome(model_recommendation)
            if STRICTNESS[rec] > STRICTNESS[d.outcome]:
                d.outcome, d.model_tightened = rec, True
                d.gate = f"{d.gate}+MODEL_TIGHTENED"
                d.reason += f" Model recommended a stricter outcome ({rec.value}); applied."
        except ValueError:
            pass
    if d.outcome in (Outcome.escalate, Outcome.needs_review):
        d.review_priority = PRIORITY_FOR_GATE.get(d.gate.split("+")[0], "other")
    return d
