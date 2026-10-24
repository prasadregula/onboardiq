"""Admin controls (w4-3): the agent kill switch and the demo data reset.

GET  /admin/agents          any signed-in reader - so every analyst's screen can show the "agents paused" banner
POST /admin/agents/pause    Admin only - stops new runs at once and running ones at their next step
POST /admin/agents/resume   Admin only
POST /admin/demo/reset      Admin only, non-production only - reload and re-run the fictional golden cases
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlmodel import Session

from .. import control
from ..auth import Principal, require_admin, require_reader
from ..config import DEV_ENVS, get_settings
from ..db import get_session, session_scope
from ..identity import principal_ref

router = APIRouter(prefix="/admin", tags=["admin"])
log = logging.getLogger("onboardiq.admin")


class PauseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Why the agents are being stopped (recorded)")


class ResumeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def _view(session: Session) -> dict:
    s = get_settings()
    st = control.get_state(session)
    return {"paused": st.paused, "reason": st.reason, "changed_by": st.changed_by, "changed_at": st.changed_at,
            "budget": control.RunBudget.from_settings().limits(),
            "demo_reset_enabled": s.env in DEV_ENVS,
            "history": [{"ts": e.ts, "actor": e.actor, "action": e.action, "reason": e.reason}
                        for e in control.history(session)]}


@router.get("/agents", dependencies=[Depends(require_reader)])
def agents(session: Session = Depends(get_session)) -> dict:
    return _view(session)


@router.post("/agents/pause")
def pause(body: PauseIn, p: Principal = Depends(require_admin), session: Session = Depends(get_session)) -> dict:
    control.set_paused(session, True, p.subject, body.reason)
    session.commit()
    log.warning("agents PAUSED by %s: %s", p.subject, body.reason)
    return _view(session)


@router.post("/agents/resume")
def resume(body: ResumeIn | None = None, p: Principal = Depends(require_admin),
           session: Session = Depends(get_session)) -> dict:
    control.set_paused(session, False, p.subject, body.reason if body else None)
    session.commit()
    log.warning("agents resumed by %s", p.subject)
    return _view(session)


def _demo_reset(initiated_by: dict) -> None:
    from ..demo import remove_golden_cases, seed_golden
    try:
        with session_scope() as s:
            n = remove_golden_cases(s)
            s.commit()
        rows = seed_golden(session_scope, None, actor="demo-reset", initiated_by=initiated_by)
        log.info("demo reset: removed %d, seeded %d golden cases", n, len(rows))
    except Exception:  # noqa: BLE001 - background task: log, never crash the worker
        log.exception("demo reset failed")


@router.post("/demo/reset")
def demo_reset(background: BackgroundTasks, p: Principal = Depends(require_admin),
               session: Session = Depends(get_session)) -> Response:
    if get_settings().env not in DEV_ENVS:
        raise HTTPException(403, "demo reset is disabled in this environment")
    try:
        control.ensure_not_paused(session)
    except control.AgentsPaused as e:
        raise HTTPException(423, str(e)) from None
    control.record_event(session, p.subject, "demo_reset", "reload and re-run the golden cases")
    session.commit()
    background.add_task(_demo_reset, principal_ref(p))
    return Response(status_code=202, media_type="application/json",
                    content='{"accepted": true, "note": "golden cases are being reloaded; refresh the queue in about 2 minutes"}')
