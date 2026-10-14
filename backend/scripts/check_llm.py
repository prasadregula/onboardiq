"""Smoke-test the configured model provider before running the eval in llm mode.

    python -m scripts.check_llm

Prints which provider/deployment ONBOARDIQ_* resolves to, then makes one tiny chat call (with a tool offered, to
prove tool calling works), one JSON-mode call and one embedding call, reporting the real model version, tokens,
cached tokens, latency and list-price cost. Exit 1 on any failure with the likely cause.
"""
from __future__ import annotations

import sys
import time

from app.config import get_settings
from app.llm import estimate_cost_usd, get_llm, reset_llm, usage_scope

PING_TOOL = [{"type": "function", "function": {
    "name": "ping", "description": "Reply to a connectivity check.",
    "parameters": {"type": "object", "properties": {"echo": {"type": "string"}}, "required": ["echo"]}}}]


def main() -> int:
    s = get_settings()
    prov = s.llm_provider
    if prov == "auto":
        prov = "azure" if s.azure_openai_endpoint else ("ollama" if s.llm_ready else "mock")
    print(f"provider           {prov}  (ONBOARDIQ_LLM_PROVIDER={s.llm_provider})")
    if prov == "azure":
        print(f"endpoint           {s.azure_openai_endpoint}")
        print(f"auth               {'api key' if s.azure_openai_api_key else 'DefaultAzureCredential (az login / managed identity)'}")
        print(f"chat deployment    {s.azure_openai_chat_deployment}   api-version {s.azure_openai_api_version}")
        print(f"embed deployment   {s.azure_openai_embedding_deployment}")
    elif prov == "ollama":
        print(f"base url           {s.ollama_base_url}")
        print(f"chat model         {s.ollama_chat_model}   embed model {s.ollama_embedding_model}")
    else:
        print("No chat model configured: set ONBOARDIQ_LLM_PROVIDER=azure + ONBOARDIQ_AZURE_OPENAI_ENDPOINT, or =ollama.")
        return 1
    print(f"orchestrator mode  {s.orchestrator_mode}   llm_ready {s.llm_ready}")

    reset_llm()
    llm = get_llm()
    ok = True
    with usage_scope() as usage:
        # 1. plain chat + tool offered
        try:
            t0 = time.perf_counter()
            res = llm.chat([{"role": "system", "content": "You are a connectivity check. Call the ping tool with echo='pong'."},
                            {"role": "user", "content": "ping"}], tools=PING_TOOL, tool_choice="auto")
            ms = int((time.perf_counter() - t0) * 1000)
            called = [c["name"] for c in res.tool_calls]
            print(f"chat+tools         OK  model={res.model} latency={ms} ms tokens in/out={res.tokens_in}/{res.tokens_out} "
                  f"cached={res.tokens_cached} tool_calls={called or 'none (model answered in text)'}")
            if "ping" not in called:
                print("                   WARNING: model did not call the tool; the orchestrator's llm mode needs tool calling.")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"chat+tools         FAIL {type(e).__name__}: {e}")
            _hint(e)
        # 2. JSON mode (used by rate_risk grounding)
        try:
            t0 = time.perf_counter()
            res = llm.chat([{"role": "system", "content": "Reply with JSON only."},
                            {"role": "user", "content": 'Return {"status": "ok"}'}], json_mode=True)
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"chat json_mode     OK  latency={ms} ms content={(res.content or '').strip()[:60]!r}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"chat json_mode     FAIL {type(e).__name__}: {e}")
        # 3. embeddings (policy index)
        try:
            t0 = time.perf_counter()
            vec = llm.embed(["customer identification program"])[0]
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"embed              OK  dims={len(vec)} latency={ms} ms")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"embed              FAIL {type(e).__name__}: {e}")
            _hint(e)
    cost = estimate_cost_usd(usage.model, usage.tokens_in, usage.tokens_out, usage.tokens_cached)
    print(f"usage              {usage.llm_calls} chat calls, {usage.embed_calls} embed calls, "
          f"{usage.tokens_in}/{usage.tokens_out} tokens, est ${cost:.6f}")
    print("RESULT             " + ("READY - run: python -m eval.run_eval --mode llm --compare eval/reports/latest-scripted.json"
                                  if ok else "NOT READY"))
    return 0 if ok else 1


def _hint(e: Exception) -> None:
    msg = str(e).lower()
    if "401" in msg or "unauthorized" in msg or "credential" in msg:
        print("                   hint: key wrong/missing, or `az login` identity lacks 'Cognitive Services OpenAI User' on the resource")
    elif "404" in msg or "deploymentnotfound" in msg:
        print("                   hint: deployment name does not exist on this resource (check the Foundry portal > Deployments)")
    elif "429" in msg:
        print("                   hint: rate limited - raise the deployment's TPM quota or retry")
    elif "connection" in msg or "name or service not known" in msg or "timed out" in msg:
        print("                   hint: endpoint unreachable - check the URL (no /openai/v1 suffix), firewall, or that Ollama is running")


if __name__ == "__main__":
    sys.exit(main())
