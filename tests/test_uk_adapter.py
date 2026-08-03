from __future__ import annotations

from datetime import date

from raglex.adapters.uk_caselaw import judgment_judges, parse_atom, parse_judgment

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Find Case Law</title>
  <link rel="self" href="https://caselaw.nationalarchives.gov.uk/atom.xml"/>
  <link rel="next" href="https://caselaw.nationalarchives.gov.uk/atom.xml?page=2"/>
  <entry>
    <title>Doe v Information Commissioner</title>
    <id>https://caselaw.nationalarchives.gov.uk/ukftt/grc/2024/123</id>
    <link rel="alternate" href="https://caselaw.nationalarchives.gov.uk/ukftt/grc/2024/123"/>
    <updated>2024-03-01T10:00:00Z</updated>
  </entry>
  <entry>
    <title>Smith v Jones</title>
    <id>https://caselaw.nationalarchives.gov.uk/d-abc123</id>
    <link rel="alternate" href="https://caselaw.nationalarchives.gov.uk/d-abc123"/>
    <updated>2024-02-10T09:00:00Z</updated>
  </entry>
</feed>
"""

JUDGMENT = b"""<?xml version="1.0" encoding="utf-8"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
  <judgment>
    <meta>
      <identification>
        <FRBRWork>
          <FRBRname value="[2024] UKFTT 123 (GRC)"/>
        </FRBRWork>
      </identification>
    </meta>
    <judgmentBody>
      <decision>
        <p>This appeal concerns the right to erasure of personal data.</p>
        <p>The tribunal considered <ref href="https://caselaw.nationalarchives.gov.uk/eu/c-311-18">Case C-311/18 (Schrems II)</ref>.</p>
      </decision>
    </judgmentBody>
  </judgment>
</akomaNtoso>
"""


def test_parse_atom_yields_stubs_and_next():
    page = parse_atom(ATOM)
    assert page.next_url == "https://caselaw.nationalarchives.gov.uk/atom.xml?page=2"
    assert len(page.stubs) == 2

    first = page.stubs[0]
    assert first.stable_id == "ukftt/grc/2024/123"
    assert first.court == "ukftt"
    assert first.hint_date == date(2024, 3, 1)
    assert first.raw_url.endswith("/ukftt/grc/2024/123/data.xml")

    second = page.stubs[1]
    assert second.stable_id == "d-abc123"  # new-style stable URI preserved


def test_parse_judgment_extracts_text_ncn_and_citation():
    text, relations, ncn, segments = parse_judgment(JUDGMENT)
    assert "right to erasure of personal data" in text
    assert ncn == "[2024] UKFTT 123 (GRC)"
    assert len(relations) == 1
    rel = relations[0]
    assert "c-311-18" in rel.raw_citation_string
    assert rel.resolution_status.value == "pending"
    # the <p> paragraphs become structural segments mapping into the text (§6b)
    assert len(segments) >= 1
    for s in segments:
        assert text[s.char_start:s.char_end].strip() != ""  # span indexes into text


HEADING_JUDGMENT = b"""<?xml version="1.0"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
 <judgment>
  <header><p>Before:</p><p><judge>THE HONOURABLE MR JUSTICE SAINI</judge></p></header>
  <judgmentBody><decision>
   <paragraph><num>I.</num><content><p><span style="font-weight:bold">Overview</span></p></content></paragraph>
   <paragraph eId="para_7"><num>7.</num><intro><p>The paragraph ends here.</p></intro>
    <subparagraph><num style="font-weight:bold">II.</num><content>
     <p><span style="font-weight:bold;text-decoration-line:underline">Procedural Chronology</span></p>
    </content></subparagraph>
   </paragraph>
   <paragraph eId="para_8"><num>8.</num><content><p>The next paragraph.</p></content></paragraph>
  </decision></judgmentBody>
 </judgment>
</akomaNtoso>"""


def test_parse_judgment_splits_embedded_headings_from_preceding_paragraph():
    text, _, _, segments = parse_judgment(HEADING_JUDGMENT)
    blocks = [(s.kind, text[s.char_start:s.char_end]) for s in segments]
    assert ("heading", "I. Overview") in blocks
    assert ("paragraph", "7. The paragraph ends here.") in blocks
    assert ("heading", "II. Procedural Chronology") in blocks
    assert not any("ends here" in block and "Procedural Chronology" in block
                   for _, block in blocks)
    assert judgment_judges(HEADING_JUDGMENT) == ["THE HONOURABLE MR JUSTICE SAINI"]


ATOM_TNA = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:tna="https://caselaw.nationalarchives.gov.uk">
  <entry>
    <title>Doe v Information Commissioner</title>
    <id>https://caselaw.nationalarchives.gov.uk/ukftt/grc/2024/123</id>
    <link rel="alternate" href="https://caselaw.nationalarchives.gov.uk/ukftt/grc/2024/123"/>
    <updated>2024-03-01T10:00:00+00:00</updated>
    <tna:contenthash>abc123</tna:contenthash>
  </entry>
  <entry>
    <title>Smith v Jones</title>
    <id>https://caselaw.nationalarchives.gov.uk/uksc/2024/9</id>
    <link rel="alternate" href="https://caselaw.nationalarchives.gov.uk/uksc/2024/9"/>
    <updated>2024-03-01T08:00:00+00:00</updated>
  </entry>
</feed>
"""


def test_parse_atom_carries_full_timestamp_cursor_and_contenthash():
    page = parse_atom(ATOM_TNA)
    first, second = page.stubs
    # the FULL <updated> timestamp is the incremental cursor (a date-only cursor
    # loses same-day arrivals forever) + the contenthash change signal
    assert first.hints["watermark"] == "2024-03-01T10:00:00+00:00"
    assert first.hints["contenthash"] == "abc123"
    assert "contenthash" not in second.hints


class _Resp:
    def __init__(self, content):
        self.content = content


class _FeedClient:
    def __init__(self, content):
        self._c = content
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        return _Resp(self._c)


def test_discover_incremental_sorts_by_transformation_and_stops_on_timestamp():
    from raglex.adapters.uk_caselaw import UKCaseLawAdapter

    client = _FeedClient(ATOM_TNA)
    ad = UKCaseLawAdapter(client=client)
    # incremental: -transformation order (the sort field IS the cursor field), and
    # entries at/older than the full-timestamp watermark are cut off
    got = list(ad.discover("2024-03-01T08:00:00+00:00", max_pages=1))
    assert [s.stable_id for s in got] == ["ukftt/grc/2024/123"]
    assert client.calls[0][1]["order"] == "-transformation"
    # same-day-but-later items are NOT lost to a date-only watermark
    got = list(ad.discover("2024-03-01T09:00:00+00:00", max_pages=1))
    assert [s.stable_id for s in got] == ["ukftt/grc/2024/123"]
    # first/full crawl keeps newest-decisions-first
    client.calls.clear()
    list(ad.discover(None, max_pages=1))
    assert client.calls[0][1]["order"] == "-date"


def test_filtered_court_can_have_an_independent_cursor_source():
    from raglex.adapters.uk_caselaw import UKCaseLawAdapter

    adapter = UKCaseLawAdapter(
        court="ukut/iac",
        source_key="uk-iac",
        client=_FeedClient(ATOM_TNA),
    )
    assert adapter.source == "uk-iac"
    assert adapter.court == "ukut/iac"


def test_statutory_instrument_regulations_are_citable_units():
    """PECR held 237 segments labelled "s. (1)", "s. (2)", "s. (1A)" … and nothing else.

    An Act's operative unit is <section>; a statutory instrument's is
    <hcontainer name="regulation">, which has no dedicated AKN tag and so fell through
    to the generic pass-through. The walk descended PAST the regulation and segmented
    its child <paragraph> elements instead, losing every regulation number and repeating
    the same handful of labels once per regulation — so no citation of "regulation 6"
    could land anywhere. Affected every SI in the corpus, not one document.
    """
    from raglex.formats.akoma_ntoso import parse_akn

    xml = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
    <act name="uksi"><body eId="body">
      <hcontainer name="regulation" eId="regulation-1"><heading>Citation</heading>
        <num>1.</num><content><p>These Regulations may be cited as the PECR 2003.</p></content></hcontainer>
      <hcontainer name="regulation" eId="regulation-6"><heading>Confidentiality of communications</heading>
        <num>6.</num>
        <paragraph eId="regulation-6-1"><num>(1)</num><content><p>A person shall not store information.</p></content></paragraph>
        <paragraph eId="regulation-6-2"><num>(2)</num><content><p>Paragraph (1) applies.</p></content></paragraph>
      </hcontainer>
      <hcontainer name="schedule"><num>SCHEDULE 1</num><heading>Modifications</heading>
        <paragraph><num>1</num><content><p>Schedule paragraph text.</p></content></paragraph></hcontainer>
    </body></act></akomaNtoso>"""

    parsed = parse_akn(xml)
    labels = [s.label for s in parsed.segments]
    assert labels == ["reg. 1 Citation", "reg. 6 Confidentiality of communications",
                      "Sch 1 Modifications", "Sch 1 para 1"]
    # the regulation is emitted whole, sub-paragraphs included — not as separate units
    reg6 = next(s for s in parsed.segments if s.label.startswith("reg. 6"))
    body = parsed.text[reg6.char_start:reg6.char_end]
    assert "shall not store information" in body and "Paragraph (1) applies" in body
    # and it is findable by the pinpoint a judgment would actually use
    from raglex.facade import _anchor_key
    assert _anchor_key("regulation 6") == _anchor_key(reg6.label) == "reg:6"


def test_judgment_quotes_do_not_become_judgment_paragraphs_and_inline_runs_join():
    """Nested quoted provisions are content, not the judgment's own paragraph sequence."""
    from raglex.adapters.uk_caselaw import parse_judgment

    xml = """<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
      <judgment><judgmentBody>
        <level><heading>THE LAW</heading>
          <paragraph eId="para_62"><num>62.</num><content>
            <p>Article 12 provides:</p>
            <paragraph><num>“1.</num><content><p>The controller shall act.</p></content></paragraph>
          </content></paragraph>
          <paragraph eId="para_63"><num>63.</num><content><p>
            <span>FF v </span><span>Ő</span><span>sterreichische</span>
            (“<span>FF</span>”).</p></content></paragraph>
        </level>
      </judgmentBody></judgment>
    </akomaNtoso>""".encode()

    text, _rels, _ncn, segments = parse_judgment(xml)
    assert [s.label for s in segments] == ["THE LAW", "62.", "63."]
    assert "“1. The controller shall act." in text
    assert "Ősterreichische" in text and "Ő sterreichische" not in text
    assert "(“FF”)" in text and "(“ FF ”)" not in text


def test_nested_styled_level_becomes_a_heading_not_a_paragraph():
    from raglex.adapters.uk_caselaw import parse_judgment

    xml = b"""<akomaNtoso><judgment><judgmentBody>
      <paragraph eId="para_33"><num>33.</num><content><p>Main text.</p>
        <level><content><p class="Heading3">Illicit cash</p></content></level>
      </content></paragraph>
      <paragraph eId="para_34"><num>34.</num><content><p>Next.</p></content></paragraph>
    </judgmentBody></judgment></akomaNtoso>"""
    text, _rels, _ncn, segments = parse_judgment(xml)
    assert [(s.label, s.kind) for s in segments] == [
        ("33.", "paragraph"), ("Illicit cash", "heading"), ("34.", "paragraph")]
    assert text.count("Illicit cash") == 1


def test_govuk_code_of_practice_segments_by_numbered_paragraph():
    """A code of practice is cited by paragraph number and nothing else.

    Run through the generic HTML extractor, the whole GOV.UK page came through — cookie
    banner, navigation, contents, footer — with NO segments at all, so a citation of
    "paragraph 3.19" had nothing to land on and the reader could not scroll to it. All
    nine IPA codes were held that way.
    """
    from raglex.facade import _anchor_key
    from raglex.formats import parse

    html = b"""<html><body>
    <div class="cookie-banner"><p>Cookies on GOV.UK</p></div>
    <nav><a href="/">Home</a></nav>
    <div class="govuk-govspeak"><div class="govspeak">
      <h2 id="introduction">1. Introduction</h2>
      <p>1.1. This Code relates to functions under Part 5 of the Act.</p>
      <h3>Necessity and proportionality</h3>
      <p>3.19. A warrant may be issued only where necessary.</p>
      <p>Unnumbered prose continuing that paragraph.</p>
      <script>var x = 1;</script>
    </div></div>
    <footer><p>Crown copyright</p></footer></body></html>"""

    parsed = parse("govuk-govspeak", html)
    assert "Cookies on GOV.UK" not in parsed.text     # page furniture, not the code
    assert "Crown copyright" not in parsed.text
    assert "var x" not in parsed.text
    labels = [s.label for s in parsed.segments]
    assert labels == ["1. Introduction", "para 1.1",
                      "Necessity and proportionality", "para 3.19"]

    # The unnumbered continuation belongs to the paragraph it continues.
    para = next(s for s in parsed.segments if s.label == "para 3.19")
    assert "Unnumbered prose" in parsed.text[para.char_start:para.char_end]

    # And the pinpoint a judgment writes folds onto that segment — 3.19 must not
    # collapse onto 3, which would make every paragraph of a chapter the same anchor.
    assert _anchor_key("paragraph 3.19") == _anchor_key("para 3.19") == "para:3.19"
    assert _anchor_key("para 3.2") != _anchor_key("para 3.19") != _anchor_key("para 3")
