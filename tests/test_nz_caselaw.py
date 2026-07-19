"""New Zealand Supreme Court adapter + PDF parser. Network-free (a fake client serves
the RSS, case page, and a synthetic judgment PDF built with PyMuPDF)."""

from __future__ import annotations

import pytest

from raglex.adapters.nz_caselaw import (
    NZSupremeCourtAdapter,
    _filename_citation,
    _find_pdf_href,
    _parse_rss,
    _party_title,
    _provisional_id,
)
from datetime import date

from raglex.core.models import DocType
from raglex.formats.nzsc_pdf import (
    _parse_coram,
    file_number_id,
    neutral_citation_id,
    parse_header,
    parse_nzsc_pdf,
)

fitz = pytest.importorskip("fitz")  # PyMuPDF — the parser's layout engine


# -- a synthetic NZSC judgment PDF ------------------------------------------
def _make_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()  # US-letter, 612 x 792
    page.insert_text((72, 72), "IN THE SUPREME COURT OF NEW ZEALAND", fontsize=12)
    page.insert_text((72, 96), "[2026] NZSC 88", fontsize=12)
    page.insert_text((72, 150), "[1] The appellant challenges the Court of Appeal decision.", fontsize=12)
    page.insert_text((72, 200), "[2] We dismiss the appeal for the reasons that follow.", fontsize=12)
    # footnote apparatus: smaller font, bottom of the page
    page.insert_text((72, 720), "1 R v Smith [2020] NZSC 1.", fontsize=9)
    page.insert_text((72, 740), "2 See also Jones v Attorney-General [2019] NZSC 5.", fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


_CASE_HTML = """
<html><head><title>Re Rafiq — Courts of New Zealand</title>
<meta property="og:title" content="Smith v Attorney-General [2026] NZSC 88"></head>
<body><h1 class="case__title">Smith v Attorney-General</h1>
<div class="case__decision"><a href="/assets/cases/2026/2026-NZSC-88.pdf">Judgment (PDF)</a>
<a href="/assets/cases/2026/MR-2026-NZSC-88.pdf">Media release</a></div>
</body></html>
"""

_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Smith v Attorney-General</title>
    <link>https://www.courtsofnz.govt.nz/cases/smith-v-ag</link>
    <pubDate>Thu, 09 Jul 2026 00:00:00 +1200</pubDate></item>
  <item><title>Older Case</title>
    <link>https://www.courtsofnz.govt.nz/cases/older</link>
    <pubDate>Mon, 06 Jan 2020 00:00:00 +1300</pubDate></item>
  <item><title>Dup</title>
    <link>https://www.courtsofnz.govt.nz/cases/smith-v-ag</link>
    <pubDate>Thu, 09 Jul 2026 00:00:00 +1200</pubDate></item>
</channel></rss>"""


class _Resp:
    def __init__(self, content: bytes):
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class _Client:
    """Serves the RSS, the case page, and the PDF by URL."""

    def __init__(self, pdf: bytes):
        self.pdf = pdf
        self.seen: list[str] = []

    def get(self, url: str, **kw):
        self.seen.append(url)
        if url.endswith("/RSS"):
            return _Resp(_RSS)
        if url.endswith(".pdf"):
            return _Resp(self.pdf)
        if "/cases/smith-v-ag" in url:
            return _Resp(_CASE_HTML.encode())
        from raglex.core.errors import FetchError
        raise FetchError(f"404 {url}", transient=False)


# -- the PDF parser ---------------------------------------------------------
def test_neutral_citation_id_from_text():
    assert neutral_citation_id("blah [2026] NZSC 88 blah") == "nzsc/2026/88"
    assert neutral_citation_id("[2019]  NZSC  5") == "nzsc/2019/5"     # tolerant spacing
    assert neutral_citation_id("no citation here") is None


def test_parser_recovers_citation_paragraphs_and_separates_footnotes():
    parsed = parse_nzsc_pdf(_make_pdf())
    assert parsed.neutral_citation == "nzsc/2026/88"
    # numbered paragraphs became citable segments, in order
    para_labels = [s.label for s in parsed.segments if s.kind == "paragraph"]
    assert para_labels == ["[1]", "[2]"]
    # footnotes lifted OUT of the body flow into their own preserved zone
    assert len(parsed.footnotes) == 2
    fn_labels = [s.label for s in parsed.segments if s.kind == "footnote"]
    assert fn_labels == ["fn 1", "fn 2"]
    body_before_footnotes = parsed.text.split("Footnotes")[0]
    assert "R v Smith" not in body_before_footnotes         # footnote text not in the body
    assert "R v Smith [2020] NZSC 1" in parsed.text          # but preserved (resolves)
    # every segment offset is valid against the assembled text
    for s in parsed.segments:
        assert 0 <= s.char_start <= s.char_end <= len(parsed.text)


def test_parser_paragraph_segment_text_matches_offsets():
    parsed = parse_nzsc_pdf(_make_pdf())
    seg = next(s for s in parsed.segments if s.label == "[1]")
    assert parsed.text[seg.char_start:seg.char_end].startswith("[1] The appellant")


# -- intituling / header metadata (the matching signal for pre-2005 cases) --
# Real 2004 NZSC leave decision: no neutral citation (introduced 2005+), so identity is
# the file number + parties + date — exactly the unreported-citation form.
_HEADER_2004 = """IN THE SUPREME COURT OF NEW ZEALAND
SC CRI 2/2004
ALAN IVO GREER
v
THE QUEEN
Coram:
Elias CJ
Blanchard J
Counsel:
B S Yeoman for Applicant
J C Pike for Crown
Judgment:
15 July 2004
JUDGMENT OF THE COURT
[1] Mr Greer seeks leave to appeal."""


def test_parse_header_extracts_unreported_citation_metadata():
    h = parse_header(_HEADER_2004)
    assert h["file_number"] == "SC CRI 2/2004"
    assert file_number_id(h["file_number"]) == "nzsc/sc-cri-2-2004"
    assert h["parties"] == "ALAN IVO GREER v THE QUEEN"
    assert h["coram"] == ["Elias CJ", "Blanchard J"]
    assert "Yeoman" in h["counsel"]
    assert h["judgment_date"] == date(2004, 7, 15)


def test_parse_coram_handles_both_bench_forms():
    # comma form with a shared trailing suffix: bare puisne names inherit the "J"
    assert _parse_coram("Elias CJ, William Young, Glazebrook, O’Regan and Ellen France JJ") == \
        ["Elias CJ", "William Young J", "Glazebrook J", "O’Regan J", "Ellen France J"]
    # space form, each judge already suffixed
    assert _parse_coram("Elias CJ Blanchard J") == ["Elias CJ", "Blanchard J"]


def test_file_number_id_is_namespaced_and_stable():
    assert file_number_id("SC 36/2018") == "nzsc/sc-36-2018"
    assert file_number_id("SC UR 6/2026") == "nzsc/sc-ur-6-2026"
    assert file_number_id(None) is None


def test_neutral_citation_only_from_first_page_not_a_cited_case():
    # A 2004 case (no own citation) that CITES a later NZSC case must NOT adopt the cited
    # case's citation as its identity — the parser only reads the intituling for identity.
    parsed_front = neutral_citation_id(_HEADER_2004)
    assert parsed_front is None


# -- identity helpers -------------------------------------------------------
def test_filename_and_provisional_ids():
    assert _filename_citation("https://x/assets/cases/2026/2026-NZSC-88.pdf") == "nzsc/2026/88"
    assert _filename_citation("https://x/assets/cases/2004/sc-cri-8-2004.pdf") is None
    assert _provisional_id("https://www.courtsofnz.govt.nz/cases/smith-v-ag/") == "nz-caselaw/smith-v-ag"


# -- case page HTML ---------------------------------------------------------
def test_find_pdf_href_prefers_decision_block_over_media_release():
    assert _find_pdf_href(_CASE_HTML) == "/assets/cases/2026/2026-NZSC-88.pdf"


def test_party_title_prefers_clean_case_heading():
    # the case-title heading (the party line) wins over the citation-suffixed og:title
    assert _party_title(_CASE_HTML) == "Smith v Attorney-General"


# -- RSS + discover ---------------------------------------------------------
def test_rss_parse_and_dedup_by_url():
    items = _parse_rss(_RSS)
    assert len(items) == 3  # raw items (dedup happens in discover)
    assert items[0].url.endswith("/smith-v-ag")


def test_discover_is_incremental_and_dedups_urls():
    ad = NZSupremeCourtAdapter(client=_Client(_make_pdf()))
    # backfill / first run: every distinct URL, newest first
    stubs = list(ad.discover(None))
    urls = [s.landing_url for s in stubs]
    assert urls == ["https://www.courtsofnz.govt.nz/cases/smith-v-ag",
                    "https://www.courtsofnz.govt.nz/cases/older"]   # dup URL collapsed
    # incremental: nothing at/older than the 2026-07-09 watermark is re-yielded
    later = list(ad.discover("2026-07-09T00:00:00+12:00"))
    assert later == []


# -- full fetch -------------------------------------------------------------
def test_fetch_builds_record_keyed_by_pdf_neutral_citation():
    ad = NZSupremeCourtAdapter(client=_Client(_make_pdf()))
    stub = next(iter(ad.discover(None)))
    rec = ad.fetch(stub)
    assert rec is not None
    assert rec.stable_id == "nzsc/2026/88"                 # identity from the PDF, not the URL
    assert rec.source == "nz-caselaw" and rec.court == "nzsc"
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.title == "Smith v Attorney-General"          # from the case-page heading
    assert rec.raw_ext == "pdf" and rec.raw_bytes
    assert any(s.kind == "paragraph" for s in rec.segments)
    assert rec.extra["neutral_citation"] == "[2026] NZSC 88"
    assert rec.extra["jurisdiction"] == "nz"


def test_fetch_returns_none_when_case_page_has_no_pdf():
    class _NoPdf(_Client):
        def get(self, url, **kw):
            if "/cases/" in url and not url.endswith(".pdf"):
                return _Resp(b"<html><body>no decision here</body></html>")
            return super().get(url, **kw)

    ad = NZSupremeCourtAdapter(client=_NoPdf(_make_pdf()))
    stub = next(iter(ad.discover(None)))
    assert ad.fetch(stub) is None


def test_fetch_reraises_transient_case_page_error():
    from raglex.core.errors import FetchError

    class _Transient(_Client):
        def get(self, url, **kw):
            if "/cases/" in url and not url.endswith(".pdf"):
                raise FetchError("503", transient=True)
            return super().get(url, **kw)

    ad = NZSupremeCourtAdapter(client=_Transient(_make_pdf()))
    stub = next(iter(ad.discover(None)))
    with pytest.raises(FetchError):
        ad.fetch(stub)
