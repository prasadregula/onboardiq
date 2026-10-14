"""Address verification provider. Mock implements structural validation + PO Box + jurisdiction risk.
A USPS / Smarty / Melissa adapter drops in behind the same interface (drop 2)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# FATF high-risk / call-for-action + increased-monitoring style list (illustrative, keep in policy config)
HIGH_RISK_COUNTRIES = {"IR", "KP", "MM", "SY", "RU", "BY", "VE", "AF", "YE", "SS"}
INCREASED_MONITORING = {"NG", "PK", "PH", "ZA", "TR", "VN", "AE", "KE", "MZ", "TZ", "HT", "JM", "ML", "BF", "CM", "CD"}

PO_BOX_RE = re.compile(r"\b(p\.?\s*o\.?\s*box|post office box|pmb|private mailbag)\b", re.I)


@dataclass
class AddressResult:
    input: str
    normalized: str
    deliverable: bool
    is_po_box: bool
    country: str
    jurisdiction_risk: str  # low | increased | high
    provider: str
    notes: list[str] = field(default_factory=list)


class AddressProvider:
    name = "base"

    def verify(self, address: str, country: str | None = None) -> AddressResult:
        raise NotImplementedError


class MockAddressProvider(AddressProvider):
    name = "mock"

    def verify(self, address: str, country: str | None = None) -> AddressResult:
        addr = (address or "").strip()
        country = (country or _guess_country(addr)).upper()
        po = bool(PO_BOX_RE.search(addr))
        has_number = bool(re.search(r"\d", addr))
        has_postal = bool(re.search(r"\b\d{5}(-\d{4})?\b", addr)) if country == "US" else len(addr) > 12
        deliverable = bool(addr) and has_number and has_postal and not addr.lower().startswith("unknown")
        risk = "high" if country in HIGH_RISK_COUNTRIES else "increased" if country in INCREASED_MONITORING else "low"
        notes = []
        if not deliverable:
            notes.append("address failed structural validation (missing street number or postal code)")
        if po:
            notes.append("PO Box - not acceptable as physical/residential address (document_standards.md §4)")
        return AddressResult(input=addr, normalized=re.sub(r"\s+", " ", addr).upper(), deliverable=deliverable,
                             is_po_box=po, country=country, jurisdiction_risk=risk, provider=self.name, notes=notes)


def _guess_country(addr: str) -> str:
    a = addr.upper()
    for token, cc in (("UNITED STATES", "US"), (", USA", "US"), (" US ", "US"), ("CANADA", "CA"), ("MEXICO", "MX"),
                      ("NIGERIA", "NG"), ("IRAN", "IR"), ("RUSSIA", "RU"), ("DUBAI", "AE"), ("UAE", "AE"),
                      ("UNITED KINGDOM", "GB"), ("LONDON", "GB"), ("CHINA", "CN"), ("HONG KONG", "HK"),
                      ("VENEZUELA", "VE"), ("CYPRUS", "CY")):
        if token in a:
            return cc
    return "US" if re.search(r"\b[A-Z]{2}\s+\d{5}(-\d{4})?\b", a) else "XX"


def get_address_provider() -> AddressProvider:
    return MockAddressProvider()
