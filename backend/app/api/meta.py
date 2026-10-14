from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlmodel import Session, func, select

from .. import identity
from ..agent.tools import REGISTRY
from ..auth import Principal, current_principal, require_admin, require_reader
from ..config import get_settings
from ..db import get_session
from ..llm import get_llm
from ..models import Case, ReviewTask
from ..providers.screening import get_screening_provider, refresh_screening_provider
from ..providers.storage import get_storage
from ..rag.store import get_policy_store

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    s = get_settings()
    return {"ok": True, "env": s.env, "auth_mode": s.auth_mode, "orchestrator_mode": s.orchestrator_mode, "run_async": s.run_async,
            "llm": get_llm().name, "llm_ready": s.llm_ready, "chat_model": getattr(get_llm(), "chat_deployment", None),
            "embedding_model": getattr(get_llm(), "embedding_deployment", None),
            "extraction_provider": s.extraction_provider, "screening_provider": get_screening_provider().name,
            "screening_entries": len(get_screening_provider().entries), "storage": get_storage().name,
            "policy_index_version": getattr(get_policy_store(), "version", None), "tracing": bool(s.applicationinsights_connection_string),
            "agent_identities": identity.configured()}


@router.get("/ready")
def ready() -> dict:
    # Deliberately does NOT query the database. Container Apps calls this probe every 10 s; a SQL query here kept the
    # serverless Azure SQL database permanently awake (never auto-paused) and billed it 24x7. Startup already proved
    # the database works (migrations run in the lifespan hook before the app accepts traffic).
    return {"ready": True}


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/me")
def me(p: Principal = Depends(current_principal)) -> dict:
    return {"subject": p.subject, "name": p.name, "roles": sorted(p.roles), "kind": p.kind}


@router.get("/me/delegation")
def me_delegation(request: Request, p: Principal = Depends(current_principal)) -> dict:
    """w3-3 demo: exchange the caller's token on-behalf-of them for Microsoft Graph and confirm Graph sees the same
    person. Shows the delegated path is available for analyst-scoped resources; KYC tools stay app-only."""
    from ..obo import OboNotConfigured, graph_me_on_behalf_of
    token = getattr(request.state, "user_token", None)
    if not token:
        raise HTTPException(400, "on-behalf-of needs an Entra user token (auth mode 'entra')")
    try:
        result = graph_me_on_behalf_of(token, (p.claims or {}).get("oid"))
    except OboNotConfigured as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001 - surface the Entra error (consent, federated credential) to the caller
        raise HTTPException(502, f"on-behalf-of exchange failed: {type(e).__name__}: {str(e)[:300]}") from e
    return {"signed_in": p.subject, **result,
            "note": "Delegated token used only for this call; KYC tools use their own agent identities."}


@router.get("/tools", dependencies=[Depends(require_reader)])
def tools() -> list[dict]:
    return [{"name": t.name, "description": t.description, "required": t.required, "business_only": t.business_only,
             "input_schema": t.input_model.model_json_schema()} for t in REGISTRY.values()]


@router.get("/policies/search", dependencies=[Depends(require_reader)])
def policy_search(q: str, k: int = 5) -> list[dict]:
    with identity.acting_as("risk"):  # policy retrieval is the risk agent's capability and identity
        return [c.__dict__ | {"ref": c.ref()} for c in get_policy_store().search(q, top_k=k)]


@router.post("/admin/screening/refresh", dependencies=[Depends(require_admin)])
def screening_refresh() -> dict:
    prov = refresh_screening_provider()
    return {"provider": prov.name, "entries": len(prov.entries), "loaded_at": getattr(prov, "loaded_at", None)}


@router.post("/admin/policies/reindex", dependencies=[Depends(require_admin)])
def policies_reindex() -> dict:
    store = get_policy_store()
    with identity.acting_as("risk"):
        n = store.build(get_settings().policies_dir) if hasattr(store, "build") else 0
    return {"chunks": n, "version": getattr(store, "version", None)}


@router.get("/stats", dependencies=[Depends(require_reader)])
def stats(session: Session = Depends(get_session)) -> dict:
    by_status = dict(session.exec(select(Case.status, func.count()).group_by(Case.status)).all())
    open_tasks = session.exec(select(func.count()).select_from(ReviewTask).where(ReviewTask.status != "done")).one()
    by_priority = dict(session.exec(select(ReviewTask.priority, func.count()).where(ReviewTask.status != "done")
                                    .group_by(ReviewTask.priority)).all())
    return {"cases_by_status": {getattr(k, "value", k): v for k, v in by_status.items()},
            "open_review_tasks": open_tasks, "open_by_priority": by_priority}
