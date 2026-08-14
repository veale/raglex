from __future__ import annotations

import io
import zipfile

from raglex.adapters.parliamentary_reports import (
    AssembleeInformationReportsAdapter,
    SenatComparativeLawAdapter,
    SenatInformationReportsAdapter,
    TweedeKamerReportsAdapter,
    _whole_report_link,
    _tk_filter,
    parse_assemblee_listing,
    parse_lc_listing,
    parse_senat_atom,
    parse_senat_listing,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO


class _Response:
    def __init__(self, content=b"", *, headers=None, payload=None):
        self.content = content
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses[url]
        if isinstance(value, list):
            value = value.pop(0)
        return value


def _docx(text: str) -> bytes:
    xml = ("<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
           f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def test_all_four_sources_are_registered_with_truthful_incremental_modes():
    assert INCREMENTAL_MODE["fr-senat-reports"] == "early-stop"
    assert INCREMENTAL_MODE["fr-senat-lc"] == "full-walk"
    assert INCREMENTAL_MODE["fr-an-reports"] == "early-stop"
    assert INCREMENTAL_MODE["nl-tk-reports"] == "server"
    for key in ("fr-senat-reports", "fr-senat-lc", "fr-an-reports", "nl-tk-reports"):
        assert key in ADAPTERS and SOURCE_INFO[key].kind == "preparatory"
        for option in SOURCE_INFO[key].options:
            ADAPTERS[key](**{option.name: option.placeholder})


def test_senat_listing_uses_notice_identity_title_and_date():
    raw = """
      <ul><li><strong>Protection sociale des indépendants</strong><ul><li>
      <a href='/notice-rapport/2025/r25-883-notice.html'>Rapport n 883</a>
      du 8 juillet 2026</li></ul></li></ul>
    """.encode()
    stub = parse_senat_listing(raw)[0]
    assert stub.stable_id == "fr/senat/r25-883"
    assert stub.title == "Protection sociale des indépendants"
    assert stub.hint_date.isoformat() == "2026-07-08"


def test_senat_atom_keeps_information_reports_not_bill_reports():
    raw = """<?xml version='1.0' encoding='ISO-8859-15'?>
    <feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><title>Information</title><link href='http://www.senat.fr/notice-rapport/2025/r25-883-notice.html'/>
        <published>2026-07-08T10:00:00Z</published></entry>
      <entry><title>Bill</title><link href='http://www.senat.fr/rap/l25-904/l25-904.html'/>
        <published>2026-07-24T10:00:00Z</published></entry>
    </feed>""".encode("iso-8859-15")
    stubs = parse_senat_atom(raw)
    assert [s.stable_id for s in stubs] == ["fr/senat/r25-883"]
    assert stubs[0].landing_url.startswith("https://")


def test_senat_fetch_follows_notice_then_prefers_complete_html():
    notice = "https://www.senat.fr/notice-rapport/2025/r25-883-notice.html"
    landing = "https://www.senat.fr/rap/r25-883/r25-883.html"
    mono = "https://www.senat.fr/rap/r25-883/r25-883_mono.html"
    client = _Client({
        notice: _Response(b'<link rel="canonical" href="' + landing.encode() + b'">'),
        landing: _Response(b'<a href="r25-883-syn.pdf">Synthese</a>'
                           b'<a href="r25-883_mono.html">En une page HTML</a>'),
        mono: _Response(("<main><nav>chrome</nav><h1>Full report</h1><p>" +
                         "complete evidence " * 30 + "</p></main>").encode(),
                        headers={"content-type": "text/html"}),
    })
    adapter = SenatInformationReportsAdapter(client=client)
    stub = parse_senat_listing(b"<li><strong>T</strong><ul><li><a href='" +
                                notice.encode() + b"'>R</a> du 8 juillet 2026</li></ul></li>")[0]
    record = adapter.fetch(stub)
    assert record.raw_ext == "html" and "complete evidence" in record.text
    assert "chrome" not in record.text
    assert record.extra["document_url"] == mono


def test_senat_whole_report_falls_back_to_pdf_not_the_summary():
    raw = (b'<a href="r25-883-syn.pdf">Voir essentiel</a>'
           b'<a href="r25-8831.pdf">PDF</a>')
    assert _whole_report_link(raw, "https://www.senat.fr/rap/r25-883/r25-883.html") == \
        "https://www.senat.fr/rap/r25-883/r25-8831.pdf"


def test_lc_collapsed_sections_are_parsed_without_javascript():
    raw = """
    <div class='accordion-item'><button class='accordion-button collapsed'>1999</button>
      <div class='collapse'><a href='/notice-rapport/1999/lc68-notice.html'>
      LC 68 : L'interruption volontaire de grossesse (janvier 2000)</a></div></div>
    """.encode()
    stub = parse_lc_listing(raw)[0]
    assert stub.stable_id == "fr/senat/lc68"
    assert stub.title == "L'interruption volontaire de grossesse"
    # The containing archive heading is the source's publication grouping.
    assert stub.hint_date.isoformat() == "1999-01-01"


def test_assemblee_parser_preserves_volume_identity_and_date():
    raw = b"""
    <ul class='liens-liste'><li data-id='OMC_RINFANR5L17B2474-tII'>
      <h3>Rapport sur la natalite - N 2474 tome II</h3>
      <ul><li><span class='heure'>Mis en ligne mercredi 22 juillet 2026 a 19h45</span></li>
      <li><a href='https://www.assemblee-nationale.fr/dyn/old/17/rap-info/i2474-tII.asp'>Document</a></li></ul>
    </li></ul>
    """
    stub = parse_assemblee_listing(raw, 17)[0]
    assert stub.stable_id == "fr/an/rinfanr5l17b2474-tii"
    assert stub.hint_date.isoformat() == "2026-07-22"


def test_assemblee_fetch_chooses_full_opendata_html_over_pdf():
    landing = "https://www.assemblee-nationale.fr/dyn/old/17/rap-info/i3074.asp"
    html = "https://www.assemblee-nationale.fr/dyn/opendata/RINFANR5L17B3074.html"
    client = _Client({
        landing: _Response(b'<a href="/dyn/17/r.pdf">PDF</a>'
                           b'<a href="/dyn/opendata/RINFANR5L17B3074.html">HTML</a>'),
        html: _Response(("<html><body><h1>RAPPORT</h1><p>" + "full body " * 40 +
                         "</p></body></html>").encode(), headers={"content-type": "text/html"}),
    })
    adapter = AssembleeInformationReportsAdapter(client=client, legislatures="17")
    from raglex.core.models import Stub
    record = adapter.fetch(Stub("fr/an/rinf", landing, landing, title="Report",
                                hints={"legislature": 17}))
    assert record.raw_ext == "html" and record.court == "French National Assembly"
    assert record.extra["document_url"] == html


def test_tweede_kamer_filter_excludes_bills_and_votes():
    query = _tk_filter(("Rapport", "Verslag van een commissiedebat"))
    assert "Soort eq 'Rapport'" in query
    assert "wetsvoorstel" not in query and "Stemming" not in query


def test_tweede_kamer_odata_is_modified_cursor_and_fetches_full_docx():
    endpoint = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0/Document"
    uuid = "030955a2-4d4f-410f-a630-3d7cd5512ce2"
    resource = f"{endpoint}({uuid})/Resource"
    payload = {"@odata.count": 1, "value": [{
        "Id": uuid, "Soort": "Verslag van een schriftelijk overleg",
        "DocumentNummer": "2026D38058", "Titel": "JBZ-Raad",
        "Onderwerp": "Verslag", "Datum": "2026-08-07T00:00:00+02:00",
        "GewijzigdOp": "2026-08-10T09:50:06.79+02:00",
        "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }]}
    body = "Volledig commissieverslag met Europese instrumenten. " * 10
    client = _Client({
        endpoint: _Response(payload=payload),
        resource: _Response(_docx(body), headers={"content-type": payload["value"][0]["ContentType"]}),
    })
    adapter = TweedeKamerReportsAdapter(client=client, page_size=250)
    stub = next(adapter.discover("2026-08-01T00:00:00+02:00"))
    params = client.calls[0][1]["params"]
    assert "GewijzigdOp ge 2026-08-01T00:00:00+02:00" in params["$filter"]
    assert params["$orderby"] == "GewijzigdOp desc"
    assert stub.hints["watermark"] == "2026-08-10T09:50:06.79+02:00"
    assert stub.hints["contenthash"] == stub.hints["watermark"]
    record = adapter.fetch(stub)
    assert record.raw_ext == "docx" and "Europese instrumenten" in record.text
    assert record.extra["document_number"] == "2026D38058"
    assert record.extra["contenthash"] == stub.hints["watermark"]
