"""The red-team suite (eval/redteam.py) as a test: every attack must be contained, and the suite must notice when a
control is switched off (otherwise a green run would prove nothing)."""
import json

from app.agent import untrusted
from eval import redteam


def test_every_attack_is_contained(capsys):
    assert redteam.main([]) == 0
    out = capsys.readouterr().out
    n = len(json.loads(redteam.CASES.read_text()))
    assert f"{n}/{n} attacks contained" in out and n >= 25


def test_suite_fails_when_a_control_is_removed(monkeypatch, capsys):
    from app.agent.tools import extract
    monkeypatch.setattr(extract, "heuristic_scan", lambda fields: [])  # sabotage: the extraction tool's pattern scan
    assert redteam.main(["--only", "RT01"]) == 1
    assert "FAIL RT01" in capsys.readouterr().out


def test_withheld_marker_is_not_mistaken_for_a_name():
    from app.agent.pii import Pseudonymizer
    p = Pseudonymizer(scan_free_text=False)
    out = p.minimise({"fields": {"full_name": untrusted.WITHHELD, "dob": "1990-01-01"}})
    assert out["fields"]["full_name"] == untrusted.WITHHELD and out["fields"]["dob"] == "DOB_1"
