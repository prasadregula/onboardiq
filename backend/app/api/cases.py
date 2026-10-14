from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from sqlmodel import Session, select

from .. import audit, services
from ..auth import Principal, current_analyst, require_case_writer, require_reader
from ..config import get_settings
from ..db import get_session, session_scope
from ..identity import principal_ref
from ..models import AuditEvent, Case, CaseRun, CaseStatus, Document, Party, ReviewTask
from ..providers.storage import get_storage
from ..schemas import ApplicationIn, CaseSummary, RunRequest

log = logging.getLogger("onboardiq.api")
router = APIRouter(prefix="/cases", tags=["cases"])
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/tiff", "image/heic", "application/octet-stream"}


def _applicant_name(session: Session, case: Case) -> str | None:
    p = session.exec(select(Party).where(Party.case_id == case.id, Party.role == "applicant")).first()
    return p.name if p else case.declared.get("entity_name")


def _summary(session: Session, c: Case) -> CaseSummary:
    return CaseSummary(id=c.id, external_ref=c.external_ref, applicant_type=c.applicant_type.value,
                       applicant_name=_applicant_name(session, c), status=c.status.value,
                       outcome=c.outcome.value if c.outcome else None, risk_rating=c.risk_rating, risk_score=c.risk_score,
                       created_at=c.created_at, updated_at=c.updated_at)


def _get_case(session: Session, case_id: str) -> Case:
    case = session.get(Case, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return case


@router.post("", status_code=201)
def create_case(app_in: ApplicationIn, p: Principal = Depends(require_case_writer), session: Session = Depends(get_session)) -> CaseSummary:
    case = services.create_case(session, app_in, actor=p.subject)
    session.commit()
    session.refresh(case)
    return _summary(session, case)


@router.get("", dependencies=[Depends(require_reader)])
def list_cases(status: str | None = None, limit: int = 100, session: Session = Depends(get_session)) -> list[CaseSummary]:
    q = select(Case).order_by(Case.created_at.desc()).limit(min(limit, 500))  # type: ignore[attr-defined]
    if status:
        q = q.where(Case.status == status)
    return [_summary(session, c) for c in session.exec(q).all()]


@router.get("/{case_id}", dependencies=[Depends(require_reader)])
def get_case(case_id: str, session: Session = Depends(get_session)) -> dict:
    case = _get_case(session, case_id)
    parties = session.exec(select(Party).where(Party.case_id == case_id)).all()
    docs = session.exec(select(Document).where(Document.case_id == case_id)).all()
    runs = session.exec(select(CaseRun).where(CaseRun.case_id == case_id).order_by(CaseRun.started_at.desc())).all()  # type: ignore[attr-defined]
    task = session.exec(select(ReviewTask).where(ReviewTask.case_id == case_id).order_by(ReviewTask.created_at.desc())).first()  # type: ignore[attr-defined]
    return {"case": _summary(session, case).model_dump() | {"declared": case.declared, "decision_reason": case.decision_reason,
                                                          "missing_items": case.missing_items},
            "parties": [p.model_dump() for p in parties],
            "documents": [{**d.model_dump(exclude={"mock_extraction"}), "has_fixture": d.mock_extraction is not None} for d in docs],
            "latest_run": runs[0].model_dump() if runs else None,
            "runs": [{"id": r.id, "mode": r.mode, "status": r.status, "outcome": r.outcome, "gate": r.gate,
                      "started_at": r.started_at, "finished_at": r.finished_at, "tool_calls": r.tool_calls} for r in runs],
            "review_task": task.model_dump() if task else None}


@router.post("/{case_id}/documents", status_code=201)
async def upload_document(case_id: str, doc_type: str = Form(...), party_id: str | None = Form(default=None),
                          file: UploadFile = File(...), p: Principal = Depends(require_case_writer),
                          session: Session = Depends(get_session)) -> dict:
    case = _get_case(session, case_id)
    if case.status == CaseStatus.processing:
        raise HTTPException(409, "case is being processed; upload after the run finishes")
    if file.content_type and file.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, f"unsupported content type {file.content_type}")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file exceeds 25 MB")
    doc = Document(case_id=case_id, party_id=party_id, doc_type=doc_type, filename=Path(file.filename or "upload").name,
                   content_type=file.content_type)
    doc.blob_uri = get_storage().put(f"{case_id}/{doc.id}_{doc.filename}", data, file.content_type)
    services.add_document(session, case, doc, actor=p.subject)
    session.commit()
    session.refresh(doc)
    return doc.model_dump(exclude={"mock_extraction"})


@router.get("/{case_id}/documents/{doc_id}/content", dependencies=[Depends(require_reader)])
def document_content(case_id: str, doc_id: str, session: Session = Depends(get_session)):
    doc = session.get(Document, doc_id)
    if not doc or doc.case_id != case_id or not doc.blob_uri:
        raise HTTPException(404, "document not found")
    storage = get_storage()
    if not storage.exists(doc.blob_uri):
        raise HTTPException(404, "document content not available")
    return Response(storage.get(doc.blob_uri), media_type=doc.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})


def _run_in_background(case_id: str, mode: str | None, actor: str, initiated_by: dict | None = None) -> None:
    # The analyst's identity is passed explicitly (not via request context) so it survives the hop to the background
    # task and lands in the run's audit trail.
    with session_scope() as session:
        case = session.get(Case, case_id)
        try:
            services.process_case(session, case, mode, actor=actor, initiated_by=initiated_by)
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("background run crashed", extra={"case_id": case_id})
            case = session.get(Case, case_id)
            case.status = CaseStatus.needs_review
            session.add(case)
            session.commit()


@router.post("/{case_id}/run")
def run(case_id: str, background: BackgroundTasks, body: RunRequest | None = None,
        p: Principal = Depends(require_case_writer), session: Session = Depends(get_session)) -> dict:
    """Start an agent run. Async by default (202 + poll GET /cases/{id}/runs/{run_id}); pass ?sync=true or
    ONBOARDIQ_RUN_ASYNC=false for the synchronous behaviour used by tests and the eval harness."""
    s = get_settings()
    case = _get_case(session, case_id)
    if case.status == CaseStatus.processing:
        raise HTTPException(409, "a run is already in progress for this case")
    sync = (body.sync if body and body.sync is not None else None)
    if sync is None:
        sync = not s.run_async
    if sync:
        run = services.process_case(session, case, body.mode if body else None, actor=p.subject,
                                    initiated_by=principal_ref(p))
        session.commit()
        session.refresh(case)
        session.refresh(run)
        return {"run": run.model_dump(), "case": _summary(session, case).model_dump() | {"decision_reason": case.decision_reason,
                                                                                        "missing_items": case.missing_items}}
    # claim the case atomically before returning 202
    case.status = CaseStatus.processing
    session.add(case)
    audit.record(session, case_id, p.subject, "run_requested", {"mode": (body.mode if body else None) or s.orchestrator_mode,
                                                                 "requested_by": principal_ref(p)})
    session.commit()
    background.add_task(_run_in_background, case_id, body.mode if body else None, p.subject, principal_ref(p))
    return Response(status_code=202, content='{"accepted": true, "case_id": "%s", "status": "processing"}' % case_id,
                    media_type="application/json")


@router.get("/{case_id}/runs", dependencies=[Depends(require_reader)])
def list_runs(case_id: str, session: Session = Depends(get_session)) -> list[dict]:
    runs = session.exec(select(CaseRun).where(CaseRun.case_id == case_id).order_by(CaseRun.started_at.desc())).all()  # type: ignore[attr-defined]
    return [r.model_dump() for r in runs]


@router.get("/{case_id}/runs/{run_id}", dependencies=[Depends(require_reader)])
def get_run(case_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    run = session.get(CaseRun, run_id)
    if not run or run.case_id != case_id:
        raise HTTPException(404, "run not found")
    return run.model_dump()


@router.get("/{case_id}/audit", dependencies=[Depends(require_reader)])
def get_audit(case_id: str, session: Session = Depends(get_session)) -> list[dict]:
    evs = session.exec(select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq)).all()  # type: ignore[arg-type]
    return [e.model_dump() for e in evs]


@router.get("/{case_id}/audit/verify", dependencies=[Depends(require_reader)])
def verify_audit(case_id: str, session: Session = Depends(get_session)) -> dict:
    return audit.verify_chain(session, case_id)


@router.post("/{case_id}/note")
def add_note(case_id: str, note: str = Form(...), analyst: str = Depends(current_analyst),
             session: Session = Depends(get_session)) -> dict:
    _get_case(session, case_id)
    ev = audit.record(session, case_id, f"analyst:{analyst}", "analyst_note", {"note": note})
    session.commit()
    return {"seq": ev.seq}
