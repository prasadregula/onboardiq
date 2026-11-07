"""T-07: personal data never reaches the model in the clear.

Everything the model receives (the case brief, every tool result, the risk fact sheet) passes through a per-run
Pseudonymizer first:

1. Known values are replaced deterministically. Every name, date of birth, address, phone number and email address
   in the case (parties, declared data, every document's extracted fields) is collected at the start of the run and
   given a stable token: PERSON_1, DOB_1, ADDRESS_1 ... The same value always gets the same token within a run, so the
   model can still reason ("PERSON_1 on the passport, PERSON_2 on the bill") without seeing either name.
2. Fields that hold personal data by name (full_name, dob, address, email ...) are tokenised even if the value was
   never seen before.
3. Free text that came from a document or the applicant (notes, purpose, any field not known to be safe) is also
   scanned by Azure AI Language PII detection when ONBOARDIQ_PII_ENDPOINT is set, so a name or phone number hidden in
   a free-text field is masked too. Keyless: a token for the calling agent's managed identity. If the service is
   configured but cannot be reached, that free text is WITHHELD rather than sent unmasked (fail closed).

The real values stay where deterministic code and analysts need them: sanctions screening, consistency checks and the
review UI read the stored records, never the model's view. Text the model writes back (decision and risk rationales)
is re-identified before it is stored, so analysts read real names.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

import httpx

from ..config import get_settings

log = logging.getLogger("onboardiq.pii")

# Field name -> token kind. Matching is on the lower-cased key. A bare "name" is deliberately NOT here: risk factors and
# policies also have names; party names are caught as known values instead (seeded from the case).
KEY_KIND = {
    "full_name": "PERSON", "first_name": "PERSON", "last_name": "PERSON", "given_names": "PERSON",
    "surname": "PERSON", "middle_name": "PERSON", "account_holder": "PERSON", "signer": "PERSON",
    "entity_name": "ORG", "legal_name": "ORG", "business_name": "ORG",
    "dob": "DOB", "date_of_birth": "DOB", "birth_date": "DOB",
    "address": "ADDRESS", "address_line1": "ADDRESS", "address_line2": "ADDRESS", "street": "ADDRESS",
    "residential_address": "ADDRESS", "service_address": "ADDRESS", "registered_address": "ADDRESS",
    "phone": "PHONE", "phone_number": "PHONE", "mobile": "PHONE", "email": "EMAIL", "email_address": "EMAIL",
    "filename": "FILE",
}
# Keys whose string values are system-generated (or built by our code from known values): known values are still
# replaced, but they are not sent to the PII service, which keeps a run to a handful of text records.
SAFE_KEYS = {"id", "case_id", "party_id", "document_id", "doc_type", "type", "role", "code", "severity", "source_tool",
             "gate", "outcome", "rating", "band", "policy_ref", "ref", "section", "title", "excerpt", "policy_id",
             "country", "nationality", "country_of_residence", "issuing_country", "applicant_type", "product",
             "industry", "status", "expiry", "statement_date", "currency", "agent_role", "identity", "tool",
             # written by our own code from case data: every personal value in them is already a known value
             "message", "reason", "error", "rationale_hint"}
PII_CATEGORY_KIND = {"Person": "PERSON", "PersonType": None, "PhoneNumber": "PHONE", "Email": "EMAIL",
                     "Address": "ADDRESS", "DateTime": None, "Organization": None, "Quantity": None, "URL": None}
MIN_SCAN_LEN = 25
WITHHELD = "[withheld: personal-data check unavailable]"


class PiiServiceUnavailable(Exception):
    pass


def language_scan(texts: list[str]) -> list[list[dict]]:
    """Azure AI Language PII detection. One entity list per text. Raises PiiServiceUnavailable on any failure."""
    s = get_settings()
    from ..identity import token_provider
    out: list[list[dict]] = []
    try:
        token = token_provider("https://cognitiveservices.azure.com/.default")()
        for i in range(0, len(texts), 5):  # synchronous PII requests take at most 5 documents
            batch = texts[i:i + 5]
            r = httpx.post(f"{s.pii_endpoint.rstrip('/')}/language/:analyze-text",
                           params={"api-version": s.pii_api_version},
                           headers={"Authorization": f"Bearer {token}"},
                           json={"kind": "PiiEntityRecognition", "parameters": {"modelVersion": "latest"},
                                 "analysisInput": {"documents": [{"id": str(n), "language": "en", "text": t}
                                                                 for n, t in enumerate(batch)]}},
                           timeout=20)
            r.raise_for_status()
            res = r.json()["results"]
            if res.get("errors"):
                raise PiiServiceUnavailable(f"document errors: {str(res['errors'])[:200]}")
            by_id = {d["id"]: d.get("entities", []) for d in res["documents"]}
            out += [by_id.get(str(n), []) for n in range(len(batch))]
        return out
    except PiiServiceUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - any failure means "not checked"
        log.warning("PII detection unavailable: %s", e)
        raise PiiServiceUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e


class Pseudonymizer:
    """Per-run, consistent tokenisation of personal data in everything sent to the model."""

    def __init__(self, scan_free_text: bool | None = None):
        self._by_value: dict[str, str] = {}   # normalised value -> token
        self._by_token: dict[str, str] = {}   # token -> original value (first seen)
        self._counts: dict[str, int] = {}
        self._scan_cache: dict[str, str] = {}
        self._patterns: dict[str, re.Pattern] = {}
        self.scan_enabled = bool(get_settings().pii_endpoint) if scan_free_text is None else scan_free_text
        self.stats = {"values_known": 0, "tokens_replaced": 0, "texts_scanned": 0, "entities_found": 0,
                      "texts_withheld": 0, "service": "azure-ai-language" if self.scan_enabled else "off"}

    # ------------------------------------------------------------------ registry
    @staticmethod
    def _norm(v: str) -> str:
        return re.sub(r"\s+", " ", str(v)).strip().lower()

    def token(self, kind: str, value: Any) -> str:
        v = str(value).strip()
        if not v:
            return v
        key = self._norm(v)
        if key not in self._by_value:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            tok = f"{kind}_{self._counts[kind]}"
            self._by_value[key] = tok
            self._by_token[tok] = v
            self.stats["values_known"] += 1
        return self._by_value[key]

    def seed(self, kind: str, values: Iterable[Any]) -> None:
        for v in values:
            if v is not None and str(v).strip():
                self.token(kind, v)

    def seed_fields(self, fields: dict | None) -> None:
        for k, v in (fields or {}).items():
            kind = KEY_KIND.get(str(k).lower())
            if kind and isinstance(v, (str, int)) and kind != "FILE":
                self.token(kind, v)

    @classmethod
    def for_case(cls, ctx) -> "Pseudonymizer":
        p = cls()
        for party in ctx.parties():
            p.token("ORG" if party.role == "entity" else "PERSON", party.name)
            p.seed("DOB", [party.dob])
        p.seed_fields(ctx.case.declared)
        for d in ctx.documents():
            for src in (d.extracted, d.mock_extraction):
                p.seed_fields((src or {}).get("fields"))
        return p

    # ------------------------------------------------------------------ masking
    def scrub_text(self, text: str) -> str:
        """Replace every known value inside free text (longest first, case-insensitive, whole words only)."""
        out = text
        for key in sorted(self._by_value, key=len, reverse=True):
            if len(key) < 3:
                continue
            pat = self._patterns.get(key)
            if pat is None:
                pat = self._patterns[key] = re.compile(
                    r"(?<![\w])" + r"\s+".join(map(re.escape, key.split(" "))) + r"(?![\w])", re.I)
            out, n = pat.subn(self._by_value[key], out)
            self.stats["tokens_replaced"] += n
        return out

    def _key_value(self, key: str, value: Any) -> Any:
        kind = KEY_KIND.get(key.lower())
        if kind == "FILE":
            return "[file name not shown]"
        if kind and isinstance(value, str) and value.startswith("[withheld"):
            return value  # a control's marker, not personal data: keep it visible to the model
        if kind and isinstance(value, (str, int)) and str(value).strip():
            self.stats["tokens_replaced"] += 1
            return self.token(kind, value)
        return None

    def _walk(self, obj: Any, key: str, pending: list) -> Any:
        if isinstance(obj, dict):
            return {k: self._walk(v, str(k), pending) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._walk(v, key, pending) for v in obj]
        if isinstance(obj, str):
            masked = self._key_value(key, obj)
            if masked is not None:
                return masked
            text = self.scrub_text(obj)
            if (self.scan_enabled and key.lower() not in SAFE_KEYS and len(text) >= MIN_SCAN_LEN and " " in text
                    and not text.startswith("[")):
                holder = {"text": text}
                pending.append(holder)
                return holder
            return text
        return obj

    def _resolve(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            if "__pii_holder__" in obj:
                return obj["value"]
            return {k: self._resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve(v) for v in obj]
        return obj

    def minimise(self, obj: Any) -> Any:
        """A copy of obj that is safe to send to the model."""
        pending: list[dict] = []
        tree = self._walk(obj, "", pending)
        if pending:
            todo = [h["text"] for h in pending if h["text"] not in self._scan_cache]
            if todo:
                try:
                    found = language_scan(todo)
                    self.stats["texts_scanned"] += len(todo)
                    for text, entities in zip(todo, found):
                        masked = text
                        for e in sorted(entities, key=lambda e: len(e.get("text", "")), reverse=True):
                            kind = PII_CATEGORY_KIND.get(e.get("category"), e.get("category", "PII").upper())
                            if kind and e.get("confidenceScore", 1) >= 0.6 and e.get("text"):
                                masked = masked.replace(e["text"], self.token(kind, e["text"]))
                                self.stats["entities_found"] += 1
                        self._scan_cache[text] = masked
                except PiiServiceUnavailable:
                    for text in todo:
                        self._scan_cache[text] = WITHHELD
                        self.stats["texts_withheld"] += 1
            for h in pending:
                h["__pii_holder__"] = True
                h["value"] = self._scan_cache[h["text"]]
        return self._resolve(tree)

    # ------------------------------------------------------------------ back to the analyst
    def reidentify(self, text: str | None) -> str | None:
        if not text:
            return text
        return re.sub(r"\b(PERSON|ORG|DOB|ADDRESS|PHONE|EMAIL)_\d+\b",
                      lambda m: self._by_token.get(m.group(0), m.group(0)), text)
