"""Untrusted document text (T-01 indirect prompt injection, T-07 data minimisation).

Everything read from an uploaded document is applicant-controlled. Three layers keep it from steering the agent:

1. Detect: before the model sees extracted fields, scan them with
   - deterministic patterns (instruction-like phrases, tool names, chat-template tokens, impossible field lengths), and
   - Azure AI Content Safety Prompt Shields (document attack detection), when ONBOARDIQ_CONTENT_SAFETY_ENDPOINT is set.
   The scan runs inside extract_document, so it uses the extraction agent's identity.
2. Quarantine: a flagged document's text fields are withheld from the model ("[withheld ...]") and the case escalates
   (gate GATE_DOCUMENT_INJECTION). Deterministic checks still read the stored values; an analyst sees the original.
   If Prompt Shields is configured but cannot be reached, the case cannot auto-approve (fail closed).
3. Minimise and mark: what the model does see is redacted (identity numbers reduced to the last 4 characters) and
   wrapped in Azure's document delimiter, with a system-prompt rule that text inside it is data, never instructions.

The model can still only propose: tool calls pass the policy enforcement point and decisions pass the fixed gates.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import get_settings

log = logging.getLogger("onboardiq.untrusted")

WITHHELD = "[withheld: possible instructions found in this document; an analyst will review the original]"

# Longest value a genuine field of each kind should ever have. A 400-character "name" is a signal by itself.
FIELD_LIMITS = {"full_name": 100, "first_name": 60, "last_name": 60, "entity_name": 150, "address": 200,
                "id_number": 40, "tin": 20, "ein": 20, "dob": 20, "expiry": 20, "statement_date": 20,
                "issuing_country": 56, "nationality": 56, "registration_number": 40}
DEFAULT_LIMIT = 300
FULL_TEXT_KEY = "_text"  # the whole OCR text of a real document (Document Intelligence): scanned, never sent to the model

_PATTERNS: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in [
    ("override_instructions", r"\b(ignore|disregard|forget)\b.{0,40}\b(instruction|rule|previous|above|policy|prompt)"),
    ("role_marker", r"\b(system|assistant|developer)\s+(note|prompt|message|instruction)s?\b|^\s*(system|assistant)\s*:"),
    ("persona_change", r"\byou are (now|no longer)\b|\bact as\b"),
    ("skip_controls", r"\b(skip|bypass|disable|do not (run|perform)|don'?t (run|perform))\b.{0,40}"
                      r"\b(screening|sanction|check|verification|review|kyc)"),
    ("force_outcome", r"\bauto[_ -]?approve\b|\bapprove (this|the) (applicant|application|case|customer)\b"
                      r"|\b(mark|rate|set|treat)\b.{0,40}\b(low[_ -]?risk|approved|pre-?verified|pre-?approved)\b"),
    ("tool_name", r"\b(record_decision|screen_parties|rate_risk|extract_document|check_consistency|verify_address)\b"),
    ("template_token", r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?INST\]|###\s*(instruction|system)"),
]]


def _strings(fields: dict[str, Any]) -> dict[str, str]:
    return {k: v for k, v in fields.items() if isinstance(v, str)}


def heuristic_scan(fields: dict[str, Any]) -> list[str]:
    """Reasons a document's extracted fields look like they carry instructions (empty list = nothing found)."""
    reasons = []
    for key, val in _strings(fields).items():
        if key != FULL_TEXT_KEY and len(val) > FIELD_LIMITS.get(key, DEFAULT_LIMIT):
            reasons.append(f"overlong:{key}({len(val)})")
        for name, rx in _PATTERNS:
            if rx.search(val):
                reasons.append(f"{name}:{key}")
    return sorted(set(reasons))


class ShieldUnavailable(Exception):
    pass


def prompt_shields(texts: list[str]) -> list[bool] | None:
    """Azure AI Content Safety Prompt Shields, document attack detection. Returns one flag per text, or None when not
    configured. Keyless: a token for the calling agent's managed identity (the extraction agent here).
    Raises ShieldUnavailable if it is configured but the call fails - callers must fail closed."""
    s = get_settings()
    if not s.content_safety_endpoint or not texts:
        return None
    from ..identity import token_provider
    try:
        token = token_provider("https://cognitiveservices.azure.com/.default")()
        r = httpx.post(f"{s.content_safety_endpoint.rstrip('/')}/contentsafety/text:shieldPrompt",
                       params={"api-version": s.content_safety_api_version},
                       headers={"Authorization": f"Bearer {token}"},
                       json={"userPrompt": "Review the applicant's onboarding documents.",
                             "documents": [t[:10_000] for t in texts]},
                       timeout=15)
        r.raise_for_status()
        return [bool(d.get("attackDetected")) for d in r.json().get("documentsAnalysis", [])]
    except Exception as e:  # noqa: BLE001
        log.warning("Prompt Shields unavailable: %s", e)
        raise ShieldUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e


def document_text(fields: dict[str, Any]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in _strings(fields).items())


# ------------------------------------------------------------------------------------------------ what the model sees
_MASK_KEYS = {"id_number", "tin", "ein", "ssn", "account_number"}


def _mask(v: Any) -> Any:
    s = str(v)
    return f"***{s[-4:]}" if len(s) > 4 else "***"


def redact_for_model(tool: str, out: dict) -> dict:
    """A copy of a tool result with identifiers reduced to their last 4 characters (the model never needs them)."""
    if tool != "extract_document":
        return out
    out = json.loads(json.dumps(out, default=str))
    for d in out.get("documents", []):
        fields = {k: (_mask(v) if k in _MASK_KEYS and v else v) for k, v in (d.get("fields") or {}).items()}
        if fields.get(FULL_TEXT_KEY):  # the model needs the typed fields, not the whole page (and its PII)
            fields[FULL_TEXT_KEY] = f"[full document text not shown to the model: {len(fields[FULL_TEXT_KEY])} characters]"
        d["fields"] = fields
    return out


def tool_message(tool: str, out: dict | None, err: str | None) -> str:
    """Content of the tool message sent back to the model. Document-derived text is wrapped in Azure's document
    delimiter so both the model (system-prompt rule) and Prompt Shields treat it as data."""
    if err:
        return json.dumps({"error": err}, default=str)[:60_000]
    body = json.dumps(redact_for_model(tool, out or {}), default=str)[:60_000]
    if tool == "extract_document":
        return f'""" <documents>\n{body}\n</documents> """'
    return body
