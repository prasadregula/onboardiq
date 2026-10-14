"""Load the golden cases into the database and run them, so the review queue has content.
    python seed.py                    # scripted mode
    python seed.py --mode llm         # against Azure OpenAI (needs ONBOARDIQ_AZURE_OPENAI_ENDPOINT)
    python seed.py --mode llm --reset # first delete earlier golden cases (G01..G12) and everything attached to them

The golden cases are fictional and carry pre-filled extraction results instead of real document images, so seeding
always uses the mock extractor (in Azure the real Document Intelligence extractor would find no blob and flag every
document DOC_UNREADABLE). Screening, the agent loop, risk and the decision gates run exactly as configured.
"""
import os

os.environ["ONBOARDIQ_EXTRACTION_PROVIDER"] = "mock"  # must be set before the app reads its settings

import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from sqlmodel import delete, select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.models import AuditEvent, Case, CaseRun, Document, Party, ReviewTask  # noqa: E402
from app.schemas import ApplicationIn  # noqa: E402
from app.services import create_case, process_case  # noqa: E402

GOLDEN = sorted((Path(__file__).parent / "eval" / "golden").glob("*.json"))

ap = argparse.ArgumentParser()
ap.add_argument("--mode", default=None)
ap.add_argument("--reset", action="store_true", help="delete earlier golden cases before seeding")
args = ap.parse_args()
init_db()

if args.reset:
    refs = [json.loads(p.read_text())["application"].get("external_ref") for p in GOLDEN]
    with session_scope() as s:
        ids = s.exec(select(Case.id).where(Case.external_ref.in_([r for r in refs if r]))).all()
        if ids:
            for model in (AuditEvent, ReviewTask, CaseRun, Document, Party):  # children first (foreign keys)
                s.exec(delete(model).where(model.case_id.in_(ids)))
            s.exec(delete(Case).where(Case.id.in_(ids)))
            s.commit()
        print(f"reset: removed {len(ids)} earlier golden case(s)")

for p in GOLDEN:
    g = json.loads(p.read_text())
    with session_scope() as s:
        case = create_case(s, ApplicationIn.model_validate(g["application"]), actor="seed")
        run = process_case(s, case, args.mode, actor="seed")
        s.commit()
        print(f"{case.id}  {p.stem:<48} -> {getattr(run.outcome, 'value', run.outcome):<14} ({run.gate})")
