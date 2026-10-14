"""Generates eval/golden/*.json. Each golden case = an ApplicationIn payload + expectations.
Re-run after changing the fixtures:  python -m eval.make_golden
All names are fictional."""
from __future__ import annotations

import json
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"


def id_doc(name, dob, num, expiry="2031-05-01", conf=0.97, dtype="drivers_license", address=None, country="US"):
    f = {"full_name": name, "dob": dob, "id_number": num, "expiry": expiry, "issuing_country": country}
    if address:
        f["address"] = address
    return {"doc_type": dtype, "filename": f"{dtype}_{name.split()[0].lower()}.jpg",
            "mock_extraction": {"fields": f, "confidence": conf}}


def poa(name, address, conf=0.93, dtype="utility_bill"):
    return {"doc_type": dtype, "filename": f"{dtype}_{name.split()[0].lower()}.pdf",
            "mock_extraction": {"fields": {"full_name": name, "address": address, "statement_date": "2026-08-20"}, "confidence": conf}}


def formation(entity, state="CA", conf=0.95):
    return {"doc_type": "articles_of_incorporation", "filename": "articles.pdf",
            "mock_extraction": {"fields": {"entity_name": entity, "state": state, "formed": "2019-04-02"}, "confidence": conf}}


def ein(entity):
    return {"doc_type": "ein_letter", "filename": "ein.pdf",
            "mock_extraction": {"fields": {"entity_name": entity, "ein": "**-***1234"}, "confidence": 0.96}}


def owners_decl(entity, owners):
    return {"doc_type": "ownership_declaration", "filename": "ubo.pdf",
            "mock_extraction": {"fields": {"entity_name": entity, "owners": owners}, "confidence": 0.94}}


ADDR_LA = "1420 Wilshire Blvd Apt 7B, Los Angeles, CA 90017"
ADDR_SF = "88 Mission St Unit 12, San Francisco, CA 94105"
ADDR_PAS = "2200 Colorado Blvd, Pasadena, CA 91107"

CASES = [
    {
        "id": "G01_individual_clean_auto_approve",
        "description": "US resident, consistent docs, low risk → straight-through approval",
        "must_pass": True,
        "application": {
            "external_ref": "G01", "applicant_type": "individual",
            "declared": {"address": ADDR_LA, "country": "US", "product": "checking", "purpose": "payroll and household",
                         "expected_monthly_volume_usd": 6000, "id_number": "D1234567"},
            "parties": [{"key": "a", "role": "applicant", "name": "Maria Elena Castillo", "dob": "1988-02-11", "country_of_residence": "US"}],
            "documents": [id_doc("Maria Elena Castillo", "1988-02-11", "D1234567", address=ADDR_LA) | {"party_key": "a"},
                          poa("Maria E. Castillo", ADDR_LA)],
        },
        "expect": {"outcome": "auto_approve", "risk_rating": "low", "must_have": [], "must_not_have": ["SCREENING_SANCTIONS_MATCH", "REQUIRED_DOC_MISSING"]},
    },
    {
        "id": "G02_individual_address_mismatch_request_more",
        "description": "Proof of address shows a different address than declared → request_more",
        "must_pass": True,
        "application": {
            "external_ref": "G02", "applicant_type": "individual",
            "declared": {"address": ADDR_LA, "country": "US", "product": "savings", "expected_monthly_volume_usd": 2000, "id_number": "D2233445"},
            "parties": [{"key": "a", "role": "applicant", "name": "Daniel Okafor", "dob": "1979-09-30", "country_of_residence": "US"}],
            "documents": [id_doc("Daniel Okafor", "1979-09-30", "D2233445") | {"party_key": "a"}, poa("Daniel Okafor", ADDR_SF)],
        },
        "expect": {"outcome": "request_more", "risk_rating": "low", "must_have": ["ADDRESS_MISMATCH"], "must_not_have": ["DOB_MISMATCH"],
                   "missing_items_contains": ["Proof of address"]},
    },
    {
        "id": "G03_individual_low_confidence_request_more",
        "description": "Blurry ID scan below the 0.80 confidence threshold → request clearer copy",
        "must_pass": True,
        "application": {
            "external_ref": "G03", "applicant_type": "individual",
            "declared": {"address": ADDR_PAS, "country": "US", "product": "checking", "expected_monthly_volume_usd": 3000, "id_number": "P99887766"},
            "parties": [{"key": "a", "role": "applicant", "name": "Hannah Weiss", "dob": "1995-06-14", "country_of_residence": "US"}],
            "documents": [id_doc("Hannah Weiss", "1995-06-14", "P99887766", conf=0.61, dtype="passport") | {"party_key": "a"}, poa("Hannah Weiss", ADDR_PAS)],
        },
        "expect": {"outcome": "request_more", "risk_rating": "low", "must_have": ["DOC_LOW_CONFIDENCE"], "must_not_have": []},
    },
    {
        "id": "G04_individual_sanctions_match_escalate",
        "description": "Applicant name + DOB match a demo SDN entry → escalate, GATE_SANCTIONS",
        "must_pass": True,
        "application": {
            "external_ref": "G04", "applicant_type": "individual",
            "declared": {"address": ADDR_LA, "country": "US", "product": "checking", "expected_monthly_volume_usd": 4000, "id_number": "X5551212"},
            "parties": [{"key": "a", "role": "applicant", "name": "Viktor A. Sokolov-Marin", "dob": "1968-03-14", "nationality": "RU", "country_of_residence": "US"}],
            "documents": [id_doc("Viktor A. Sokolov-Marin", "1968-03-14", "X5551212", dtype="passport", country="RU") | {"party_key": "a"}, poa("Viktor Sokolov-Marin", ADDR_LA)],
        },
        "expect": {"outcome": "escalate", "gate": "GATE_SANCTIONS", "must_have": ["SCREENING_SANCTIONS_MATCH"], "must_not_have": [], "review_priority": "sanctions"},
    },
    {
        "id": "G05_individual_pep_escalate",
        "description": "Applicant is a PEP → escalate for EDD, rated High regardless of score",
        "must_pass": True,
        "application": {
            "external_ref": "G05", "applicant_type": "individual",
            "declared": {"address": ADDR_SF, "country": "US", "product": "private_banking", "private_banking": True,
                         "expected_monthly_volume_usd": 120000, "id_number": "N77001"},
            "parties": [{"key": "a", "role": "applicant", "name": "Delphine Okonkwo-Baptiste", "dob": "1971-05-30", "nationality": "NG", "country_of_residence": "US"}],
            "documents": [id_doc("Delphine Okonkwo-Baptiste", "1971-05-30", "N77001", dtype="passport", country="NG") | {"party_key": "a"}, poa("Delphine Okonkwo-Baptiste", ADDR_SF)],
        },
        "expect": {"outcome": "escalate", "gate": "GATE_PEP_EDD", "risk_rating": "high", "must_have": ["SCREENING_PEP_MATCH"], "must_not_have": [], "review_priority": "pep"},
    },
    {
        "id": "G06_individual_possible_match_needs_review",
        "description": "Name is close to an SDN entry but DOB differs → possible match, analyst dispositions",
        "must_pass": True,
        "application": {
            "external_ref": "G06", "applicant_type": "individual",
            "declared": {"address": ADDR_PAS, "country": "US", "product": "checking", "expected_monthly_volume_usd": 3500, "id_number": "D6677889"},
            "parties": [{"key": "a", "role": "applicant", "name": "Carlos Enrique Valdez", "dob": "1984-01-05", "country_of_residence": "US"}],
            "documents": [id_doc("Carlos Enrique Valdez", "1984-01-05", "D6677889", address=ADDR_PAS) | {"party_key": "a"}, poa("Carlos E. Valdez", ADDR_PAS)],
        },
        "expect": {"outcome": "needs_review", "must_have": ["SCREENING_SANCTIONS_POSSIBLE"], "must_not_have": ["SCREENING_SANCTIONS_MATCH"], "review_priority": "possible_match"},
    },
    {
        "id": "G07_individual_dob_conflict_escalate",
        "description": "DOB on the government ID conflicts with declared DOB → possible identity fraud, escalate (not a customer request)",
        "must_pass": True,
        "application": {
            "external_ref": "G07", "applicant_type": "individual",
            "declared": {"address": ADDR_LA, "country": "US", "product": "checking", "expected_monthly_volume_usd": 2500, "id_number": "D4455667"},
            "parties": [{"key": "a", "role": "applicant", "name": "Priya Raman", "dob": "1990-12-01", "country_of_residence": "US"}],
            "documents": [id_doc("Priya Raman", "1992-12-01", "D4455667") | {"party_key": "a"}, poa("Priya Raman", ADDR_LA)],
        },
        "expect": {"outcome": "escalate", "gate": "GATE_CRITICAL_DATA", "must_have": ["DOB_MISMATCH"], "must_not_have": []},
    },
    {
        "id": "G08_business_ubo_gap_request_more",
        "description": "LLC with only 40% of ownership identified and control person without ID → request_more",
        "must_pass": True,
        "application": {
            "external_ref": "G08", "applicant_type": "business",
            "declared": {"entity_name": "Bluewater Landscaping LLC", "address": "455 Grand Ave Suite 300, Oakland, CA 94610", "country": "US",
                         "industry": "landscaping services", "product": "business_checking", "expected_monthly_volume_usd": 45000},
            "parties": [{"key": "e", "role": "entity", "name": "Bluewater Landscaping LLC", "country_of_residence": "US"},
                        {"key": "o1", "role": "beneficial_owner", "name": "Tomas Ruiz", "dob": "1976-04-19", "country_of_residence": "US", "ownership_pct": 40},
                        {"key": "c1", "role": "controller", "name": "Angela Park", "dob": "1981-08-08", "country_of_residence": "US", "is_control_person": True}],
            "documents": [formation("Bluewater Landscaping LLC"), ein("Bluewater Landscaping LLC"),
                          owners_decl("Bluewater Landscaping LLC", [{"name": "Tomas Ruiz", "pct": 40}]),
                          id_doc("Tomas Ruiz", "1976-04-19", "D8899001") | {"party_key": "o1"}],
        },
        "expect": {"outcome": "request_more", "must_have": ["UBO_COVERAGE_GAP", "UBO_MISSING_ID"], "must_not_have": ["UBO_OVER_100"],
                   "missing_items_contains": ["Angela Park"]},
    },
    {
        "id": "G09_business_clean_low_risk_auto_approve",
        "description": "Corporation, two owners at 60/40 both with ID, control person, domestic, low risk → auto_approve",
        "must_pass": True,
        "application": {
            "external_ref": "G09", "applicant_type": "business",
            "declared": {"entity_name": "Sierra Dental Group Inc", "address": "910 Main St, Fresno, CA 93721", "country": "US",
                         "industry": "dental practice", "product": "business_checking", "expected_monthly_volume_usd": 80000},
            "parties": [{"key": "e", "role": "entity", "name": "Sierra Dental Group Inc", "country_of_residence": "US"},
                        {"key": "o1", "role": "beneficial_owner", "name": "Grace Lin", "dob": "1972-03-03", "country_of_residence": "US", "ownership_pct": 60, "is_control_person": True},
                        {"key": "o2", "role": "beneficial_owner", "name": "Samuel Adeyemi", "dob": "1978-10-21", "country_of_residence": "US", "ownership_pct": 40}],
            "documents": [formation("Sierra Dental Group Inc"), ein("Sierra Dental Group Inc"),
                          owners_decl("Sierra Dental Group Inc", [{"name": "Grace Lin", "pct": 60}, {"name": "Samuel Adeyemi", "pct": 40}]),
                          id_doc("Grace Lin", "1972-03-03", "D1010101") | {"party_key": "o1"},
                          id_doc("Samuel Adeyemi", "1978-10-21", "D2020202") | {"party_key": "o2"}],
        },
        "expect": {"outcome": "auto_approve", "risk_rating": "low", "must_have": [], "must_not_have": ["UBO_COVERAGE_GAP", "UBO_MISSING_ID", "CONTROL_PERSON_MISSING"]},
    },
    {
        "id": "G10_business_high_risk_layered_escalate",
        "description": "Layered ownership through a Cyprus holding, crypto exchange, international wires → High risk, escalate",
        "must_pass": True,
        "application": {
            "external_ref": "G10", "applicant_type": "business",
            "declared": {"entity_name": "Meridian Digital Assets LLC", "address": "600 Congress Ave, Austin, TX 78701", "country": "US",
                         "industry": "virtual asset exchange (crypto)", "product": "business_checking", "expected_monthly_volume_usd": 900000,
                         "international_wires": True,
                         "ownership_tree": [{"owner": "Helios Holdings Ltd", "of": "Meridian Digital Assets LLC", "pct": 70},
                                            {"owner": "Nikolas Petrou", "of": "Meridian Digital Assets LLC", "pct": 30},
                                            {"owner": "Andreas Georgiou", "of": "Helios Holdings Ltd", "pct": 50},
                                            {"owner": "Elena Christou", "of": "Helios Holdings Ltd", "pct": 50}]},
            "parties": [{"key": "e", "role": "entity", "name": "Meridian Digital Assets LLC", "country_of_residence": "US"},
                        {"key": "o1", "role": "beneficial_owner", "name": "Nikolas Petrou", "dob": "1980-05-05", "country_of_residence": "US", "ownership_pct": 30, "is_control_person": True},
                        {"key": "o2", "role": "beneficial_owner", "name": "Andreas Georgiou", "dob": "1969-11-11", "country_of_residence": "CY", "extra": {"address": "12 Makarios Ave, Limassol 3025, Cyprus"}},
                        {"key": "o3", "role": "beneficial_owner", "name": "Elena Christou", "dob": "1974-02-02", "country_of_residence": "CY", "extra": {"address": "8 Griva Digeni, Nicosia 1066, Cyprus"}}],
            "documents": [formation("Meridian Digital Assets LLC", state="TX"), ein("Meridian Digital Assets LLC"),
                          owners_decl("Meridian Digital Assets LLC", [{"name": "Helios Holdings Ltd", "pct": 70}, {"name": "Nikolas Petrou", "pct": 30}]),
                          id_doc("Nikolas Petrou", "1980-05-05", "D3030303") | {"party_key": "o1"},
                          id_doc("Andreas Georgiou", "1969-11-11", "K4040404", dtype="passport", country="CY") | {"party_key": "o2"},
                          id_doc("Elena Christou", "1974-02-02", "K5050505", dtype="passport", country="CY") | {"party_key": "o3"}],
        },
        "expect": {"outcome": "escalate", "gate": "GATE_HIGH_RISK", "risk_rating": "high", "must_have": ["RISK_HIGH", "OWNERSHIP_COMPLEX"],
                   "must_not_have": ["UBO_COVERAGE_GAP"], "review_priority": "high_risk"},
    },
    {
        "id": "G11_business_over_100_pct_escalate",
        "description": "Declared ownership sums to 110% with a possible adverse-media hit → escalate",
        "must_pass": True,
        "application": {
            "external_ref": "G11", "applicant_type": "business",
            "declared": {"entity_name": "Northwind Capital Partners LLC", "address": "1 Market Plaza, San Francisco, CA 94105", "country": "US",
                         "industry": "investment advisory", "product": "business_checking", "expected_monthly_volume_usd": 150000},
            "parties": [{"key": "e", "role": "entity", "name": "Northwind Capital Partners LLC", "country_of_residence": "US"},
                        {"key": "o1", "role": "beneficial_owner", "name": "Owen Fitzgerald", "dob": "1970-07-07", "country_of_residence": "US", "ownership_pct": 60, "is_control_person": True},
                        {"key": "o2", "role": "beneficial_owner", "name": "Beatrice Moreau", "dob": "1975-01-25", "country_of_residence": "US", "ownership_pct": 50}],
            "documents": [formation("Northwind Capital Partners LLC"), ein("Northwind Capital Partners LLC"),
                          owners_decl("Northwind Capital Partners LLC", [{"name": "Owen Fitzgerald", "pct": 60}, {"name": "Beatrice Moreau", "pct": 50}]),
                          id_doc("Owen Fitzgerald", "1970-07-07", "D6060606") | {"party_key": "o1"},
                          id_doc("Beatrice Moreau", "1975-01-25", "D7070707") | {"party_key": "o2"}],
        },
        "expect": {"outcome": "escalate", "must_have": ["UBO_OVER_100", "SCREENING_ADVERSE_MEDIA"], "must_not_have": []},
    },
    {
        "id": "G12_individual_cash_intensive_medium_needs_review",
        "description": "Sole proprietor car wash (cash intensive, 20) + increased-monitoring nationality (0, US resident) → Medium → analyst confirms",
        "must_pass": True,
        "application": {
            "external_ref": "G12", "applicant_type": "individual",
            "declared": {"address": "77 Sunset Blvd, Los Angeles, CA 90028", "country": "US", "industry": "car wash (sole proprietor)",
                         "product": "business_checking", "expected_monthly_volume_usd": 300000, "id_number": "D8080808"},
            "parties": [{"key": "a", "role": "applicant", "name": "Ibrahim Sall", "dob": "1983-03-23", "country_of_residence": "US"}],
            "documents": [id_doc("Ibrahim Sall", "1983-03-23", "D8080808") | {"party_key": "a"}, poa("Ibrahim Sall", "77 Sunset Blvd, Los Angeles, CA 90028")],
        },
        "expect": {"outcome": "needs_review", "gate": "GATE_MEDIUM_RISK", "risk_rating": "medium", "must_have": ["RISK_MEDIUM"], "must_not_have": [], "review_priority": "medium_risk"},
    },
]


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for c in CASES:
        (GOLDEN_DIR / f"{c['id']}.json").write_text(json.dumps(c, indent=2))
    print(f"wrote {len(CASES)} golden cases to {GOLDEN_DIR}")


if __name__ == "__main__":
    main()
