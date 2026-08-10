"""Which instrument a bare "Article 50(2)" or "section 40A" is taken to belong to.

Every case here came from a document in the corpus that got the answer wrong. The
Commission's own drafting form puts the pinpoint first and the instrument's short name
after it with no "of the" — "Article 50(2) AI Act" — so an instrument missing from the
name map doesn't just fail to link: the bare pinpoint falls through to the
carry-forward pass and is attributed to whichever instrument was named most recently,
which in a Commission opinion is a cross-reference to something else entirely.
"""

from __future__ import annotations

from raglex.citations import extract_citations
from raglex.citations.extractor import valid_shorthand


def _by_method(text, **kw):
    return [(c.method, c.candidate_id, c.pinpoint, c.raw) for c in extract_citations(text, **kw)]


# -- the digital acquis by short name ----------------------------------------
def test_ai_act_resolves_from_the_commissions_postfix_form():
    # "Article 50(2) AI Act" — no "of the", pinpoint in front. The Commission's
    # Opinion on the Code of Practice on transparency of AI-generated content is
    # written entirely this way; 31 of its Articles had been attributed to the DSA.
    cites = {c.pinpoint: c for c in extract_citations(
        "Article 50(2) AI Act requires providers of AI systems to mark outputs.")}
    assert cites["Article 50(2)"].candidate_id == "32024R1689"
    assert cites["Article 50(2)"].method != "carry_forward"


def test_the_named_instrument_beats_the_last_one_mentioned():
    txt = ("designated in accordance with Article 33(4) of Regulation (EU) 2022/2065. "
           "Section 1 of the Code operationalises the obligations in Article 50(2) AI Act.")
    got = {(c.candidate_id, c.pinpoint) for c in extract_citations(txt)}
    assert ("32024R1689", "Article 50(2)") in got
    # and nothing claimed "Section 1" for the DSA
    assert not any(cid == "32022R2065" and (pin or "").startswith("s.") for cid, pin in got)


def test_more_of_the_digital_acquis_is_recognised():
    for text, celex in [
        ("Article 5 of the Data Act", "32023R2854"),
        ("Article 20 of the Data Governance Act", "32022R0868"),
        ("Article 21 NIS2", "32022L2555"),
        ("Article 6 of the European Media Freedom Act", "32024R1083"),
        # "the Copyright Directive" is 2001/29 — the Court's own usage, in Laserdisken,
        # FAPL, Svensson and YouTube/Cyando. The DSM is "the DSM Directive"/"the CDSM
        # Directive", and it keeps both of those names. Read as the DSM, this nickname
        # gave that instrument 975 of somebody else's 1,252 name-matched citations.
        ("Article 3(1) of the Copyright Directive", "32001L0029"),
        ("Article 17 of the DSM Directive", "32019L0790"),
        ("Article 17 of the CDSM Directive", "32019L0790"),
        ("Article 4 of the Cyber Resilience Act", "32024R2847"),
    ]:
        assert any(c.candidate_id == celex for c in extract_citations(text)), text


def test_a_short_name_that_is_the_tail_of_a_longer_one_is_not_the_instrument():
    # "Data Act" is a proper suffix of the Nordic Personal Data Acts, which the DPA
    # decisions on gdprhub name constantly: 26% of the Data Act's whole citer graph.
    for text in ["The Data Inspectorate applied the Personal Data Act.",
                 "in breach of the Patient Data Act and section 4",
                 "under the Police Data Act 2018"]:
        assert not [c for c in extract_citations(text) if c.candidate_id], text
    # …while the qualifiers that leave identity alone, and an acronym glossing the name
    # in a heading, both still resolve.
    for text, celex in [("under the EU Data Act", "32023R2854"),
                        ("the Union's Digital Markets Act", "32022R1925"),
                        ("DSA Digital Services Act", "32022R2065")]:
        assert any(c.candidate_id == celex for c in extract_citations(text)), text


def test_a_name_cannot_match_from_the_middle_of_a_word():
    # _EU_PINPOINT is optional, so without a leading \b "nis 2" matched inside the Dutch
    # and German words for judgment, obstacle and result — 48% of every NIS 2 citation.
    for text in ["het kort geding vonnis 2 De cassatiedagvaarding",
                 "BGHR StPO Abs. 3 Verfahrenshindernis 2).",
                 "pursuant to Section 16(1)(b) of the FTAI Act."]:
        assert not [c for c in extract_citations(text) if c.candidate_id], text
    assert any(c.candidate_id == "32022L2555"
               for c in extract_citations("Article 34 of the NIS 2 Directive"))


# -- a provision that names its own host --------------------------------------
def test_a_provision_naming_its_own_act_is_not_carried_forward():
    # Law Commission report on automated vehicles: "the effect of section 40A of the
    # Road Traffic Act" was linked to the Automated and Electric Vehicles Act 2018,
    # which was simply the last Act named. The text says what it means; if the
    # grammars can't resolve that name, no edge is the honest answer.
    txt = ("The Automated and Electric Vehicles Act 2018 applies. "
           "The effect of section 40A of the Road Traffic Act is different.")
    assert not [c for c in extract_citations(txt)
                if c.method == "carry_forward" and c.raw.lower().startswith("section 40a")]


def test_a_section_of_a_code_of_practice_is_not_a_section_of_a_regulation():
    txt = ("in accordance with Regulation (EU) 2022/2065. "
           "Section 2 of the Code sets out eight commitments.")
    assert not [c for c in extract_citations(txt) if c.method == "carry_forward"]


def test_bare_section_never_attaches_to_an_eu_regulation():
    # EU regulations are divided into Articles. "Regulation" as an entity_kind covers
    # both a CELEX instrument and a UK SI, so the CELEX shape is what separates them.
    txt = "Regulation (EU) 2016/679 applies here. The duty in section 12 is engaged."
    assert not [c for c in extract_citations(txt)
                if c.method == "carry_forward" and c.candidate_id == "32016R0679"]


# -- damaged text --------------------------------------------------------------
def test_a_pdf_split_articles_does_not_leave_a_section_behind():
    # PyMuPDF renders "Articles 50(2) and (5)" as "Article s 50(2) and (5)"; the
    # orphaned "s 50(2)" then read as a UK section and carried forward to the DSA.
    txt = ("under Regulation (EU) 2022/2065. "
           "The Code operationalises the obligations in Article s 50(2) AI Act.")
    assert not [c for c in extract_citations(txt)
                if c.method == "carry_forward" and (c.pinpoint or "").startswith("s. 50")]


def test_citations_are_not_read_out_of_urls():
    # a footnote in a Law Commission report: the URL's own "/article/" and digits were
    # being read as provisions of whatever Act the report was discussing
    txt = ("The Consumer Rights Act 2015 is relevant. See "
           "https://www.sciencedirect.com/science/article/pii/S2590198220300245.")
    assert not [c for c in extract_citations(txt) if c.method == "carry_forward"]


# -- shorthands ----------------------------------------------------------------
def test_a_provision_reference_is_never_a_shorthand():
    # the store had "Article 8" → the ECHR with is_abbrev set, which turned every
    # "Article 8" in every document citing the Convention into a Convention link
    assert not valid_shorthand("Article 8")
    assert not valid_shorthand("art. 6")
    assert not valid_shorthand("section 3")
    assert not valid_shorthand("Recital 47")


def test_generic_role_and_instrument_nouns_are_never_shorthands():
    for junk in ["the Court", "appellant", "Appellants", "the claimant", "Act", "Code",
                 "the judge", "Analysis", "Application", "the grounds", "the parties"]:
        assert not valid_shorthand(junk), junk


def test_sentence_fragments_are_never_shorthands():
    for junk in ["Code in", "Code by", "Code. As", "ets of our ", "may make",
                 "Code can", "of the", "a"]:
        assert not valid_shorthand(junk), junk


def test_real_shorthands_still_pass():
    for good in ["Suncor", "FMIOA", "Vienna Convention", "Dunsmuir", "GDPR",
                 "the Vienna Convention", "Digital Rights Ireland", "BPRs",
                 "Suncor Energy", "the 1967 Act"]:
        assert valid_shorthand(good), good


# -- multilingual acronyms ------------------------------------------------------
def test_avg_is_the_gdpr_only_where_it_is_written_as_a_citation():
    # "each owned 40% of the shares in ASU Dominica and ASU AVG" — an Ontario company
    # name, flagged by a reader as linking to the GDPR
    assert not any(c.candidate_id == "32016R0679"
                   for c in extract_citations("40% of the shares in ASU AVG."))
    # the Dutch usage still links
    assert any(c.candidate_id == "32016R0679"
               for c in extract_citations("op grond van de AVG is dit onrechtmatig"))
    assert any(c.candidate_id == "32016R0679"
               for c in extract_citations("Article 6 AVG"))
