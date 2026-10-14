"""Entra ID bearer validation with a locally generated RSA key served as a static JWKS (no network)."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.config import get_settings
from tests.conftest import golden

TENANT = "11111111-2222-3333-4444-555555555555"
AUD = "api://onboardiq-api"


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key()
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(pub, as_dict=True) | {"kid": "test-kid", "use": "sig"}
    cache = auth_mod.JwksCache("https://unused")
    cache.load_static({"keys": [jwk]})
    auth_mod.reset_jwks(cache)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    yield pem
    auth_mod.reset_jwks(None)


def mint(pem, roles, upn="analyst1@contoso.example", aud=AUD, exp_delta=600, kid="test-kid", iss=None):
    claims = {"aud": aud, "iss": iss or f"https://login.microsoftonline.com/{TENANT}/v2.0", "exp": int(time.time()) + exp_delta,
              "iat": int(time.time()), "preferred_username": upn, "name": "Prasad", "roles": roles, "sub": "abc", "oid": "oid-1", "scp": "access_as_user"}
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def entra_client(keypair):
    s = get_settings()
    s.auth_mode, s.entra_tenant_id, s.entra_audience = "entra", TENANT, AUD
    from app.main import app
    with TestClient(app) as c:
        yield c, keypair
    s.auth_mode = "apikey"


def test_no_token_is_401(entra_client):
    c, _ = entra_client
    assert c.get("/cases").status_code == 401
    assert c.get("/cases", headers={"X-API-Key": "dev-key"}).status_code == 401  # api key ignored in entra mode


def test_valid_token_roles_and_identity(entra_client):
    c, pem = entra_client
    h = {"Authorization": f"Bearer {mint(pem, ['KycAnalyst'])}"}
    me = c.get("/me", headers=h).json()
    assert me["subject"] == "analyst1@contoso.example" and me["roles"] == ["KycAnalyst"]
    cid = c.post("/cases", json=golden("G05"), headers=h).json()["id"]
    c.post(f"/cases/{cid}/run", json={"sync": True}, headers=h)
    tid = next(q for q in c.get("/review/queue", headers=h).json() if q["case_id"] == cid)["task_id"]
    r = c.post(f"/review/tasks/{tid}/action", json={"action": "claim"}, headers=h)
    assert r.json()["task"]["assigned_to"] == "analyst1@contoso.example"
    last = c.get(f"/cases/{cid}/audit", headers=h).json()[-1]
    assert last["actor"] == "analyst:analyst1@contoso.example"


def test_auditor_is_read_only(entra_client):
    c, pem = entra_client
    h = {"Authorization": f"Bearer {mint(pem, ['Auditor'])}"}
    assert c.get("/cases", headers=h).status_code == 200
    assert c.post("/cases", json=golden("G01"), headers=h).status_code == 403
    assert c.post("/admin/screening/refresh", headers=h).status_code == 403


def test_no_role_is_403(entra_client):
    c, pem = entra_client
    assert c.get("/cases", headers={"Authorization": f"Bearer {mint(pem, [])}"}).status_code == 403


def test_expired_wrong_audience_wrong_key_rejected(entra_client):
    c, pem = entra_client
    assert c.get("/cases", headers={"Authorization": f"Bearer {mint(pem, ['Admin'], exp_delta=-10)}"}).status_code == 401
    assert c.get("/cases", headers={"Authorization": f"Bearer {mint(pem, ['Admin'], aud='api://other')}"}).status_code == 401
    assert c.get("/cases", headers={"Authorization": f"Bearer {mint(pem, ['Admin'], kid='nope')}"}).status_code == 401
    assert c.get("/cases", headers={"Authorization": f"Bearer {mint(pem, ['Admin'], iss='https://evil/')}"}).status_code == 401


def test_bsa_officer_is_implicit_second_approver_on_sanctions(entra_client):
    c, pem = entra_client
    analyst = {"Authorization": f"Bearer {mint(pem, ['KycAnalyst'])}"}
    bsa = {"Authorization": f"Bearer {mint(pem, ['BsaOfficer'], upn='bsa@contoso.example')}"}
    cid = c.post("/cases", json=golden("G04"), headers=analyst).json()["id"]
    c.post(f"/cases/{cid}/run", json={"sync": True}, headers=analyst)
    tid = next(q for q in c.get("/review/queue", headers=analyst).json() if q["case_id"] == cid)["task_id"]
    assert c.post(f"/review/tasks/{tid}/action", json={"action": "approve", "notes": "fp"}, headers=analyst).status_code == 403
    r = c.post(f"/review/tasks/{tid}/action", json={"action": "approve", "notes": "false positive, DOB and nationality differ"}, headers=bsa)
    assert r.status_code == 200 and "bsa@contoso.example" in r.json()["task"]["disposition"]["second_approver"]
