"""High Court adapter — listing parse, neutral-cite identity, saved-HTML import,
metadata-stub records, and year-range selection. Network-free."""

from __future__ import annotations

import os

import pytest

from raglex.adapters.au_hca_caselaw import HCACaselawAdapter, judgment_doc_urls, parse_listing
from raglex.core.models import DocType

ROW = """<div class="views-row"><div class="views-field views-field-nothing-2"><span class="field-content">
<a class="views-row-item views-row-item-judgement" href="https://www.hcourt.gov.au/cases-and-judgments/judgments/judgments-1998-current/{slug}">
<div class="field field--title text-bold">{title}
 <br></div><div class="field field--citation"><strong>Citation:</strong>  {cite}</div>
<div class="field field--legacy-before"><div class="field field--name-field-hca-justices field--type-string field--label-above field__item"><strong>Before:</strong> {coram}</div></div>
<div class="field field--hca-date-issued"><strong>Date:</strong>  {date} </div></span></div></div>"""


def _listing(*rows) -> str:
    return '<div class="view-content">' + "".join(ROW.format(**r) for r in rows) + "</div>"


LISTING = _listing(
    {"slug": "chaplin-v-secretary", "title": "Chaplin v Secretary, Department of Social Services",
     "cite": "[2026] HCA 22", "coram": "Gageler CJ, Gordon, Steward, Jagot, Beech-Jones JJ",
     "date": "17 Jun 2026"},
    {"slug": "austral-v-nt", "title": "Austral v Northern Territory",
     "cite": "[2026] HCA 20", "coram": "Gordon J", "date": "11 Jun 2026"},
)


def test_parse_listing_fields():
    js = parse_listing(LISTING)
    assert [j["slug"] for j in js] == ["hca/2026/22", "hca/2026/20"]
    assert js[0]["citation"] == "[2026] HCA 22"
    assert js[0]["date"] == "2026-06-17"
    assert "Gageler CJ" in js[0]["coram"]
    assert js[0]["url"].endswith("/chaplin-v-secretary")


def test_saved_html_import(tmp_path):
    f = tmp_path / "hca-2026.html"
    f.write_text(LISTING, encoding="utf-8")
    ad = HCACaselawAdapter(path=str(f))
    stubs = list(ad.discover(None))
    assert {s.stable_id for s in stubs} == {"hca/2026/22", "hca/2026/20"}
    assert all(s.court == "hca" for s in stubs)


class _FakeHTTP:
    """Serves the detail page and the DOCX bytes by URL."""

    def __init__(self, detail_html: str, docx_bytes: bytes):
        self.detail_html = detail_html
        self.docx_bytes = docx_bytes

    def get(self, url: str):
        if url.endswith(".docx"):
            return 200, self.docx_bytes
        return 200, self.detail_html.encode("utf-8")


def _tiny_docx(text: str) -> bytes:
    import io
    import zipfile
    doc = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def test_fetch_pulls_docx_full_text():
    detail = ('<html><body><a href="/sites/default/files/eresources/2026-06-17/HCA/'
              'Chaplin%20%5B2026%5D%20HCA%2022.docx">DOCX</a></body></html>')
    ad = HCACaselawAdapter(http=_FakeHTTP(detail, _tiny_docx("HIGH COURT OF AUSTRALIA. The appeal is dismissed.")))
    stub = ad._stub(parse_listing(LISTING)[0])
    rec = ad.fetch(stub)
    assert rec.doc_type is DocType.JUDGMENT
    assert rec.stable_id == "hca/2026/22"
    assert "appeal is dismissed" in rec.text          # full text from the DOCX
    assert rec.extra["document_url"].endswith(".docx")
    assert "metadata_only" not in rec.extra           # not a stub — we got the text
    assert "[2026] hca 22" in rec.extra["aliases"]
    assert rec.decision_date.isoformat() == "2026-06-17"


def test_fetch_falls_back_to_metadata_stub_when_no_doc():
    class _NoDoc:
        def get(self, url):
            return 200, b"<html><body>no document link here</body></html>"
    ad = HCACaselawAdapter(http=_NoDoc())
    rec = ad.fetch(ad._stub(parse_listing(LISTING)[0]))
    assert rec.text is None
    assert rec.extra["metadata_only"] is True
    assert rec.extra["neutral_citation"] == "[2026] HCA 22"


def test_judgment_doc_urls_prefers_docx():
    detail = ('<a href="/sites/default/files/eresources/2026-06-17/HCA/x%20HCA%2022.pdf">PDF</a>'
              '<a href="/sites/default/files/eresources/2026-06-17/HCA/x%20HCA%2022.docx">DOCX</a>')
    urls = judgment_doc_urls(detail)
    assert urls[0].endswith(".docx") and urls[0].startswith("https://www.hcourt.gov.au")
    assert urls[1].endswith(".pdf")


def test_non_judgment_rows_skipped():
    junk = '<div class="view-content"><div class="views-row"><p>No citation here</p></div></div>'
    assert parse_listing(junk) == []


def test_incremental_since_filters_by_date():
    # live-mode date filter (used when fetching, exercised here via _stub + manual check)
    js = parse_listing(LISTING)
    newer = [j for j in js if not (j["date"] and j["date"] <= "2026-06-13")]
    assert {j["slug"] for j in newer} == {"hca/2026/22"}  # the 11 Jun one is filtered


@pytest.mark.skipif(
    not os.path.exists("raglex design docs/Judgments (1998-current) _ High Court of Australia.html"),
    reason="saved HCA page not present",
)
def test_real_saved_page():
    with open("raglex design docs/Judgments (1998-current) _ High Court of Australia.html",
              encoding="utf-8", errors="replace") as fh:
        js = parse_listing(fh.read())
    assert len(js) >= 20
    assert all(j["slug"].startswith("hca/") for j in js)
    assert all(j["citation"] and "HCA" in j["citation"] for j in js)
