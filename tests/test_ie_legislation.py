"""Irish legislation — the eISB/LRC XML + HTML parsers, index/RDFa/ISBC scraping, and
the two adapters. Network-free: every fixture is a trimmed copy of the real markup.
"""

from __future__ import annotations

from datetime import date

from raglex.adapters.ie_legislation import (
    IrishRevisedActsAdapter,
    IrishStatuteBookAdapter,
    _normalise_id,
    _since_year,
    _year_range,
    parse_isbc,
    parse_rdfa,
    parse_revised_list,
    parse_year_index,
    revised_manifest,
)
from raglex.core.models import DocType, RelationshipType
from raglex.formats.eisb_html import href_target, parse_eisb_html
from raglex.formats.eisb_xml import (
    eli_id,
    expand_entities,
    parse_annotation,
    parse_eisb_xml,
    prose_date,
)
from raglex.resolve.matchers import first_candidate

# -- fixtures (trimmed from the live services) ------------------------------

ACT_XML = b"""<?xml version="1.0"?>
<act><metadata><title>Finance Act 2016</title><number>13</number><year>2016</year>
<dateofenactment>20161026</dateofenactment></metadata>
<frontmatter><p class="0 0 0 center 1 0"><graphic href="harp.jpg"/></p></frontmatter>
<body>
<part id="PART1"><title><p>PART 1</p><p>Preliminary</p></title>
<sect id="SEC1"><number>1.</number><title><p><b>Interpretation</b></p></title>
<p class="-2 8 0 left 1 0"><b>1.</b> (1) In this Act<emdash/></p>
<p><odq/>Minister<cdq/> means the Minister for Finance;</p>
<p>(2) A reference to the State<csq/>s functions under
<i><xref href="ZZA38Y2014S26" xml:link="simple">section 26</xref></i> applies.</p>
</sect>
<sect id="SEC2"><number>2.</number><title><p><b>Amendment of Act of 1972</b></p></title>
<p>2. The <xref href="EN.ACT.1972.0027#SEC5">European Communities Act 1972</xref>
is amended. An t<Ufada/>dar<afada/>s p<afada/>irt<ifada/>. Cost: <euro/>500.</p>
</sect>
</part></body>
<backmatter><schedule id="SCHED"><title><p>SCHEDULE</p></title>
<p>Terms of the Agreement.</p></schedule></backmatter></act>"""

REVISED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE act SYSTEM "legislation.dtd"[
\t<!ENTITY updatedtodate "1 June 2025">
\t<!ENTITY lastact "<i>Finance Act 2025</i> (4/2025), enacted 20 May 2025">
]>
<act><metadata><title>Official Languages Act 2003</title><number>32</number>
<year>2003</year><dateofenactment>20030714</dateofenactment></metadata>
<frontmatter><coverpage><p><b>Updated to &updatedtodate;</b></p>
<p>Last Act: &lastact;</p></coverpage></frontmatter>
<body>
<sect id="SEC4"><number>4</number><title><p>Regulations.</p></title>
<p>4. The Minister may make regulations.</p>
<div class="annotations"><p><b>Annotations:</b></p>
<div class="f-notes"><p><b>Amendments:</b></p>
<div class="f-note"><p>Substituted (25.05.2018) by <i>Data Protection Act 2018</i>
(7/2018), s. 194, S.I. No. 174 of 2018, art. 3.</p></div></div>
<div class="e-notes"><div class="e-note"><p>Power pursuant to s. 1(2) exercised
(19.01.2004) by <i>An tOrd&uacute; 2004</i> (S.I. No. 32 of 2004).</p></div></div>
</div></sect>
</body></act>"""

SI_HTML = b"""<html><head><title>S.I. No. 201/2016 - Equidae Regulations 2016.</title></head>
<body><nav>chrome</nav><div class="act-content" id="act"><table>
<tr><td></td><td></td><td><p style="display:block;text-align:justify;">
I, in exercise of the powers conferred by
<a href="/2013/en/act/pub/0015/print.html#sec36">section 36</a> (1) of the
<a href="/2013/en/act/pub/0015/print.html">Animal Health and Welfare Act 2013</a>,
hereby make the following regulations:</p>
<tr><td></td><td></td><td><p style="display:block;text-indent:0.50em;">
1. These Regulations may be cited as the Equidae Regulations 2016.</p>
<tr><td></td><td></td><td><p style="display:block;text-indent:0.50em;">
2. A person shall&mdash;</p>
<tr><td></td><td></td><td><p style="display:block;margin-left:1.50em;">
(<i>a</i>) complete a declaration, and</p>
<tr><td></td><td></td><td><p style="display:block;">EXPLANATORY NOTE</p>
<tr><td></td><td></td><td><p style="display:block;">(This note is not part of the
Statutory Instrument.)</p>
</table></div></body></html>"""

ACT_PRINT_HTML = b"""<html><head><title>Finance Act 2016</title></head>
<body><div class="act-content" id="act"><table>
<tr><td></td><td></td><td><p>Number 13 of 2016</p>
<tr><td></td><td></td><td><p><a href="/2016/en/act/pub/0013/print.html#sec1">1. Interpretation</a></p>
<tr><td></td><td></td><td><p><a name="sec1"></a></p>
<tr><td></td><td></td><td><p>1. In this Act, "Minister" means the Minister.</p>
<tr><td></td><td></td><td><p><a name="sec2"></a></p>
<tr><td></td><td></td><td><p>2. This Act may be cited as the Finance Act 2016.</p>
<tr><td></td><td></td><td><p><a name="sched"></a></p>
<tr><td></td><td></td><td><p>Terms of the Agreement.</p>
</table></div></body></html>"""

ACT_INDEX_HTML = """<table class="datatable" id="public-acts-dtb"><tbody>
<tr><td class="align-center">1</td><td><a href="en/act/pub/0001/index.html">Credit Guarantee (Amendment) Act 2016</a></td><td class="align-center"><a href="../pdf/2016/en.act.2016.0001.pdf" aria-label="Open PDF version of Credit Guarantee (Amendment) Act 2016; PDF size 434 kilobytes"><i></i></a></td></tr>
<tr><td class="align-center">2</td><td><a href="en/act/pub/0002/index.html">Horse Racing Ireland Act 2016</a></td><td class="align-center"><a href="../pdf/2016/en.act.2016.0002.pdf" aria-label="PDF size 440 kilobytes"><i></i></a></td></tr>
</tbody></table>"""

RDFA_HTML = """<html><head>
<meta about="eisb:2016/act/13/enacted" typeof="eli:LegalResource" />
<meta about="eisb:2016/act/13/enacted" property="eli:has_part" resource="http://www.irishstatutebook.ie/eli/2016/act/13/section/1" />
<meta about="eisb:2016/act/13/enacted" property="eli:has_part" resource="http://www.irishstatutebook.ie/eli/2016/act/13/schedule" />
<meta about="eisb:2016/act/13/enacted" property="eli:changes" resource="http://www.irishstatutebook.ie/eli/2014/act/38/enacted" />
<meta about="eisb:2016/act/13/enacted" property="eli:date_document" CONTENT="2016-10-26" datatype="xsd:date" />
<meta about="eisb:2016/act/13/enacted" property="eli:transposes" resource="http://data.europa.eu/eli/dir/2014/57/oj" />
<meta about="eisb:2016/act/13/enacted/en" property="eli:title" CONTENT="Finance Act 2016" xml:lang="en" />
<meta about="eisb:2016/act/13/enacted/en" property="eli:description" CONTENT="An Act to make provision..." />
<meta about="eisb:2016/act/13/enacted" property="eli:number" content="13" />
<meta about="eisb:2016/act/13/enacted" property="eli:based_on" resource="http://www.irishstatutebook.ie/eli/1972/act/27/enacted" />
<meta about="eisb:2016/act/13/enacted/en" property="eli:is_embodied_by" resource="http://www.irishstatutebook.ie/eli/2016/act/13/enacted/en/html" />
<meta about="eisb:2016/act/13/enacted/en" property="eli:is_embodied_by" resource="http://www.irishstatutebook.ie/eli/2016/act/13/enacted/en/xml" />
<meta about="eisb:2016/act/13/enacted/en/html" property="eli:legal_value" resource="http://data.europa.eu/eli/ontology#LegalValue-official" />
</head></html>"""

ISBC_HTML = """<div class="act-content" id="act"><h3><b><span>Updated to 10 July 2026</span></b></h3>
<h3 id="commencement">Commencement</h3><table><tbody>
<tr><td>S. 1</td><td>14 July 2003</td></tr>
<tr><td>Ss. 2-4</td><td>30 October 2003</td></tr>
</tbody></table>
<h3 id="effects">Amendments and other effects</h3><table><tbody>
<tr><td>Functions transferred</td><td><a href="http://www.irishstatutebook.ie/2024/en/act/pub/0007/index.html">7/2024</a></td></tr>
<tr><td>Section substituted</td><td><a href="http://www.irishstatutebook.ie/2021/en/act/pub/0049/sec0002.html">49/2021</a></td></tr>
</tbody></table>
<h3 id="associatedsecondary">SIs made under the Act</h3><table><tbody>
<tr><td>S. 4</td><td><a href="http://www.irishstatutebook.ie/2020/en/si/0230.html">S.I. No. 230 of 2020</a></td></tr>
</tbody></table></div>"""

REVISED_LIST_HTML = """<table><tbody id="A">
<tr class=" odd " data-eli="2003/act/32/en" style="">
  <td class="pdf"><a href="/eli/2003/act/32/revised/en/pdf?annotations=true" title="with annotations"><img></a></td>
  <td class="pdf"><a href="/eli/2003/act/32/revised/en/pdf?annotations=false" title="without annotations"><img></a></td>
  <td><span>No. 32/2003</span></td>
  <td><a href="/eli/2003/act/32/front/revised/en/html">Official Languages Act 2003</a></td>
  <td class="updated"><span>1 Jun 2025</span><span data-lrc-chevron="true" data-eli="2003/act/32/en">&#x25C0;</span></td>
</tr>
<tr class=" odd " data-eli="2003/act/32/en" style=" display:none ">
  <td class="pdf"><a href="https://rev-acts.s3.eu-west-1.amazonaws.com/2003/32/act_new.pdf?versionId=ABC" title="with annotations"><img></a></td>
  <td class="pdf"><a href="https://rev-acts.s3.eu-west-1.amazonaws.com/2003/32/act_new_plain.pdf?versionId=DEF" title="without annotations"><img></a></td>
  <td></td><td></td>
  <td class="updated"><span>21 Dec 2024</span></td>
</tr>
<tr class=" even repealed " data-eli="1988/act/25/en" style="">
  <td class="pdf"><a href="/eli/1988/act/25/revised/en/pdf?annotations=true" title="with annotations"><img></a></td>
  <td class="pdf"><a href="/eli/1988/act/25/revised/en/pdf?annotations=false" title="without annotations"><img></a></td>
  <td><span>No. 25/1988</span></td>
  <td><a href="/eli/1988/act/25/front/revised/en/html">Data Protection Act 1988 (Repealed)</a></td>
  <td class="updated"><span>3 Jul 2023</span></td>
</tr>
</tbody></table>"""


class _Resp:
    def __init__(self, content: bytes | str, status: int = 200):
        self.content = content if isinstance(content, bytes) else content.encode()
        self.status_code = status

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class _Client:
    """Serves fixtures by URL suffix; anything unmapped 404s like the real service."""

    def __init__(self, routes: dict[str, bytes | str]):
        self.routes = routes
        self.seen: list[str] = []

    def get(self, url, **kw):
        from raglex.core.errors import FetchError

        self.seen.append(url)
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                return _Resp(body)
        raise FetchError(f"404 {url}")


# -- ids ---------------------------------------------------------------------
def test_eli_id_keeps_numbers_as_strings_and_language_only_when_not_english():
    assert eli_id("act", 2018, "7") == "ie/2018/act/7"
    assert eli_id("act", "2018", "0007") == "ie/2018/act/7"   # legacy zero padding
    assert eli_id("sro", 1923, "1a") == "ie/1923/sro/1a"      # NOT an integer
    assert eli_id("act", 2003, "32", "ga") == "ie/2003/act/32/ga"


def test_normalise_id_accepts_every_form_a_citation_uses():
    forms = {
        "ie/2018/act/7": "ie/2018/act/7",
        "2018/act/7": "ie/2018/act/7",
        "https://www.irishstatutebook.ie/eli/2018/act/7/enacted/en/html": "ie/2018/act/7",
        "No. 7 of 2018": "ie/2018/act/7",
        "S.I. No. 201 of 2016": "ie/2016/si/201",
    }
    for raw, expected in forms.items():
        assert _normalise_id(raw)[0] == expected, raw
    assert _normalise_id("not a citation") is None


def test_matcher_resolves_eli_and_legacy_urls_to_the_same_act():
    for raw in ("https://www.irishstatutebook.ie/eli/1993/si/266/made/en/html",
                "http://www.irishstatutebook.ie/1993/en/si/0266.html"):
        assert first_candidate(raw).value == "ie/1993/si/266"
    # a section pinpoint still resolves to the Act, not a separate document
    assert first_candidate(
        "http://www.irishstatutebook.ie/2013/en/act/pub/0015/print.html#sec36"
    ).value == "ie/2013/act/15"
    assert first_candidate(
        "https://revisedacts.lawreform.ie/eli/2003/act/32/front/revised/en/html"
    ).value == "ie/2003/act/32"


# -- XML ---------------------------------------------------------------------
def test_act_xml_glyph_elements_become_characters():
    doc = parse_eisb_xml(ACT_XML)
    # dropping these silently corrupts the text — quotes, apostrophes, Irish accents
    assert "In this Act—" in doc.text
    assert "“Minister” means" in doc.text
    assert "the State’s functions" in doc.text
    assert "An tÚdarás páirtí" in doc.text
    assert "€500" in doc.text


def test_act_xml_structure_and_metadata():
    doc = parse_eisb_xml(ACT_XML)
    assert doc.title == "Finance Act 2016"
    assert doc.decision_date == date(2016, 10, 26)  # YYYYMMDD normalised
    labels = [(s.label, s.kind, s.level) for s in doc.segments]
    assert ("PART 1 Preliminary", "part", 0) in labels
    assert ("s. 1 Interpretation", "section", 1) in labels
    assert any(k == "schedule" for _, k, _ in labels)
    # every segment's offsets index exactly into the flat text
    for seg in doc.segments:
        assert doc.text[seg.char_start:seg.char_end].strip()


def test_act_xml_external_xrefs_become_edges_internal_ones_do_not():
    doc = parse_eisb_xml(ACT_XML)
    targets = {(r.dst_id, r.dst_anchor) for r in doc.relations}
    assert ("ie/2014/act/38", "s. 26") in targets   # ZZA38Y2014S26
    assert ("ie/1972/act/27", "s. 5") in targets    # EN.ACT.1972.0027#SEC5
    assert all(r.dst_id and not r.dst_id.startswith("#") for r in doc.relations)


def test_expand_entities_reads_the_documents_own_internal_dtd_subset():
    # a DTD-less parse of the LRC XML fails outright on &updatedtodate;
    expanded = expand_entities(REVISED_XML.decode())
    assert "&updatedtodate;" not in expanded and "1 June 2025" in expanded
    assert "<!DOCTYPE" not in expanded
    assert "<i>Finance Act 2025</i>" in expanded  # entity values may carry markup


def test_revised_xml_stamps_its_consolidation_date_and_lifts_annotations_out():
    doc = parse_eisb_xml(REVISED_XML)
    assert doc.metadata["revised"] is True
    assert doc.metadata["updated_to"] == date(2025, 6, 1)
    # the operative text must not carry the annotation prose
    assert "The Minister may make regulations." in doc.text
    assert "Substituted" not in doc.text
    notes = doc.metadata["annotations"]
    assert {n.note_type for n in notes} == {"F", "E"}
    f_note = next(n for n in notes if n.note_type == "F")
    assert f_note.provision == "s. 4 Regulations."
    assert f_note.effect == "Substituted"
    assert f_note.affecting_id == "ie/2018/act/7"
    # the bracketed date is when the effect COMMENCED, not when the Act was passed
    assert f_note.operative_date == date(2018, 5, 25)


def test_parse_annotation_handles_si_and_act_affecting_instruments():
    si = parse_annotation("C", "s. 1", "Application extended (26.11.2001) by "
                                       "Prevention of Corruption Act 2001 (27/2001) ss. 3-6")
    assert si.affecting_id == "ie/2001/act/27" and si.operative_date == date(2001, 11, 26)
    made = parse_annotation("E", None, "Power exercised (4.06.2025) by Delegation Order "
                                       "2025 (S.I. No. 244 of 2025), art. 2")
    assert made.affecting_id == "ie/2025/si/244"
    # unparseable prose is still preserved rather than dropped
    plain = parse_annotation("E", None, "A commencement table is available online.")
    assert plain.affecting_id is None and plain.text.startswith("A commencement table")


def test_prose_date_accepts_full_and_abbreviated_months():
    assert prose_date("Updated to 1 June 2025") == date(2025, 6, 1)
    assert prose_date("21 Dec 2024") == date(2024, 12, 21)
    assert prose_date("no date here") is None


# -- HTML --------------------------------------------------------------------
def test_si_html_segments_on_leading_numbers_and_flags_the_explanatory_note():
    doc = parse_eisb_html(SI_HTML)
    labels = [s.label for s in doc.segments]
    assert "s. 1" in labels and "s. 2" in labels
    assert "Explanatory Note" in labels
    assert doc.metadata["has_explanatory_note"] is True
    note = next(s for s in doc.segments if s.kind == "note")
    assert "not part of the" in doc.text[note.char_start:note.char_end]
    # the enabling Act is linked at section granularity, via the legacy path scheme
    assert ("ie/2013/act/15", "s. 36") in {(r.dst_id, r.dst_anchor) for r in doc.relations}


def test_act_print_html_prefers_the_structural_anchors_over_the_contents_list():
    doc = parse_eisb_html(ACT_PRINT_HTML)
    labels = [s.label for s in doc.segments]
    # anchors define the provisions; the TOC repeating every heading must not re-split
    assert labels.count("s. 1") == 1 and labels.count("s. 2") == 1
    assert "Schedule" in labels
    body = doc.text[doc.segments[labels.index("s. 1")].char_start:]
    assert body.startswith("1. In this Act")


def test_href_target_ignores_in_page_and_offsite_links():
    assert href_target("#sec4") is None
    assert href_target("https://example.com/x") is None
    assert href_target("/2013/en/act/prv/0002/index.html")[0] == "ie/2013/prv/2"


def test_html_parser_rejects_a_soft_404():
    assert parse_eisb_html(b"<html><head><title>404 Not Found</title></head></html>").text is None


# -- discovery / metadata scraping ------------------------------------------
def test_parse_year_index_reads_numbers_titles_and_pdf_sizes():
    rows = parse_year_index(ACT_INDEX_HTML, 2016, "act")
    assert [r.number for r in rows] == ["1", "2"]
    assert rows[0].title == "Credit Guarantee (Amendment) Act 2016"
    # the advertised PDF size is a free change signal — no fetch required
    assert rows[0].pdf_kb == 434 and rows[1].pdf_kb == 440


def test_parse_rdfa_yields_the_graph_edges_and_the_authoritativeness_flag():
    meta = parse_rdfa(RDFA_HTML)
    assert meta.title == "Finance Act 2016" and meta.number == "13"
    assert meta.date_document == date(2016, 10, 26)
    assert meta.changes == ("ie/2014/act/38",)
    assert meta.transposes == ("32014L0057",)   # EU ELI → CELEX, so it lands on the EU node
    assert meta.based_on == ("ie/1972/act/27",)
    assert set(meta.formats) == {"html", "xml"}
    assert meta.is_authoritative is True
    assert len(meta.subdivisions) == 1  # section/1 (the bare "schedule" has no number)


def test_parse_rdfa_absent_block_is_normal_not_an_error():
    assert parse_rdfa("<html><head><title>x</title></head></html>").title is None


def test_parse_isbc_gives_the_inverse_amendment_direction():
    tables = parse_isbc(ISBC_HTML)
    assert tables.updated_to == date(2026, 7, 10)
    # what amended THIS Act — a direction RDFa's eli:changes never records
    assert set(tables.affected_by) == {"ie/2024/act/7", "ie/2021/act/49"}
    assert tables.sis_made_under == ("ie/2020/si/230",)
    assert tables.commencement_rows == 2


def test_parse_revised_list_separates_current_from_collapsed_prior_versions():
    rows = parse_revised_list(REVISED_LIST_HTML)
    assert len(rows) == 3
    manifest = dict(((c.work, c.language), (c, p)) for c, p in revised_manifest(rows))
    current, prior = manifest[("ie/2003/act/32", "en")]
    assert current.updated_to == date(2025, 6, 1)
    # each consolidation is its own point-in-time id, never an overwrite
    assert current.stable_id == "ie/2003/act/32@2025-06-01"
    assert [p.updated_to for p in prior] == [date(2024, 12, 21)]
    assert prior[0].pdf_annotated.endswith("versionId=ABC")  # version-pinned archive PDF
    # repealed items stay in the corpus, flagged
    repealed, _ = manifest[("ie/1988/act/25", "en")]
    assert repealed.repealed is True


# -- adapters ----------------------------------------------------------------
def test_statute_book_adapter_probes_formats_and_falls_back_past_a_missing_xml():
    # an SI: /xml 404s (the normal case), so the print rendition is used instead
    client = _Client({
        "/2016/si/201/made/en": RDFA_HTML,
        "/2016/si/201/made/en/print": SI_HTML,
        "/2016/si/201/made/en/html": SI_HTML,
    })
    adapter = IrishStatuteBookAdapter(ids="ie/2016/si/201", client=client)
    record = adapter.fetch(next(iter(adapter.discover(None))))
    assert record.doc_type is DocType.LEGISLATION
    assert record.stable_id == "ie/2016/si/201"
    assert record.extra["format"] == "eisb-html"
    assert record.extra["formats_available"] == ["print", "html"]  # xml absent, recorded
    assert record.extra["text_status"] == "as made"
    assert record.extra["is_authoritative"] is True
    assert record.segments and record.text


def test_statute_book_adapter_mints_the_rdfa_and_isbc_edges():
    client = _Client({
        "/2016/act/13/enacted/en": RDFA_HTML,
        "/2016/act/13/enacted/en/xml": ACT_XML,
        "/eli/isbc/2016_13.html": ISBC_HTML,
    })
    adapter = IrishStatuteBookAdapter(ids="ie/2016/act/13", client=client)
    record = adapter.fetch(next(iter(adapter.discover(None))))
    assert record.extra["format"] == "eisb-xml"
    by_type: dict[str, set[str]] = {}
    for rel in record.relations:
        by_type.setdefault(rel.relationship_type.value, set()).add(rel.dst_id)
    assert "ie/2014/act/38" in by_type[RelationshipType.AMENDS.value]
    assert "32014L0057" in by_type[RelationshipType.IMPLEMENTS.value]   # transposition
    assert "ie/1972/act/27" in by_type[RelationshipType.IMPLEMENTS.value]  # enabling power
    # the ISBC table supplies what amended this Act — RDFa never does
    assert "ie/2024/act/7" in by_type[RelationshipType.AMENDED_BY.value]
    assert record.extra["isbc_commencement_rows"] == 2


def test_statute_book_adapter_returns_none_when_nothing_is_served():
    adapter = IrishStatuteBookAdapter(ids="ie/1999/act/999", client=_Client({}))
    assert adapter.fetch(next(iter(adapter.discover(None)))) is None


def test_year_walk_always_rewalks_the_open_year():
    assert _since_year(None, 2026) == 1922
    assert _since_year("2024", 2026) == 2024   # re-walks 2024, not 2025
    assert _year_range("2016-2018,2020") == (2016, 2017, 2018, 2020)


def test_revised_adapter_stamps_the_point_in_time_and_flags_non_authoritative():
    client = _Client({
        "/revacts/alpha": REVISED_LIST_HTML,
        "/eli/2003/act/32/revised/en/xml": REVISED_XML,
    })
    adapter = IrishRevisedActsAdapter(ids="ie/2003/act/32", client=client)
    stub = next(s for s in adapter.discover(None) if s.stable_id.startswith("ie/2003"))
    record = adapter.fetch(stub)
    assert record.stable_id == "ie/2003/act/32@2025-06-01"
    assert record.title.endswith("(revised to 2025-06-01)")
    assert record.extra["is_authoritative"] is False
    assert "not an authoritative statement" in record.extra["disclaimer"]
    assert record.extra["updated_to"] == "2025-06-01"
    # the PDF-only archive of earlier consolidations rides along as metadata
    assert record.extra["prior_versions"][0]["updated_to"] == "2024-12-21"
    rels = {(r.relationship_type.value, r.dst_id) for r in record.relations}
    assert (RelationshipType.POINT_IN_TIME_OF.value, "ie/2003/act/32") in rels
    assert (RelationshipType.AMENDED_BY.value, "ie/2018/act/7") in rels


def test_revised_adapter_cursor_skips_unchanged_consolidations():
    client = _Client({"/revacts/alpha": REVISED_LIST_HTML})
    adapter = IrishRevisedActsAdapter(client=client)
    assert [s.stable_id for s in adapter.discover("2025-06-01")] == []
    # only the Act whose "Updated to" moved past the cursor is pulled; the 2023
    # consolidation is already held and costs nothing
    assert [s.stable_id for s in adapter.discover("2024-01-01")] == [
        "ie/2003/act/32@2025-06-01"]
    assert [s.stable_id for s in adapter.discover(None)] == [
        "ie/2003/act/32@2025-06-01", "ie/1988/act/25@2023-07-03"]
