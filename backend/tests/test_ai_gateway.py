"""AI gateway (Azure API Management) configuration: the policy only admits agent identities, calls the model with the
gateway's own identity, never logs bodies, and every placeholder in it is filled by main.bicep."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from app.config import get_settings

INFRA = Path(__file__).resolve().parents[2] / "infra"


def test_policy_admits_only_agent_identities_and_swaps_in_the_gateway_identity():
    root = ET.parse(INFRA / "apim-openai-policy.xml").getroot()
    inbound = root.find("inbound")
    tags = [el.tag for el in inbound]
    assert tags.index("validate-azure-ad-token") < tags.index("authentication-managed-identity")  # check, then swap
    ids = [el.text for el in inbound.find("validate-azure-ad-token/client-application-ids")]
    assert ids == ["__ORCHESTRATOR_APPID__", "__RISK_APPID__"]  # extraction/screening/decision agents: no model access
    assert inbound.find("validate-azure-ad-token").get("failed-validation-httpcode") == "401"
    assert inbound.find("llm-emit-token-metric/dimension").get("name") == "agent"


def test_every_placeholder_is_filled_by_bicep_and_bodies_are_not_logged():
    policy = (INFRA / "apim-openai-policy.xml").read_text()
    bicep = (INFRA / "main.bicep").read_text()
    placeholders = set(re.findall(r"__[A-Z_]+__", policy))
    assert placeholders == {"__TENANT_ID__", "__ORCHESTRATOR_APPID__", "__RISK_APPID__"}
    assert all(f"'{p}'" in bicep for p in placeholders)
    diag = bicep[bicep.index("resource apimDiag"):]
    assert diag.count("body: { bytes: 0 }") == 4  # applicant data never reaches the gateway logs


def test_health_reports_the_llm_route(client):
    assert client.get("/health").json()["llm_route"] == "direct"
    get_settings().llm_gateway = "apim"
    try:
        assert client.get("/health").json()["llm_route"] == "gateway:apim"
    finally:
        get_settings().llm_gateway = None
