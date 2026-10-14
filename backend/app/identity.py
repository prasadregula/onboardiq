"""Per-agent workload identities (week 3, w3-1).

Every agent role acts under its OWN Entra identity, so least privilege is enforced by Azure RBAC rather than by the
prompt, and the audit trail shows which identity performed each step:

    role           tools                                          Azure access (granted in infra)
    orchestrator   LLM planning loop, check_consistency,          Azure OpenAI (chat)
                   verify_address, resolve_beneficial_owners
    extraction     extract_document                               Document Intelligence, Blob (READ only)
    screening      screen_parties                                 none - OFAC lists are a read-only file mount
    risk           rate_risk (+ policy index embeddings)          Azure OpenAI (chat + embeddings)
    decision       record_decision                                none in Azure (writes go through the API's DB session)
    api            HTTP handlers, uploads, SQL                    SQL, Blob (read/write)  - system-assigned identity

In Azure each role's user-assigned managed identity client id arrives as ONBOARDIQ_IDENTITY_<ROLE>. Locally nothing is
set and every role falls back to DefaultAzureCredential (your `az login`), so development is unchanged.

The model chooses WHICH tool to call; code maps the tool to its identity. A prompt cannot change that mapping.
Limitation (documented in THREATMODEL.md): all identities live in one process, so this contains model-driven misuse
and gives attribution, but not a code-execution compromise - that needs one container per agent.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

ROLES = ("orchestrator", "extraction", "screening", "risk", "decision", "api")

TOOL_ROLE: dict[str, str] = {
    "extract_document": "extraction",
    "screen_parties": "screening",
    "rate_risk": "risk",
    "record_decision": "decision",
    "check_consistency": "orchestrator",
    "verify_address": "orchestrator",
    "resolve_beneficial_owners": "orchestrator",
}

_current_role: ContextVar[str] = ContextVar("onboardiq_agent_role", default="api")
# The human (or calling app) a run is performed for - w3-3. Agents act with their OWN identity (app-only) for Azure
# calls; this records WHO they acted for, so every audit event ties an agent action to an accountable principal.
_on_behalf_of: ContextVar[dict | None] = ContextVar("onboardiq_on_behalf_of", default=None)
_creds: dict[str, object] = {}
_lock = threading.Lock()


def role_for_tool(tool_name: str) -> str:
    return TOOL_ROLE.get(tool_name, "orchestrator")


def current_role() -> str:
    return _current_role.get()


@contextmanager
def acting_as(role: str) -> Iterator[str]:
    """Run the enclosed code (Azure calls, LLM calls) under `role`'s identity."""
    if role not in ROLES:
        raise ValueError(f"unknown agent role {role!r}")
    token = _current_role.set(role)
    try:
        yield role
    finally:
        _current_role.reset(token)


def principal_ref(p) -> dict:
    """Minimal, non-secret reference to an authenticated principal (auth.Principal) for runs and audit records."""
    c = getattr(p, "claims", {}) or {}
    return {"subject": p.subject, "name": p.name, "kind": p.kind, "oid": c.get("oid"), "tid": c.get("tid"),
            "roles": sorted(p.roles), "token_id": c.get("uti") or c.get("jti")}


def system_ref(name: str) -> dict:
    return {"subject": name, "name": name, "kind": "system", "oid": None, "tid": None, "roles": [], "token_id": None}


@contextmanager
def acting_for(principal: dict | None) -> Iterator[dict | None]:
    token = _on_behalf_of.set(principal)
    try:
        yield principal
    finally:
        _on_behalf_of.reset(token)


def on_behalf_of() -> dict | None:
    return _on_behalf_of.get()


def on_behalf_of_short() -> dict | None:
    p = _on_behalf_of.get()
    return {"subject": p["subject"], "oid": p.get("oid"), "kind": p.get("kind")} if p else None


def client_id_for(role: str) -> str | None:
    return os.getenv(f"ONBOARDIQ_IDENTITY_{role.upper()}") or None


def identity_label(role: str | None = None) -> str:
    """Short, non-secret label for audit records: 'risk:1a2b3c4d' (client-id prefix) or 'api:system' / 'risk:local'."""
    role = role or current_role()
    cid = client_id_for(role)
    if cid:
        return f"{role}:{cid[:8]}"
    return f"{role}:system" if os.getenv("IDENTITY_ENDPOINT") else f"{role}:local"


def credential_for(role: str):
    """azure-identity credential for an agent role (cached per role)."""
    with _lock:
        if role not in _creds:
            from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
            cid = client_id_for(role)
            if cid:
                _creds[role] = ManagedIdentityCredential(client_id=cid)
            elif os.getenv("IDENTITY_ENDPOINT"):
                _creds[role] = ManagedIdentityCredential()  # system-assigned identity of the Container App
            else:
                _creds[role] = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return _creds[role]


def current_credential():
    return credential_for(current_role())


def token_provider(scope: str):
    """Callable for SDKs that take a token provider (openai's azure_ad_token_provider). Resolves the identity at CALL
    time from the current agent role, so one client serves every role."""
    def _get() -> str:
        return current_credential().get_token(scope).token
    return _get


def configured() -> dict[str, bool]:
    """Which roles have a dedicated identity (for /health)."""
    return {r: bool(client_id_for(r)) for r in ROLES if r != "api"}


def reset_for_tests() -> None:
    with _lock:
        _creds.clear()
