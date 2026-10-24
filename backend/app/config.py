"""Runtime configuration. Everything defaults to offline/mock so the platform runs with no Azure at all.

Set ONBOARDIQ_* environment variables (or a .env file) to point at real services.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


DEV_ENVS = {"dev", "ci", "test", "local"}  # environments where dev conveniences (API-key auth, demo reset) are allowed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONBOARDIQ_", env_file=".env", extra="ignore")

    env: str = "dev"
    api_key: str = "dev-key"  # X-API-Key for local dev; replaced by Entra app roles in cloud
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'onboardiq.db'}"
    blob_dir: Path = BASE_DIR / "data" / "blobs"
    cors_origins: str = "http://localhost:5180"

    # Orchestration
    orchestrator_mode: Literal["scripted", "llm"] = "scripted"
    max_tool_calls: int = 14  # soft step budget for the model loop: when used up, the checklist finishes deterministically
    # Hard per-run budgets (w4-3, app/control.py): exceeding one stops the run and sends the case to an analyst.
    run_max_tool_calls: int = 30
    run_max_tokens: int = 100_000
    run_max_cost_usd: float = 0.05
    run_max_seconds: int = 180
    # Azure AI Content Safety Prompt Shields (T-01): scans document text for injected instructions before the model
    # sees it. Unset = deterministic pattern scan only. Set but unreachable = fail closed (no auto-approval).
    content_safety_endpoint: Optional[str] = None
    content_safety_api_version: str = "2024-09-01"
    prompt_version: str = "kyc-orchestrator/v3"  # v3: document text delimited and declared data-only (T-01)

    # LLM provider: auto (azure if endpoint set, else ollama if reachable, else mock) | azure | ollama | mock
    llm_provider: Literal["auto", "azure", "ollama", "mock"] = "auto"

    # Ollama (local, OpenAI-compatible API). Tool calling needs a tools-capable model: llama3.1, qwen2.5, mistral-nemo...
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_chat_model: str = "qwen2.5:14b"
    ollama_embedding_model: str = "nomic-embed-text"

    # Azure OpenAI (a Foundry / Azure OpenAI resource)
    azure_openai_endpoint: str | None = None  # e.g. https://<resource>.openai.azure.com/
    azure_openai_api_key: str | None = None  # omit to use DefaultAzureCredential
    azure_openai_api_version: str = "2024-10-21"  # GPT-5-family / reasoning deployments need a newer version: use the
    # one shown on the deployment's page in the Foundry portal (ONBOARDIQ_AZURE_OPENAI_API_VERSION)
    azure_openai_chat_deployment: str = "gpt-4.1-mini"
    azure_openai_embedding_deployment: str = "text-embedding-3-small"

    # Azure Document Intelligence
    doc_intel_endpoint: str | None = None
    doc_intel_api_key: str | None = None

    # Azure AI Search (optional; local index used when unset)
    search_endpoint: str | None = None
    search_api_key: str | None = None
    search_index: str = "onboardiq-policies"

    # Providers: "mock" or a real adapter name
    extraction_provider: Literal["mock", "azure"] = "mock"
    screening_provider: Literal["mock", "ofac"] = "mock"
    address_provider: Literal["mock"] = "mock"
    ofac_data_dir: Path = BASE_DIR / "data" / "ofac"  # sdn.csv, alt.csv, cons_prim.csv, cons_alt.csv (scripts/refresh_ofac.py)
    ofac_refresh_hours: int = 24

    # --- Auth: apikey (dev) | entra (Microsoft Entra ID JWT bearer with app roles)
    auth_mode: Literal["apikey", "entra"] = "apikey"
    entra_tenant_id: str | None = None
    entra_audience: str | None = None  # api://<client-id> or the client id itself
    # On-behalf-of (w3-3): API app registration client id + client id of the managed identity it trusts via a
    # federated identity credential (secretless OBO). Unset = OBO disabled.
    obo_client_id: str | None = None
    obo_assertion_identity: str | None = None
    entra_jwks_url: str | None = None  # default derived from tenant: https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys
    entra_issuer: str | None = None  # default https://login.microsoftonline.com/<tenant>/v2.0 (v1 tokens also accepted)

    # --- Document storage: local (blob_dir) | azure (Blob Storage, managed identity or connection string)
    storage_provider: Literal["local", "azure"] = "local"
    storage_account_url: str | None = None  # https://<account>.blob.core.windows.net
    storage_connection_string: str | None = None
    storage_container: str = "documents"

    # Reasoning models (o-series, GPT-5 family, GPT-6) reject a custom temperature and spend extra "reasoning" tokens.
    # auto: detect from the deployment/model name; yes/no: force. reasoning_effort (low|medium|high) is sent only when set.
    llm_reasoning: Literal["auto", "yes", "no"] = "auto"
    llm_reasoning_effort: Literal["low", "medium", "high"] | None = None

    # --- Runs
    run_async: bool = True  # POST /cases/{id}/run returns 202 and runs in the background
    llm_max_retries: int = 3

    # --- Cost (USD per 1M tokens: [input, output, cached_input]); matched by prefix of the model/deployment name.
    # List prices as configured on 2026-09 - check the Azure OpenAI pricing page and override with
    # ONBOARDIQ_LLM_PRICES='{"gpt-4.1-mini": [0.4, 1.6, 0.1]}' (JSON). Ollama / unknown models cost 0.
    llm_prices: dict[str, list[float]] = Field(default_factory=lambda: {
        "gpt-4.1-mini": [0.40, 1.60, 0.10], "gpt-4.1-nano": [0.10, 0.40, 0.025], "gpt-4.1": [2.00, 8.00, 0.50],
        "gpt-4o-mini": [0.15, 0.60, 0.075], "gpt-4o": [2.50, 10.00, 1.25], "text-embedding-3-small": [0.02, 0.0, 0.0]})

    # --- Observability
    applicationinsights_connection_string: str | None = Field(default=None, validation_alias=AliasChoices(
        "ONBOARDIQ_APPLICATIONINSIGHTS_CONNECTION_STRING", "APPLICATIONINSIGHTS_CONNECTION_STRING"))
    otel_service_name: str = "onboardiq-api"
    log_json: bool = False

    # Screening thresholds (policy: sanctions_screening_procedure.md §3)
    screening_match_threshold: float = 0.90
    screening_possible_threshold: float = 0.75
    # A "confirmed" match needs a secondary identifier (DOB or country) agreeing, not just a name. Name-only hits are
    # "possible" and go to an analyst for disposition (KYC-POL-004 §3) - avoids auto-escalating common names.
    screening_require_corroboration: bool = True
    # DEV/DEMO ONLY: add the fictional demo sanctions entries to the real OFAC lists so the golden cases still exercise
    # the sanctions gate in the cloud. Never enable in production.
    screening_demo_entries: bool = False
    doc_confidence_threshold: float = 0.80
    ubo_threshold_pct: float = 25.0

    # RAG
    rag_top_k: int = 5
    rag_index_path: Path = BASE_DIR / "data" / "policy_index.json"
    policies_dir: Path = BASE_DIR / "app" / "rag" / "policies"

    @property
    def jwks_url(self) -> str:
        return self.entra_jwks_url or f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def issuers(self) -> list[str]:
        if self.entra_issuer:
            return [self.entra_issuer]
        t = self.entra_tenant_id
        return [f"https://login.microsoftonline.com/{t}/v2.0", f"https://sts.windows.net/{t}/"]

    @property
    def llm_ready(self) -> bool:
        """True when a chat-capable model is configured (Azure or Ollama)."""
        if self.llm_provider == "mock":
            return False
        if self.llm_provider == "azure":
            return bool(self.azure_openai_endpoint)
        if self.llm_provider == "ollama":
            return True
        return bool(self.azure_openai_endpoint) or _ollama_reachable(self.ollama_base_url)


def _ollama_reachable(base_url: str) -> bool:
    import httpx
    try:
        return httpx.get(base_url.removesuffix("/v1").rstrip("/") + "/api/tags", timeout=0.5).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.blob_dir.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
    return s
