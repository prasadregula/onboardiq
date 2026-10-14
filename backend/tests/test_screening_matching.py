"""Regression tests for the false positives seen against the real OFAC lists in Azure (Oct 2026)."""
from app.providers.screening import ListEntry, MockScreeningProvider, name_score, normalize_name


def s(a, b):
    return name_score(normalize_name(a), normalize_name(b))


def test_shared_first_name_is_not_a_match():
    assert s("Maria Elena Castillo", "MARIA") < 0.6          # was 1.00 with plain token_set_ratio
    assert s("Daniel Okafor", "DANIEL") < 0.75
    assert s("Sierra Dental Group Inc", "SIERRA") < 0.6


def test_real_variants_still_score_high():
    assert s("Viktor A. Sokolov-Marin", "Viktor Andreyevich Sokolov-Marin") >= 0.9
    assert s("Carlos Enrique Valdez", "Carlos Enrique Valdez Roca") >= 0.9   # missing second surname


def test_vessels_are_not_screened_against_people():
    p = MockScreeningProvider(entries=[ListEntry("Maria Elena Castillo", "OFAC_SDN", "sanctions", entity_type="Vessel")])
    assert p.screen("Maria Elena Castillo") == []


def test_name_only_hit_is_possible_not_confirmed():
    from types import SimpleNamespace

    from app.agent.tools import screening as tool
    from app.config import get_settings
    prov = MockScreeningProvider(entries=[ListEntry("Jonas Brandt Keller", "OFAC_SDN", "sanctions", program="X")])
    party = SimpleNamespace(id="p1", name="Jonas Brandt Keller", role="applicant", dob=None, country_of_residence=None,
                            nationality=None)
    ctx = SimpleNamespace(settings=get_settings(), parties=lambda: [party])
    orig = tool.get_screening_provider
    tool.get_screening_provider = lambda: prov
    try:
        out = tool.screen_parties(ctx, tool.ScreenInput())
    finally:
        tool.get_screening_provider = orig
    assert [h.classification for h in out.hits] == ["possible"]
    assert [f.code for f in out.findings] == ["SCREENING_SANCTIONS_POSSIBLE"]
