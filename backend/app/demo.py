"""Golden-case demo data: remove the fictional golden cases and run them again. Used by seed.py and by the Admin-only
"Reset demo data" action (non-production environments only).

The golden cases are fictional and carry pre-filled extraction results instead of document images, so they always
use fixture extraction. Screening, the agent loop, the run controls, risk and the decision gates run as configured.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlmodel import Session, delete, select

from .models import AuditEvent, Case, CaseRun, Document, Party, ReviewTask
from .providers.extraction import fixture_extraction
from .schemas import ApplicationIn
from .services import create_case, process_case

log = logging.getLogger("onboardiq.demo")
GOLDEN_DIR = Path(__file__).resolve().parent.parent / "eval" / "golden"


def golden_files() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("*.json"))


def remove_golden_cases(session: Session) -> int:
    refs = [r for r in (json.loads(p.read_text())["application"].get("external_ref") for p in golden_files()) if r]
    ids = list(session.exec(select(Case.id).where(Case.external_ref.in_(refs))).all())  # type: ignore[union-attr]
    if ids:
        for model in (AuditEvent, ReviewTask, CaseRun, Document, Party):  # children first (foreign keys)
            session.exec(delete(model).where(model.case_id.in_(ids)))  # type: ignore[attr-defined]
        session.exec(delete(Case).where(Case.id.in_(ids)))  # type: ignore[union-attr]
        session.flush()
    return len(ids)


def seed_golden(session_factory, mode: str | None = None, actor: str = "seed", initiated_by: dict | None = None,
                on_case=None) -> list[tuple[str, str, str | None, str | None]]:
    """Create and run every golden case, one transaction per case. Returns (case_id, file, outcome, gate)."""
    results = []
    with fixture_extraction():
        for p in golden_files():
            g = json.loads(p.read_text())
            with session_factory() as s:
                case = create_case(s, ApplicationIn.model_validate(g["application"]), actor=actor)
                run = process_case(s, case, mode, actor=actor, initiated_by=initiated_by)
                s.commit()
                row = (case.id, p.stem, getattr(run.outcome, "value", run.outcome), run.gate or run.status)
            results.append(row)
            if on_case:
                on_case(row)
    return results
