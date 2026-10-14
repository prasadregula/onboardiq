"""Sanctions / PEP screening providers.

MockScreeningProvider  bundled demo list (fictional names) - exercises every gate offline.
OfacScreeningProvider  the real OFAC lists loaded from CSV files in ONBOARDIQ_OFAC_DATA_DIR:
                         sdn.csv + alt.csv           Specially Designated Nationals + aliases
                         cons_prim.csv + cons_alt.csv Consolidated non-SDN lists (SSI, FSE, NS-PLC, ...) + aliases
                       Download/refresh with `python -m scripts.refresh_ofac` (daily timer in Container Apps) or
                       POST /admin/screening/refresh after the files change. The public OFAC files carry no PEP data;
                       the demo PEP entries stay in so the PEP gate remains testable until a vendor adapter is added.

Matching: names normalised (diacritics, honorifics, corporate suffixes), rapidfuzz token_set_ratio against a
first-token blocking index so ~20k entries screen in milliseconds; DOB/country agreement adjusts the score.
Thresholds live in Settings (policy KYC-POL-004 §3).
"""
from __future__ import annotations

import csv
import logging
import re
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from ..config import get_settings

log = logging.getLogger("onboardiq.screening")


@dataclass
class ListEntry:
    name: str
    list_name: str  # OFAC_SDN | OFAC_CONSOLIDATED | EU_CONSOLIDATED | UN | PEP | ADVERSE_MEDIA
    hit_type: str  # sanctions | pep | adverse_media
    dob: str | None = None
    country: str | None = None
    program: str | None = None
    entry_id: str | None = None
    entity_type: str | None = None  # Individual | Entity | Vessel | Aircraft
    aliases: list[str] = field(default_factory=list)


@dataclass
class Hit:
    party_name: str
    matched_name: str
    list_name: str
    hit_type: str
    score: float
    program: str | None
    entry_id: str | None
    reasons: list[str]


_HONORIFICS = re.compile(r"\b(mr|mrs|ms|dr|inc|llc|ltd|corp|co|the|sa|ag|gmbh|plc|llp|lp|bv|nv|spa|srl|pty|jsc|ooo|fze|fzc)\b")


def normalize_name(n: str) -> str:
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z0-9 ]", " ", n.lower())
    n = _HONORIFICS.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


class ScreeningProvider:
    name = "base"
    entries: list[ListEntry]
    loaded_at: str | None = None

    def __init__(self):
        self.entries = []
        self._index: dict[str, list[tuple[str, ListEntry]]] = defaultdict(list)  # first token -> (norm alias, entry)
        self._all: list[tuple[str, ListEntry]] = []

    def _build_index(self) -> None:
        self._index.clear()
        self._all = []
        for e in self.entries:
            for alias in [e.name, *e.aliases]:
                norm = normalize_name(alias)
                if not norm:
                    continue
                self._all.append((norm, e))
                for tok in set(norm.split()):
                    if len(tok) >= 3:
                        self._index[tok].append((norm, e))

    def _candidates(self, q: str) -> list[tuple[str, ListEntry]]:
        toks = [t for t in q.split() if len(t) >= 3]
        seen: set[int] = set()
        out = []
        for t in toks:
            for norm, e in self._index.get(t, ()):
                if id(e) not in seen:
                    seen.add(id(e))
                    out.append((norm, e))
        return out if toks else self._all

    def screen(self, name: str, dob: str | None = None, country: str | None = None) -> list[Hit]:
        s = get_settings()
        q = normalize_name(name)
        if not q:
            return []
        best: dict[int, tuple[float, str, ListEntry, list[str]]] = {}
        for norm, e in self._candidates(q):
            if (e.entity_type or "").lower() in _NON_PARTY_TYPES:
                continue  # vessels/aircraft are not customers; their one-word names caused false hits ("MARIA")
            base = name_score(q, norm)
            if base < 0.60:
                continue
            score, reasons = base, [f"name similarity {base:.2f}" + (f" (alias '{norm}')" if norm != normalize_name(e.name) else "")]
            if dob and e.dob:
                if dob == e.dob:
                    score, reasons = min(1.0, score + 0.10), reasons + ["dob match"]
                else:
                    score, reasons = score - 0.15, reasons + ["dob differs"]
            if country and e.country and country.upper() == e.country.upper():
                score, reasons = min(1.0, score + 0.03), reasons + ["country match"]
            if score >= s.screening_possible_threshold and (id(e) not in best or score > best[id(e)][0]):
                best[id(e)] = (score, norm, e, reasons)
        hits = [Hit(party_name=name, matched_name=e.name, list_name=e.list_name, hit_type=e.hit_type, score=round(sc, 3),
                    program=e.program, entry_id=e.entry_id, reasons=r) for sc, _, e, r in best.values()]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:10]


_NON_PARTY_TYPES = {"vessel", "aircraft"}


def _tok(n: str) -> list[str]:
    return [t for t in n.split() if len(t) >= 2]  # initials ("a") don't count towards coverage


def name_score(q: str, cand: str) -> float:
    """Coverage-aware name similarity in [0, 1].

    rapidfuzz token_set_ratio alone scores 1.0 whenever one name's tokens are a subset of the other's, so the applicant
    'Maria Elena Castillo' scored 1.0 against the one-word OFAC entry 'MARIA'. A single shared token is now scaled by
    how much of the longer name it covers; full-name token_sort_ratio is the floor."""
    qt, ct = _tok(q), _tok(cand)
    if not qt or not ct:
        return 0.0
    short, long_ = (qt, ct) if len(qt) <= len(ct) else (ct, qt)
    matched = sum(1 for t in short if any(fuzz.ratio(t, u) >= 85 for u in long_))
    set_r = fuzz.token_set_ratio(q, cand) / 100.0
    # A whole multi-token name contained in the other (e.g. a missing second surname: 'Carlos Enrique Valdez' vs
    # 'Carlos Enrique Valdez Roca') keeps the full subset score; a single shared token is scaled by coverage.
    subset = set_r if (matched >= 2 and matched == len(short)) else set_r * matched / len(long_)
    return max(fuzz.token_sort_ratio(q, cand) / 100.0, subset)


DEMO_ENTRIES = [
    # All names below are fictional. They exist only so the demo and golden cases can exercise each gate.
    ListEntry("Viktor Andreyevich Sokolov-Marin", "OFAC_SDN", "sanctions", dob="1968-03-14", country="RU",
              program="RUSSIA-EO14024", entry_id="SDN-DEMO-0001", aliases=["Viktor Sokolov", "V. A. Sokolov-Marin"]),
    ListEntry("Marin Trading FZE", "OFAC_SDN", "sanctions", country="AE", program="IRAN", entry_id="SDN-DEMO-0002"),
    ListEntry("Ahmad Reza Qasemi-Nejad", "OFAC_SDN", "sanctions", dob="1975-11-02", country="IR", program="IRAN",
              entry_id="SDN-DEMO-0003"),
    ListEntry("Golden Meridian Shipping Ltd", "EU_CONSOLIDATED", "sanctions", country="CY", program="EU-RUS",
              entry_id="EU-DEMO-0004"),
    ListEntry("Carlos Enrique Valdez Roca", "UN", "sanctions", dob="1959-07-21", country="VE", program="UN-1718",
              entry_id="UN-DEMO-0005"),
    ListEntry("Delphine Okonkwo-Baptiste", "PEP", "pep", dob="1971-05-30", country="NG", program="Minister of Finance",
              entry_id="PEP-DEMO-0101"),
    ListEntry("Rafael Ignacio Montero", "PEP", "pep", dob="1963-09-09", country="MX", program="State Governor",
              entry_id="PEP-DEMO-0102"),
    ListEntry("Li Wenhao", "PEP", "pep", dob="1966-01-18", country="CN", program="Provincial official",
              entry_id="PEP-DEMO-0103"),
    ListEntry("Northwind Capital Partners LLC", "ADVERSE_MEDIA", "adverse_media", country="US",
              program="Fraud investigation (news)", entry_id="AM-DEMO-0201"),
]


class MockScreeningProvider(ScreeningProvider):
    name = "mock-demo-list"

    def __init__(self, entries: list[ListEntry] | None = None):
        super().__init__()
        self.entries = list(entries or DEMO_ENTRIES)
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        self._build_index()


# --------------------------------------------------------------------------------------------- OFAC CSV parsing
# sdn.csv / cons_prim.csv columns (no header): ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type,
#   Tonnage, GRT, Vess_flag, Vess_owner, Remarks
# alt.csv / cons_alt.csv columns: ent_num, alt_num, alt_type (aka|fka|nka), alt_name, alt_remarks
_DOB_RE = re.compile(r"DOB\s+(\d{1,2}\s+\w{3}\s+\d{4}|\d{4})", re.I)
_NAT_RE = re.compile(r"(?:nationality|citizen)\s+([A-Za-z ]+?)(?:;|$)", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_COUNTRY_CODES = {"russia": "RU", "iran": "IR", "syria": "SY", "north korea": "KP", "korea, north": "KP", "cuba": "CU", "venezuela": "VE",
                  "belarus": "BY", "china": "CN", "united arab emirates": "AE", "turkey": "TR", "lebanon": "LB", "iraq": "IQ",
                  "afghanistan": "AF", "myanmar": "MM", "burma": "MM", "nigeria": "NG", "mexico": "MX", "colombia": "CO"}


def _parse_remarks(remarks: str) -> tuple[str | None, str | None]:
    dob = country = None
    if m := _DOB_RE.search(remarks or ""):
        raw = m.group(1)
        parts = raw.split()
        if len(parts) == 3 and parts[1][:3].lower() in _MONTHS:
            dob = f"{parts[2]}-{_MONTHS[parts[1][:3].lower()]:02d}-{int(parts[0]):02d}"
        elif len(parts) == 1:
            dob = None  # year-only DOBs are too weak to boost/penalise
    if m := _NAT_RE.search(remarks or ""):
        country = _COUNTRY_CODES.get(m.group(1).strip().lower())
    return dob, country


def _load_ofac_pair(prim: Path, alt: Path | None, list_name: str) -> list[ListEntry]:
    aliases: dict[str, list[str]] = defaultdict(list)
    if alt and alt.exists():
        with open(alt, newline="", encoding="utf-8", errors="ignore") as f:
            for row in csv.reader(f):
                if len(row) >= 4 and row[3] and row[3] != "-0-":
                    aliases[row[0]].append(row[3].strip('"'))
    entries: list[ListEntry] = []
    with open(prim, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.reader(f):
            if len(row) < 4 or not row[1] or row[1] == "-0-":
                continue
            name = row[1].strip('"')
            if "," in name and (row[2] or "").lower() == "individual":  # "LAST, FIRST" -> "FIRST LAST"
                last, _, first = name.partition(",")
                name = f"{first.strip()} {last.strip()}"
            remarks = row[11] if len(row) > 11 else ""
            dob, country = _parse_remarks(remarks)
            entries.append(ListEntry(name=name, list_name=list_name, hit_type="sanctions", dob=dob, country=country,
                                     program=(row[3] or "").strip() or None, entry_id=f"{list_name}-{row[0]}",
                                     entity_type=(row[2] or None), aliases=aliases.get(row[0], [])))
    return entries


class OfacScreeningProvider(ScreeningProvider):
    name = "ofac"

    def __init__(self, data_dir: Path, include_demo_pep: bool = True, include_demo_sanctions: bool = False):
        super().__init__()
        self.data_dir = data_dir
        self.include_demo_pep = include_demo_pep
        self.include_demo_sanctions = include_demo_sanctions
        self.load()

    def load(self) -> None:
        t0 = time.perf_counter()
        entries: list[ListEntry] = []
        sdn = self.data_dir / "sdn.csv"
        if sdn.exists():
            entries += _load_ofac_pair(sdn, self.data_dir / "alt.csv", "OFAC_SDN")
        cons = self.data_dir / "cons_prim.csv"
        if cons.exists():
            entries += _load_ofac_pair(cons, self.data_dir / "cons_alt.csv", "OFAC_CONSOLIDATED")
        if not entries:
            raise FileNotFoundError(f"no OFAC files in {self.data_dir}; run `python -m scripts.refresh_ofac`")
        if self.include_demo_pep:
            entries += [e for e in DEMO_ENTRIES if e.hit_type in ("pep", "adverse_media")]
        if self.include_demo_sanctions:  # DEV/DEMO ONLY: fictional entries (ids *-DEMO-*) so the golden cases' gates fire
            entries += [e for e in DEMO_ENTRIES if e.hit_type == "sanctions"]
        self.entries = entries
        self._build_index()
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        log.info("OFAC lists loaded: %d entries (%d aliases) in %.0f ms", len(entries), sum(len(e.aliases) for e in entries),
                 (time.perf_counter() - t0) * 1000)


_provider: ScreeningProvider | None = None
_lock = threading.Lock()


def get_screening_provider() -> ScreeningProvider:
    global _provider
    if _provider is None:
        with _lock:
            if _provider is None:
                s = get_settings()
                if s.screening_provider == "ofac":
                    _provider = OfacScreeningProvider(Path(s.ofac_data_dir), include_demo_sanctions=s.screening_demo_entries)
                else:
                    _provider = MockScreeningProvider()
    return _provider


def refresh_screening_provider() -> ScreeningProvider:
    """Rebuild from disk (after scripts/refresh_ofac.py downloaded new files). Atomic swap."""
    global _provider
    s = get_settings()
    new = OfacScreeningProvider(Path(s.ofac_data_dir), include_demo_sanctions=s.screening_demo_entries) if s.screening_provider == "ofac" else MockScreeningProvider()
    with _lock:
        _provider = new
    return new


def reset_screening_provider() -> None:
    global _provider
    _provider = None
