from __future__ import annotations

from raglex.core.structure import line_depths, line_structure


def depths(text: str) -> list[int]:
    return [d for _s, _e, d in line_depths(text)]


def paths(text: str) -> list[str]:
    return [p for _s, _e, _d, p in line_structure(text)]


def test_senior_courts_act_s24_nests_paragraphs_under_their_subsection():
    # the real shape of ukpga/1981/54 s.24 in the corpus: (1)(2)(a)(b)(c)(3)
    text = (
        "(1) In sections 20 to 23 and this section, unless the context otherwise requires—\n"
        "(2) Nothing in sections 20 to 23 shall—\n"
        "(a) be construed as limiting the jurisdiction of the High Court…\n"
        "(b) affect the provisions of section 226 of the Merchant Shipping Act 1995…\n"
        "(c) authorise proceedings in rem in respect of any claim against the Crown…\n"
        "(3) In this section— “ Her Majesty’s ships ”…"
    )
    # (a)-(c) hang off (2); (3) returns to the subsection tier
    assert depths(text) == [0, 0, 1, 1, 1, 0]


def test_three_tiers_nest_and_pop_back_out():
    text = ("(1) first\n"
            "(a) alpha under one\n"
            "(i) roman under a\n"
            "(ii) still roman\n"
            "(b) back out to alpha\n"
            "(2) back out to the subsection")
    assert depths(text) == [0, 1, 2, 2, 1, 0]


def test_alpha_run_passing_through_i_is_not_read_as_roman():
    # the genuinely ambiguous token: after (h), "(i)" is the LETTER i, not roman 1,
    # so it must stay on the alpha tier instead of opening a nested one
    text = "(g) seven\n(h) eight\n(i) nine\n(j) ten"
    assert depths(text) == [0, 0, 0, 0]


def test_roman_opens_a_new_tier_when_no_alpha_run_expects_it():
    # here (i) cannot continue the alpha run (which sits at (b)), so it nests
    text = "(a) one\n(b) two\n(i) roman one\n(ii) roman two\n(c) three"
    assert depths(text) == [0, 0, 1, 1, 0]


def test_letters_before_numbers_still_level_correctly():
    # the order of tiers is NOT assumed — an Act that runs (a) then (1) nests the
    # numbers under the letters, the reverse of the usual arrangement
    text = "(a) first\n(1) under a\n(2) still under a\n(b) second"
    assert depths(text) == [0, 1, 1, 0]


def test_inserted_provisions_stay_on_their_own_tier():
    # amending Acts insert (4A)/(4B) beside (4) — same tier, not a sub-tier
    text = "(3) three\n(4) four\n(4A) inserted\n(4B) also inserted\n(5) five"
    assert depths(text) == [0, 0, 0, 0, 0]
    # and the multi-character suffix form
    assert depths("(2) two\n(2ZA) inserted\n(3) three") == [0, 0, 0]


def test_continuation_lines_stay_with_their_provision():
    # an unmarked line is part of the provision above it, so it must not snap back
    # to the left margin mid-sentence
    text = "(1) opening words—\n(a) the first limb\ncontinued onto a second line\n(b) the second limb"
    assert depths(text) == [0, 1, 1, 1]


def test_upper_case_tiers_are_distinct_from_lower_case():
    text = "(a) lower\n(A) upper nests\n(B) still upper\n(b) back to lower"
    assert depths(text) == [0, 1, 1, 0]


def test_restarting_a_sub_run_under_the_next_parent():
    text = ("(1) one\n(a) a\n(b) b\n"
            "(2) two\n(a) a again\n(b) b again\n"
            "(3) three")
    assert depths(text) == [0, 1, 1, 0, 1, 1, 0]


def test_dotted_enumerators_are_recognised():
    assert depths("1. first\n2. second\n3. third") == [0, 0, 0]


def test_prose_parentheticals_are_not_mistaken_for_enumerators():
    # a line opening with a parenthetical phrase is prose, not a provision
    text = "(1) the rule\n(see section 5 for the exception) which applies generally\n(2) the next rule"
    assert depths(text) == [0, 0, 0]


def test_offsets_cover_the_text_exactly():
    text = "(1) one\n(a) two\n(b) three"
    spans = line_depths(text)
    assert [text[s:e] for s, e, _d in spans] == ["(1) one", "(a) two", "(b) three"]


def test_depth_is_bounded():
    # pathological input must not indent off the page
    text = "\n".join(f"({'i' * 1}) x" for _ in range(20))
    assert max(depths(text)) < 8


def test_empty_and_single_line_text():
    assert line_depths("") == [(0, 0, 0)]
    assert depths("no markers here at all") == [0]


def test_depth_never_jumps_more_than_one_tier():
    # The invariant that makes the render legible: a provision may open at most one
    # level deeper than the line before it. Verified to hold across a random sample
    # of real UK statutes in the corpus; asserted here so a future tweak to the
    # candidate/continuation rules can't quietly reintroduce a skipped tier.
    samples = [
        "(1) a\n(a) b\n(i) c\n(ii) d\n(b) e\n(2) f",
        "(a) a\n(1) b\n(i) c\n(2) d\n(b) e",
        "(1) a\ncontinuation\n(2) b\n(a) c\n(A) d\n(B) e\n(b) f",
        "1. a\n2. b\n(a) c\n(i) d",
    ]
    for text in samples:
        ds = depths(text)
        for prev, cur in zip(ds, ds[1:]):
            assert cur - prev <= 1, f"skipped a tier in {text!r}: {ds}"


def test_document_body_emits_line_depths_for_legislation_only(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "s.json", embed_provider="local-hashing",
                 embed_model=None)
    f = Facade(cfg)
    body = "<p>(1) The rule.\n(2) Subject to—\n(a) the first case;\n(b) the second case.</p>".encode()
    act = f.import_bytes(data=body, filename="act.html",
                         doc_type="legislation", title="An Act")["stable_id"]
    judgment = f.import_bytes(data=body, filename="j.html",
                              doc_type="judgment", title="A v B")["stable_id"]

    got = f.document_body(act)
    # this import produces no segments, so the depths ride on the top-level fallback
    lines = [ln for s in got["segments"] for ln in s.get("lines", [])] or got["lines"]
    assert lines, "legislation should carry per-line depths"
    assert [ln["depth"] for ln in lines] == [0, 0, 1, 1]
    # offsets are absolute into the document text, so the reader can slice directly
    text = got["text"]
    assert text[lines[2]["start"]:lines[2]["end"]].strip().startswith("(a)")

    # judgments are flat numbered paragraphs — no hierarchy to recover, none emitted
    jb = f.document_body(judgment)
    assert jb["lines"] is None
    assert all("lines" not in s for s in jb["segments"])


def test_definition_heads_reset_but_their_sub_paragraphs_nest():
    # A Criminal Code-style interpretation section: each defined term sits at the
    # margin and its lettered sub-paragraphs nest one tier in — successive
    # definitions must NOT march ever deeper (flag 14).
    text = (
        "In this Act,\n"
        "Act includes\n"
        "(a) an Act of Parliament,\n"
        "(b) an Act of the legislature of a province;\n"
        "appearance notice means a notice in Form 9;\n"
        "Attorney General\n"
        "(a) with respect to some proceedings, means X, and\n"
        "(b) with respect to other proceedings, means Y;\n"
        "bank-note includes any negotiable instrument;"
    )
    assert depths(text) == [0, 0, 1, 1, 0, 0, 1, 1, 0]


def test_definition_head_pattern_does_not_fire_on_ordinary_prose():
    # a subsection that happens to contain "means" mid-sentence is not a new head
    text = ("(1) The court shall consider what fairness means in the circumstances\n"
            "(a) having regard to the parties, and\n"
            "(b) the wider public interest.")
    assert depths(text) == [0, 1, 1]


# --- line_structure: the marker PATH each provision line carries -------------
def test_line_structure_accumulates_the_marker_path():
    # (a)/(b)/(c) hang off (2), so they carry the full sub-part path "(2)(a)" — the
    # exact anchor a pincite ("s. 24(2)(a)") uses, so its badge lands on that line.
    text = (
        "(1) In sections 20 to 23…\n"
        "(2) Nothing in sections 20 to 23 shall—\n"
        "(a) be construed as limiting…\n"
        "(b) affect the provisions…\n"
        "(3) In this section—…"
    )
    assert paths(text) == ["(1)", "(2)", "(2)(a)", "(2)(b)", "(3)"]


def test_line_structure_three_tiers_and_pop_back():
    text = ("(1) first\n(a) alpha under one\n(i) roman under a\n(ii) still roman\n"
            "(b) back out to alpha\n(2) back out")
    assert paths(text) == ["(1)", "(1)(a)", "(1)(a)(i)", "(1)(a)(ii)", "(1)(b)", "(2)"]


def test_line_structure_continuation_and_lead_in_carry_no_path():
    # a continuation (wrapped) line is not a citable sub-provision → empty path, so no
    # badge is misattached to it
    text = "(1) opening words—\n(a) the first limb\ncontinued onto a second line\n(b) the second limb"
    assert paths(text) == ["(1)", "(1)(a)", "", "(1)(b)"]


def test_line_structure_inserted_provision_keeps_its_own_token():
    text = "(4) four\n(4A) inserted\n(5) five"
    assert paths(text) == ["(4)", "(4A)", "(5)"]


def test_line_depths_still_matches_line_structure():
    # the back-compat wrapper must stay in lock-step with the richer function
    text = "(1) a\n(a) b\n(i) c\n(2) d"
    assert [d for _s, _e, d in line_depths(text)] == [d for _s, _e, d, _p in line_structure(text)]


def test_document_body_lines_carry_the_sub_part_anchor(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "s.json", embed_provider="local-hashing",
                 embed_model=None)
    f = Facade(cfg)
    body = "<p>(1) The rule.\n(2) Subject to—\n(a) the first case;\n(b) the second case.</p>".encode()
    act = f.import_bytes(data=body, filename="act.html",
                         doc_type="legislation", title="An Act")["stable_id"]
    got = f.document_body(act)
    lines = [ln for s in got["segments"] for ln in s.get("lines", [])] or got["lines"]
    assert [ln.get("anchor") for ln in lines] == ["(1)", "(2)", "(2)(a)", "(2)(b)"]


def test_an_article_led_regulation_does_not_lose_its_articles_to_its_sections():
    """An assimilated EU regulation divides its chapters into <section> elements —
    "Section 1 Transparency and modalities" — whose children are the citable articles.
    <section> is also a UK Act's own unit, emitted whole without descending, so the walk
    stopped at Chapter III's sections and never reached Articles 12 to 23: the UK GDPR
    indexed 57 of its ~99 articles, and no citation of Article 15 could land."""
    from raglex.formats.akoma_ntoso import parse_akn

    akn = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
      <act><body>
        <chapter eId="chapter-III"><num>CHAPTER III</num>
          <heading>Rights of the data subject</heading>
          <section eId="chapter-III-section-1">
            <num>Section 1</num><heading>Transparency and modalities</heading>
            <article eId="article-12"><num>Article 12</num>
              <heading>Transparent information</heading>
              <paragraph><content><p>The controller shall take appropriate measures.</p>
              </content></paragraph></article>
            <article eId="article-15"><num>Article 15</num>
              <heading>Right of access</heading>
              <paragraph><content><p>The data subject shall have the right.</p>
              </content></paragraph></article>
          </section>
        </chapter>
      </body></act></akomaNtoso>"""
    parsed = parse_akn(akn)
    labels = [s.label for s in parsed.segments]
    assert any(l.startswith("Article 12") for l in labels), labels
    assert any(l.startswith("Article 15") for l in labels), labels
    assert "The data subject shall have the right." in parsed.text
    # the Section survives as a heading, not as a citable unit swallowing the articles
    assert any(l.startswith("Section 1") for l in labels)


def test_an_act_still_cites_by_section():
    """The rule keys on the instrument's own shape, so an Act — which has sections and
    no articles — is untouched."""
    from raglex.formats.akoma_ntoso import parse_akn

    akn = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
      <act><body>
        <part><num>Part 1</num><heading>General</heading>
          <section eId="section-5"><num>5</num><heading>Duties</heading>
            <subsection><content><p>A person must comply.</p></content></subsection>
          </section>
        </part>
      </body></act></akomaNtoso>"""
    parsed = parse_akn(akn)
    assert any((s.label or "").startswith("s. 5") for s in parsed.segments), \
        [s.label for s in parsed.segments]


def test_an_article_is_typed_article_not_section():
    """The labels were right — "Article 15" anchors correctly — but the segments were
    RECORDED as kind 'section', so asking the UK GDPR for outline_kind='article'
    returned an empty list and its articles could only be found by asking for sections.
    The parser was applying UK-Act typing to an assimilated EU instrument."""
    from raglex.formats.akoma_ntoso import parse_akn

    akn = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
      <act><body>
        <chapter><num>CHAPTER I</num><heading>General</heading>
          <article eId="article-15"><num>Article 15</num><heading>Right of access</heading>
            <paragraph><content><p>The data subject shall have the right.</p></content></paragraph>
          </article>
        </chapter>
      </body></act></akomaNtoso>"""
    segs = {s.label: s.kind for s in parse_akn(akn).segments}
    assert segs["Article 15 Right of access"] == "article"


def test_an_acts_sections_are_still_sections():
    """Only articles change kind; Acts, SIs and judgments keep the historical typing."""
    from raglex.formats.akoma_ntoso import parse_akn

    akn = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
      <act><body>
        <section eId="section-5"><num>5</num><heading>Duties</heading>
          <subsection><content><p>A person must comply.</p></content></subsection>
        </section>
      </body></act></akomaNtoso>"""
    segs = {s.label: s.kind for s in parse_akn(akn).segments}
    assert segs["s. 5 Duties"] == "section"


def test_uk_akn_prefers_canonical_dc_title_with_publisher_spacing():
    """Adjacent long-title paragraphs have no XML whitespace between them (``2016on``)."""
    from raglex.formats.akoma_ntoso import parse_akn

    akn = b'''<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
        xmlns:dc="http://purl.org/dc/elements/1.1/">
      <act><meta><proprietary><dc:title>Regulation (EU) 2016/1927 of 4 November
      2016 on templates</dc:title></proprietary></meta>
      <preface><longTitle><p>Regulation (EU) 2016/1927</p><p>of 4 November 2016</p>
      <p>on templates</p></longTitle></preface><body/></act></akomaNtoso>'''
    assert parse_akn(akn).title == "Regulation (EU) 2016/1927 of 4 November 2016 on templates"
