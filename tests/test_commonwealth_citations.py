"""Commonwealth neutral citations and law reporters — the court registry's collision
and bucket handling, the reporter jurisdiction rules, and the citation forms that break
the generic grammar. Network-free.
"""

from __future__ import annotations

import pytest

from raglex.citations.commonwealth import HK_REGISTRY_PREFIXES, hk_registry_court
from raglex.citations.courts import (
    AMBIGUOUS_CODES,
    COURTS,
    COURTS_BY_CODE,
    KNOWN_COURTS,
    LII_ASSIGNED,
    classify,
    lookup,
    lookup_prefix,
)
from raglex.citations.extractor import extract_citations
from raglex.citations.grammars import REPORT_SERIES
from raglex.citations.reporters import (
    COLLIDING_ABBREVS,
    WIDE_TRAVELLING,
    reporter_jurisdiction,
)
from raglex.citations.taxonomy import CATEGORY_LABELS, JURISDICTION_CATEGORY


def candidates(text: str) -> dict[str, str | None]:
    return {c.raw.strip(): c.candidate_id for c in extract_citations(text)}


# -- the court registry ------------------------------------------------------

def test_every_registered_jurisdiction_has_a_corpus_map_bucket():
    """A court with no category would land its citations in "Other / unrouted",
    which is the outcome the buckets exist to prevent."""
    non_uk = {c.jurisdiction for c in COURTS} - {"GB"}
    missing = {j for j in non_uk if j not in JURISDICTION_CATEGORY}
    assert not missing, f"jurisdictions with no Corpus Map bucket: {sorted(missing)}"
    for category in JURISDICTION_CATEGORY.values():
        assert category in CATEGORY_LABELS


def test_colliding_codes_are_disambiguated_by_bracket_style():
    """FCA is the Federal Court of Australia AND the Federal Court of Appeal of Canada.
    Only the brackets say which: [2020] FCA 1 vs 2020 FCA 1."""
    assert "FCA" in AMBIGUOUS_CODES
    assert lookup("FCA", bracketed=True).jurisdiction == "AU"
    assert lookup("FCA", bracketed=False).jurisdiction == "CA"
    # both registrations survive — the old code-keyed dict silently dropped one
    assert len(COURTS_BY_CODE["FCA"]) == 2


def test_lookup_without_a_bracket_hint_stays_backward_compatible():
    assert lookup("FCA") is KNOWN_COURTS["FCA"]
    assert lookup("NZSC").jurisdiction == "NZ"
    assert lookup("nope") is None


def test_unknown_tribunal_falls_back_to_its_jurisdiction_bucket():
    """The MNC convention puts the ISO country code first, so an unregistered tribunal
    is still placeable — Kenyan case law, not an unplaceable unknown."""
    bucket = classify("KEELRC2")
    assert bucket.jurisdiction == "KE" and bucket.generic is True
    assert classify("NZXYZ").jurisdiction == "NZ"
    assert classify("ZAFOOHC").jurisdiction == "ZA"


def test_bucket_lookup_refuses_to_guess_when_nothing_matches():
    """A token with no known prefix must stay unknown so it keeps surfacing in the
    snowball rather than being mislabelled."""
    assert lookup_prefix("ZZZZ") is None
    assert classify("QQQ") is None


def test_exact_court_beats_its_jurisdiction_bucket():
    exact = classify("KESC")
    assert exact.generic is False and exact.name == "Supreme Court of Kenya"


def test_lii_assigned_identifiers_are_flagged_as_such():
    """Laws.Africa/PacLII mint neutral-LOOKING ids that no court issued — presenting
    them as court-issued would overstate their authority."""
    assert lookup("KESC").authority == LII_ASSIGNED
    assert lookup("FJHC").authority == LII_ASSIGNED
    assert lookup("CanLII", bracketed=False).authority == LII_ASSIGNED
    assert lookup("NZSC").authority != LII_ASSIGNED


def test_generic_buckets_are_excluded_from_the_irish_court_slugs():
    from raglex.citations.courts import IRISH_COURTS
    assert "iesc" in IRISH_COURTS and "iehc" in IRISH_COURTS
    assert not any("*" in slug for slug in IRISH_COURTS)


# -- courts must never be suppressed as report series ------------------------

def test_a_court_code_is_never_folded_into_the_reporter_rejection_set():
    """Regression: SGCA was catalogued as a report series, so the neutral grammar
    rejected it as a "reporter" and every [YEAR] SGCA n minted no candidate — the
    entire Singapore Court of Appeal was invisible."""
    assert "SGCA" not in REPORT_SERIES
    assert candidates("[2011] SGCA 9")["[2011] SGCA 9"] == "sgca/2011/9"
    for code in COURTS_BY_CODE:
        assert code not in REPORT_SERIES, f"court {code} suppressed as a report series"


def test_session_cases_is_still_rejected_as_a_court():
    """The fold must keep doing its job: "1999 SC 583" is Session Cases, not a court."""
    assert "SC" in REPORT_SERIES
    assert candidates("Smith v Brown 1999 SC 583").get("1999 SC 583") is None


# -- reporters ---------------------------------------------------------------

def test_wide_travelling_series_imply_no_jurisdiction():
    """"[2015] 3 LRC 1" names the series, not the country — inferring one is wrong."""
    for series in ("LRC", "BHRC", "WIR", "EA", "MLJ"):
        assert series in WIDE_TRAVELLING
        assert reporter_jurisdiction(series) is None


def test_jurisdiction_tied_series_resolve_to_their_country():
    assert reporter_jurisdiction("NWLR") == "NG"
    assert reporter_jurisdiction("NZLR") == "NZ"
    assert reporter_jurisdiction("HKCFAR") == "HK"
    assert reporter_jurisdiction("CLR") == "AU"
    assert reporter_jurisdiction("DLR") == "CA"


def test_colliding_abbreviations_refuse_to_name_a_jurisdiction():
    """SA is both the South African Law Reports and South Australia; SCC is the
    Supreme Court of Canada and India's dominant reporter."""
    for token in ("SA", "SC", "IR", "SCC"):
        assert token in COLLIDING_ABBREVS
        assert reporter_jurisdiction(token) is None


# -- citation forms that break the generic grammar ---------------------------

def test_canadian_neutral_citation_is_bracketless_and_year_first():
    assert candidates("Kerr v Baranow, 2011 SCC 10")["2011 SCC 10"] == "scc/2011/10"


def test_canlii_identifier_mints_a_candidate():
    """CanLII ids are stable and addressable, unlike a printed page reference."""
    got = candidates("R v Miller, 1998 CanLII 5115 (ONCA)")
    assert got["1998 CanLII 5115 (ONCA)"] == "canlii/1998/5115"


def test_indian_colon_neutral_citation():
    got = candidates("See 2023:DHC:1234 and 2023 INSC 445.")
    assert got["2023:DHC:1234"] == "dhc/2023/1234"
    assert got["2023 INSC 445"] == "insc/2023/445"


def test_air_beats_the_bracketless_neutral_read_of_its_tail():
    """Without the AIR rule the trailing "1973 SC 1461" parses as a neutral citation
    whose court is Session Cases — an Indian reporter read as Scottish."""
    got = candidates("Kesavananda AIR 1973 SC 1461 at 1500.")
    assert "AIR 1973 SC 1461" in got and got["AIR 1973 SC 1461"] is None


def test_south_african_report_shape_and_its_parallel_neutral_citation():
    got = candidates("S v Makwanyane 1995 (3) SA 391 (CC); [1995] ZACC 3")
    assert got["1995 (3) SA 391 (CC)"] is None      # a printed page — not fetchable
    assert got["[1995] ZACC 3"] == "zacc/1995/3"    # the neutral citation resolves


def test_nigerian_nwlr_part_format():
    got = candidates("Abacha v Fawehinmi (2019) 12 NWLR (Pt 1685) 1")
    assert "(2019) 12 NWLR (Pt 1685) 1" in got


def test_eklr_is_recognised_as_a_database_identifier():
    """[2019] eKLR has no sequence number at all — it is neither a neutral citation
    nor a page reference, and must not be parsed as either."""
    got = candidates("Mwangi v Republic [2019] eKLR")
    assert got["[2019] eKLR"] is None


@pytest.mark.parametrize("text,number", [
    ("Re FACV 1/2018", "FACV 1/2018"),
    ("HCA 18515/1999 was heard first", "HCA 18515/1999"),
    ("CACV 123/2015", "CACV 123/2015"),
])
def test_hong_kong_registry_numbers_are_not_read_as_neutral_citations(text, number):
    """The /YYYY suffix is the branch signal. "HCA 18515/1999" must never resolve as
    the High Court of Australia."""
    got = candidates(text)
    assert number in got and got[number] is None


def test_hk_registry_prefix_maps_to_its_court():
    assert hk_registry_court("FACV 1/2018") == "HKCFA"
    assert hk_registry_court("DCCJ 900/2020") == "HKDC"
    assert hk_registry_court("XXXX 1/2020") is None
    assert set(HK_REGISTRY_PREFIXES.values()) <= set(COURTS_BY_CODE)


def test_parallel_commonwealth_citations_both_survive():
    """A judgment line gives the neutral citation then its reported parallels; the
    neutral one must resolve and the reporter stay candidate-less."""
    got = candidates("ABC v DEF (2018) 21 HKCFAR 123, [2018] HKCFA 1")
    assert got["[2018] HKCFA 1"] == "hkcfa/2018/1"
    assert got["(2018) 21 HKCFAR 123"] is None


@pytest.mark.parametrize("citation,expected", [
    ("[2020] NZSC 12", "nzsc/2020/12"),
    ("[2011] SGHC 12", "sghc/2011/12"),
    ("[1995] ZACC 3", "zacc/1995/3"),
    ("[2020] KESC 1", "kesc/2020/1"),
    ("[2020] FJHC 5", "fjhc/2020/5"),
    ("[2021] GHASC 12", "ghasc/2021/12"),
    ("[2019] TZHC 3", "tzhc/2019/3"),
    ("[2020] JMCA 5", "jmca/2020/5"),
])
def test_bracketed_commonwealth_neutral_citations_resolve(citation, expected):
    assert candidates(citation)[citation] == expected
