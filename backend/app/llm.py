"""Azure OpenAI wrapper (chat with tools, JSON mode, embeddings) plus an offline mock.

Auth: API key if ONBOARDIQ_AZURE_OPENAI_API_KEY is set, else DefaultAzureCredential (managed identity /
az login) with the Cognitive Services scope.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, get_settings


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {id, name, arguments(dict)}
    raw_message: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0  # prompt tokens served from the provider's prompt cache (billed at a discount)
    tokens_reasoning: int = 0  # hidden reasoning tokens (reasoning models); already included in tokens_out for billing
    latency_ms: int = 0
    model: str = ""


# ---- per-run usage accounting -------------------------------------------------------------------------------
@dataclass
class Usage:
    """Everything a run spent on the model, wherever the calls came from (orchestrator loop, rate_risk grounding,
    embeddings). Collected through `usage_scope()`; nested scopes all see the same calls."""
    llm_calls: int = 0
    embed_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_reasoning: int = 0
    llm_latency_ms: int = 0  # wall time inside model calls (sequential, so it is also the model's share of run latency)
    models: list[str] = field(default_factory=list)

    def add_chat(self, res: "ChatResult") -> None:
        self.llm_calls += 1
        self.tokens_in += res.tokens_in
        self.tokens_out += res.tokens_out
        self.tokens_cached += res.tokens_cached
        self.tokens_reasoning += res.tokens_reasoning
        self.llm_latency_ms += res.latency_ms
        if res.model and res.model not in self.models:
            self.models.append(res.model)

    @property
    def model(self) -> str | None:
        return self.models[0] if self.models else None

    def as_dict(self) -> dict[str, Any]:
        return {"llm_calls": self.llm_calls, "embed_calls": self.embed_calls, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "tokens_cached": self.tokens_cached,
                "tokens_reasoning": self.tokens_reasoning, "llm_latency_ms": self.llm_latency_ms,
                "model": self.model}


_usage_sinks: contextvars.ContextVar[tuple[Usage, ...]] = contextvars.ContextVar("onboardiq_usage", default=())


@contextmanager
def usage_scope() -> Iterator[Usage]:
    """Collect model usage for everything executed inside the block (thread/async safe via contextvars)."""
    u = Usage()
    token = _usage_sinks.set(_usage_sinks.get() + (u,))
    try:
        yield u
    finally:
        _usage_sinks.reset(token)


def _record_chat(res: ChatResult) -> None:
    for u in _usage_sinks.get():
        u.add_chat(res)


def _record_embed() -> None:
    for u in _usage_sinks.get():
        u.embed_calls += 1


REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")


def is_reasoning_model(name: str | None, setting: str = "auto") -> bool:
    """Reasoning models reject a custom temperature. Deployment names are user-chosen, so 'auto' is a best guess from
    the name; set ONBOARDIQ_LLM_REASONING=yes|no when the deployment name does not reveal the model."""
    if setting in ("yes", "no"):
        return setting == "yes"
    n = (name or "").lower()
    return n.startswith(REASONING_PREFIXES) and "chat" not in n


def price_known(model: str | None, prices: dict[str, list[float]] | None = None) -> bool:
    prices = prices if prices is not None else get_settings().llm_prices
    return any((model or "").lower().startswith(k.lower()) for k in prices)


def estimate_cost_usd(model: str | None, tokens_in: int, tokens_out: int, tokens_cached: int = 0,
                      prices: dict[str, list[float]] | None = None) -> float:
    """List-price estimate from the settings price table (USD per 1M tokens: [input, output, cached_input]).
    Matched by longest key that prefixes the model/deployment name; unknown or local models cost 0."""
    prices = prices if prices is not None else get_settings().llm_prices
    key = model or ""
    match = max((k for k in prices if key.lower().startswith(k.lower())), key=len, default=None)
    if match is None:
        return 0.0
    p_in, p_out, p_cached = (list(prices[match]) + [0.0, 0.0, 0.0])[:3]
    uncached = max(0, tokens_in - tokens_cached)
    return round((uncached * p_in + tokens_cached * p_cached + tokens_out * p_out) / 1_000_000, 6)


class BaseLLM:
    name = "base"
    chat_deployment: str = ""

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | dict = "auto", json_mode: bool = False, temperature: float = 0.0) -> ChatResult:
        """Public entry point: times the call and books it into every active usage_scope(). Subclasses implement _chat."""
        t0 = time.perf_counter()
        res = self._chat(messages, tools, tool_choice, json_mode, temperature)
        res.latency_ms = res.latency_ms or int((time.perf_counter() - t0) * 1000)
        res.model = res.model or self.chat_deployment or self.name
        _record_chat(res)
        return res

    def _chat(self, messages: list[dict], tools: list[dict] | None = None,
              tool_choice: str | dict = "auto", json_mode: bool = False, temperature: float = 0.0) -> ChatResult:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> list[list[float]]:
        _record_embed()
        return self._embed(texts)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class MockLLM(BaseLLM):
    """Deterministic stand-in: hashing embeddings, no chat (the scripted orchestrator never calls chat)."""
    name = "mock"
    dim = 256

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for tok in _tokens(t):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0
                vec[(h >> 8) % self.dim] += 0.5
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    def _chat(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0) -> ChatResult:
        raise RuntimeError("MockLLM has no chat model. Set ONBOARDIQ_ORCHESTRATOR_MODE=scripted or configure Azure OpenAI.")


def _tokens(text: str) -> list[str]:
    import re
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2]


class AzureOpenAILLM(BaseLLM):
    name = "azure-openai"

    def __init__(self, s: Settings):
        from openai import AzureOpenAI
        kwargs: dict[str, Any] = {"azure_endpoint": s.azure_openai_endpoint.rstrip("/").removesuffix("/openai/v1"),
                                  "api_version": s.azure_openai_api_version}
        if s.azure_openai_api_key:
            kwargs["api_key"] = s.azure_openai_api_key
        else:
            from .identity import token_provider  # identity chosen per call from the current agent role
            kwargs["azure_ad_token_provider"] = token_provider("https://cognitiveservices.azure.com/.default")
        self.client = AzureOpenAI(**kwargs)
        self.chat_deployment = s.azure_openai_chat_deployment
        self.embedding_deployment = s.azure_openai_embedding_deployment

    def _chat(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0) -> ChatResult:
        from .telemetry import llm_call_span
        with llm_call_span(self.name, self.chat_deployment) as span:
            res = _with_retry(lambda: self._request(messages, tools, tool_choice, json_mode, temperature))
            span.set_attribute("llm.tokens_in", res.tokens_in)
            span.set_attribute("llm.tokens_out", res.tokens_out)
            span.set_attribute("llm.tokens_cached", res.tokens_cached)
            return res

    def _request(self, messages, tools=None, tool_choice="auto", json_mode=False, temperature=0.0) -> ChatResult:
        s = get_settings()
        kwargs: dict[str, Any] = {"model": self.chat_deployment, "messages": messages}
        if is_reasoning_model(self.chat_deployment, s.llm_reasoning):
            if s.llm_reasoning_effort:
                kwargs["reasoning_effort"] = s.llm_reasoning_effort
        else:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc.function.arguments}
            calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        usage = resp.usage
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
        out_details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(out_details, "reasoning_tokens", 0) if out_details is not None else 0
        return ChatResult(content=msg.content, tool_calls=calls, raw_message=msg.model_dump(exclude_none=True),
                          tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                          tokens_out=getattr(usage, "completion_tokens", 0) or 0, tokens_cached=cached or 0,
                          tokens_reasoning=reasoning or 0, model=resp.model or "")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        resp = _with_retry(lambda: self.client.embeddings.create(model=self.embedding_deployment, input=texts))
        return [d.embedding for d in resp.data]


def _with_retry(fn):
    """Retry transient OpenAI errors (429/5xx/connection) with exponential backoff; auth/4xx fail fast."""
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
    attempts = get_settings().llm_max_retries

    @retry(stop=stop_after_attempt(max(1, attempts)), wait=wait_exponential_jitter(initial=1, max=20),
           retry=retry_if_exception_type((RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)), reraise=True)
    def _go():
        return fn()
    return _go()


class OllamaLLM(AzureOpenAILLM):
    """Local Ollama through its OpenAI-compatible endpoint (http://localhost:11434/v1).
    Same chat/tools/JSON/embeddings surface as Azure, so the orchestrator does not know the difference.
    Use a tools-capable model (qwen2.5, llama3.1, mistral-nemo); embeddings via nomic-embed-text (768 dims)."""
    name = "ollama"

    def __init__(self, s: Settings):  # noqa: D107 - deliberately not calling super().__init__
        from openai import OpenAI
        self.client = OpenAI(base_url=s.ollama_base_url, api_key="ollama")
        self.chat_deployment = s.ollama_chat_model
        self.embedding_deployment = s.ollama_embedding_model


_llm: BaseLLM | None = None


def get_llm() -> BaseLLM:
    global _llm
    if _llm is None:
        s = get_settings()
        prov = s.llm_provider
        if prov == "auto":
            prov = "azure" if s.azure_openai_endpoint else ("ollama" if s.llm_ready else "mock")
        _llm = {"azure": lambda: AzureOpenAILLM(s), "ollama": lambda: OllamaLLM(s), "mock": MockLLM}[prov]()
    return _llm


def reset_llm() -> None:
    global _llm
    _llm = None
