"""The Commission's digital-strategy library (``eu-digital-strategy``).

The listing is pre-filtered server-side to policy/legislation + reports; the substance is
one level down, in each item's downloads panel, behind newsroom redirection links. The
rule that needs pinning hardest is the language one: where a document is published in
several languages the panel lists one title per language, and only English is wanted.
"""

from __future__ import annotations

from datetime import date

from raglex.adapters.eu_digital_strategy import (
    DigitalStrategyLibraryAdapter,
    LibraryItem,
    english_only,
    has_next_page,
    parse_item_page,
    parse_library_page,
    _title_default_instrument,
)

LISTING = """
<div>
<article class="ecl-content-item">
  <div class="ecl-content-block">
    <ul class="ecl-content-block__primary-meta-container">
      <li class="ecl-content-block__primary-meta-item">Policy and legislation</li>
      <li class="ecl-content-block__primary-meta-item">20 July 2026</li>
    </ul>
    <div class="ecl-content-block__title">
      <a href="/en/library/guidelines-transparency-obligations-providers-and-deployers-ai-systems">
        <span>Guidelines on transparency obligations for providers and deployers of AI systems</span></a>
    </div>
  </div>
</article>
<article class="ecl-content-item">
  <div class="ecl-content-block">
    <ul class="ecl-content-block__primary-meta-container">
      <li class="ecl-content-block__primary-meta-item">Report / Study</li>
      <li class="ecl-content-block__primary-meta-item">13 July 2026</li>
    </ul>
    <div class="ecl-content-block__title">
      <a href="/en/library/special-panel-report-child-safety-online"><span>Special panel report: Child safety online</span></a>
    </div>
    <div class="ecl-content-block__description"><div><p>A report on protecting minors.</p></div></div>
  </div>
</article>
<a href="?type=25%7C28&amp;page=1" class="ecl-pagination__link" aria-label="Go to next page">Next</a>
</div>
"""

ITEM = """
<div>
  <h1 class="ecl-page-header__title"><span>Guidelines on transparency obligations</span></h1>
  <li class="ecl-page-header__meta-item">Publication 20 July 2026</li>
  <div class="ecl-file">
    <div class="ecl-file__title">1 - Guidelines on the implementation of the transparency obligations</div>
    <a href="https://ec.europa.eu/newsroom/dae/redirection/document/131215">Download</a>
  </div>
  <div class="ecl-file">
    <div class="ecl-file__title">2 - Communication to the Commission</div>
    <a href="https://ec.europa.eu/newsroom/dae/redirection/document/131214">Download</a>
  </div>
</div>
"""

MULTILINGUAL = """
<div>
  <div class="ecl-file"><div class="ecl-file__title">Code of Practice (English)</div>
    <a href="https://ec.europa.eu/newsroom/dae/redirection/document/1">Download</a></div>
  <div class="ecl-file"><div class="ecl-file__title">Code of Practice (French)</div>
    <a href="https://ec.europa.eu/newsroom/dae/redirection/document/2">Download</a></div>
  <div class="ecl-file"><div class="ecl-file__title">Code of Practice (German)</div>
    <a href="https://ec.europa.eu/newsroom/dae/redirection/document/3">Download</a></div>
  <div class="ecl-file"><div class="ecl-file__title">Annex: technical specification</div>
    <a href="https://ec.europa.eu/newsroom/dae/redirection/document/4">Download</a></div>
</div>
"""


def test_listing_gives_slug_kind_and_date():
    items = parse_library_page(LISTING)
    assert [i.slug for i in items] == [
        "guidelines-transparency-obligations-providers-and-deployers-ai-systems",
        "special-panel-report-child-safety-online"]
    assert items[0].kind == "Policy and legislation"
    assert items[0].published == date(2026, 7, 20)
    assert items[1].summary == "A report on protecting minors."
    assert has_next_page(LISTING) and not has_next_page("<div></div>")


def test_downloads_panel_is_read_in_order_without_its_numbering():
    item = parse_item_page(ITEM, LibraryItem(slug="x", url="https://e/x", title="Item"))
    assert [f["title"] for f in item.files] == [
        "Guidelines on the implementation of the transparency obligations",
        "Communication to the Commission"]
    assert item.files[0]["url"].endswith("/131215")
    assert item.published == date(2026, 7, 20)


def test_only_the_english_version_of_a_multilingual_document_is_taken():
    item = parse_item_page(MULTILINGUAL, LibraryItem(slug="x", url="https://e/x", title="Item"))
    titles = [f["title"] for f in item.files]
    # the three language versions collapse to English; a document with no language
    # suffix is untouched
    assert titles == ["Code of Practice (English)", "Annex: technical specification"]


def test_language_grouping_is_by_title_and_keeps_documents_that_have_no_english():
    files = [{"title": "Report (French)", "url": "a"}, {"title": "Report (German)", "url": "b"}]
    # no English version → keep what there is rather than dropping the document entirely
    assert english_only(files) == files
    # unrelated titles are never grouped together
    mixed = [{"title": "Study A (English)", "url": "a"}, {"title": "Study B (English)", "url": "b"}]
    assert english_only(mixed) == mixed


def test_ai_act_title_declares_the_guidance_default_instrument():
    assert _title_default_instrument(
        "Guidelines on transparency obligations under Article 50 of the AI Act"
    ) == {"id": "32024R1689", "kind": "regulation"}
    assert _title_default_instrument("Study of the AI Act and Digital Services Act") is None


class _Resp:
    def __init__(self, content: bytes) -> None:
        self.content, self.text = content, content.decode()


class _Client:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.seen = pages, []

    def get(self, url: str, **_kw):
        self.seen.append(url)
        for key, body in self.pages.items():
            if key in url:
                return _Resp(body.encode())
        return _Resp(b"<div></div>")


def test_incremental_run_stops_at_the_cursor():
    a = DigitalStrategyLibraryAdapter(client=_Client({"/en/library?": LISTING}))
    assert [s.stable_id for s in a.discover(None, max_pages=1)] == [
        "eu/digital-strategy/guidelines-transparency-obligations-providers-and-deployers-ai-systems",
        "eu/digital-strategy/special-panel-report-child-safety-online"]

    a2 = DigitalStrategyLibraryAdapter(client=_Client({"/en/library?": LISTING}))
    # the cursor sits on the older of the two: only the newer one is new
    got = list(a2.discover("2026-07-13", max_pages=3))
    assert [s.hints["watermark"] for s in got] == ["2026-07-20"]


def test_fetch_stores_the_document_and_records_its_annexes(monkeypatch):
    pdf = b"%PDF-1.7 " + b"x" * 400
    a = DigitalStrategyLibraryAdapter(client=_Client({
        "/en/library/guidelines": ITEM, "/en/library?": LISTING}))
    monkeypatch.setattr(
        "raglex.adapters.eu_digital_strategy.PdfExtractor.extract",
        lambda self, data, **kw: type("E", (), {"text": "1. The Guidelines apply. " * 30,
                                                "needs_ocr": False, "page_spans": []})())

    class _PdfClient(_Client):
        def get(self, url, **kw):
            self.seen.append(url)
            if "redirection/document" in url:
                return _Resp(pdf)
            return super().get(url, **kw)

    a._client = _PdfClient({"/en/library/guidelines": ITEM})
    stub = list(DigitalStrategyLibraryAdapter(
        client=_Client({"/en/library?": LISTING})).discover(None, max_pages=1))[0]
    rec = a.fetch(stub)
    assert rec.raw_ext == "pdf" and rec.doc_type.value == "guidance"
    assert rec.title == "Guidelines on the implementation of the transparency obligations"
    assert rec.extra["download_url"].endswith("/131215")
    assert [f["title"] for f in rec.extra["other_files"]] == ["Communication to the Commission"]
    assert rec.extra["issuer"].startswith("European Commission")


def test_a_proposals_title_is_read_from_its_own_face():
    """A proposal's title sits ABOVE the enacting terms, and the HTML parser keeps the
    enacting terms — right for an act, wrong for a proposal. 52023PC0348's stored text
    begins "Subject matter", so 332 of 963 preparatory documents were titled with their
    own CELEX."""
    from raglex.adapters.eu_preparatory import title_from_html, title_from_text

    html = (b"<html><body><p>EUROPEAN COMMISSION</p><p>Brussels, 4.7.2023</p>"
            b"<p>COM(2023) 348 final</p><p>2023/0202(COD)</p>"
            b"<p>Proposal for a</p>"
            b"<p>REGULATION OF THE EUROPEAN PARLIAMENT AND OF THE COUNCIL</p>"
            b"<p>laying down additional procedural rules relating to the enforcement "
            b"of Regulation (EU) 2016/679</p>"
            b"<p>EXPLANATORY MEMORANDUM</p><p>1. CONTEXT OF THE PROPOSAL</p></body></html>")
    title = title_from_html(html)
    # Rejoined across the block elements EUR-Lex splits it over, and the shouted line
    # softened — not "Regulation Of The European Parliament And Of The Council".
    assert title == ("Proposal for a Regulation of the European Parliament and of the "
                     "Council laying down additional procedural rules relating to the "
                     "enforcement of Regulation (EU) 2016/679")
    # The explanatory memorandum is where the title stops.
    assert "EXPLANATORY" not in title
    # The parsed body has no header to read, which is the whole problem.
    assert title_from_text("Subject matter\nThis Regulation lays down procedural rules.") is None


def test_a_proposal_does_not_adopt_the_act_it_amends_as_its_citation_home():
    """The title names the instrument the proposal would amend; every bare "Article 5"
    in its text is a reference to the PROPOSED text. Filing the proposal's own
    provisions against the act it amends would be wrong — and this rule only became
    reachable for proposals once they had titles at all."""
    from raglex.adapters.eu_consumer_guidance import title_default_instrument
    from raglex.adapters.eu_preparatory import preparatory_subtype

    title = ("Proposal for a Directive of the European Parliament and of the Council "
             "amending Directive 2011/83/EU")
    # The rule itself still recognises the directive …
    assert title_default_instrument(title) == {"id": "32011L0083", "kind": "directive"}
    # … and the adapter refuses to apply it to a document with enacting terms of its own.
    assert preparatory_subtype("52023PC0348")[0] == "proposals"
    assert preparatory_subtype("52023DC0348")[0] == "communications"
