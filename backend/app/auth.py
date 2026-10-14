"""Authentication & authorization.

Modes (ONBOARDIQ_AUTH_MODE):
  apikey - X-API-Key header (local dev, CI). Identity from X-Analyst header; role from X-Role header (default KycAnalyst).
  entra  - Microsoft Entra ID bearer tokens validated against the tenant JWKS (RS256), audience = api://<client-id>.
           Roles come from the token's `roles` claim (app roles on the API app registration) - standard Entra app-role model.

App roles:
  Admin       everything, incl. screening list refresh and config endpoints
  BsaOfficer  analyst actions + second approver on sanctions, escalate_to_bsa target
  KycAnalyst  create/run cases, claim and disposition review tasks
  Auditor     read-only over cases, runs and the audit chain
  Service     machine-to-machine (an upstream onboarding portal or integration): create cases, upload documents, run, read case status
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings, get_settings

log = logging.getLogger("onboardiq.auth")

ROLE_ADMIN, ROLE_BSA, ROLE_ANALYST, ROLE_AUDITOR, ROLE_SERVICE = "Admin", "BsaOfficer", "KycAnalyst", "Auditor", "Service"
ALL_ROLES = {ROLE_ADMIN, ROLE_BSA, ROLE_ANALYST, ROLE_AUDITOR, ROLE_SERVICE}
READERS = {ROLE_ADMIN, ROLE_BSA, ROLE_ANALYST, ROLE_AUDITOR, ROLE_SERVICE}
CASE_WRITERS = {ROLE_ADMIN, ROLE_BSA, ROLE_ANALYST, ROLE_SERVICE}
REVIEWERS = {ROLE_ADMIN, ROLE_BSA, ROLE_ANALYST}


@dataclass
class Principal:
    subject: str  # upn / preferred_username / app id
    name: str
    roles: set[str] = field(default_factory=set)
    kind: str = "user"  # user | app
    claims: dict[str, Any] = field(default_factory=dict)

    def has_any(self, roles: set[str]) -> bool:
        return bool(self.roles & roles)

    @property
    def analyst_id(self) -> str:
        return self.subject


# --------------------------------------------------------------------------------------------------- JWKS cache
class JwksCache:
    def __init__(self, url: str, ttl: int = 3600):
        self.url, self.ttl, self._keys, self._fetched = url, ttl, {}, 0.0

    def key_for(self, kid: str):
        stale = time.time() - self._fetched > self.ttl
        unknown = kid not in self._keys and time.time() - self._fetched > 60  # key rollover: refetch at most once a minute
        if stale or unknown:
            self.refresh()
        return self._keys.get(kid)

    def refresh(self) -> None:
        try:
            data = httpx.get(self.url, timeout=10).json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("JWKS refresh failed (%s); keeping cached keys", e)
            self._fetched = time.time()  # back off
            return
        keys = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in data.get("keys", []) if k.get("kty") == "RSA"}
        if keys:
            self._keys = keys
        self._fetched = time.time()

    def load_static(self, jwks: dict) -> None:  # tests
        self._keys = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in jwks["keys"]}
        self._fetched = time.time()


_jwks: JwksCache | None = None


def get_jwks() -> JwksCache:
    global _jwks
    if _jwks is None:
        _jwks = JwksCache(get_settings().jwks_url)
    return _jwks


def reset_jwks(cache: JwksCache | None = None) -> None:
    global _jwks
    _jwks = cache


# --------------------------------------------------------------------------------------------------- validation
def validate_entra_token(token: str, s: Settings) -> Principal:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"malformed token: {e}") from e
    key = get_jwks().key_for(header.get("kid", ""))
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown signing key")
    audiences = [a for a in {s.entra_audience, (s.entra_audience or "").removeprefix("api://")} if a]
    try:
        claims = jwt.decode(token, key=key, algorithms=["RS256"], audience=audiences, issuer=s.issuers,
                            options={"require": ["exp", "iss", "aud"]})
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e
    roles = set(claims.get("roles") or [])
    is_app = "idtyp" in claims and claims["idtyp"] == "app" or ("upn" not in claims and "preferred_username" not in claims and "oid" in claims and "scp" not in claims)
    subject = claims.get("preferred_username") or claims.get("upn") or claims.get("azp") or claims.get("appid") or claims.get("oid") or claims["sub"]
    return Principal(subject=subject, name=claims.get("name") or subject, roles=roles & ALL_ROLES,
                     kind="app" if is_app else "user", claims=claims)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def current_principal(request: Request, authorization: str | None = Header(default=None),
                      x_api_key: str | None = Header(default=None), x_analyst: str | None = Header(default=None),
                      x_role: str | None = Header(default=None)) -> Principal:
    s = get_settings()
    if s.auth_mode == "entra":
        token = _bearer(authorization)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
        p = validate_entra_token(token, s)
        request.state.user_token = token  # kept only for an explicit on-behalf-of exchange (app/obo.py); never logged
    else:
        token = x_api_key or _bearer(authorization)
        if token != s.api_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")
        roles = {r.strip() for r in (x_role or ROLE_ANALYST).split(",")} & ALL_ROLES or {ROLE_ANALYST}
        p = Principal(subject=x_analyst or "analyst", name=x_analyst or "analyst", roles=roles)
    if not p.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no OnboardIQ app role assigned")
    request.state.principal = p
    return p


def require_roles(*roles: str):
    allowed = set(roles)

    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has_any(allowed):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires one of roles {sorted(allowed)}")
        return p
    return dep


# Convenience dependencies
require_reader = require_roles(*READERS)
require_case_writer = require_roles(*CASE_WRITERS)
require_reviewer = require_roles(*REVIEWERS)
require_admin = require_roles(ROLE_ADMIN)


def current_analyst(p: Principal = Depends(require_reviewer)) -> str:
    return p.analyst_id


@lru_cache
def _noop():  # keeps import-time side effects nil for tests
    return None
