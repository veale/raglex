from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters.eu_enisa import (
    ENISAPublicationsAdapter,
    doc_type_for,
    index_rows,
    parse_publication,
    sitemap_publications,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO
from raglex.core.errors import FetchError
from raglex.core.models import DocType, Stub

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


def test_a_dead_url_is_absent_but_a_blip_is_not():
    """§3: "the page is gone" and "the site is having a moment" must not produce the same
    outcome. Swallowing a 429 as None files a publication we still hold no text for as
    not-found, and with no date cursor nothing ever goes back for it."""
    class _Client:
        def __init__(self, error): self.error = error
        def get(self, *_a, **_kw): raise self.error

    gone = ENISAPublicationsAdapter(client=_Client(FetchError("404", transient=False)))
    assert gone.fetch(Stub(stable_id="eu/enisa/x", landing_url="u", raw_url="u")) is None

    blip = ENISAPublicationsAdapter(client=_Client(FetchError("429", transient=True)))
    with pytest.raises(FetchError):
        blip.fetch(Stub(stable_id="eu/enisa/x", landing_url="u", raw_url="u"))


def test_publication_type_separates_the_agency_accounting_for_itself():
    assert doc_type_for("ENISA Reports", "NIS2 Technical Implementation Guidance") is DocType.GUIDANCE
    assert doc_type_for("ENISA Annual Activity Report", None) is DocType.PREPARATORY
    assert doc_type_for("Single Programming Document", "ENISA Work Programme 2026") is DocType.PREPARATORY
    assert doc_type_for(None, "ENISA's view on cybersecurity") is DocType.GUIDANCE


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.enisa.europa.eu/</loc><changefreq>daily</changefreq></url>
<url><loc>https://www.enisa.europa.eu/news/some-news-item</loc><lastmod>2026-08-05T09:00Z</lastmod></url>
<url><loc>https://www.enisa.europa.eu/publications/sme-cra-survey-report</loc><lastmod>2026-08-05T09:00Z</lastmod></url>
<url><loc>https://www.enisa.europa.eu/publications/nis2-technical-implementation-guidance</loc><lastmod>2025-06-26T10:31Z</lastmod></url>
<url><loc>https://www.enisa.europa.eu/publications/an-undated-one</loc></url>
</urlset>"""


class _FakeClient:
    """Answers the sitemap and the paged index; records what was asked for."""

    def __init__(self, pages: dict[int, str] | None = None, sitemap: str = SITEMAP_XML) -> None:
        self.pages = pages or {}
        self.sitemap = sitemap
        self.asked: list[int] = []
        self.sitemap_reads = 0

    def get(self, url, params=None, **_kw):
        if "sitemap" in url:
            self.sitemap_reads += 1
            body = self.sitemap
        else:
            page = int((params or {}).get("page") or 0)
            self.asked.append(page)
            body = self.pages.get(page, "<div class='view-content'></div>")

        class _R:
            content = body.encode()

        _R.url = url
        return _R()


def test_the_sitemap_is_the_manifest_and_lists_only_publications():
    rows = sitemap_publications(SITEMAP_XML)
    assert [r["url"].rsplit("/", 1)[-1] for r in rows] == [
        "sme-cra-survey-report", "nis2-technical-implementation-guidance", "an-undated-one"]
    assert rows[0]["lastmod"] == date(2026, 8, 5)
    assert rows[-1]["lastmod"] is None


def test_discovery_enumerates_the_sitemap_not_the_broken_pager():
    """The paged index repeats page 0 for about a third of its page numbers, with a 200
    and a well-formed body, so a deep walk of it stops early and reports itself complete
    — 20 of 593 on the first live run. The sitemap is complete in one request."""
    client = _FakeClient(pages={0: INDEX_HTML})
    stubs = list(ENISAPublicationsAdapter(client=client).discover(None))
    assert client.sitemap_reads == 1
    assert [s.stable_id for s in stubs] == [
        "eu/enisa/sme-cra-survey-report",
        "eu/enisa/nis2-technical-implementation-guidance",
        "eu/enisa/an-undated-one"]
    # The sitemap's lastmod is when the NODE changed. Offering it as the publication date
    # would back-date this year's edit onto a 2012 report; fetch() reads the real one.
    assert all(s.hint_date is None for s in stubs)


def test_since_is_ignored_because_a_resumed_backfill_arrives_carrying_one():
    """The pipeline hands a backfill its recorded FRONTIER as `since`. Branching on it
    sent a resumed backfill down the keep-current path, which discovered ONE document and
    reported done — the same silent shortfall in a new costume."""
    a = list(ENISAPublicationsAdapter(client=_FakeClient()).discover(None))
    b = list(ENISAPublicationsAdapter(client=_FakeClient()).discover("2026-07-30"))
    assert [s.stable_id for s in a] == [s.stable_id for s in b] and len(a) == 3


def test_the_index_head_supplies_what_the_sitemap_has_not_caught_up_with():
    """One request to the only page that always answers, so a publication posted this
    morning is not invisible until the sitemap regenerates."""
    fresh = INDEX_HTML.replace("sme-cra-survey-report", "brand-new-publication")
    stubs = list(ENISAPublicationsAdapter(client=_FakeClient({0: fresh})).discover(None))
    assert stubs[0].stable_id == "eu/enisa/brand-new-publication"
    # …and it comes with the index's date, which the sitemap cannot give.
    assert stubs[0].hint_date == date(2026, 5, 4)
    assert len(stubs) == 4          # the newcomer plus the three in the manifest


def test_a_flaky_index_never_fails_a_run_the_manifest_can_answer():
    class _Broken(_FakeClient):
        def get(self, url, params=None, **kw):
            if "sitemap" not in url:
                raise FetchError("index down", transient=True)
            return super().get(url, params, **kw)

    stubs = list(ENISAPublicationsAdapter(client=_Broken()).discover(None))
    assert len(stubs) == 3


def test_an_empty_sitemap_is_an_outage_not_an_empty_register():
    client = _FakeClient(sitemap="<urlset></urlset>")
    with pytest.raises(FetchError):
        list(ENISAPublicationsAdapter(client=client).discover(None))


def test_discovery_reports_a_cursor_over_the_whole_feed():
    """§1: the offset counts the FEED, not a position within one page — a per-page
    counter restarts a walk in the middle of whichever page it reached."""
    back = list(ENISAPublicationsAdapter(client=_FakeClient()).discover(None))
    assert [s.hints["resume_offset"] for s in back] == [0, 1, 2]


def test_a_reported_cursor_is_accepted_back_and_resumes_early():
    """The bug this whole rule exists for: a resumed backfill that raises TypeError is
    recorded as done, and the run looks finished with 14,000 of 357,000 harvested."""
    # resume_floor backs the checkpoint off by a whole page, so a run that reported it
    # had reached item 12 restarts at 2 — re-emitting ten the pipeline already holds
    # rather than risking one it does not.
    ad = ENISAPublicationsAdapter(client=_FakeClient(), start_offset="12")
    assert [s.hints["resume_offset"] for s in ad.discover(None)] == [2]
    # A cursor past the end of the manifest simply yields nothing, and never raises.
    ad2 = ENISAPublicationsAdapter(client=_FakeClient(), start_offset="900")
    assert list(ad2.discover(None)) == []


def test_enisa_is_registered_with_a_declared_jurisdiction():
    """§2: jurisdiction is declared once, in SourceInfo — a source that omits it falls
    through the legacy prefix table into "Other"."""
    assert ADAPTERS["eu-enisa"] is ENISAPublicationsAdapter
    assert SOURCE_INFO["eu-enisa"].jurisdiction == "EU"
    assert SOURCE_INFO["eu-enisa"].kind == "guidance"
    assert INCREMENTAL_MODE["eu-enisa"] == "full-walk"


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
        ("Article 4 of the CER Directive", "32022L2557"),
    ):
        got = {c.candidate_id for c in extract_citations(text, aliases=aliases)}
        assert target in got, f"{text} → {got}"
    # …and the bare acronyms assert nothing without them. ("the CER Directive" is
    # deliberately absent from this list: the qualified name is unambiguous anywhere and
    # is mapped corpus-wide; it is the bare "CER" that is scoped.)
    for text in ("Article 13 of the CRA", "Article 8 of DORA", "Article 51 of the CSA"):
        assert not [c for c in extract_citations(text) if c.candidate_id], text


def test_an_alias_that_is_also_an_english_word_is_not_in_the_list():
    """Aliases match case-insensitively, so a bare acronym that is also a word matches
    the word. Measured on the live register: "RED" hit "Scores in red", "Red Hat LLC"
    and "red teaming" and nothing else; "CER" hit the Community of European Railway; and
    "NIS" hit "NIS sectors", "NIS360" and — worst — the inside of "the Network and
    Information Systems (NIS) 2 Directive", filing a NIS2 reference against NIS1."""
    from raglex.citations import extract_citations
    from raglex.citations.stage import _SOURCE_ALIASES

    aliases = _SOURCE_ALIASES["eu-enisa"]
    for text in ("Scores in red indicate values below 2.5",
                 "Dave Russo Red Hat LLC",
                 "an always-on capability for red teaming",
                 "expert groups in railway associations (EIM, CER, UNIFE)",
                 "the agency supported the NIS sectors through sectoral communities",
                 "ENISA NIS360 latest insights",
                 "the Network and Information Systems (NIS) 2 Directive",
                 "referenced in the Implementing Regulation establishing the EUCC"):
        got = [c.candidate_id for c in extract_citations(text, aliases=aliases)
               if c.candidate_id in ("32014L0053", "32022L2557", "32016L1148", "32024R2690")]
        assert not got, f"{text} → {got}"
