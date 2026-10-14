"""Case lifecycle services used by the API and the eval harness (same code path)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlmodel import Session, select

from . import audit
from .agent.orchestrator import run_case
from .models import Case, CaseRun, CaseStatus, Document, Outcome, Party, ReviewTask, utcnow
from .schemas import ApplicationIn, ReviewActionIn

SLA_HOURS = {"sanctions": 4, "pep": 24, "high_risk": 48, "identity": 24, "possible_match": 24, "medium_risk": 72, "other": 72}
STATUS_FOR_OUTCOME = {Outcome.auto_approve: CaseStatus.approved, Outcome.request_more: CaseStatus.awaiting_customer,
                      Outcome.escalate: CaseStatus.escalated, Outcome.needs_review: CaseStatus.needs_review}


def create_case(session: Session, app_in: ApplicationIn, actor: str = "api") -> Case:
    case = Case(external_ref=app_in.external_ref, applicant_type=app_in.applicant_type, declared=app_in.declared)
    session.add(case)
    session.flush()
    key_to_id: dict[str, str] = {}
    for p in app_in.parties:
        party = Party(case_id=case.id, role=p.role, name=p.name, dob=p.dob, nationality=p.nationality,
                      country_of_residence=p.country_of_residence, ownership_pct=p.ownership_pct,
                      is_control_person=p.is_control_person, extra=p.extra)
        session.add(party)
        session.flush()
        if p.key:
            key_to_id[p.key] = party.id
    for d in app_in.documents:
        doc = Document(case_id=case.id, party_id=key_to_id.get(d.party_key) if d.party_key else None, doc_type=d.doc_type,
                       filename=d.filename, mock_extraction=d.mock_extraction)
        session.add(doc)
        session.flush()
        party = session.get(Party, doc.party_id) if doc.party_id else None
        if party and doc.doc_type in ("passport", "drivers_license", "national_id") and not party.id_document_id:
            party.id_document_id = doc.id
            session.add(party)
    audit.record(session, case.id, actor, "case_created",
                 {"applicant_type": case.applicant_type.value, "parties": len(app_in.parties), "documents": len(app_in.documents),
                  "external_ref": case.external_ref})
    session.flush()
    return case


def add_document(session: Session, case: Case, doc: Document, actor: str = "api") -> Document:
    session.add(doc)
    session.flush()
    audit.record(session, case.id, actor, "document_added", {"document_id": doc.id, "doc_type": doc.doc_type, "filename": doc.filename})
    if case.status == CaseStatus.awaiting_customer:
        case.status = CaseStatus.intake
        session.add(case)
    return doc


def process_case(session: Session, case: Case, mode: str | None = None, actor: str = "api",
                 initiated_by: dict | None = None) -> CaseRun:
    """`initiated_by`: the authenticated principal (identity.principal_ref) the run is performed for; scripts and jobs
    pass nothing and are recorded as a system principal named after `actor`."""
    from .identity import system_ref
    case.status = CaseStatus.processing
    session.add(case)
    session.flush()
    run = run_case(session, case, mode, initiated_by=initiated_by or system_ref(actor))
    if run.status != "finished":
        case.status = CaseStatus.needs_review
        _open_task(session, case, run, "other", ["RUN_FAILED"])
    else:
        outcome = Outcome(run.outcome)
        case.outcome = outcome
        case.status = STATUS_FOR_OUTCOME[outcome]
        case.risk_rating = (run.risk or {}).get("rating")
        case.risk_score = (run.risk or {}).get("score")
        decision = _decision_payload(session, run)
        case.decision_reason = decision.get("reason")
        case.missing_items = decision.get("missing_items", [])
        if outcome in (Outcome.escalate, Outcome.needs_review):
            _open_task(session, case, run, decision.get("review_priority") or "other", decision.get("triggering_codes", []))
    case.updated_at = utcnow()
    session.add(case)
    session.flush()
    return run


def _decision_payload(session: Session, run: CaseRun) -> dict[str, Any]:
    ev = session.exec(select(audit.AuditEvent).where(audit.AuditEvent.run_id == run.id,
                                                    audit.AuditEvent.event_type == "decision")).first()
    return ev.payload if ev else {}


def _open_task(session: Session, case: Case, run: CaseRun, priority: str, codes: list[str]) -> ReviewTask:
    existing = session.exec(select(ReviewTask).where(ReviewTask.case_id == case.id, ReviewTask.status != "done")).first()
    if existing:
        existing.priority, existing.reason_codes, existing.run_id = priority, codes, run.id
        session.add(existing)
        return existing
    task = ReviewTask(case_id=case.id, run_id=run.id, priority=priority, reason_codes=codes,
                      due_at=utcnow() + timedelta(hours=SLA_HOURS.get(priority, 72)))
    session.add(task)
    session.flush()
    audit.record(session, case.id, "system", "review_task_created",
                 {"task_id": task.id, "priority": priority, "reason_codes": codes, "due_at": task.due_at}, run.id)
    return task


def review_action(session: Session, task: ReviewTask, case: Case, action: ReviewActionIn, analyst: str) -> ReviewTask:
    before = {"task_status": task.status, "case_status": case.status.value, "assigned_to": task.assigned_to}
    a = action.action
    if a == "claim":
        task.status, task.assigned_to = "claimed", analyst
    elif a == "release":
        task.status, task.assigned_to = "open", None
    elif a == "reassign":
        task.assigned_to = action.assign_to
        task.status = "claimed" if action.assign_to else "open"
    else:
        if task.priority == "sanctions" and a == "approve" and not (action.disposition or {}).get("second_approver"):
            raise PermissionError("Approving a sanctions-priority task requires a second approver (disposition.second_approver)")
        task.status, task.analyst_action, task.analyst_notes = "done", a, action.notes
        task.disposition, task.resolved_at = action.disposition, utcnow()
        task.assigned_to = task.assigned_to or analyst
        case.status = {"approve": CaseStatus.approved, "reject": CaseStatus.rejected,
                       "request_more": CaseStatus.awaiting_customer, "escalate_to_bsa": CaseStatus.escalated}[a]
        if a == "request_more" and action.message_to_customer:
            case.missing_items = list(case.missing_items) + [action.message_to_customer]
        if a == "escalate_to_bsa":
            task.status, task.priority, task.assigned_to = "open", "sanctions", None
    case.updated_at = utcnow()
    session.add(task)
    session.add(case)
    audit.record(session, case.id, f"analyst:{analyst}", "review_action",
                 {"task_id": task.id, "action": a, "notes": action.notes, "disposition": action.disposition,
                  "message_to_customer": action.message_to_customer, "before": before,
                  "after": {"task_status": task.status, "case_status": case.status.value, "assigned_to": task.assigned_to}})
    session.flush()
    return task
