"""OnboardIQ API.  uvicorn app.main:app --reload --port 8080"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import telemetry as tm
from .api import admin, cases, meta, review
from .config import DEV_ENVS, get_settings
from .db import init_db
from .identity import acting_as
from .providers.screening import get_screening_provider
from .rag.store import get_policy_store

log = logging.getLogger("onboardiq")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tm.setup_logging()
    s = get_settings()
    if s.auth_mode == "apikey" and s.env not in DEV_ENVS:  # T-17: header-asserted identity is for dev/CI only
        raise RuntimeError(f"auth_mode 'apikey' is not allowed when ONBOARDIQ_ENV={s.env!r}; use 'entra'")
    tm.setup_tracing(app)
    try:
        init_db()
        with acting_as("risk"):  # policy-index embeddings belong to the risk agent's identity
            try:
                get_policy_store().ensure()
            except Exception as e:  # noqa: BLE001 - e.g. risk identity's role not propagated yet; rate_risk retries lazily
                log.warning("policy index not ready at startup (%s: %s); will build on first use", type(e).__name__, e)
        prov = get_screening_provider()
    except Exception:
        log.exception("startup failed")  # make the cause visible in Log Analytics before the worker exits
        raise
    log.info("startup complete: screening=%s entries=%d llm=%s", prov.name, len(prov.entries), get_settings().llm_provider)
    yield


app = FastAPI(title="OnboardIQ", version="0.2.0", description="Agentic KYC / account onboarding", lifespan=lifespan,
              docs_url="/docs" if get_settings().env != "prod" else None)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-Id"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or tm.new_request_id()
    token = tm.request_id_var.set(rid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001
        log.exception("unhandled error %s %s", request.method, request.url.path)
        response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
    finally:
        tm.request_id_var.reset(token)
    route = getattr(request.scope.get("route"), "path", request.url.path)
    tm.HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).observe(time.perf_counter() - t0)
    response.headers["X-Request-Id"] = rid
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


app.include_router(meta.router)
app.include_router(cases.router)
app.include_router(review.router)
app.include_router(admin.router)
