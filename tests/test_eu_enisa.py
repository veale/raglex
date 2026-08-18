from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.eu_enisa import (
    ENISAPublicationsAdapter,
    doc_type_for,
    index_rows,
    last_page,
    parse_publication,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.core.errors import FetchError
from raglex.core.models import DocType

# Trimmed from https://www.enisa.europa.eu/publications, 2026-08-18. Note the TRAILING
# SPACE inside ENISA's own hrefs — it is in the live markup, not a transcription slip.
INDEX_HTML = """
<div class="view-content">
  <div class="publications-item">
    <div class="publication-content">
      <h3><a href="/publications/sme-cra-survey-report">SME CRA Survey Report</a></h3>
      <p class="metadata"><span class="date">
        <time datetime="2026-05-04T09:00:00+02:00" class="datetime">4 May, 2026</time></span></p>
      <div class="content"><p>How SMEs are preparing for the Cyber Resilience Act.</p></div>
    </div>
  </div>
  <div class="publications-item">
    <div class="publication-image"><a href="/publications/nis2-technical-implementation-guidance ">
      <img src="/sites/default/files/2025-06/cover.png"></a></div>
    <div class="publication-content">
      <h3><a href="/publications/nis2-technical-implementation-guidance ">NIS2 Technical Implementation Guidance </a></h3>
      <p class="metadata"><span class="date">
        <time datetime="2025-06-26T10:31:38+02:00" class="datetime">26 June, 2025</time></span></p>
      <div class="content"><p>Technical guidance for the NIS2 digital infrastructure sectors.</p></div>
    </div>
  </div>
</div>
<nav class="pager"><ul>
  <li><a href="?page=1">2</a></li>
  <li><a href="?page=58">Last</a></li>
</ul></nav>
"""

DETAIL_HTML = """
<h1><span class="field field--name-title">NIS2 Technical Implementation Guidance</span></h1>
<article class="node node--type-publications">
  <div class="publication-image">
    <a href="/sites/default/files/2025-06/ENISA_guidance_v1.0.pdf" target="_blank">
      <img src="/sites/default/files/2025-06/cover.png"></a>
    <p class="btn-download-file">
      <a href="/sites/default/files/2025-06/ENISA_guidance_v1.0.pdf" target="_blank">Download</a></p>
  </div>
  <div class="publication-content">
    <p class="publish-date"><span class="label-detail">Publication date:</span>
      <span class="date">June 26, 2025</span></p>
    <div class="field field--name-field-description"><p>This report provides technical
      guidance to support the implementation of the NIS2 Directive.</p></div>
    <div class="field field--name-body"><p><strong>Additional materials</strong></p>
      <p><a href="https://www.enisa.europa.eu/sites/default/files/2025-09/Mapping_table_v1.2.xlsx">Mapping table</a></p></div>
  </div>
  <div class="publication-metadata col-lg-3">
    <ul class="publication-metadata-detail">
      <li><span class="label-detail">Content written for</span>
        <span class="text"><a href="https://www.enisa.europa.eu/audience/private-sector">Private Sector</a></span></li>
      <li><span class="label-detail">Publication type</span><span class="text">ENISA Reports</span></li>
      <li class="related-topics"><span class="label-detail">Topics</span>
        <ul><li><a href="/topics/cybersecurity-of-critical-sectors">Cybersecurity of Critical Sectors</a></li></ul></li>
      <li class="lang"><span class="label-detail">Language</span>
        <span class="text"><a href="/sites/default/files/2025-06/ENISA_guidance_v1.0.pdf" target="_blank">EN</a></span></li>
    </ul>
  </div>
</article>
"""


def test_index_rows_read_the_card_and_survive_enisas_trailing_space():
    rows = index_rows(INDEX_HTML)
    assert [r["url"] for r in rows] == [
        "https://www.enisa.europa.eu/publications/sme-cra-survey-report",
        "https://www.enisa.europa.eu/publications/nis2-technical-implementation-guidance",
    ]
    assert rows[1]["title"] == "NIS2 Technical Implementation Guidance"
    assert rows[1]["date"] == date(2025, 6, 26)
    assert "digital infrastructure" in rows[1]["summary"]
    assert last_page(INDEX_HTML) == 58


def test_publication_page_yields_the_pdf_the_index_never_shows():
    """The whole point of following the card: the index carries an abstract and the
    document is one page down."""
    parsed = parse_publication(DETAIL_HTML, "https://www.enisa.europa.eu/publications/x")
    assert parsed["title"] == "NIS2 Technical Implementation Guidance"
    assert parsed["date"] == date(2025, 6, 26)
    assert parsed["publication_type"] == "ENISA Reports"
    assert parsed["topics"] == ["Cybersecurity of Critical Sectors"]
    assert parsed["audience"] == ["Private Sector"]
    assert "implementation of the NIS2 Directive" in parsed["description"]
    # The PDF is the document; the .xlsx mapping table rides along unread. The cover
    # image link points at the same PDF and must not be listed twice.
    assert [(f["url"].rsplit("/", 1)[-1], f["readable"]) for f in parsed["files"]] == [
        ("ENISA_guidance_v1.0.pdf", True), ("Mapping_table_v1.2.xlsx", False)]


def test_publication_type_separates_the_agency_accounting_for_itself():
    assert doc_type_for("ENISA Reports", "NIS2 Technical Implementation Guidance") is DocType.GUIDANCE
    assert doc_type_for("ENISA Annual Activity Report", None) is DocType.PREPARATORY
    assert doc_type_for("Single Programming Document", "ENISA Work Programme 2026") is DocType.PREPARATORY
    assert doc_type_for(None, "ENISA's view on cybersecurity") is DocType.GUIDANCE


class _FakeClient:
    def __init__(self, pages: dict[int, str]) -> None:
        self.pages = pages
        self.asked: list[int] = []

    def get(self, _url, params=None, **_kw):
        page = int((params or {}).get("page") or 0)
        self.asked.append(page)

        class _R:
            content = self.pages.get(page, "<div class='view-content'></div>").encode()
            url = _url
        return _R()


def test_discovery_reports_a_cursor_over_the_whole_feed():
    """§1: the offset counts the FEED, not the position within a page — a per-page
    counter restarts a 59-page backfill in the middle of whichever page it reached."""
    page1 = INDEX_HTML.replace('href="?page=58"', 'href="?page=1"')
    ad = ENISAPublicationsAdapter(client=_FakeClient({0: page1, 1: page1}))
    stubs = list(ad.discover(None))
    assert [s.stable_id for s in stubs] == [
        "eu/enisa/sme-cra-survey-report", "eu/enisa/nis2-technical-implementation-guidance"]
    assert [s.hints["resume_offset"] for s in stubs] == [0, 1]


def test_a_reported_cursor_is_accepted_back_and_resumes_a_page_early():
    """The bug this whole rule exists for: a resumed backfill that raises TypeError is
    recorded as done, and the run looks finished with 14,000 of 357,000 harvested."""
    client = _FakeClient({2: INDEX_HTML, 3: INDEX_HTML})
    ad = ENISAPublicationsAdapter(client=client, start_offset="30")
    list(ad.discover(None, max_pages=1))
    # 30 // 10 == page 3; resume_floor backs off one page, so the walk restarts at 2 and
    # re-reads ten cards the pipeline already holds rather than skipping any.
    assert client.asked == [2]


def test_an_empty_page_before_the_end_is_an_outage_not_an_exhausted_feed():
    """§3/§5c: "the register is broken" and "there is nothing left" must never produce
    the same outcome — a silently short walk reports a complete backfill."""
    ad = ENISAPublicationsAdapter(client=_FakeClient({0: INDEX_HTML}))
    with pytest.raises(FetchError):
        list(ad.discover(None))


def test_discovery_stops_at_the_cursor():
    ad = ENISAPublicationsAdapter(client=_FakeClient({0: INDEX_HTML, 1: INDEX_HTML}))
    stubs = list(ad.discover("2026-01-01"))
    assert [s.stable_id for s in stubs] == ["eu/enisa/sme-cra-survey-report"]


def test_walking_past_the_last_page_does_not_loop_on_page_zero():
    """ENISA answers an out-of-range ?page= with a 200 carrying page 0 — ten valid
    cards. Read as a fresh page it is an infinite backfill that never finishes and never
    errors."""
    wrapped = INDEX_HTML.replace('href="?page=58"', 'href="?page=1"')
    client = _FakeClient({0: wrapped, 1: wrapped, 2: wrapped})
    stubs = list(ENISAPublicationsAdapter(client=client).discover(None))
    assert len(stubs) == 2 and client.asked == [0, 1]


def test_enisa_is_registered_with_a_declared_jurisdiction():
    """§2: jurisdiction is declared once, in SourceInfo — a source that omits it falls
    through the legacy prefix table into "Other"."""
    assert ADAPTERS["eu-enisa"] is ENISAPublicationsAdapter
    assert SOURCE_INFO["eu-enisa"].jurisdiction == "EU"
    assert SOURCE_INFO["eu-enisa"].kind == "guidance"
    assert INCREMENTAL_MODE["eu-enisa"] == "early-stop"


def test_the_cybersecurity_acquis_resolves_from_the_names_enisa_writes():
    """ENISA guidance names its instruments in prose and essentially never by CELEX. A
    register that resolved none of them would hold the operative detail of EU
    cybersecurity law with no edge to the law it is about."""
    from raglex.citations import extract_citations

    for text, target in (
        ("Article 21 of the NIS2 Directive", "32022L2555"),
        ("the NIS Directive", "32016L1148"),
        ("Article 13 of the Cyber Resilience Act", "32024R2847"),
        ("the Cybersecurity Act", "32019R0881"),
        ("Article 5 of the eIDAS Regulation", "32014R0910"),
        ("the Digital Operational Resilience Act", "32022R2554"),
        ("the Critical Entities Resilience Directive", "32022L2557"),
        ("the Cyber Solidarity Act", "32025R0038"),
    ):
        got = {c.candidate_id for c in extract_citations(text)}
        assert target in got, f"{text} → {got}"


def test_eidas_and_eidas_2_are_different_instruments():
    """Regulation (EU) 2024/1183 amends eIDAS to create the Digital Identity Wallet.
    Letting the eIDAS acronym swallow "eIDAS 2" would file the Wallet's provisions
    against a Regulation written ten years before it."""
    from raglex.citations import extract_citations

    assert {c.candidate_id for c in extract_citations("under eIDAS the trust service")} \
        == {"32014R0910"}
    assert "32024R1183" in {c.candidate_id for c in extract_citations("the eIDAS 2 Regulation")}
    assert "32014R0910" not in {c.candidate_id for c in extract_citations("the eIDAS 2 Regulation")}


def test_the_bare_acronyms_are_enisas_alone():
    """"CRA" is the Canada Revenue Agency in the Canadian corpus and "DORA" is the
    Defence of the Realm Act; the certainty is spent only where it holds."""
    from raglex.citations import extract_citations
    from raglex.citations.stage import _SOURCE_ALIASES

    aliases = _SOURCE_ALIASES["eu-enisa"]
    for text, target in (
        ("Article 13 of the CRA", "32024R2847"),
        ("Article 8 of DORA", "32022R2554"),
        ("Article 51 of the CSA", "32019R0881"),
        ("the CER Directive", "32022L2557"),
    ):
        got = {c.candidate_id for c in extract_citations(text, aliases=aliases)}
        assert target in got, f"{text} → {got}"
    # …and the bare acronyms assert nothing without them. ("the CER Directive" is
    # deliberately absent from this list: the qualified name is unambiguous anywhere and
    # is mapped corpus-wide; it is the bare "CER" that is scoped.)
    for text in ("Article 13 of the CRA", "Article 8 of DORA", "Article 51 of the CSA",
                 "Article 4 of CER"):
        assert not [c for c in extract_citations(text) if c.candidate_id], text
