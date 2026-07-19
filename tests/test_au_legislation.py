"""Australian legislation — the Commonwealth OData adapter, the LawMaker states adapter,
the FRL/LawMaker content parsers, and the citation matcher. Network-free.
"""

from __future__ import annotations

import json

from raglex.adapters.au_legislation import (
    CommonwealthAdapter,
    LawMakerAdapter,
    _norm_docids,
    _title_id,
    frl_stable_id,
    lawmaker_stable_id,
    parse_crawler_feed,
    parse_reasons,
)
from raglex.core.models import DocType, RelationshipType
from raglex.formats.frl_html import parse_frl_html
from raglex.formats.lawmaker_html import au_id, lawmaker_target, parse_lawmaker_html
from raglex.resolve.matchers import first_candidate

# -- fixtures (trimmed from the live services) ------------------------------

FRL_HTML = b"""<html><body>
<p class="ActNo">Acts Interpretation Act 1901</p>
<p class="TOC2"><span>Part</span><span>1</span><span>Preliminary</span></p>
<p class="TOC5"><span>1</span><span>Short title</span></p>
<p id="n1" class="ActHead2"><span class="CharPartNo">Part</span><span> </span><span class="CharPartNo">1</span><span>&#8212;</span><span class="CharPartText">Preliminary</span></p>
<p id="n2" class="ActHead5"><span class="CharSectno">1</span><span>&#160; </span><span>Short title</span></p>
<p class="subsection"><span>This Act may be cited as the </span><span style="font-style:italic">Acts Interpretation Act 1901</span><span>.</span></p>
<p id="n3" class="ActHead5"><span class="CharSectno">2</span><span> </span><span>Application of Act</span></p>
<p class="subsection">(1) This Act applies to all Acts.</p>
<p class="ENotesHeading1">Endnote 4&#8212;Amendment history</p>
<p class="ENoteTableText">Acts Interpretation Amendment Act 2011 (No. 46, 2011)</p>
</body></html>"""

FRL_SHELL = b"""<html><body><frl-root _nghost-ng-c1=""><main>
The requested title could not be loaded.</main></frl-root></body></html>"""

TITLE_JSON = {
    "id": "C1901A00002", "name": "Acts Interpretation Act 1901", "collection": "Act",
    "seriesType": "Act", "year": 1901, "number": 2, "isPrincipal": True,
    "isInForce": True, "status": "InForce", "makingDate": "1901-07-12T00:00:00",
    "asMadeRegisteredAt": "2013-01-22T20:21:37", "hasCommencedUnincorporatedAmendments": False,
    "originatingBillUri": "https://parlinfo.aph.gov.au/parlInfo/x",
    "nameHistory": [{"name": "Acts Interpretation Act 1901", "start": "1901-07-12T00:00:00"}],
    "statusHistory": [{"status": "InForce", "start": "1901-07-12T00:00:00", "reasons": [
        {"affect": "Amend", "markdown": "the [Statute Law Revision Act 1934](/C1934A00045)",
         "affectedByTitle": {"titleId": "C1934A00045", "name": "Statute Law Revision Act 1934",
                             "provisions": "s 2", "year": 1934, "number": 45, "seriesType": "Act"}}]}],
}

VERSION_JSON = {
    "titleId": "C1901A00002", "start": "2026-03-28T00:00:00",
    "retrospectiveStart": "2026-03-28T00:00:00", "isCurrent": True, "isLatest": True,
    "status": "InForce", "registerId": "C2026C00117", "compilationNumber": "39",
    "hasUnincorporatedAmendments": False,
    "reasons": [{"affect": "Amend", "markdown": "sch 1 of the [Law and Justice Act 2026](/C2026A00004)",
                 "affectedByTitle": {"titleId": "C2026A00004", "name": "Law and Justice Act 2026",
                                     "provisions": "sch 1", "year": 2026, "number": 4, "seriesType": "Act"}}]}

DOCS_EPUB_JSON = {"value": [
    {"start": "2024-12-11T00:00:00", "retrospectiveStart": "2024-12-11T00:00:00"},
    {"start": "2023-08-12T00:00:00", "retrospectiveStart": "2023-08-12T00:00:00"}]}

LAWMAKER_HTML = b"""<html><head><title>View - Queensland Legislation</title></head><body>
<div id="fragview"><div class="content">
<span class='TopHeadingSpan'>Multicultural Recognition Act 2016</span>
<p class='LongTitleParagraph'><P class="LeftParagraph LongTitle">An Act to provide for a Multicultural Queensland Charter</p>
<div class="PartHeadingParagraph"><p class="PartHeadingParagraph"><a name="pt.1"></a><span class="HeadingNumber">Part 1</span> <b><span class="PartHeadingName">Preliminary</span></b></p></div>
<P class="HeadingParagraph"><a name="sec.1"></a><B class="HeadingStyle">1</B><span class="HeadingName">Short title</span></P>
<BLOCKQUOTE class="FlatParagraph">This Act may be cited as the <a href="/link?doc.id=act-2016-001&#38;date=2024-02-01&#38;type=act"><I>Multicultural Recognition Act 2016</I></a>.</BLOCKQUOTE>
<P class="HeadingParagraph"><a name="sec.5"></a><B class="HeadingStyle">5</B><span class="HeadingName">Definitions</span></P>
<BLOCKQUOTE class="FlatParagraph">A term has the meaning in the <a href="/link?doc.id=act-2022-034&#38;type=act">Another Act 2022</a>.</BLOCKQUOTE>
</div></div></body></html>"""

CRAWLER_FEED = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title type="html">Multicultural Recognition Act 2016</title>
<link rel="alternate" href="https://www.legislation.qld.gov.au:443/view/whole/html/inforce/2024-02-01/act-2016-001"/>
<id>https://www.legislation.qld.gov.au:443/view/whole/html/inforce/2024-02-01/act-2016-001</id>
<updated>2024-02-01T00:00:00+10:00</updated></entry>
<entry><title type="html">First Home Owner Grant Amendment Act 2014 (Repealed)</title>
<link rel="alternate" href="https://www.legislation.tas.gov.au:443/view/whole/html/inforce/2015-06-23/act-2014-005"/>
<id>https://www.legislation.tas.gov.au:443/view/whole/html/inforce/2015-06-23/act-2014-005</id>
<updated>2026-07-10T00:00:00+10:00</updated></entry>
</feed>"""


class _Resp:
    def __init__(self, content: bytes | str, status: int = 200):
        self.content = content if isinstance(content, bytes) else content.encode()
        self.status_code = status

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class _Client:
    """Serves fixtures by matching URL substrings; unmatched → FetchError (like a 404)."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[str] = []

    def get(self, url, params=None, **kw):
        from raglex.core.errors import FetchError

        full = url + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
        self.seen.append(full)
        for needle, body in self.routes.items():
            if needle in full:
                return _Resp(body)
        raise FetchError(f"404 {full}")


# -- ids ---------------------------------------------------------------------
def test_au_id_bakes_in_jurisdiction_and_strips_padding():
    assert au_id("qld", "act", 2016, "001") == "au/qld/act/2016/1"
    assert au_id("cth", "act", "1901", "00002") == "au/cth/act/1901/2"


def test_frl_stable_id_maps_the_register_grammar():
    assert frl_stable_id("C1901A00002") == "au/cth/act/1901/2"
    assert frl_stable_id("F2008L02133") == "au/cth/sl/2008/2133"   # Legislative Instrument
    # an unmodelled shape is carried, never dropped
    assert frl_stable_id("X9999Z00001") == "au/cth/x9999z00001"


def test_title_id_accepts_register_id_corpus_id_and_url():
    assert _title_id("C1901A00002") == "C1901A00002"
    assert _title_id("au/cth/act/1901/2") == "C1901A00002"
    assert _title_id("https://www.legislation.gov.au/C1901A00002") == "C1901A00002"


def test_norm_docid_preserves_width_but_derives_from_corpus_id():
    assert _norm_docids("act-2016-001") == ["act-2016-001"]      # verbatim — width varies
    assert _norm_docids("sl-2023-0107") == ["sl-2023-0107"]
    assert _norm_docids("https://x/view/whole/html/inforce/2024-02-01/act-2016-001") == ["act-2016-001"]
    # corpus-id width is ambiguous → try both the 4-digit and the 3-digit form
    assert _norm_docids("au/qld/act/2016/1") == ["act-2016-0001", "act-2016-001"]


def test_lawmaker_stable_id_and_target():
    assert lawmaker_stable_id("tas", "act-2000-019") == "au/tas/act/2000/19"
    assert lawmaker_target("/link?doc.id=act-2016-001&type=act", "qld") == "au/qld/act/2016/1"
    assert lawmaker_target("/link?nothing", "qld") is None


def test_matcher_resolves_australian_urls():
    assert first_candidate("https://www.legislation.gov.au/C1901A00002").value == "au/cth/act/1901/2"
    assert first_candidate(
        "https://www.legislation.qld.gov.au/view/whole/html/inforce/2024-02-01/act-2016-001"
    ).value == "au/qld/act/2016/1"
    # a compilation id resolves under cth, keyed on the register id until the Title lands
    assert first_candidate("https://www.legislation.gov.au/Details/C2026C00117"
                           ).value == "au/cth/c2026c00117"


# -- FRL parser --------------------------------------------------------------
def test_frl_html_segments_on_parts_and_sections_and_lifts_endnotes():
    doc = parse_frl_html(FRL_HTML)
    labels = [(s.label, s.kind) for s in doc.segments]
    assert ("Part 1—Preliminary", "part") in labels
    assert ("s. 1 Short title", "section") in labels
    assert ("s. 2 Application of Act", "section") in labels
    # the TOC repeats headings — it must not create duplicate sections
    assert [l for l, k in labels].count("s. 1 Short title") == 1
    # amendment-history endnotes are held out of the operative text
    assert "Amendment history" not in (doc.text or "")
    assert any("Amendment history" in e for e in doc.metadata["endnotes"])


def test_frl_html_rejects_the_angular_shell():
    assert parse_frl_html(FRL_SHELL).text is None


# -- LawMaker parser ---------------------------------------------------------
def test_lawmaker_html_parses_title_longtitle_sections_and_links():
    doc = parse_lawmaker_html(LAWMAKER_HTML, jurisdiction="qld")
    assert doc.title == "Multicultural Recognition Act 2016"
    assert doc.metadata["long_title"].startswith("An Act to provide")
    labels = [s.label for s in doc.segments]
    assert "Part 1 Preliminary" in labels
    assert "s. 1 Short title" in labels and "s. 5 Definitions" in labels
    # a cross-reference to another Act becomes a jurisdiction-scoped edge
    assert "au/qld/act/2022/34" in {r.dst_id for r in doc.relations}


# -- feed --------------------------------------------------------------------
def test_parse_crawler_feed_extracts_docid_status_pit_and_repealed():
    items = parse_crawler_feed(CRAWLER_FEED)
    assert len(items) == 2
    a = items[0]
    assert a.docid == "act-2016-001" and a.status == "inforce" and a.pit_date == "2024-02-01"
    assert a.url.endswith("/act-2016-001") and ":443" not in a.url
    b = items[1]
    assert b.repealed is True and "(Repealed)" not in b.title


# -- Commonwealth adapter ----------------------------------------------------
def test_parse_reasons_reads_the_structured_amendment_edge():
    ams = parse_reasons(VERSION_JSON["reasons"])
    assert ams[0].affect == "Amend" and ams[0].affected_by_id == "C2026A00004"
    assert ams[0].provisions == "sch 1"


def test_commonwealth_adapter_builds_metadata_amendment_graph_and_falls_back_for_text():
    # current compilation's EPUB HTML 404s; the fallback finds an earlier one that exists
    client = _Client({
        "titles('C1901A00002')": json.dumps(TITLE_JSON),
        "Versions/Find(titleId='C1901A00002'": json.dumps(VERSION_JSON),
        "/Documents?": json.dumps(DOCS_EPUB_JSON),
        "/2024-12-11/2024-12-11/text/1/epub": FRL_HTML,
    })
    adapter = CommonwealthAdapter(ids="C1901A00002", client=client)
    record = adapter.fetch(next(iter(adapter.discover(None))))
    assert record.doc_type is DocType.LEGISLATION
    assert record.stable_id == "au/cth/act/1901/2"
    assert record.extra["is_authoritative"] is True and record.extra["jurisdiction"] == "cth"
    assert record.extra["compilation_number"] == "39"
    assert record.extra["point_in_time"] == "2024-12-11"     # the version we actually hold
    assert record.extra["originating_bill_uri"].startswith("https://parlinfo")
    assert record.text and record.segments
    # both the statusHistory edge and the current-compilation edge are minted
    amended = {(r.dst_id, r.dst_anchor) for r in record.relations
               if r.relationship_type is RelationshipType.AMENDED_BY}
    assert ("au/cth/act/1934/45", "Amend") in amended
    assert ("au/cth/act/2026/4", "Amend") in amended


def test_commonwealth_adapter_returns_metadata_node_when_no_text_reachable():
    client = _Client({
        "titles('C1901A00002')": json.dumps(TITLE_JSON),
        "Versions/Find": json.dumps(VERSION_JSON),
        "/Documents?": json.dumps({"value": []}),   # no EPUB compilations resolve
    })
    adapter = CommonwealthAdapter(ids="C1901A00002", client=client)
    record = adapter.fetch(next(iter(adapter.discover(None))))
    # still a real, resolvable node carrying the amendment graph — text can backfill later
    assert record is not None and record.text is None
    assert any(r.relationship_type is RelationshipType.AMENDED_BY for r in record.relations)


def test_commonwealth_discover_query_pages_and_filters():
    page = {"value": [{"id": "C2024A00002", "name": "Fair Work Act 2024",
                       "isPrincipal": True, "asMadeRegisteredAt": "2024-02-01T00:00:00"}]}
    client = _Client({"/titles?": json.dumps(page)})
    adapter = CommonwealthAdapter(collection="Act", client=client, page_size=1)
    stubs = list(adapter.discover(None, max_pages=1))
    assert stubs[0].stable_id == "au/cth/act/2024/2"
    # isPrincipal is post-filtered client-side — the FRL API no longer accepts it as a
    # $filter predicate (it 400s), so the query must NOT send it.
    assert "collection eq 'Act'" in client.seen[0]
    assert "isPrincipal eq true" not in client.seen[0]


def test_commonwealth_discover_post_filters_non_principal_titles():
    """isPrincipal is applied in Python now — a non-principal title is dropped after the
    fetch, since the API can no longer filter on it server-side."""
    page = {"value": [
        {"id": "C2024A00002", "name": "Fair Work Act 2024", "isPrincipal": True,
         "asMadeRegisteredAt": "2024-02-01T00:00:00"},
        {"id": "C2024A00009", "name": "Fair Work Amendment Act 2024", "isPrincipal": False,
         "asMadeRegisteredAt": "2024-03-01T00:00:00"},
    ]}
    client = _Client({"/titles?": json.dumps(page)})
    adapter = CommonwealthAdapter(collection="Act", client=client, page_size=2)
    ids = [s.stable_id for s in adapter.discover(None, max_pages=1)]
    assert ids == ["au/cth/act/2024/2"]           # the amendment (non-principal) is dropped


def test_commonwealth_page_size_is_capped_at_the_api_limit():
    """The FRL API caps $top at 100; a larger page 400s and silently returned nothing."""
    assert CommonwealthAdapter(page_size=200).page_size == 100
    assert CommonwealthAdapter(page_size=50).page_size == 50


# -- LawMaker adapter --------------------------------------------------------
def test_lawmaker_adapter_fetches_a_pit_view_and_flags_authoritative():
    client = _Client({"/view/whole/html/inforce/": LAWMAKER_HTML})
    adapter = LawMakerAdapter(jurisdiction="qld", ids="act-2016-001", client=client)
    stub = next(iter(adapter.discover(None)))
    assert stub.stable_id == "au/qld/act/2016/1"
    record = adapter.fetch(stub)
    assert record.stable_id == "au/qld/act/2016/1"
    assert record.extra["jurisdiction"] == "qld" and record.extra["is_authoritative"] is True
    assert record.extra["text_status"] == "inforce"
    assert record.title == "Multicultural Recognition Act 2016"
    assert record.text and record.segments


def test_lawmaker_fetch_reraises_transient_error_instead_of_reporting_absence():
    # A transient failure must NOT be swallowed into a None (which the pipeline reads as
    # a positive absence → 90-day miss list). It has to propagate so the cursor freezes
    # and the item is retried.
    import pytest

    from raglex.core.errors import FetchError

    class _Transient:
        def get(self, url, params=None, **kw):
            raise FetchError("temporary upstream 503", transient=True)

    adapter = LawMakerAdapter(jurisdiction="qld", ids="act-2016-001",
                              client=_Client({"/view/whole/html/inforce/": LAWMAKER_HTML}))
    stub = next(iter(adapter.discover(None)))
    adapter._client = _Transient()
    with pytest.raises(FetchError):
        adapter.fetch(stub)

    # a genuine (fatal) 404 is still an absence → returns None
    class _Gone:
        def get(self, url, params=None, **kw):
            raise FetchError("404 not found", transient=False)

    adapter._client = _Gone()
    assert adapter.fetch(stub) is None


def test_lawmaker_adapter_feed_discovery_scopes_to_its_jurisdiction():
    client = _Client({"/feed": CRAWLER_FEED})
    adapter = LawMakerAdapter(jurisdiction="qld", client=client)
    stubs = list(adapter.discover(None))
    # the feed fixture carries a Tas row too; a Qld adapter still keys everything as qld
    # (a register's feed only lists its own titles — jurisdiction is fixed per adapter)
    ids = [s.stable_id for s in stubs]
    assert "au/qld/act/2016/1" in ids
    qld = next(s for s in stubs if s.stable_id == "au/qld/act/2016/1")
    assert qld.hints["pit"] == "2024-02-01" and qld.hints["status"] == "inforce"


def test_lawmaker_adapter_incremental_cursor_on_published():
    client = _Client({"/feed": CRAWLER_FEED})
    adapter = LawMakerAdapter(jurisdiction="qld", client=client)
    # nothing published after this cursor
    assert list(adapter.discover("2027-01-01T00:00:00+10:00")) == []


# -- the FRL content endpoint (the route that actually serves text) -----------

def _epub_bytes(*members: tuple[str, str]) -> bytes:
    """A minimal EPUB: a zip whose OEBPS members are the frl-html the parser reads."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml", "<container/>")
        for name, html in members:
            zf.writestr(name, html)
    return buf.getvalue()


_ACT_HTML = (
    "<html><body><h1>Widget Act 1975</h1>"
    "<p>Act No. 28 of 1975</p>"
    "<h2>Part I&#8212;Preliminary</h2>"
    "<p>1  Short title</p><p>This Act may be cited as the Widget Act 1975.</p>"
    "<p>2  Commencement</p><p>This Act commences on Royal Assent.</p>"
    "</body></html>"
)


def test_content_endpoint_needs_the_whole_odata_signature():
    """OData won't resolve the function unless every parameter is present — dropping
    uniqueTypeNumber/volumeNumber/rectificationSpecification 404s in a way that reads like
    "no such document", which is what made this look like a coverage gap."""
    from raglex.adapters.au_legislation import CommonwealthAdapter

    url = CommonwealthAdapter(client=_Client({}))._content_url("C2004A00250", "Current")
    for required in ("titleid='C2004A00250'", "asatspecification='Current'", "type='Primary'",
                     "format='Epub'", "uniqueTypeNumber=0", "volumeNumber=0",
                     "rectificationSpecification='Latest'"):
        assert required in url, required
    assert "/documents/find(" in url


def test_body_comes_from_the_binary_epub_not_the_metadata_json():
    from raglex.adapters.au_legislation import CommonwealthAdapter

    client = _Client({"asatspecification='Current'":
                      _epub_bytes(("OEBPS/document_1/document_1.html", _ACT_HTML))})
    doc, as_at = CommonwealthAdapter(client=client).fetch_body_api("C2004A00250")
    assert as_at == "Current"
    assert doc is not None and "Widget Act 1975" in doc.text
    assert doc.metadata["format"] == "frl-epub"


def test_falls_through_current_to_latest_for_a_repealed_title():
    """A repealed or uncompiled Act has no 'Current' document but does have a 'Latest'
    one — trying only 'Current' is what left repealed Acts textless."""
    from raglex.adapters.au_legislation import CommonwealthAdapter

    client = _Client({"asatspecification='Latest'":
                      _epub_bytes(("OEBPS/document_1/document_1.html", _ACT_HTML))})
    doc, as_at = CommonwealthAdapter(client=client).fetch_body_api("C1901A00004")
    assert as_at == "Latest" and doc is not None and doc.text


def test_multi_volume_epub_members_merge_with_shifted_segment_offsets():
    from raglex.adapters.au_legislation import CommonwealthAdapter

    second = _ACT_HTML.replace("Widget Act 1975", "Widget Act 1975 (Volume 2)")
    client = _Client({"asatspecification='Current'": _epub_bytes(
        ("OEBPS/document_1/document_1.html", _ACT_HTML),
        ("OEBPS/document_2/document_2.html", second))})
    doc, _ = CommonwealthAdapter(client=client).fetch_body_api("C1968A00063")
    assert "Volume 2" in doc.text
    for seg in doc.segments:
        assert 0 <= seg.char_start <= seg.char_end <= len(doc.text)


def test_a_non_zip_response_is_not_mistaken_for_a_document():
    """The site serves a 200 HTML app-shell for unknown routes; only a real zip counts."""
    from raglex.adapters.au_legislation import CommonwealthAdapter

    client = _Client({"documents/find(": "<html><body>Just a moment…</body></html>"})
    doc, as_at = CommonwealthAdapter(client=client).fetch_body_api("C2004A00250")
    assert doc is None and as_at is None
