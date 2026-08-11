"""Five national citation vocabularies, and the rule that keeps them out of each other.

Austria is the reason this file exists. Austrian and German judgments are written in one
language and cite in one notation, and the statutes behind the shared abbreviations are
different acts: KSchG is consumer protection in Vienna and dismissal protection in
Berlin, MSchG is trade marks in one and maternity leave in the other, and the ABGB has no
German counterpart at all. No amount of pattern work can tell those apart, because the
text really is identical — only the document can. So both readings are produced, both
survive the overlap dedupe, and ``stage._gate_national_grammars`` keeps the one that
belongs to the citing document's own system.

The other four are here for the same reason in a weaker form (a Finnish pykälä and a
Swedish SFS number do not collide with anything), and for the failure the existing suite
caught the moment they were added: a bare "DSA" is a duty solicitor advice scheme in an
English judgment and "DMA" a French marketing syndicate, and a corpus-wide acronym pass
with no context requirement turns both into EU regulations.
"""

from __future__ import annotations

import pytest

from raglex.citations.austrian import austrian_citations, norm_citations
from raglex.citations.estonian import act_key, estonian_citations
from raglex.citations.extractor import extract_citations
from raglex.citations.finnish import finnish_citations
from raglex.citations.slovak import eli_to_id, slovak_citations
from raglex.citations.stage import _gate_national_grammars, _grammar_host
from raglex.citations.swedish import swedish_citations


class _Doc(dict):
    """A document row as ``stage`` reads one — ``doc["source"]`` with no KeyError."""

    def __getitem__(self, key):
        return self.get(key)


def _ids(cites):
    return {(c.method, c.candidate_id) for c in cites}


def _gated(text: str, **doc) -> set[tuple[str, str | None]]:
    return _ids(_gate_national_grammars(_Doc(doc), extract_citations(text)))


# ── Austria: the same words, two legal systems ───────────────────────────────
AMBIGUOUS = ("Nach § 6 Abs 1 Z 9 KSchG und § 1295 ABGB, "
             "vgl 6 Ob 127/20z sowie Art 6 Abs 1 lit f DSGVO.")


def test_the_same_citation_reads_austrian_in_vienna_and_german_in_berlin():
    austrian = _gated(AMBIGUOUS, source="at-justiz", stable_id="ECLI:AT:OGH0002:2026:RS1")
    german = _gated(AMBIGUOUS, source="de-rii", stable_id="ECLI:DE:BGH:2020:X")
    assert ("at_law_reference", "at/gesetz/kschg") in austrian
    assert ("at_law_reference", "at/gesetz/abgb") in austrian
    assert not any(str(cid).startswith("de/") for _m, cid in austrian)
    # …and in a German document the German reading is the one that survives.
    assert any(str(cid).startswith("de/") for _m, cid in german)
    assert not any(str(cid).startswith("at/") for _m, cid in german)


def test_the_eu_reading_is_right_in_both_and_survives_either_way():
    for source, stable_id in (("at-justiz", "ECLI:AT:OGH0002:2026:RS1"),
                              ("de-rii", "ECLI:DE:BGH:2020:X"),
                              ("uk-caselaw", "uksc/2020/1")):
        gated = _gated(AMBIGUOUS, source=source, stable_id=stable_id)
        assert any(cid == "32016R0679" for _m, cid in gated), source


def test_an_austrian_docket_does_not_appear_in_a_foreign_document():
    # "6 Ob 127/20z" is an OGH case number. In a UK or Dutch judgment it is not a
    # citation of anything, and this pass runs over the whole corpus.
    for source in ("uk-caselaw", "nl-rechtspraak", "de-rii"):
        gated = _gated(AMBIGUOUS, source=source, stable_id="x/1")
        assert not any(str(cid).startswith("at:case:") for _m, cid in gated), source


def test_the_ambiguous_reading_is_not_dropped_before_the_document_can_choose():
    """The dedupe used to pick by list order and the gate then deleted the survivor, so
    the citation was lost rather than merely mis-attributed."""
    raw = _ids(extract_citations("Nach § 6 Abs 1 KSchG."))
    assert any(cid == "at/gesetz/kschg" for _m, cid in raw)
    assert any(str(cid).startswith("de/gesetz/kschg") for _m, cid in raw)


@pytest.mark.parametrize("source,expected", [
    ("at-dsb", "at"), ("sk-ress", "sk"), ("fi-kko", "fi"),
    ("se-domstol", "se"), ("ee-lahend", "ee"), ("de-rii", "de"),
    ("uk-caselaw", None), ("eu-cellar", None),
])
def test_the_host_is_read_from_the_source_key(source, expected):
    assert _grammar_host(_Doc(source=source, stable_id="")) == expected


def test_an_ecli_places_a_document_whose_source_does_not():
    assert _grammar_host(_Doc(source="", stable_id="ECLI:AT:OGH0002:2021:RS0133477")) == "at"
    assert _grammar_host(_Doc(source="", stable_id="fi/kko/2024/1")) == "fi"


# ── Austria: the vocabulary itself ───────────────────────────────────────────
def test_the_ziffer_is_the_rung_the_german_grammar_cannot_cross():
    """Austria writes "Z 1" where Germany writes "Nr. 1". The German pattern must run
    unbroken from § to the abbreviation, so an unknown rung ends the match early and
    reads the rung itself as the law."""
    cites = {c.candidate_id: c.pinpoint for c in austrian_citations("§ 611 Abs 2 Z 1 ZPO")}
    assert cites == {"at/gesetz/zpo": "§ 611 Abs 2 Z 1"}
    assert "de/gesetz/z" not in _ids(extract_citations("§ 611 Abs 2 Z 1 ZPO"))


def test_the_year_in_an_austrian_short_title_is_part_of_the_act():
    # TKG 2003 and TKG 2021 are different acts with different section numbers, and the
    # 2021 one is the EECC transposition.
    ids = {c.candidate_id for c in austrian_citations("§ 4 TKG 2021 und § 4 TKG 2003")}
    assert ids == {"at/gesetz/tkg2021", "at/gesetz/tkg2003"}


def test_a_lander_act_keeps_the_land_that_distinguishes_nine_of_them():
    assert dict(((t, a) for t, a, _raw in norm_citations(
        ["RPG Vlbg §45 Abs1 lite", "Oö BauO 1994 §49a"]))) == {
        "at/gesetz/vlbgrpg": "§ 45 Abs 1 lit e",
        "at/gesetz/oobauo1994": "§ 49a"}


def test_the_ris_register_index_is_not_part_of_the_provision():
    # "ABGB §833 A" — the trailing letter is the norm register's own classification, and
    # an anchor that keeps it can never match "§ 833 ABGB" as a judgment prints it.
    assert [a for _t, a, _raw in norm_citations(["ABGB §833 A"])] == ["§ 833"]
    assert [a for _t, a, _raw in norm_citations(["ZPO §500 Abs2 IIA1"])] == ["§ 500 Abs 2"]


def test_the_two_word_orders_of_the_normen_field_are_the_same_reference():
    """The courts write "ZPO §611"; the Gleichbehandlungskommission and the disciplinary
    bodies write "§13 Abs1 Z5 B-GlBG". One field, one register, opposite order."""
    assert norm_citations(["§126 Abs2 BDG"])[0][:2] == ("at/gesetz/bdg", "§ 126 Abs 2")
    assert norm_citations(["ZPO §581"])[0][:2] == ("at/gesetz/zpo", "§ 581")


def test_one_normen_entry_may_name_two_acts():
    assert [t for t, _a, _r in norm_citations(["AsylG 1997 §7 AsylG 1997 §12"])] == [
        "at/gesetz/asylg1997", "at/gesetz/asylg1997"]
    assert [a for _t, a, _r in norm_citations(["AsylG 1997 §7 AsylG 1997 §12"])] == [
        "§ 7", "§ 12"]


def test_austrian_decisions_are_recognised_by_the_shape_of_their_own_dockets():
    text = ("6 Ob 127/20z, 10 ObS 45/21b, 11 Os 32/20t, Ra 2018/16/0040, "
            "VfGH G 123/2020, W123 2284751-3, LVwG-2023/27/0673-5, "
            "RIS-Justiz RS0042418, VfSlg 19.632/2012")
    ids = {c.candidate_id for c in austrian_citations(text)}
    assert {"at:case:OGH:6Ob127/20z", "at:case:OGH:10ObS45/21b",
            "at:case:OGH:11Os32/20t", "at:case:VWGH:Ra2018/16/0040",
            "at:case:VFGH:G123/2020", "at:case:BVWG:W1232284751-3",
            "at:case:LVWG:LVwG-2023/27/0673-5", "at/rs/RS0042418",
            "at/vfslg/19632"} <= ids


def test_a_bare_vfgh_proceeding_letter_needs_the_court_beside_it():
    # "G 123/2020" is one letter and four digits; without the court it is a docket, a
    # grid reference and a footnote number.
    assert not [c for c in austrian_citations("see G 123/2020 in the appendix")]


def test_an_austrian_report_series_is_not_a_law():
    # Both directions of the same problem: a §-reference trailing INTO a series name, and
    # a German series name followed by the statute it files under.
    assert not [c for c in austrian_citations("§ 5 Abs 1 JBl") if c.candidate_id]
    assert not [c for c in extract_citations("BGHR StPO Abs. 3 Verfahrenshindernis 2).")
                if c.candidate_id]


# ── Slovakia ─────────────────────────────────────────────────────────────────
def test_a_slovak_statute_is_its_collection_number():
    cites = {c.candidate_id: c.pinpoint for c in slovak_citations(
        "Podľa § 9 ods. 1 písm. a) zákona č. 514/2003 Z. z.")}
    assert cites == {"sk/zz/2003/514": "§ 9 ods. 1 písm. a"}


def test_a_declined_code_name_reaches_the_same_number_as_the_citation_form():
    """Slovak declines: the code named "Občiansky zákonník" is "Občianskeho zákonníka" in
    every citation of it, and matching the dictionary form finds nothing at all."""
    for text, target in (("§ 420 Občianskeho zákonníka", "sk/zz/1964/40"),
                         ("§ 8 Trestného zákona", "sk/zz/2005/300"),
                         ("§ 221 Trestného poriadku", "sk/zz/2005/301"),
                         ("§ 12 Obchodného zákonníka", "sk/zz/1991/513"),
                         ("čl. 152 Ústavy Slovenskej republiky", "sk/zz/1992/460")):
        assert {c.candidate_id for c in slovak_citations(text)} == {target}, text


def test_a_greedy_code_name_does_not_swallow_the_citation_after_it():
    # The capture reaches four words ahead so a multi-word code name fits; resuming at
    # the end of the regex match consumed the "čl" of the next reference.
    ids = {c.candidate_id for c in slovak_citations(
        "§ 12 Obchodného zákonníka a čl. 152 Ústavy Slovenskej republiky")}
    assert ids == {"sk/zz/1991/513", "sk/zz/1992/460"}


def test_the_slovak_eli_fragment_is_a_fully_resolved_citation():
    assert eli_to_id("/SK/ZZ/2005/300/#paragraf-221.odsek-3.pismeno-a") == (
        "sk/zz/2005/300", "§ 221 ods. 3 písm. a")
    assert eli_to_id("/SK/ZZ/2005/300") == ("sk/zz/2005/300", None)
    assert eli_to_id("nonsense") is None


def test_a_slovak_ecli_stops_at_the_end_of_the_identifier_not_the_sentence():
    # Slovak ECLIs carry an internal dot, and so does the sentence they sit in.
    ids = {c.candidate_id for c in slovak_citations("Pozri ECLI:SK:NSSR:2025:6322010282.1.")}
    assert ids == {"ECLI:SK:NSSR:2025:6322010282.1"}


# ── Finland ──────────────────────────────────────────────────────────────────
def test_a_finnish_act_is_found_through_its_inflection():
    for text, target in (("Yrityssaneerauslain 35 §:n 2 momentin nojalla", "fi/act/1993/47"),
                         ("Perustuslain 12 §", "fi/act/1999/731"),
                         ("rikoslain 38 luvun 8 §", "fi/act/1889/39")):
        assert {c.candidate_id for c in finnish_citations(text)} == {target}, text


def test_the_participial_act_name_is_the_form_judgments_actually_use():
    """Finnish drafting names an act by what it was enacted about — "takaisinsaannista
    konkurssipesään annettu laki" — whose word order is the reverse of the short title."""
    cites = finnish_citations(
        "takaisinsaannista konkurssipesään annetun lain 2 §:n 3 momentissa")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [("fi/act/1991/758", "2 § 3 mom.")]


def test_a_finnish_decision_carries_its_court_in_the_citation():
    ids = {c.candidate_id for c in finnish_citations(
        "KKO:2024:1, KHO 2023:45, MAO:123/24, TT 2024:61, ECLI:FI:KKO:2024:1")}
    # The Market Court writes a two-digit year; reading "24" as the year files a 2024
    # decision two millennia early.
    assert {"fi/kko/2024/1", "fi/kho/2023/45", "fi/mao/2024/123", "fi/tt/2024/61",
            "ECLI:FI:KKO:2024:1"} <= ids


def test_the_finnish_article_takes_its_instrument_from_the_nearer_side():
    """Finnish puts the instrument before the article, and a fixed window puts the
    PREVIOUS sentence's instrument in range."""
    cites = finnish_citations(
        "Tietosuoja-asetuksen 6 artiklan 1 kohdan f alakohta. SEUT 267 artikla.")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("32016R0679", "Article 6(1)(f)"), ("12016E", "Article 267")]


# ── Sweden ───────────────────────────────────────────────────────────────────
def test_a_swedish_statute_is_its_sfs_number_however_it_is_named():
    for text, target, anchor in (
            ("56 a § lagen (1960:729) om upphovsrätt", "se/sfs/1960/729", "56 a §"),
            ("2 kap. 3 § brottsbalken", "se/sfs/1962/700", "2 kap. 3 §"),
            ("4 kap. 1 § RB", "se/sfs/1942/740", "4 kap. 1 §")):
        cites = [c for c in swedish_citations(text) if c.candidate_id == target]
        assert cites and cites[0].pinpoint == anchor, text


def test_the_chapter_is_part_of_the_swedish_anchor():
    # "2 kap. 3 §" and "3 §" are different provisions of the same act, and the chapter
    # comes FIRST — the opposite of the German and Austrian order.
    a = next(c for c in swedish_citations("2 kap. 3 § brottsbalken"))
    b = next(c for c in swedish_citations("3 § brottsbalken"))
    assert a.pinpoint != b.pinpoint


def test_a_section_range_is_one_marker_not_two():
    # "§§" is a range of sections. Spacing each sign individually made the anchor
    # "3 § §", which matches no provision anyone cites.
    cites = [c for c in swedish_citations("3 §§ brottsbalken")]
    assert cites and cites[0].pinpoint == "3 §§"


def test_sweden_subdivides_an_eu_article_with_dots():
    cites = swedish_citations("artikel 6.1 f i dataskyddsförordningen")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [("32016R0679", "Article 6(1)(f)")]


def test_a_swedish_decision_is_cited_by_its_report_because_there_is_no_ecli():
    ids = {c.candidate_id for c in swedish_citations(
        "NJA 2020 s. 123, HFD 2021 ref. 12, AD 2019 nr 5, MÖD 2020:12, mål Ö 4337-25")}
    assert {"se/nja/2020/123", "se/hfd/2021/12", "se/ad/2019/5", "se/mod/2020/12",
            "se:mal:Ö 4337-25"} <= ids


# ── Estonia ──────────────────────────────────────────────────────────────────
def test_an_estonian_abbreviation_is_the_citation_and_the_id():
    cites = {c.candidate_id: c.pinpoint for c in estonian_citations(
        "KarS § 199 lg 2 p 1 ja § 121 lg 1 HKMS")}
    assert cites == {"ee/seadus/kars": "§ 199 lg 2 p 1", "ee/seadus/hkms": "§ 121 lg 1"}


def test_a_superscript_section_is_a_different_provision_not_a_subdivision():
    assert [c.pinpoint for c in estonian_citations("KarS § 43¹")] == ["§ 43-1"]
    assert [c.pinpoint for c in estonian_citations("KarS § 43")] == ["§ 43"]


def test_an_act_named_either_way_lands_on_one_id():
    # lahend.ee returns the act sometimes as its abbreviation and sometimes as its title,
    # in the same list; two ids would split one statute's citers between them.
    assert act_key("RÕS") == act_key("riigi õigusabi seadus") == "ee/seadus/ros"
    assert act_key("Halduskohtumenetluse seadustik") == "ee/seadus/hkms"


def test_an_estonian_case_number_keeps_the_document_it_names():
    ids = {c.candidate_id for c in estonian_citations("3-25-3458/5 ja 3-2-1-45-12")}
    assert ids == {"ee/lahend/3-25-3458/5", "ee/lahend/3-2-1-45-12"}


# ── the acronym guard the existing suite caught ──────────────────────────────
@pytest.mark.parametrize("text", [
    "The detainee obtained advice under a Duty Solicitor Advice (DSA) scheme; "
    "both the DSA scheme and the DSA surgery were available.",
    "Syndicat professionnel Data et Marketing France (DMA France), represented by",
    "the EIS report and the SEU questionnaire were filed",
])
def test_a_short_acronym_alone_is_not_an_eu_regulation(text):
    assert not [c for c in extract_citations(text) if c.candidate_id]


@pytest.mark.parametrize("text,target", [
    ("Podľa nariadenia GDPR a podľa aktu DSA.", "32022R2065"),
    ("Enligt förordningen DSA och EKMR artikel 8.", "echr/convention"),
    ("IKÜM alusel ja ELTL kohaselt.", "12016E"),
])
def test_the_same_acronym_beside_its_own_language_does_resolve(text, target):
    assert target in {c.candidate_id for c in extract_citations(text)}
