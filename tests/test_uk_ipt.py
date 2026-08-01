from __future__ import annotations

from raglex.adapters.uk_ipt import (
    neutral_citation_id,
    paragraph_segments,
    parse_listing,
)

# One judgment card, as both discovery modes render it (the listing page inlines this
# markup; the AJAX endpoint returns the same thing inside a JSON string).
CARD = """
<article id="post-3559" class="elementor-post type-judgement">
  <h3 class="elementor-heading-title">AFG and Others v Chief Constable of Dyfed Powys Police</h3>
  <h4 class="elementor-heading-title">IPT/17/110-112/CH </h4>
  <a class="elementor-button" href="https://investigatorypowerstribunal.org.uk/judgement/afg-and-others-v-chief-constable-of-dyfed-powys-police/">
    <span class="elementor-button-text">Read more</span></a>
  <h4 class="elementor-heading-title">15 October 2025</h4>
</article>
"""

JUDGMENT_HEAD = """AFG and Others v Chief Constable of Dyfed Powys Police
IPT/17/110-112/CH
15 October 2025
Neutral Citation Number: [2025] UKIPTrib 10
Case Nos: IPT/17/110-112/CH
IN THE INVESTIGATORY POWERS TRIBUNAL
Date: 15 October 2025
OPEN JUDGMENT
1.This is the judgment of the Tribunal.
2.The three Claimants have each brought human rights proceedings.
7. The Tribunal held an OPEN and a CLOSED hearing on 28 May 2025.
"""


def test_listing_cards_carry_link_case_number_and_date():
    cards = parse_listing(CARD)
    assert len(cards) == 1
    card = cards[0]
    assert card["url"].endswith("/judgement/afg-and-others-v-chief-constable-of-dyfed-powys-police/")
    assert card["title"] == "AFG and Others v Chief Constable of Dyfed Powys Police"
    assert card["case_number"] == "IPT/17/110-112/CH"
    assert card["published"].isoformat() == "2025-10-15"


def test_listing_degrades_to_links_when_the_theme_changes():
    """A template revision should cost FIELDS, not judgments: the link is the one thing
    a rebuild of the site's cards cannot take away without removing the judgment."""
    bare = '<div><a href="/judgement/some-new-case/">Read more</a></div>'
    cards = parse_listing(bare)
    assert [c["url"] for c in cards] == [
        "https://investigatorypowerstribunal.org.uk/judgement/some-new-case/"]
    assert cards[0]["case_number"] is None


def test_neutral_citation_is_the_identity():
    assert neutral_citation_id(JUDGMENT_HEAD) == "ukiptrib/2025/10"
    assert neutral_citation_id("Neutral Citation No [2021] UKIPTrib 5") == "ukiptrib/2021/5"
    # An older ruling predates the neutral citation entirely; the caller falls back to
    # a slug id rather than inventing one.
    assert neutral_citation_id("IN THE MATTER OF APPLICATION No IPT/01/62") is None


def test_paragraphs_are_segmented_as_the_citable_unit():
    segments = paragraph_segments(JUDGMENT_HEAD)
    labels = [s.label for s in segments]
    # Everything before paragraph 1 — intituling, case numbers, the bench — is kept as
    # one opening segment rather than dropped.
    assert labels[0] == "Opening"
    assert labels[1:] == ["para 1", "para 2", "para 7"]
    para7 = next(s for s in segments if s.label == "para 7")
    assert JUDGMENT_HEAD[para7.char_start:para7.char_end].startswith("7. The Tribunal held")


def test_a_restarted_number_does_not_rewrite_the_sequence():
    """A schedule of issues or a closed annex restarts at 1. Only advancing means a
    stray "1." cannot make the rest of the judgment its own paragraph 1."""
    text = "1. First.\n2. Second.\n1. Annex item.\n3. Third.\n"
    assert [s.label for s in paragraph_segments(text)] == ["para 1", "para 2", "para 3"]


def test_strasbourg_judgment_on_the_site_is_not_an_ipt_judgment():
    """Kennedy v the United Kingdom is republished here for convenience. Its infobox
    gives an application number where a Tribunal judgment gives a case number, and the
    corpus holds ECtHR judgments under their HUDOC identity — a copy keyed to an IPT
    slug would be a second, worse record of another court's work."""
    from raglex.adapters.uk_ipt import _APPLICATION_NO_RE, _CASE_NO_RE

    echr = "Case of Kennedy v. the United Kingdom\nApplication no. 26839/05\n23 January 2003"
    tribunal = "Kennedy v Security Services, GCHQ and The Met\nIPT/01/62\n23 January 2003"
    assert _APPLICATION_NO_RE.search(echr) and not _CASE_NO_RE.search(echr)
    assert _CASE_NO_RE.search(tribunal)


def test_ripa_and_ipa_are_shorthand_in_this_source_only():
    """In an IPT judgment those letters always mean the two Acts the Tribunal applies.
    In the wider corpus "IPA" is an insolvency practitioners' association and a beer, so
    the certainty is spent only where it holds."""
    from raglex.citations import extract_citations
    from raglex.citations.stage import _SOURCE_ALIASES

    aliases = _SOURCE_ALIASES["uk-ipt"]
    for text, target in (
        ("section 65 of RIPA", "ukpga/2000/23"),
        ("under RIPA 2000 the Tribunal", "ukpga/2000/23"),
        ("Part 2 of the IPA 2016", "ukpga/2016/25"),
        ("the IPA applies", "ukpga/2016/25"),
    ):
        got = [c.candidate_id for c in extract_citations(text, aliases=aliases)]
        assert got == [target], text
        # …and without the source's aliases, nothing is asserted at all.
        assert not [c for c in extract_citations(text) if c.candidate_id], text


def test_the_full_act_names_resolve_everywhere():
    """Unambiguous spelled-out names need no scoping — and the IPA 2016 was previously
    unreachable by its own name."""
    from raglex.citations import extract_citations

    by = {c.candidate_id for c in extract_citations(
        "the Investigatory Powers Act 2016 and the Regulation of Investigatory Powers "
        "Act 2000")}
    assert {"ukpga/2016/25", "ukpga/2000/23"} <= by
