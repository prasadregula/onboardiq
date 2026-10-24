"""Load the golden cases into the database and run them, so the review queue has content.
    python seed.py                    # scripted mode
    python seed.py --mode llm         # against Azure OpenAI (needs ONBOARDIQ_AZURE_OPENAI_ENDPOINT)
    python seed.py --mode llm --reset # first delete earlier golden cases (G01..G13) and everything attached to them

In Azure the easier way is the Admin-only "Reset demo data" button in the review queue (non-production only).
The golden cases are fictional and carry pre-filled extraction results instead of document images, so seeding always
uses fixture extraction (app/demo.py). Screening, the agent loop, risk and the decision gates run exactly as configured.
"""
import argparse

from app.db import init_db, session_scope
from app.demo import remove_golden_cases, seed_golden

ap = argparse.ArgumentParser()
ap.add_argument("--mode", default=None)
ap.add_argument("--reset", action="store_true", help="delete earlier golden cases before seeding")
args = ap.parse_args()
init_db()

if args.reset:
    with session_scope() as s:
        n = remove_golden_cases(s)
        s.commit()
    print(f"reset: removed {n} earlier golden case(s)")

seed_golden(session_scope, args.mode,
            on_case=lambda r: print(f"{r[0]}  {r[1]:<48} -> {r[2] or '-':<14} ({r[3]})"))
