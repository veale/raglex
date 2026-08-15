from __future__ import annotations

from datetime import date

from raglex.adapters.de_datenschutzarchiv import (
    DatenschutzArchivReportsAdapter,
    detail_metadata,
    is_activity_report,
    jurisdiction_code,
    last_page,
    listing_items,
    rss_items,
    stable_id,
)
from raglex.adapters.registry import source_catalog


LISTING = b"""<div id="tx-solr-search">
<a class="solr-custom-tlp-listpreviewimage__item"
 href="/detailansicht/Dokumente/2025/TB_Oesterreich_2025_de.pdf">
 <div class="dt-header"><h3>T\xc3\xa4tigkeitsbericht \xc3\x96sterreich DSB 2025</h3></div>
 <div class="dt-language">de</div><div class="dt-doctype"><span>T\xc3\xa4tigkeitsbericht</span></div>
</a>
<a class="solr-custom-tlp-listpreviewimage__item"
 href="/detailansicht/Dokumente/2026/ST_EDPB_Opinion_202612_en.pdf">
 <div class="dt-header"><h3>EDPB: Opinion 12/2026</h3></div>
 <div class="dt-language">en</div><div class="dt-doctype"><span>T\xc3\xa4tigkeitsbericht</span></div>
</a>
<nav class="solr-pagination"><a href="?tx_solr%5Bpage%5D=87">87</a></nav>
</div>"""

DETAIL = b"""<div class="tx-dsaextension"><h1>T\xc3\xa4tigkeitsbericht EDPS 2025</h1>
<div class="tx-dsaextension-fileinfos__item">
 <div class="tx-dsaextension-fileinfos__item__label"><p>Dokumentensprache</p></div>
 <div class="tx-dsaextension-fileinfos__item__content"><p>Englisch</p></div></div>
<div class="tx-dsaextension-fileinfos__item">
 <div class="tx-dsaextension-fileinfos__item__label"><p>Organisation</p></div>
 <div class="tx-dsaextension-fileinfos__item__content"><ul><li>EDPS</li></ul></div></div>
<div class="tx-dsaextension-fileinfos__item">
 <div class="tx-dsaextension-fileinfos__item__label"><p>Ver\xc3\xb6ffentlichungsdatum</p></div>
 <div class="tx-dsaextension-fileinfos__item__content"><p>07.05.2026</p></div></div>
<div class="tx-dsaextension-filedownload">
 <a href="/fileadmin/Dokumente/2025/TB_EDPS_2025_en.pdf">Download PDF</a></div>
</div>"""

RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss><channel>
<item><title>T\xc3\xa4tigkeitsbericht EDPS 2025</title>
 <link>https://datenschutzarchiv.org/detailansicht/Dokumente/2025/TB_EDPS_2025_en.pdf</link>
 <pubDate>Tue, 04 Aug 2026 00:00:00 +0000</pubDate></item>
<item><title>EDPB Opinion 18/2026</title>
 <link>https://datenschutzarchiv.org/detailansicht/Dokumente/2026/ST_EDPB_Opinion18_en.pdf</link>
 <pubDate>Thu, 13 Aug 2026 00:00:00 +0000</pubDate></item>
</channel></rss>"""


class Response:
    def __init__(self, content: bytes, url: str):
        self.content = content
        self.url = url
        self.status_code = 200


class Client:
    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, params=None, headers=None):
        self.calls.append(url)
        return Response(self.responses[url], url)


def test_listing_uses_real_type_but_rejects_the_polluted_edpb_tail():
    rows = listing_items(LISTING)
    assert len(rows) == 2 and last_page(LISTING) == 87
    assert is_activity_report(rows[0]) is True
    # The live site itself labels these opinions "Tätigkeitsbericht" on page 86/87.
    assert rows[1]["document_type"] == "Tätigkeitsbericht"
    assert is_activity_report(rows[1]) is False


def test_rss_watch_requires_taetigkeitsbericht_and_shares_listing_identity():
    rows = rss_items(RSS)
    assert len(rows) == 1
    assert rows[0]["date"] == date(2026, 8, 4)
    assert stable_id(rows[0]["url"]) == stable_id(
        "https://datenschutzarchiv.org/fileadmin/Dokumente/2025/TB_EDPS_2025_en.pdf")


def test_detail_page_is_html_and_exposes_the_actual_fileadmin_pdf():
    row = detail_metadata(DETAIL, page_url=(
        "https://datenschutzarchiv.org/detailansicht/Dokumente/2025/TB_EDPS_2025_en.pdf"))
    assert row["title"] == "Tätigkeitsbericht EDPS 2025"
    assert row["date"] == date(2026, 5, 7)
    assert row["organisations"] == ["EDPS"]
    assert row["pdf_url"] == (
        "https://datenschutzarchiv.org/fileadmin/Dokumente/2025/TB_EDPS_2025_en.pdf")


def test_foreign_issuer_patterns_are_filed_in_their_own_jurisdictions():
    from raglex.facade import Facade

    assert jurisdiction_code("Tätigkeitsbericht Österreich DSB 2025", []) == "at"
    assert jurisdiction_code("Tätigkeitsbericht Liechtenstein 2021", []) == "li"
    assert jurisdiction_code("12. Jahresbericht Art. 29 Gruppe 2008", []) == "eu"
    assert jurisdiction_code("Tätigkeitsbericht EDBP 2025", []) == "eu"  # archive typo
    assert jurisdiction_code("Tätigkeitsbericht Berlin LfD 2025", []) == "de"
    assert Facade._BODY_JURISDICTIONS[  # the read model overrides the DE register bucket
        "european data protection supervisor (edps)"] == "European Union"
    assert Facade._BODY_JURISDICTIONS[
        "austrian data protection authority"] == "Austria"
    assert Facade._BODY_JURISDICTIONS[
        "data protection authority of liechtenstein"] == "Liechtenstein"


def test_incremental_discovery_reads_only_the_filtered_rss_surface():
    from raglex.adapters.de_datenschutzarchiv import RSS as FEED

    client = Client({FEED: RSS})
    rows = list(DatenschutzArchivReportsAdapter(client=client).discover("2026-08-01"))
    assert len(rows) == 1 and rows[0].title == "Tätigkeitsbericht EDPS 2025"
    assert rows[0].hints["discovered_via"] == "rss"
    assert client.calls == [FEED]


def test_fetch_follows_detail_then_pdf_and_keeps_verbose_eu_issuer(monkeypatch):
    from raglex.adapters import de_datenschutzarchiv as adapter

    detail = "https://datenschutzarchiv.org/detailansicht/Dokumente/2025/TB_EDPS_2025_en.pdf"
    pdf = "https://datenschutzarchiv.org/fileadmin/Dokumente/2025/TB_EDPS_2025_en.pdf"
    client = Client({detail: DETAIL, pdf: b"%PDF-1.7 test"})
    monkeypatch.setattr(adapter, "text_or_ocr", lambda *_a, **_k: (
        "A readable annual report " * 20, False, [(1, 0, 100)], "pdf-text"))
    stub = next(DatenschutzArchivReportsAdapter(client=Client({
        adapter.RSS: RSS})).discover("2026-08-01"))
    record = DatenschutzArchivReportsAdapter(client=client).fetch(stub)

    assert client.calls == [detail, pdf]
    assert record is not None and record.raw_bytes.startswith(b"%PDF")
    assert record.court == "European Data Protection Supervisor (EDPS)"
    assert record.extra["jurisdiction"] == "eu"
    assert record.language == "en" and record.segments[0].label == "p. 1"


def test_source_catalog_declares_monthly_watch_capability_and_options():
    row = next(row for row in source_catalog()
               if row["key"] == "de-datenschutzarchiv-reports")
    assert row["incremental_mode"] == "early-stop"
    assert row["kind"] == "preparatory"
    assert {option["name"] for option in row["options"]} == {"ocr", "max_ocr_pages"}
