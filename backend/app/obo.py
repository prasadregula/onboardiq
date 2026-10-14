"""OAuth 2.0 on-behalf-of (OBO) - week 3, w3-3.

Two kinds of identity are used deliberately:

* App-only (agent identities, app/identity.py) for every KYC tool. Tools act with the BANK's authority on bank data.
  If they used the analyst's own delegated permissions, a tool's reach would silently depend on who clicked "run".
* Delegated (OBO) only when a downstream resource must enforce the ANALYST's own permissions - e.g. reading evidence
  from the analyst's mailbox or a SharePoint site they can access. The API exchanges the analyst's incoming token
  (audience = OnboardIQ-API) for a token to the downstream API that still represents the analyst.

Secretless: the API app registration trusts a user-assigned managed identity through a federated identity credential,
so the OBO exchange uses a managed-identity assertion instead of a client secret. `GET /me/delegation` demonstrates it
against Microsoft Graph /me and checks the result is the same person as the incoming token.

In every case the human principal is recorded on the run and on each tool event (identity.acting_for), so auditors see
both "which agent identity acted" and "for whom".
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings

GRAPH_SCOPE = "https://graph.microsoft.com/User.Read"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me?$select=id,displayName,userPrincipalName"
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange/.default"


class OboNotConfigured(RuntimeError):
    pass


def obo_credential(user_assertion: str):
    """azure-identity OnBehalfOfCredential for the analyst's token, authenticated by a managed-identity assertion."""
    s = get_settings()
    if not (s.obo_client_id and s.obo_assertion_identity and s.entra_tenant_id):
        raise OboNotConfigured("set ONBOARDIQ_OBO_CLIENT_ID, ONBOARDIQ_OBO_ASSERTION_IDENTITY and the tenant id")
    from azure.identity import ManagedIdentityCredential, OnBehalfOfCredential
    mi = ManagedIdentityCredential(client_id=s.obo_assertion_identity)

    def assertion() -> str:  # short-lived token proving "I am the API's trusted managed identity"
        return mi.get_token(TOKEN_EXCHANGE_AUDIENCE).token

    return OnBehalfOfCredential(tenant_id=s.entra_tenant_id, client_id=s.obo_client_id,
                                client_assertion_func=assertion, user_assertion=user_assertion)


def graph_me_on_behalf_of(user_assertion: str, expected_oid: str | None) -> dict[str, Any]:
    token = obo_credential(user_assertion).get_token(GRAPH_SCOPE).token
    r = httpx.get(GRAPH_ME, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    me = r.json()
    return {"delegated_as": me.get("userPrincipalName"), "display_name": me.get("displayName"),
            "graph_object_id": me.get("id"), "matches_signed_in_user": bool(expected_oid) and me.get("id") == expected_oid}
