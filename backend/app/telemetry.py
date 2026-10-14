"""Observability: OpenTelemetry traces (→ Azure Monitor when APPLICATIONINSIGHTS_CONNECTION_STRING is set),
Prometheus metrics at /metrics (for local Grafana), structured JSON logs.

Rules: log and tag case ids, run ids, finding codes, tool names - never names, DOBs, ID numbers or addresses.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

from .config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_tracer = trace.get_tracer("onboardiq")

# ---- metrics ------------------------------------------------------------------------------------------------
CASES_DECIDED = Counter("onboardiq_cases_decided_total", "Cases decided by outcome and gate", ["outcome", "gate", "mode"])
RUNS_FAILED = Counter("onboardiq_runs_failed_total", "Agent runs that failed", ["mode"])
TOOL_LATENCY = Histogram("onboardiq_tool_seconds", "Tool execution time", ["tool"], buckets=(.01, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))
TOOL_ERRORS = Counter("onboardiq_tool_errors_total", "Tool errors returned to the model", ["tool"])
LLM_CALLS = Counter("onboardiq_llm_calls_total", "LLM chat calls", ["provider", "model"])
LLM_TOKENS = Counter("onboardiq_llm_tokens_total", "LLM tokens", ["provider", "model", "direction"])
LLM_LATENCY = Histogram("onboardiq_llm_seconds", "LLM call latency", ["provider"], buckets=(.25, .5, 1, 2, 4, 8, 16, 32, 64))
FINDINGS = Counter("onboardiq_findings_total", "Findings raised by code", ["code", "severity"])
REVIEW_ACTIONS = Counter("onboardiq_review_actions_total", "Analyst actions", ["action", "priority"])
SCREENING_HITS = Counter("onboardiq_screening_hits_total", "Screening hits", ["list", "classification"])
HTTP_REQUESTS = Histogram("onboardiq_http_seconds", "HTTP request latency", ["method", "route", "status"],
                          buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))


# ---- tracing ------------------------------------------------------------------------------------------------
def setup_tracing(app=None) -> None:
    s = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": s.otel_service_name, "deployment.environment": s.env}))
    if s.applicationinsights_connection_string:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=s.applicationinsights_connection_string)))
    trace.set_tracer_provider(provider)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def span(name: str, **attrs):
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v)
        yield sp


@contextlib.contextmanager
def tool_span(tool: str, case_id: str, run_id: str):
    t0 = time.perf_counter()
    with span(f"tool.{tool}", **{"onboardiq.tool": tool, "onboardiq.case_id": case_id, "onboardiq.run_id": run_id}) as sp:
        try:
            yield sp
        finally:
            TOOL_LATENCY.labels(tool).observe(time.perf_counter() - t0)


@contextlib.contextmanager
def llm_call_span(provider: str, model: str):
    t0 = time.perf_counter()
    LLM_CALLS.labels(provider, model).inc()
    with span("llm.chat", **{"llm.provider": provider, "llm.model": model}) as sp:
        try:
            yield sp
        finally:
            LLM_LATENCY.labels(provider).observe(time.perf_counter() - t0)
            for d in ("tokens_in", "tokens_out"):
                v = sp.attributes.get(f"llm.{d}") if getattr(sp, "attributes", None) else None
                if v:
                    LLM_TOKENS.labels(provider, model, d).inc(v)


# ---- logging ------------------------------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage(), "request_id": request_id_var.get()}
        for k in ("case_id", "run_id", "tool", "gate", "outcome", "analyst"):
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def setup_logging() -> None:
    s = get_settings()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter() if s.log_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
    if not s.log_json:
        old = logging.getLogRecordFactory()

        def factory(*a, **k):
            r = old(*a, **k)
            r.request_id = request_id_var.get()
            return r
        logging.setLogRecordFactory(factory)
    root.handlers = [h]
    for noisy in ("azure", "httpx", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
