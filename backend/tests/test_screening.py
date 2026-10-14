from app.providers.screening import MockScreeningProvider, normalize_name


def test_normalize_strips_honorifics_and_diacritics():
    assert normalize_name("Dr. José Álvarez-Núñez, Inc.") == "jose alvarez nunez"


def test_exact_name_and_dob_is_confirmed():
    hits = MockScreeningProvider().screen("Viktor Andreyevich Sokolov-Marin", "1968-03-14", "RU")
    assert hits and hits[0].score >= 0.90 and hits[0].list_name == "OFAC_SDN"


def test_dob_conflict_lowers_to_possible_or_below():
    hits = MockScreeningProvider().screen("Carlos Enrique Valdez Roca", "1984-01-05")
    assert all(h.score < 0.90 for h in hits)


def test_unrelated_name_has_no_hits():
    assert MockScreeningProvider().screen("Maria Elena Castillo", "1988-02-11") == []
