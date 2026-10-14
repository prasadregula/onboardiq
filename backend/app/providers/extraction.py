"""Document extraction providers.

Normalised output (every provider returns this shape) so tools never care where it came from:
{
  "doc_type": "...", "fields": {"full_name": ..., "dob": ..., "id_number": ..., "expiry": ..., "address": ...,
                                 "issuing_country": ..., "entity_name": ..., "owners": [...]},
  "confidence": 0.0-1.0, "provider": "mock|azure-document-intelligence", "model": "...", "pages": n
}
"""
from __future__ import annotations

from typing import Any

from ..config import Settings, get_settings
from ..models import Document

ID_DOC_TYPES = {"passport", "drivers_license", "national_id"}
DI_MODEL_FOR = {
    "passport": "prebuilt-idDocument",
    "drivers_license": "prebuilt-idDocument",
    "national_id": "prebuilt-idDocument",
    "utility_bill": "prebuilt-read",
    "bank_statement": "prebuilt-layout",
    "articles_of_incorporation": "prebuilt-layout",
    "ownership_declaration": "prebuilt-layout",
    "ein_letter": "prebuilt-read",
}


class ExtractionProvider:
    name = "base"

    def extract(self, doc: Document) -> dict[str, Any]:
        raise NotImplementedError


class MockExtractionProvider(ExtractionProvider):
    """Returns the fixture attached to the document (Document.mock_extraction)."""
    name = "mock"

    def extract(self, doc: Document) -> dict[str, Any]:
        fx = dict(doc.mock_extraction or {})
        if not fx:
            return {"doc_type": doc.doc_type, "fields": {}, "confidence": 0.0, "provider": self.name,
                    "model": "fixture", "pages": 0, "error": "no fixture and no OCR provider configured"}
        return {"doc_type": doc.doc_type, "fields": fx.get("fields", {}), "confidence": float(fx.get("confidence", 0.95)),
                "provider": self.name, "model": "fixture", "pages": int(fx.get("pages", 1))}


class AzureDocumentIntelligenceProvider(ExtractionProvider):
    name = "azure-document-intelligence"

    def __init__(self, s: Settings):
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        if s.doc_intel_api_key:
            from azure.core.credentials import AzureKeyCredential
            cred = AzureKeyCredential(s.doc_intel_api_key)
        else:
            from ..identity import credential_for
            cred = credential_for("extraction")
        self.client = DocumentIntelligenceClient(endpoint=s.doc_intel_endpoint, credential=cred)

    def _analyze(self, model_id: str, data: bytes):
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.core.exceptions import HttpResponseError, ServiceRequestError
        from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10),
               retry=retry_if_exception_type((ServiceRequestError, HttpResponseError)), reraise=True)
        def _go():
            return self.client.begin_analyze_document(model_id, AnalyzeDocumentRequest(bytes_source=data)).result()
        return _go()

    def extract(self, doc: Document) -> dict[str, Any]:
        from .storage import get_storage
        model_id = DI_MODEL_FOR.get(doc.doc_type, "prebuilt-read")
        storage = get_storage()
        data = storage.get(doc.blob_uri) if doc.blob_uri and storage.exists(doc.blob_uri) else None
        if data is None:
            return {"doc_type": doc.doc_type, "fields": {}, "confidence": 0.0, "provider": self.name,
                    "model": model_id, "pages": 0, "error": "blob not found"}
        result = self._analyze(model_id, data)
        fields: dict[str, Any] = {}
        confs: list[float] = []
        if result.documents:
            d = result.documents[0]
            for key, f in (d.fields or {}).items():
                val = getattr(f, "value_string", None) or getattr(f, "value_date", None) or getattr(f, "content", None)
                fields[_norm_key(key)] = str(val) if val is not None else None
                if f.confidence is not None:
                    confs.append(float(f.confidence))
            if d.confidence is not None:
                confs.append(float(d.confidence))
        fields["_text"] = (result.content or "")[:20000]
        conf = min(confs) if confs else (0.9 if result.content else 0.0)
        return {"doc_type": doc.doc_type, "fields": fields, "confidence": conf, "provider": self.name,
                "model": model_id, "pages": len(result.pages or [])}


_KEYMAP = {"FirstName": "first_name", "LastName": "last_name", "DocumentNumber": "id_number",
           "DateOfBirth": "dob", "DateOfExpiration": "expiry", "CountryRegion": "issuing_country",
           "Address": "address", "Nationality": "nationality"}


def _norm_key(k: str) -> str:
    return _KEYMAP.get(k, k.lower())


def get_extraction_provider() -> ExtractionProvider:
    s = get_settings()
    if s.extraction_provider == "azure" and s.doc_intel_endpoint:
        return AzureDocumentIntelligenceProvider(s)
    return MockExtractionProvider()
