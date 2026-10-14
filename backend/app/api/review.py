from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import services
from .. import telemetry as tm
from ..auth import ROLE_ADMIN, ROLE_BSA, Principal, require_reader, require_reviewer
from ..db import get_session
from ..models import Case, Party, ReviewTask
from ..schemas import QueueItem, ReviewActionIn

router = APIRouter(prefix="/review", tags=["review"])
PRIORITY_ORDER = {"sanctions": 0, "pep": 1, "high_risk": 2, "identity": 3, "possible_match": 4, "medium_risk": 5, "other": 6}


@router.get("/queue", dependencies=[Depends(require_reader)])
def queue(status: str = "open,claimed", assigned_to: str | None = None, session: Session = Depends(get_session)) -> list[QueueItem]:
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    q = select(ReviewTask, Case).join(Case, Case.id == ReviewTask.case_id).where(ReviewTask.status.in_(statuses))  # type: ignore[attr-defined]
    if assigned_to:
        q = q.where(ReviewTask.assigned_to == assigned_to)
    items = []
    for t, c in session.exec(q).all():
        p = session.exec(select(Party).where(Party.case_id == c.id, Party.role == "applicant")).first()
        items.append(QueueItem(task_id=t.id, case_id=c.id, applicant_name=p.name if p else c.declared.get("entity_name"),
                               applicant_type=c.applicant_type.value, priority=t.priority, reason_codes=t.reason_codes,
                               status=t.status, assigned_to=t.assigned_to, outcome=c.outcome.value if c.outcome else None,
                               risk_rating=c.risk_rating, created_at=t.created_at, due_at=t.due_at))
    items.sort(key=lambda i: (PRIORITY_ORDER.get(i.priority, 9), i.created_at))
    return items


@router.post("/tasks/{task_id}/action")
def act(task_id: str, body: ReviewActionIn, p: Principal = Depends(require_reviewer),
        session: Session = Depends(get_session)) -> dict:
    task = session.get(ReviewTask, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if task.status == "done":
        raise HTTPException(409, "task already resolved")
    analyst = p.analyst_id
    if task.status == "claimed" and task.assigned_to not in (None, analyst) and body.action not in ("reassign", "release"):
        if not p.has_any({ROLE_ADMIN, ROLE_BSA}):
            raise HTTPException(409, f"task is claimed by {task.assigned_to}")
    case = session.get(Case, task.case_id)
    # Sanctions-priority approvals: a BsaOfficer acting counts as the second approver; analysts must name one.
    if task.priority == "sanctions" and body.action == "approve" and p.has_any({ROLE_BSA, ROLE_ADMIN}):
        disp = dict(body.disposition or {})
        disp.setdefault("second_approver", f"{analyst} [{'/'.join(sorted(p.roles & {ROLE_BSA, ROLE_ADMIN}))}]")
        body.disposition = disp
    try:
        services.review_action(session, task, case, body, analyst)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    tm.REVIEW_ACTIONS.labels(body.action, task.priority).inc()
    session.commit()
    session.refresh(task)
    session.refresh(case)
    return {"task": task.model_dump(), "case_status": case.status.value}
