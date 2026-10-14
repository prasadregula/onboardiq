from app.findings import Finding
from app.models import Outcome
from app.policy.decision_policy import decide

REQ = ["extract_document", "check_consistency", "screen_parties", "verify_address", "rate_risk"]
ALL = set(REQ)


def f(code, sev="warning", fixable=False, item=None):
    return Finding(code=code, severity=sev, message=code, source_tool="t", fixable_by_customer=fixable, missing_item=item)


def test_sanctions_always_escalates_even_if_model_says_approve():
    d = decide([f("SCREENING_SANCTIONS_MATCH", "critical")], "low", REQ, ALL, model_recommendation="auto_approve")
    assert d.outcome == Outcome.escalate and d.gate == "GATE_SANCTIONS" and d.review_priority == "sanctions"


def test_model_can_only_tighten():
    d = decide([], "low", REQ, ALL, model_recommendation="needs_review")
    assert d.outcome == Outcome.needs_review and d.model_tightened
    d = decide([f("RISK_MEDIUM")], "medium", REQ, ALL, model_recommendation="auto_approve")
    assert d.outcome == Outcome.needs_review and not d.model_tightened


def test_incomplete_checks_never_approve():
    d = decide([], "low", REQ, ALL - {"screen_parties"})
    assert d.outcome == Outcome.needs_review and d.gate == "GATE_CHECKS_INCOMPLETE"


def test_missing_info_collects_items():
    d = decide([f("REQUIRED_DOC_MISSING", fixable=True, item="Proof of address"), f("UBO_MISSING_ID", fixable=True, item="ID for X")],
               "low", REQ, ALL)
    assert d.outcome == Outcome.request_more and d.missing_items == ["ID for X", "Proof of address"]


def test_dob_conflict_escalates_not_request_more():
    d = decide([f("DOB_MISMATCH", "critical"), f("ADDRESS_MISMATCH", fixable=True, item="POA")], "low", REQ, ALL)
    assert d.outcome == Outcome.escalate and d.gate == "GATE_CRITICAL_DATA"


def test_unreadable_doc_is_customer_fixable():
    d = decide([f("DOC_UNREADABLE", "critical", fixable=True, item="Clear copy")], "low", REQ, ALL)
    assert d.outcome == Outcome.request_more


def test_clean_low_risk_auto_approves():
    d = decide([], "low", REQ, ALL, model_recommendation="auto_approve")
    assert d.outcome == Outcome.auto_approve and d.gate == "GATE_STP"
