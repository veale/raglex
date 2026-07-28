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
