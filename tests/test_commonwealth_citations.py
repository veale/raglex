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


def test_retrieval_buckets_split_commonwealth_by_country():
    """The Westlaw/Lexis export filter buckets per country now, so an Australian FCR /
    SR (NSW) or a Canadian OR doesn't leak into a UK-only batch under one "commonwealth"
    lump (the reported jurisdiction-leak bug)."""
    from raglex.facade import (
        RETRIEVAL_JURISDICTIONS,
        _CATEGORY_JURISDICTION,
        _candidate_jurisdiction,
        _retrieval_bucket,
    )
    from raglex.citations.reporters import report_series, series_jurisdiction

    keys = {k for k, _ in RETRIEVAL_JURISDICTIONS}
    assert "commonwealth" not in keys
    assert {"ca", "au", "nz", "sg", "hk", "za", "us"} <= keys
    # a report series resolves to its own country, folded into the picker vocabulary
    for raw, want in [("(1993) 43 FCR 280", "au"), ("(1946) 46 SR (NSW) 318", "au"),
                      ("[1932] O.R. 675", "ca"), ("[2008] 2 NZLR 321", "nz")]:
        jur = _retrieval_bucket(series_jurisdiction(report_series(raw), raw))
        assert jur == want, (raw, jur)
    # a neutral citation with no series resolves off the candidate's court token
    assert _candidate_jurisdiction("hkcfa/2003/46") == "hk"
    assert _candidate_jurisdiction("nswca/2014/17") == "au"
    # every category bucket is a valid picker key (the long tail folds to a region)
    for bucket in _CATEGORY_JURISDICTION.values():
        assert _retrieval_bucket(bucket) in keys


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


# -- Corpus Map routing for the legislation registers ------------------------

@pytest.mark.parametrize("source,stable_id,category,subtype_label", [
    ("ca-federal", "ca/act/a-1", "ca-legislation", "Acts"),
    ("ca-federal", "ca/regulation/crc-c-870", "ca-legislation", "Regulations"),
    ("hk-legislation", "hk/cap/486", "hk-legislation",
     "Ordinances & subsidiary legislation"),
    ("hk-legislation", "hk/instrument/a101", "hk-legislation",
     "Constitutional instruments"),
    ("nz-legislation", "nz/act/public/1990/109", "nz-legislation", "Acts"),
    # Australia is nine registers under one banner, so it splits by jurisdiction
    ("au-cth", "au/cth/act/1901/2", "au-legislation", "Commonwealth"),
    ("au-qld", "au/qld/sl/2023/107", "au-legislation", "Queensland"),
])
def test_commonwealth_registers_route_out_of_other_unrouted(
        source, stable_id, category, subtype_label):
    """Without a category these registers land in "Other / unrouted", which hid
    several thousand held documents on the first deployment."""
    from raglex.citations.taxonomy import classify_document
    tax = classify_document(source=source, stable_id=stable_id)
    assert tax.category == category
    assert tax.subtype_label == subtype_label


# -- Canadian legislation citations -----------------------------------------

def test_canadian_consolidated_statute_resolves_to_its_chapter_code():
    """R.S.C. chapter codes ARE the consolidated id — "R.S.C. 1985, c. C-46" is the
    Criminal Code, held as ca/act/c-46."""
    assert candidates("under the Criminal Code, R.S.C. 1985, c. C-46")[
        "R.S.C. 1985, c. C-46"] == "ca/act/c-46"
    assert candidates("R.S., c. I-23")["R.S., c. I-23"] == "ca/act/i-23"


def test_canadian_statute_pinpoint_is_captured():
    cites = {c.raw.strip(): c for c in extract_citations(
        "s. 8 of the Privacy Act, R.S.C., 1985, c. P-21")}
    c = next(iter(cites.values()))
    assert c.candidate_id == "ca/act/p-21" and c.pinpoint == "s. 8"


def test_canadian_annual_statute_is_candidateless_pending_its_alias():
    """The annual chapter number ("c. 18") is not the consolidated id, so it resolves
    only via the alias the ca-federal import mints — candidate-less at extraction."""
    got = candidates("enacted by S.C. 2019, c. 18, s. 2")
    assert "S.C. 2019, c. 18" in got and got["S.C. 2019, c. 18"] is None


def test_canadian_regulations_resolve_including_the_french_series_names():
    assert candidates("made under SOR/2018-69")["SOR/2018-69"] == "ca/regulation/sor-2018-69"
    # DORS/TR are the French names for SOR/SI — the same instrument
    assert candidates("DORS/2002-227")["DORS/2002-227"] == "ca/regulation/sor-2002-227"
    assert candidates("SI/2005-91")["SI/2005-91"] == "ca/regulation/si-2005-91"


def test_provincial_and_reporter_lookalikes_are_not_matched_as_federal_statutes():
    """"R.S.O. 1990, c. P.33" is Ontario (letter-DOT chapter), and "1999 S.C. 583" is
    Session Cases — neither is a federal statute citation."""
    assert candidates("R.S.O. 1990, c. P.33").get("R.S.O. 1990, c. P.33") is None
    # the S.C. reporter has no ", c. N", so the annual grammar can't grab it
    got = candidates("Brown v Gray 1999 S.C. 583")
    assert all(not (v or "").startswith("ca/act") for v in got.values())


def test_ca_federal_mints_annual_citation_aliases():
    from raglex.adapters.ca_legislation import _annual_aliases
    assert _annual_aliases("2019, c. 10") == ["S.C. 2019, c. 10", "L.C. 2019, c. 10"]
    assert _annual_aliases("A-1") == []          # a consolidated code, not an annual number
    assert _annual_aliases("") == []


# -- Australian legislation citations ---------------------------------------

def test_australian_statute_citation_is_name_only_and_captures_juris_and_pinpoint():
    """Australian registers publish the act NUMBER, not the citation, so there is no id to
    build — the reference stays name-only and resolves by title against harvested AU
    legislation. The (Juris) tag is consumed so the match beats the generic UK grammar."""
    cites = {c.raw.strip(): c for c in extract_citations("s 61 of the Crimes Act 1900 (NSW)")}
    c = next(iter(cites.values()))
    assert c.candidate_id is None and c.entity_kind == "act" and c.pinpoint == "s. 61"
    assert "(NSW)" in list(cites)[0]   # the jurisdiction tag is part of the match


def test_australian_citation_does_not_misresolve_to_a_same_named_uk_act():
    """Without the AU grammar, "Companies Act 2006 (Cth)" would hit the UK statute
    gazetteer and wrongly mint ukpga/2006/46. The consumed (Cth) tag keeps it off the
    gazetteer entirely."""
    got = candidates("under the Companies Act 2006 (Cth)")
    assert all(v is None for v in got.values())


def test_reference_key_strips_the_australian_jurisdiction_tag():
    """So a name-only "Fair Work Act 2009 (Cth)" resolves against the held title
    "Fair Work Act 2009" — normalise_title leaves the tag word, reference_key removes it."""
    from raglex.citations.statute_gazetteer import normalise_title, reference_key
    assert reference_key("Fair Work Act 2009 (Cth)") == normalise_title("Fair Work Act 2009")
    assert reference_key("s 5 of the Crimes Act 1900 (NSW)") == normalise_title("Crimes Act 1900")
    # a UK reference with no tag is unchanged
    assert reference_key("the Human Rights Act 1998") == "human rights act 1998"


def test_canadian_tribunal_codes_have_natural_language_names():
    # the bulk Canadian corpora key these by their own short codes; unregistered
    # they surfaced in the Explore facets as "Sst", "Rad", "Citt"
    from raglex.citations.courts import lookup

    for code, expect in [("SST", "Social Security Tribunal"),
                         ("RAD", "Refugee Appeal Division"),
                         ("RPD", "Refugee Protection Division"),
                         ("CITT", "Canadian International Trade Tribunal"),
                         ("CHRT", "Canadian Human Rights Tribunal"),
                         ("FPSLREB", "Federal Public Sector Labour"),
                         ("CIRB", "Canada Industrial Relations Board"),
                         ("CT", "Competition Tribunal")]:
        c = lookup(code, bracketed=False)
        assert c and c.name and expect in c.name, (code, c and c.name)


def test_fca_disambiguates_by_citation_style():
    # "FCA" is the Federal Court of AUSTRALIA when bracketed ([2020] FCA 1) and the
    # Federal Court of APPEAL of Canada when not (2020 FCA 1)
    from raglex.citations.courts import lookup

    assert "Australia" in lookup("FCA", bracketed=True).name
    assert "Canada" in lookup("FCA", bracketed=False).name
