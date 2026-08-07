"""European Parliament resolutions: the two markups, the adapter, and the registry."""

import io
import zipfile

import pytest

from raglex.adapters.eu_ep_followups import EPFollowUpsAdapter, followup_stubs, ta_reference
from raglex.adapters.eu_ep_resolutions import (
    EPResolutionsAdapter,
    aliases_for,
    family,
    portal_id,
)
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, source_catalog
from raglex.citations.extractor import extract_citations
from raglex.core.models import DocType, Stub
from raglex.formats import parse
from raglex.formats.ep_resolution import classify, is_heading

# ---------------------------------------------------------------- fixtures

#: The 2025 drafting: visas in <GRVISA>, recitals in <GRCONS>, operative paragraphs in
#: <DISPOSITIF> under bold <ACTINIT> headings.
SDOCTA_MODERN = b"""<?xml version="1.0" encoding="UTF-8"?>
<SDOCTA STATUS="DEF4"><IDENT><STRING BOLD="on">P10_TA(2025)0343</STRING></IDENT>
<TI><STRING BOLD="on">Rule of law conditionality</STRING></TI>
<HIDDEN><STRING ITALIC="on">(A10-0240/2025 - Rapporteurs: Jean-Marc Germain, Monika Hohlmeier)</STRING></HIDDEN>
<TXTLST><RESOL><TI><STRING BOLD="on">European Parliament resolution of <DATE ISO="20251218">18 December 2025</DATE> on the implementation of the rule of law conditionality regime (2025/2061(INI))</STRING></TI>
<TEXT><PREAMBLE><PRINIT><STRING ITALIC="on">The European Parliament,</STRING></PRINIT>
<GRVISA><VISA><P><NO.P>&#8211;</NO.P>having regard to Regulation (EU, Euratom) 2020/2092<FOOTNOTE><FIELD>OJ L 433 I, 22.12.2020, p. 1.</FIELD></FOOTNOTE>,</P></VISA></GRVISA>
<GRCONS><CONS><P><NO.P>A.</NO.P>whereas the rule of law is a founding value;</P></CONS>
<CONS><P><NO.P>B.</NO.P>whereas the Conditionality Regulation applies across the budget;</P></CONS></GRCONS></PREAMBLE>
<DISPOSITIF><ACTLST><ACTINIT><STRING BOLD="on">Introduction and legal context</STRING></ACTINIT>
<ACTION><P><NO.P>1.</NO.P>Recalls that respect for the rule of law is an essential prerequisite;</P></ACTION>
<ACTION><P><NO.P>2.</NO.P>Highlights the adoption of the Conditionality Regulation in 2020;</P></ACTION>
</ACTLST></DISPOSITIF></TEXT></RESOL></TXTLST></SDOCTA>"""

#: The 2017 drafting of the SAME schema: there is no <GRCONS>, and the lettered recitals
#: are <ACTION> elements under <DISPOSITIF> beside the numbered paragraphs. The annex is
#: a second <TXTLST>.
SDOCTA_LEGACY = b"""<?xml version="1.0" encoding="UTF-8"?>
<SDOCTA STATUS="DEF2"><IDENT><STRING BOLD="on">P8_TA(2017)0051</STRING></IDENT>
<TI><STRING BOLD="on">Civil Law Rules on Robotics</STRING></TI>
<HIDDEN><STRING ITALIC="on">(A8-0005/2017 - Rapporteur: Mady Delvaux)</STRING></HIDDEN>
<TXTLST><RESOL><TI><STRING BOLD="on">European Parliament resolution of <DATE ISO="20170216">16 February 2017</DATE> with recommendations to the Commission on Civil Law Rules on Robotics (2015/2103(INL))</STRING></TI>
<TEXT><PREAMBLE><PRINIT><STRING ITALIC="on">The European Parliament,</STRING></PRINIT>
<GRVISA><VISA><P><NO.P>&#8211;</NO.P>having regard to Council Directive 85/374/EEC,</P></VISA></GRVISA></PREAMBLE>
<DISPOSITIF><ACTLST><ACTINIT><STRING BOLD="on">Introduction</STRING></ACTINIT>
<ACTION><P><NO.P>A.</NO.P>whereas people have fantasised about intelligent machines;</P></ACTION>
<ACTION><P><NO.P>1.</NO.P>Calls on the Commission to propose a definition of smart robots;</P></ACTION>
<ACTINIT><STRING BOLD="on">o o o</STRING></ACTINIT>
<ACTION><P><NO.P>68.</NO.P>Instructs its President to forward this resolution.</P></ACTION>
</ACTLST></DISPOSITIF></TEXT></RESOL></TXTLST>
<TXTLST><ANNEX><TI><STRING BOLD="on">ANNEX TO THE RESOLUTION:</STRING></TI><P>A common European definition for smart autonomous robots should be established.</P></ANNEX></TXTLST></SDOCTA>"""

#: Formex <GENERAL>: the resolution proper. Larger than the annex member below.
FORMEX_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<GENERAL><BIB.INSTANCE><DOCUMENT.REF FILE="C_2018252EN.01023901.doc.xml"><COLL>C</COLL><NO.OJ>252</NO.OJ><YEAR>2018</YEAR></DOCUMENT.REF><PAGE.FIRST>239</PAGE.FIRST></BIB.INSTANCE>
<TITLE><TI><P>P8_TA(2017)0051</P><P>Civil Law Rules on Robotics</P><P>European Parliament resolution of <DATE ISO="20170216">16 February 2017</DATE> with recommendations to the Commission on Civil Law Rules on Robotics (2015/2103(INL))</P><NO.DOC.C>2018/C 252/25</NO.DOC.C></TI></TITLE>
<CONTENTS><PREAMBLE.GEN><PREAMBLE.INIT><P><HT TYPE="ITALIC">The European Parliament</HT>,</P></PREAMBLE.INIT>
<LIST TYPE="DASH"><ITEM><P>having regard to Article 225 of the Treaty on the Functioning of the European Union,</P></ITEM>
<ITEM><P>having regard to Council Directive 85/374/EEC,</P></ITEM></LIST></PREAMBLE.GEN>
<GR.SEQ LEVEL="1"><TITLE><TI><P><HT TYPE="BOLD">Introduction</HT></P></TI></TITLE>
<LIST TYPE="ALPHA"><ITEM><NP><NO.P>A.</NO.P><TXT>whereas people have fantasised about intelligent machines;</TXT></NP></ITEM>
<ITEM><NP><NO.P>B.</NO.P><TXT>whereas the legislature must consider the legal implications;</TXT></NP></ITEM></LIST>
<NP><NO.P>1.</NO.P><TXT>Calls on the Commission to propose a definition of smart robots;</TXT></NP>
<NP><NO.P>12.</NO.P><TXT>Highlights the principle of transparency in decisions taken with the aid of AI;</TXT></NP></GR.SEQ>
<GR.SEQ LEVEL="1"><TITLE><TI><P>o o o</P></TI></TITLE>
<NP><NO.P>68.</NO.P><TXT>Instructs its President to forward this resolution.</TXT></NP></GR.SEQ>
</CONTENTS></GENERAL>"""

#: The annex, in its own — SMALLER — member. Selecting the largest member drops it.
FORMEX_ANNEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<ANNEX><BIB.INSTANCE><NO.SEQ>0026.0001</NO.SEQ><PAGE.FIRST>252</PAGE.FIRST></BIB.INSTANCE>
<TITLE><TI><P><HT TYPE="BOLD">ANNEX TO THE RESOLUTION:</HT></P></TI></TITLE>
<P>A common European definition for smart autonomous robots should be established.</P></ANNEX>"""


def formex_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("C_2018252EN.01023901.doc.xml", b"<DOC><BIB.DOC/></DOC>")
        zf.writestr("C_2018252EN.01023901.xml", FORMEX_BODY)
        zf.writestr("C_2018252EN.01025201.xml", FORMEX_ANNEX)
    return buf.getvalue()


def labels(doc, kind=None):
    return [s.label for s in doc.segments if kind is None or s.kind == kind]


def body(doc, label):
    seg = next(s for s in doc.segments if s.label == label)
    return doc.text[seg.char_start:seg.char_end]


# ---------------------------------------------------------------- markers

def test_marker_classification_is_by_marker_not_by_tag():
    assert classify("A.") == ("Recital A", "recital")
    assert classify("(AH)") == ("Recital AH", "recital")
    assert classify("12.") == ("Paragraph 12", "paragraph")
    assert classify("(7)") == ("Paragraph 7", "paragraph")
    assert classify("–") == ("Preamble", "preamble")
    assert classify("") == ("Preamble", "preamble")
    assert classify("3a.") is None


def test_typographic_separator_is_not_a_heading():
    assert is_heading("Introduction")
    assert not is_heading("o o o")
    assert not is_heading("* * *")


# ---------------------------------------------------------------- SDOCTA

def test_sdocta_modern_structure_title_and_metadata():
    doc = parse("ep-ta-xml", SDOCTA_MODERN)
    assert doc.metadata == {"ep_document_id": "P10_TA(2025)0343",
                            "report": "A10-0240/2025",
                            "rapporteurs": ["Jean-Marc Germain", "Monika Hohlmeier"]}
    # the title is one element with an inline <DATE>; everything after the date is that
    # element's TAIL, and dropping it left "…resolution of 18 December 2025" with no subject
    assert doc.title == ("European Parliament resolution of 18 December 2025 on the "
                         "implementation of the rule of law conditionality regime "
                         "(2025/2061(INI))")
    assert labels(doc, "recital") == ["Recital A", "Recital B"]
    assert labels(doc, "paragraph") == ["Paragraph 1", "Paragraph 2"]
    assert labels(doc, "section") == ["Introduction and legal context"]
    # the visa's footnote is where the OJ reference of the invoked act lives
    preamble = body(doc, "Preamble")
    assert preamble.startswith("The European Parliament,")
    assert "Regulation (EU, Euratom) 2020/2092 (OJ L 433 I, 22.12.2020, p. 1.)" in preamble
    # the marker is the label, not part of the text
    assert body(doc, "Recital A") == "whereas the rule of law is a founding value;"


def test_sdocta_legacy_recitals_are_action_elements_and_annex_is_a_second_txtlst():
    doc = parse("ep-ta-xml", SDOCTA_LEGACY)
    assert doc.metadata["report"] == "A8-0005/2017"
    assert doc.metadata["rapporteurs"] == ["Mady Delvaux"]
    # 2017 put the lettered recitals under <DISPOSITIF> beside the paragraphs; a parser
    # that classified on the element would have called this one "Paragraph A"
    assert labels(doc, "recital") == ["Recital A"]
    assert labels(doc, "paragraph") == ["Paragraph 1", "Paragraph 68"]
    assert labels(doc, "section") == ["Introduction"]      # NOT the "o o o" rule
    assert labels(doc, "annex") == ["ANNEX TO THE RESOLUTION:"]
    assert "smart autonomous robots" in body(doc, "ANNEX TO THE RESOLUTION:")


# ---------------------------------------------------------------- Formex

def test_formex_resolution_keeps_the_annex_that_is_not_the_largest_member():
    doc = parse("formex-resolution", formex_zip())
    assert labels(doc, "annex") == ["ANNEX TO THE RESOLUTION:"]
    assert "smart autonomous robots" in body(doc, "ANNEX TO THE RESOLUTION:")
    # …and the publication manifest is not prose: it used to prefix the annex with
    # "C 252 2018 EN 239 …"
    assert not body(doc, "ANNEX TO THE RESOLUTION:").startswith("C 252")


def test_formex_resolution_structure_matches_the_parliaments_own_markup():
    doc = parse("formex-resolution", formex_zip())
    assert doc.title == ("European Parliament resolution of 16 February 2017 with "
                         "recommendations to the Commission on Civil Law Rules on "
                         "Robotics (2015/2103(INL))")
    assert labels(doc, "recital") == ["Recital A", "Recital B"]
    assert labels(doc, "paragraph") == ["Paragraph 1", "Paragraph 12", "Paragraph 68"]
    assert labels(doc, "section") == ["Introduction"]
    assert body(doc, "Paragraph 12").startswith("Highlights the principle of transparency")
    assert "having regard to Article 225" in body(doc, "Preamble")


def test_the_act_parser_is_why_this_format_exists():
    """Handed the same package, ``formex-legislation`` keeps only the ANNEX — a
    resolution has no ARTICLE and no CONSID, so its recitals, its sixty-eight operative
    paragraphs and its preamble all fall out, and what survives is the appendix. This
    is the defect the resolution parser exists to fix, so it is asserted rather than
    described."""
    act = parse("formex-legislation", formex_zip())
    assert [s.kind for s in act.segments] == ["annex"]
    assert "Highlights the principle of transparency" not in (act.text or "")

    resolution = parse("formex-resolution", formex_zip())
    assert "Highlights the principle of transparency" in resolution.text


def test_a_resolution_with_two_paragraph_ones_gets_unique_labels():
    doc = parse("ep-ta-xml", SDOCTA_MODERN.replace(b"<NO.P>2.</NO.P>", b"<NO.P>1.</NO.P>"))
    assert labels(doc, "paragraph") == ["Paragraph 1", "Paragraph 1 (2)"]


# ---------------------------------------------------------------- identifiers

def test_celex_family_and_the_three_forms_a_resolution_is_cited_by():
    assert family("52017IP0051")[0] == "resolutions"
    assert family("52024AP0138")[0] == "legislative-resolutions"
    assert family("52025BP0325")[0] == "budget"
    assert portal_id("P8_TA(2017)0051") == "TA-8-2017-0051"
    assert aliases_for("52017IP0051", "P8_TA(2017)0051") == [
        "52017IP0051", "P8_TA(2017)0051", "P8_TA-PROV(2017)0051", "T8-0051/2017"]
    assert aliases_for("51985IP0100", None) == ["51985IP0100"]


def test_both_written_forms_of_an_adopted_text_reach_one_candidate():
    found = {c.candidate_id for c in extract_citations(
        "resolution P9_TA(2024)0138, printed as T9-0138/2024, and P9_TA-PROV(2024)0138")}
    assert found == {"P9_TA(2024)0138"}


def test_the_general_court_is_not_mistaken_for_an_adopted_text():
    """``T9-0138/2024`` is a resolution and ``T-604/18`` is a General Court case. The
    OJ-form grammar is anchored on a digit before the hyphen and a four-digit item
    number, so it cannot swallow the case numbering."""
    found = {c.candidate_id for c in extract_citations("Case T-604/18 P and Case T-1/24 R")}
    assert "62018TJ0604" in found
    assert not any(str(c or "").startswith("P") and "_TA(" in str(c) for c in found)


# ---------------------------------------------------------------- adapter

class _Resp:
    def __init__(self, content=b"", status=200, payload=None):
        self.content, self.status_code, self._payload = content, status, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _portal_record(path):
    return {"data": [{"id": "eli/dl/doc/TA-8-2017-0051",
                      "is_realized_by": [{"id": "eli/dl/doc/TA-8-2017-0051/en",
                                          "is_embodied_by": [{"is_exemplified_by": path}]}]}]}


def test_fetch_prefers_the_parliaments_own_xml_over_the_official_journal(monkeypatch):
    """A resolution reaches CELLAR only when the OJ C carrying it is published — a year
    late for the 2017 text. If the Parliament has it, read it from there."""
    calls: list[str] = []
    path = "distribution/reds_iPlTa_Itm/TA-8-2017-0051/TA-8-2017-0051-FNL_en.xml"

    def fake_get(url, **kw):
        calls.append(url)
        if "/adopted-texts/" in url:
            return _Resp(b"{}", payload=_portal_record(path))
        if url.endswith(".xml"):
            return _Resp(SDOCTA_LEGACY)
        raise AssertionError(f"unexpected fetch: {url}")

    ad = EPResolutionsAdapter(celex="52017IP0051")
    monkeypatch.setattr(ad._client, "get", fake_get)
    rec = ad.fetch(Stub(stable_id="52017IP0051",
                        hints={"title": None, "ta_reference": "P8_TA(2017)0051"}))
    assert rec.source == "eu-ep-resolutions" and rec.doc_type is DocType.PREPARATORY
    assert rec.extra["format"] == "ep-ta-xml" and rec.extra["ep_family"] == "resolutions"
    assert rec.extra["report"] == "A8-0005/2017"
    assert "T8-0051/2017" in rec.extra["aliases"]
    assert "Recital A" in [s.label for s in rec.segments]
    # the OJ was never asked for
    assert not any("publications.europa.eu" in u for u in calls)


def test_fetch_falls_back_to_formex_when_the_parliament_has_nothing(monkeypatch):
    ad = EPResolutionsAdapter(celex="52017IP0051")
    monkeypatch.setattr(ad, "_portal_xml", lambda ta: None)
    monkeypatch.setattr(ad, "_fetch_formex", lambda url, lang="en": formex_zip())
    rec = ad.fetch(Stub(stable_id="52017IP0051", hints={"title": None, "ta_reference": None}))
    assert rec.extra["format"] == "formex-resolution"
    assert "Paragraph 12" in [s.label for s in rec.segments]


def test_a_pre_1995_resolution_with_no_rendition_is_still_a_resolvable_node(monkeypatch):
    """CELLAR holds the work but no expression before ~1994. Dropping it would lose a
    citable authority; storing it metadata-only keeps the citation resolving."""
    ad = EPResolutionsAdapter(celex="51985IP0100")
    monkeypatch.setattr(ad, "_portal_xml", lambda ta: None)
    monkeypatch.setattr(ad, "_fetch_formex", lambda url, lang="en": None)
    monkeypatch.setattr(ad, "_fetch_html", lambda celex, lang="en": None)
    monkeypatch.setattr(ad, "_pdf", lambda celex: None)
    rec = ad.fetch(Stub(stable_id="51985IP0100",
                        hints={"title": "Resolution on data protection", "ta_reference": None}))
    assert rec.extra["metadata_only"] is True
    assert rec.text in (None, "")
    assert rec.title == "Resolution on data protection"


def test_a_resolution_about_one_directive_declares_it_as_the_home_for_bare_articles(monkeypatch):
    ad = EPResolutionsAdapter(celex="52020IP0032")
    monkeypatch.setattr(ad, "_portal_xml", lambda ta: None)
    monkeypatch.setattr(ad, "_fetch_formex", lambda url, lang="en": formex_zip())
    rec = ad.fetch(Stub(stable_id="52020IP0032", hints={
        "title": "European Parliament resolution on the implementation of Directive 2005/29/EC",
        "ta_reference": None}))
    assert rec.extra["citation_default_instrument"] == {"id": "32005L0029", "kind": "directive"}


EURLEX_LEGACY_HTML = b"""<html><head><title>EUR-Lex - 52004IP0005 - EN</title></head><body>
<p>Important legal notice</p><p>|</p><p>52004IP0005</p>
<p>European Parliament resolution on the outcome of the Buenos Aires Conference on climate change</p>
<p>Official Journal 247 E , 06/10/2005 P. 0144 - 0146</p><p>P6_TA(2005)0005</p>
<p>The European Parliament,</p>
<p>- having regard to the Kyoto Protocol to the UNFCCC of 11 December 1997,</p></body></html>"""


def test_the_pre_2007_route_recovers_the_title_and_reference_from_the_page_itself(monkeypatch):
    """EUR-Lex's legacy HTML titles every page "EUR-Lex - 52005IP0005 - EN" and CELLAR
    records no work_id_document that far back, so both the title and the Parliament
    reference have to come off the document. Without the reference, a citation to
    P6_TA(2005)0005 has nothing to resolve against."""
    ad = EPResolutionsAdapter(celex="52005IP0005", use_ep_portal="false")
    monkeypatch.setattr(ad, "_fetch_formex", lambda url, lang="en": None)
    monkeypatch.setattr(ad, "_fetch_html", lambda celex, lang="en": EURLEX_LEGACY_HTML)
    rec = ad.fetch(Stub(stable_id="52005IP0005",
                        hints={"title": None, "ta_reference": None}))
    assert rec.extra["format"] == "eurlex-html"
    # bounded at the OJ reference — it used to run on into the first visa
    assert rec.title == ("European Parliament resolution on the outcome of the Buenos "
                         "Aires Conference on climate change")
    assert rec.extra["ta_reference"] == "P6_TA(2005)0005"
    assert "T6-0005/2005" in rec.extra["aliases"]


def test_a_resolution_is_dated_because_its_celex_cannot_date_it(monkeypatch):
    """``effective_date``'s identifier rung needs a ``/YYYY/`` path segment, which a
    CELEX has not got — so without an explicit decision_date every resolution in the
    corpus sorted and filtered as undated."""
    from raglex.storage.catalogue import effective_date

    assert effective_date(None, None, "52025IP0256") == (None, "none")

    ad = EPResolutionsAdapter(celex="52025IP0256", use_ep_portal="false")
    monkeypatch.setattr(ad, "_fetch_formex", lambda url, lang="en": formex_zip())
    rec = ad.fetch(Stub(stable_id="52025IP0256",
                        hints={"title": None, "ta_reference": None,
                               "watermark": "2025-10-23"}))
    assert rec.decision_date.isoformat() == "2025-10-23"


def test_a_resolution_with_no_cellar_date_is_dated_from_its_own_title(monkeypatch):
    ad = EPResolutionsAdapter(celex="52017IP0051", use_ep_portal="false")
    monkeypatch.setattr(ad, "_fetch_formex", lambda url, lang="en": formex_zip())
    rec = ad.fetch(Stub(stable_id="52017IP0051",
                        hints={"title": None, "ta_reference": None, "watermark": None}))
    # "European Parliament resolution of 16 February 2017 with recommendations …"
    assert rec.decision_date.isoformat() == "2017-02-16"


def test_discovery_carries_a_resumable_offset_and_the_parliament_reference(monkeypatch):
    ad = EPResolutionsAdapter(page_size=2, start_offset=4)
    pages = [
        [{"celex": "52024IP0002", "date": "2024-01-18", "title": "On X",
          "docids": "celex:52024IP0002|immc:P9_TA(2024)0002"},
         {"celex": "52024IP0001", "date": "2024-01-17", "title": "On Y", "docids": ""}],
        [],
    ]
    monkeypatch.setattr(ad, "_sparql", lambda q: pages.pop(0))
    stubs = list(ad.discover(None))
    assert [s.stable_id for s in stubs] == ["52024IP0002", "52024IP0001"]
    assert stubs[0].hints["ta_reference"] == "P9_TA(2024)0002"
    assert stubs[1].hints["ta_reference"] is None
    assert {s.hints["resume_offset"] for s in stubs} == {4}


def test_the_enumeration_query_is_scoped_to_the_requested_families():
    ad = EPResolutionsAdapter(types="IP,AP", years="2015-2016")
    query = ad._enumerate_query("2016-01-01", 0)
    assert "(IP|AP)" in query
    assert '"2015-01-01"' in query and '"2016-12-31"' in query
    assert 'STR(?date) > "2016-01-01"' in query
    assert "OFFSET 0" in query


# ---------------------------------------------------------------- follow-ups

def test_followup_is_linked_to_the_adopted_text_it_answers():
    assert ta_reference("SP-2026-04-14-TA-10-2025-0343") == "P10_TA(2025)0343"
    assert ta_reference("SP-2026-04-14") is None
    stubs = followup_stubs({"data": [
        {"id": "eli/dl/doc/SP-2026-04-14-TA-10-2025-0343",
         "answers_to": "eli/dl/doc/TA-10-2025-0343", "document_date": "2026-04-14"}]})
    assert stubs[0].stable_id == "ep/followup/SP-2026-04-14-TA-10-2025-0343"
    assert stubs[0].hints["answers_to"] == "TA-10-2025-0343"
    assert stubs[0].hints["watermark"] == "2026-04-14"


def test_followup_fetch_writes_the_edge_towards_the_resolution(monkeypatch):
    ad = EPFollowUpsAdapter()
    monkeypatch.setattr(ad, "_english_pdf",
                        lambda doc_id: (b"%PDF-1.4 x", "https://example/x.pdf",
                                        "Follow up to T10-0343/2025"))
    monkeypatch.setattr("raglex.extraction.extract_bytes",
                        lambda *a, **k: type("E", (), {"text": "The Commission recalls "
                                                       "Regulation (EU) 2020/2092."})())
    rec = ad.fetch(Stub(stable_id="ep/followup/SP-2026-04-14-TA-10-2025-0343",
                        hints={"doc_id": "SP-2026-04-14-TA-10-2025-0343",
                               "answers_to": "TA-10-2025-0343"}))
    assert rec.doc_type is DocType.PREPARATORY
    assert rec.extra["ta_reference"] == "P10_TA(2025)0343"
    assert rec.extra["require_recognized_legal_citation"] is True
    assert [(r.relationship_type.value, r.dst_id) for r in rec.relations] == [
        ("related_to", "P10_TA(2025)0343")]


def test_a_refused_first_page_is_an_error_not_an_empty_register(monkeypatch):
    """The service answers pressure with a 404 or an empty page, never a 429. Returning
    quietly recorded "discovered 0 — done" over a register of 4,301 records it had not
    read, which is the same lie as a truncated walk wearing a success."""
    from raglex.core.errors import FetchError

    ad = EPFollowUpsAdapter()

    def refuse(url, **kw):
        raise FetchError("HTTP 404", transient=True)

    monkeypatch.setattr(ad._client, "get", refuse)
    with pytest.raises(FetchError):
        list(ad.discover(None))

    # …and its quiet form: 200 with no rows while meta still claims thousands
    monkeypatch.setattr(ad._client, "get",
                        lambda url, **kw: _Resp(b"{}", payload={"data": [], "meta": {"total": 4301}}))
    with pytest.raises(FetchError):
        list(ad.discover(None))

    # a genuinely empty register is not an error
    monkeypatch.setattr(ad._client, "get",
                        lambda url, **kw: _Resp(b"{}", payload={"data": [], "meta": {"total": 0}}))
    assert list(ad.discover(None)) == []


def test_followup_discovery_stops_at_the_reported_total(monkeypatch):
    ad = EPFollowUpsAdapter()
    payload = {"data": [{"id": "eli/dl/doc/SP-2026-04-14-TA-10-2025-0343",
                         "answers_to": "eli/dl/doc/TA-10-2025-0343",
                         "document_date": "2026-04-14"}],
               "meta": {"total": 1}}
    monkeypatch.setattr(ad._client, "get", lambda url, **kw: _Resp(b"{}", payload=payload))
    stubs = list(ad.discover(None))
    assert len(stubs) == 1 and stubs[0].hints["feed_total"] == 1


def test_followup_discovery_ignores_since_because_the_register_has_no_order(monkeypatch):
    """The register offers neither a date filter nor a sort, so nothing about a record's
    position implies its date — and every cursor in the pipeline assumes otherwise.

    Filtering on ``since`` here dropped rows from the middle of an offset-paged walk,
    and the backfill frontier (sound only for a newest-first feed) then truncated it
    outright: a resumed backfill discovered 4 of 4,301 records and called itself done.
    The walk is whole every time, which is what ``full-walk`` promises."""
    ad = EPFollowUpsAdapter()
    payload = {"data": [
        {"id": "eli/dl/doc/SP-2020-01-01-TA-9-2019-0001", "document_date": "2020-01-01"},
        {"id": "eli/dl/doc/SP-2026-04-14-TA-10-2025-0343", "document_date": "2026-04-14"}],
        "meta": {"total": 2}}
    monkeypatch.setattr(ad._client, "get", lambda url, **kw: _Resp(b"{}", payload=payload))
    assert [s.hints["doc_id"] for s in ad.discover("2025-01-01")] == [
        "SP-2020-01-01-TA-9-2019-0001", "SP-2026-04-14-TA-10-2025-0343"]


# ---------------------------------------------------------------- registry

@pytest.mark.parametrize("key,mode", [("eu-ep-resolutions", "early-stop"),
                                      ("eu-ep-followups", "full-walk")])
def test_sources_are_in_the_catalogue_with_a_truthful_incremental_mode(key, mode):
    row = next(r for r in source_catalog() if r["key"] == key)
    assert row["kind"] == "preparatory" and row["jurisdiction"] == "EU"
    assert row["group_label"] == "European Union"
    assert INCREMENTAL_MODE[key] == mode


@pytest.mark.parametrize("key", ["eu-ep-resolutions", "eu-ep-followups"])
def test_every_declared_option_is_accepted_by_the_constructor(key):
    row = next(r for r in source_catalog() if r["key"] == key)
    kwargs = {opt["name"]: "1" for opt in row.get("options") or []}
    assert ADAPTERS[key](**kwargs) is not None
