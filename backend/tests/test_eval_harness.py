"""The eval harness itself: llm-mode guard, usage columns in the report, and --compare against a baseline."""
import json

import pytest

from app import llm as llm_mod
from app.config import get_settings
from eval import run_eval
from tests.test_usage import CountingLLM


def test_llm_mode_refuses_to_run_without_a_model(tmp_path):
    s = get_settings()
    s.azure_openai_endpoint = None
    s.llm_provider = "mock"
    try:
        with pytest.raises(run_eval.ModeUnavailable):
            run_eval.run("llm", None, f"sqlite:///{tmp_path / 'e.db'}")
    finally:
        s.llm_provider = "auto"
        s.orchestrator_mode = "scripted"


def test_scripted_then_llm_report_with_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "REPORT_DIR", tmp_path / "reports")
    base = run_eval.run("scripted", "G01", f"sqlite:///{tmp_path / 'a.db'}")
    assert base["summary"]["passed"] == 1 and base["summary"]["total_cost_usd"] == 0
    md = run_eval.write_report(base)
    assert (tmp_path / "reports" / "latest-scripted.json").exists()
    assert "| Latency |" in md.read_text()

    s = get_settings()
    s.azure_openai_endpoint = "https://fake.openai.azure.com/"
    s.llm_provider = "azure"

    class RepeatingLLM(CountingLLM):  # fresh plan per case
        def _chat(self, *a, **k):
            if not self.plan:
                self.plan = ["extract_document", "check_consistency", "screen_parties", "verify_address", "rate_risk",
                             "record_decision"]
            return super()._chat(*a, **k)

    monkeypatch.setattr(llm_mod, "_llm", RepeatingLLM())
    try:
        rep = run_eval.run("llm", "G01", f"sqlite:///{tmp_path / 'b.db'}")
    finally:
        s.azure_openai_endpoint = None
        s.llm_provider = "auto"
        s.orchestrator_mode = "scripted"
        llm_mod.reset_llm()
    sm = rep["summary"]
    assert sm["provider"] == "azure" and sm["deployment"] == "gpt-4.1-mini"
    assert sm["llm_calls"] == 7 and sm["total_cost_usd"] > 0 and sm["tokens_cached"] > 0
    assert sm["mode_mismatches"] == [] and rep["results"][0]["checks"]["ran_in_requested_mode"]
    rep["comparison"] = run_eval.compare(rep, json.loads((tmp_path / "reports" / "latest-scripted.json").read_text()))
    assert rep["comparison"]["changed"] == [] and rep["comparison"]["regressed"] == []
    md = run_eval.write_report(rep)
    text = md.read_text()
    assert "cost $" in text and "Compared with scripted baseline" in text and "gpt-4.1-mini-2025-04-14" in text
