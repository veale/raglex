"""Austria, Slovakia, Finland, Sweden and Estonia — five APIs, five ways to be wrong.

Each of these sources has at least one failure that returns HTTP 200 and looks like
success. Every test here is one of them, or one of the structures the corpus would
otherwise lose:

* Austria answers a rejected parameter with ``200`` and an ``Error`` body, dates a
  Rechtssatz by its most recent APPLICATION, and collapses one field to a string and the
  field beside it to a list.
* Slovakia accepts its own output date format as a filter input and silently ignores it,
  returning all 4.68 million rows as if they were the week you asked for.
* Finland's listing hands back retrieval URLs that 404, with a court segment the
  retrieval route refuses.
* Sweden pages from zero and offers sort parameters that do not order the paged set.
* Estonia's only public interface answers JSON-RPC over ``text/event-stream``.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.adapters import registry
from raglex.adapters.at_ris import (
    APPLICATIONS,
    AustrianRISAdapter,
    _aliases,
    _dockets,
    _hits,
    _norm_relations,
    _rechtssatz_relations,
    _stammrechtssatz_relations,
    originating_date,
    stable_id_for,
)
from raglex.adapters.ee_lahend import _eu_relations, _section_relations, parse_sse
from raglex.adapters.fi_finlex import SERIES, retrieval_url, work_uri_matches
from raglex.adapters.se_domstol import _fritext, _lagrum_relations, _reference_relations
from raglex.adapters.sk_ress import _api_date, _appeal_relations, _iso, _metadata_text
from raglex.core.errors import FetchError
from raglex.core.models import RelationshipType
from raglex.formats.domstol_html import parse_domstol_html
from raglex.formats.finlex_akn import parse_finlex_akn
from raglex.formats.lahend_md import parse_lahend_md
from raglex.formats.ris_xml import parse_ris


# ── Austria ──────────────────────────────────────────────────────────────────
def test_a_rejected_ris_parameter_is_an_error_not_an_empty_window():
    """RIS answers a bad parameter with HTTP 200 and an ``Error`` body. Read as an empty
    result, a whole date window would be recorded as holding nothing."""
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"OgdSearchResult": {"Error": {
                "Applikation": "Justiz", "Message": "Schema Validation Error"}}}

    class _Client:
        def get(self, *a, **kw):
            return _Resp()

    adapter = AustrianRISAdapter(application="Justiz", client=_Client())
    with pytest.raises(FetchError, match="RIS rejected"):
        adapter._get({})


def test_a_rechtssatz_is_dated_by_its_origin_not_by_its_latest_application():
    # Entscheidungsdatum on a Rechtssatz is the date of the most RECENT decision to apply
    # it: a 1954 proposition carried 2026-05-26. The originating date is in the id, and
    # using the other one scrambles every date filter built on top.
    assert originating_date("JJR_19540519_OGH0002_0010OB00346_5400000_002") == date(1954, 5, 19)
    assert originating_date("JJT_20260130_OGH0002_018OCG00003_25B0000_000") == date(2026, 1, 30)
    assert originating_date("no-date-here") is None


def test_geschaeftszahl_is_a_semicolon_string_and_normen_beside_it_is_a_list():
    """Two different degenerate shapes in one record — see the module docstring."""
    field = {"item": "1Ob346/54; 8Ob110/65; 6Ob127/20z"}
    assert _dockets(field) == ["1Ob346/54", "8Ob110/65", "6Ob127/20z"]
    # …and the singular case is a bare string, not a one-element list.
    assert _dockets({"item": "18OCg3/25b"}) == ["18OCg3/25b"]


def test_hits_reads_the_total_not_the_page_length():
    payload = {"OgdDocumentResults": {"Hits": {"@pageNumber": "1", "@pageSize": "100",
                                              "#text": "138453"}}}
    assert _hits(payload) == 138453
    assert _hits({}) == 0


def test_an_austrian_norm_index_reaches_the_eu_corpus_and_the_austrian_one():
    edges = _norm_relations({"item": ["DSGVO Art6 Abs1 litf", "ZPO §611 Abs2 Z1",
                                      "AEUV Lissabon Art267", "DSGVO ErwGr101"]})
    by_target = {(e.dst_id, e.dst_anchor) for e in edges}
    # The GDPR reference lands on the CELEX the corpus already holds, in the anchor
    # vocabulary an English citation of the same article produces.
    assert ("32016R0679", "Article 6(1)(f)") in by_target
    assert ("12016E", "Article 267") in by_target
    assert ("32016R0679", "Recital 101") in by_target
    # …and the domestic one is Austrian, not the German act of the same abbreviation.
    assert ("at/gesetz/zpo", "§ 611 Abs 2 Z 1") in by_target
    assert not any((e.dst_id or "").startswith("de/") for e in edges)
    assert all(e.relationship_type == RelationshipType.INTERPRETS for e in edges)


def test_the_rechtssatz_treatment_vocabulary_becomes_typed_edges():
    specific = {"Entscheidungstexte": {"item": [
        {"Geschaeftszahl": "1 Ob 346/54", "Gericht": "OGH", "Entscheidungsdatum": "1954-05-19"},
        {"Geschaeftszahl": "2 Ob 514/77", "Gericht": "OGH", "Anmerkung": "vgl auch"},
        {"Geschaeftszahl": "6 Ob 55/19a", "Gericht": "OGH", "Anmerkung": "Gegenteilig"},
        {"Geschaeftszahl": "6 Ob 127/20z", "Gericht": "OGH",
         "Entscheidungsart": "Verstärkter Senat",
         "Anmerkung": "Bewertungsausspruch nicht vorzunehmen (Ablehnung von 3 Ob 110/14y). (T17)"},
    ]}}
    edges, treatments = _rechtssatz_relations(specific)
    kinds = {(e.dst_id, e.relationship_type) for e in edges}
    assert ("at:case:OGH:1Ob346/54", RelationshipType.APPLIES) in kinds
    assert ("at:case:OGH:2Ob514/77", RelationshipType.CONSIDERS) in kinds
    assert ("at:case:OGH:6Ob55/19a", RelationshipType.DISTINGUISHES) in kinds
    # The express rejection is an assertion about the NAMED decision, and the applying
    # decision's own edge stays an application: 6 Ob 127/20z applied the proposition AND
    # rejected 3 Ob 110/14y, which are two different facts about two different cases.
    assert ("at:case:OGH:6Ob127/20z", RelationshipType.APPLIES) in kinds
    assert ("at:case:OGH:3Ob110/14y", RelationshipType.OVERRULES) in kinds
    rejection = next(e for e in edges if e.dst_id == "at:case:OGH:3Ob110/14y")
    # …and the audit trail has to say who did the rejecting.
    assert "6 Ob 127/20z" in (rejection.raw_citation_string or "")
    assert any(t.get("enlarged_panel") for t in treatments)
    assert any(t.get("rejects") == "3 Ob 110/14y" for t in treatments)


def test_the_vwgh_records_which_proposition_this_one_restates():
    edges = _stammrechtssatz_relations({
        "Stammrechtssatznummer": "JWR_2010030165_20111021X05",
        "HinweisAufStammrechtssatz": "GRS wie 2010/03/0165 E 21. Oktober 2011 RS 5"})
    assert [(e.relationship_type, e.dst_id) for e in edges] == [
        (RelationshipType.FOLLOWS, "at/ris/JWR_2010030165_20111021X05")]


def test_a_rechtssatz_does_not_claim_the_dockets_of_the_cases_that_applied_it():
    """Its Geschaeftszahl lists every applying decision. Registering those against the
    headnote would make each of those judgments resolve to the headnote instead."""
    rs = _aliases("JJR_1", ["1Ob346/54", "6Ob127/20z"], ["RS0042418"], "", "Justiz", True)
    assert "at/rs/RS0042418" in rs
    assert not any(a.startswith("at:case:") for a in rs)
    # An Entscheidungstext DOES own its docket — that is the form a later judgment writes.
    text = _aliases("JJT_1", ["18OCg3/25b"], [], "", "Justiz", False)
    assert "at:case:OGH:18OCg3/25b" in text


def test_a_vfgh_collection_number_becomes_the_alias_a_citation_uses():
    aliases = _aliases("JFT_1", ["E2493/2023"], [], "20676", "Vfgh", False)
    assert "at/vfslg/20676" in aliases


def test_an_austrian_document_is_keyed_by_its_ecli_where_it_has_one():
    assert stable_id_for("ECLI:AT:OGH0002:2026:RS0142730", "JJR_x") == \
        "ECLI:AT:OGH0002:2026:RS0142730"
    assert stable_id_for("", "JJR_x") == "at/ris/JJR_x"


def test_every_ris_application_is_registered_and_bfg_is_not_one_of_them():
    assert set(APPLICATIONS) >= {"Justiz", "Vwgh", "Vfgh", "Bvwg", "Lvwg", "Dsk",
                                 "Gbk", "Verg", "Dok", "Pvak", "Uvs", "AsylGH",
                                 "Ubas", "Umse", "Bks"}
    # The Bundesfinanzgericht publishes through findok.bmf.gv.at, not RIS. Planning a
    # tax adapter around this API would find nothing.
    assert "Bfg" not in APPLICATIONS
    with pytest.raises(ValueError, match="unknown RIS Applikation"):
        AustrianRISAdapter(application="Bfg")


def test_ris_xml_reads_as_the_judgment_rather_than_as_a_page_of_furniture():
    xml = """<?xml version="1.0" encoding="utf-8"?>
<risdok xmlns="http://www.bka.gv.at"><metadaten/><nutzdaten><abschnitt>
<kzinhalt typ="p"><absatz typ="kz">www.ris.bka.gv.at Seite 1 von 2</absatz></kzinhalt>
<fzinhalt typ="f"><absatz typ="fz">www.ris.bka.gv.at</absatz></fzinhalt>
<ueberschrift typ="titel">Gericht</ueberschrift>
<absatz typ="erltext" ct="gericht">OGH</absatz>
<ueberschrift typ="titel">Geschäftszahl</ueberschrift>
<absatz typ="erltext" ct="gz">18OCg3/25b</absatz>
<ueberschrift typ="titel">Norm</ueberschrift>
<absatz typ="erltext" ct="norm">ZPO §611 Abs2 Z1</absatz>
<ueberschrift typ="titel">Spruch</ueberschrift>
<absatz typ="erltext" ct="spruch">Das Klagebegehren wird <b>abgewiesen</b>.</absatz>
<ueberschrift typ="titel">Text</ueberschrift>
<absatz typ="erltext" ct="text"><gs> [1] </gs>Die Schiedsklägerin leitete<gdash/>ein.</absatz>
<absatz typ="erltext" ct="text"> [2] Der Senat erwog.</absatz>
<ueberschrift typ="titel">European Case Law Identifier</ueberschrift>
<absatz typ="erltext" ct="ecli">ECLI:AT:OGH0002:2026:018OCG00003.25B.013</absatz>
</abschnitt></nutzdaten></risdok>""".encode()
    parsed = parse_ris(xml)
    # The printed page header and footer repeat on every page and are not content.
    assert "www.ris.bka.gv.at" not in (parsed.text or "")
    assert "Seite 1 von 2" not in (parsed.text or "")
    # The metadata zones restate what the API already gave us as fields; keeping them in
    # the body makes a docket search match every document that merely prints one.
    assert parsed.metadata["court"] == "OGH"
    assert parsed.metadata["docket"] == "18OCg3/25b"
    assert parsed.metadata["ecli"] == "ECLI:AT:OGH0002:2026:018OCG00003.25B.013"
    assert "18OCg3/25b" not in (parsed.text or "")
    # …but Norm IS the list of provisions the decision turns on, in citable form.
    assert "ZPO §611 Abs2 Z1" in parsed.text
    # The Randnummer is the citable unit and becomes the label, not part of the prose.
    paragraphs = [s for s in parsed.segments if s.kind == "paragraph"]
    assert [s.label for s in paragraphs] == ["[1]", "[2]"]
    assert not parsed.text.startswith("[1]")
    # The element-spelled dash survives as a character.
    assert "leitete–ein" in parsed.text
    # Bold survives as a re-derivable projection anchored into the document text.
    bold = [f for s in parsed.segments for f in s.formatting if f["kind"] == "bold"]
    assert bold and parsed.text[bold[0]["start"]:bold[0]["end"]] == "abgewiesen"


# ── Slovakia ─────────────────────────────────────────────────────────────────
def test_the_slovak_date_filter_is_sent_in_the_format_it_obeys():
    """The API PRINTS ``21.09.2018`` and ACCEPTS it as a filter — then ignores it and
    returns the whole 4.68-million-row register. Only ISO is obeyed."""
    assert _api_date("21.09.2018") == "2018-09-21"
    assert _api_date("2018-09-21") == "2018-09-21"
    assert _api_date(date(2026, 8, 1)) == "2026-08-01"
    assert _api_date(None) is None
    # …and the OUTPUT is read in the format it actually arrives in.
    assert _iso("21.09.2018") == date(2018, 9, 21)


def test_the_court_below_is_a_stated_appellate_edge_typed_by_the_outcome():
    affirming = _appeal_relations({
        "povaha": ["Potvrdzujúce"],
        "povodnySud": {"nazov": "Mestský súd Bratislava II"},
        "povodnaSpisovaZnacka": "16Co/166/2018"})
    assert [(e.relationship_type, e.dst_id) for e in affirming] == [
        (RelationshipType.FOLLOWS, "sk:case:16Co/166/2018")]
    annulling = _appeal_relations({"povaha": ["Zrušujúce"],
                                   "povodnaSpisovaZnacka": "6S/74/2018"})
    assert annulling[0].relationship_type == RelationshipType.OVERRULES
    # No outcome stated is still an appellate edge — it asserts review and nothing more.
    bare = _appeal_relations({"povodnaSpisovaZnacka": "6S/74/2018"})
    assert bare[0].relationship_type == RelationshipType.CONSIDERS
    # …and a decision with no court below has no edge to invent.
    assert _appeal_relations({"povaha": ["Potvrdzujúce"]}) == []


def test_a_decision_whose_pdf_is_missing_is_still_a_decision():
    text = _metadata_text({
        "sud": {"nazov": "Najvyšší súd Slovenskej republiky"},
        "formaRozhodnutia": "Uznesenie", "spisovaZnacka": "2Tdo/78/2024",
        "ecli": "ECLI:SK:NSSR:2025:6322010282.1", "povaha": ["Potvrdzujúce"]})
    assert "Najvyšší súd" in text and "2Tdo/78/2024" in text
    assert "ECLI:SK:NSSR:2025:6322010282.1" in text


# ── Finland ──────────────────────────────────────────────────────────────────
def test_the_akn_uri_finlex_returns_is_not_the_url_that_serves_it():
    """The whole ``/judgment/`` tree 404s and the retrieval route refuses the court
    segment, so following the returned URI as given fetches nothing at all."""
    url, work = retrieval_url(
        "https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/judgment/"
        "court-of-appeal-decision/helsinki/2024/1563/fin@")
    assert url == ("https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/doc/"
                   "court-of-appeal-decision/2024/1563/fin@")
    # The Work path keeps the court, because that is the identity to check against.
    assert work == "court-of-appeal-decision/helsinki/2024/1563"
    plain, _ = retrieval_url(
        "https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/judgment/"
        "supreme-court-precedent/2024/1/fin@")
    assert plain.endswith("/doc/supreme-court-precedent/2024/1/fin@")
    assert retrieval_url("not a uri") is None


def test_dropping_the_court_segment_is_checked_against_what_came_back():
    """A decision number is only unique within its court, so the shortcut that makes
    retrieval work is also the one that could return Vaasa's 1563 for Helsinki's."""
    assert work_uri_matches("/akn/fi/judgment/court-of-appeal-decision/helsinki/2024/1563",
                            "court-of-appeal-decision/helsinki/2024/1563")
    assert not work_uri_matches("/akn/fi/judgment/court-of-appeal-decision/vaasa/2024/1563",
                                "court-of-appeal-decision/helsinki/2024/1563")
    # Nothing to compare against keeps the document rather than dropping it.
    assert work_uri_matches("", "anything")


def test_a_finnish_judgment_keeps_the_outline_the_court_wrote():
    xml = """<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
      xmlns:finlex="http://data.finlex.fi/schema/finlex">
  <judgment name="main"><meta><identification>
    <FRBRWork>
      <FRBRuri value="/akn/fi/judgment/supreme-court-precedent/2024/1"/>
      <FRBRalias name="ecli" value="ECLI:FI:KKO:2024:1"/>
      <FRBRalias name="diaryNumber" value="S2022/290"/>
      <FRBRdate date="2024-01-03" name="dateIssued"/>
      <FRBRauthor href="#organization_fi.court-of-appeal-helsinki"/>
    </FRBRWork></identification>
    <classification><keyword showAs="Konkurssi" value="konkurssi"/></classification>
    <references><TLCOrganization eId="organization_fi.court-of-appeal-helsinki"
       showAs="Helsingin hovioikeus"/></references>
    <proprietary><finlex:legalBasis refersTo="#concept_legal-basis.eu.gdpr"/></proprietary>
  </meta>
  <header><p><docNumber>KKO:2024:1</docNumber></p></header>
  <judgmentBody>
    <introduction><p>Verohallinto oli hakenut.</p></introduction>
    <background><tblock><heading>Asian käsittely käräjäoikeudessa</heading>
        <tblock><heading>Asian tausta</heading><p>A Oy oli hakenut.</p></tblock>
      </tblock></background>
    <motivation><p>Korkein oikeus katsoi.</p></motivation>
  </judgmentBody></judgment></akomaNtoso>""".encode()
    parsed = parse_finlex_akn(xml)
    meta = parsed.metadata
    assert meta["ecli"] == "ECLI:FI:KKO:2024:1"
    assert meta["diary_number"] == "S2022/290"
    assert meta["doc_number"] == "KKO:2024:1"
    # The court's name comes from the document's own organisation registry, so a court
    # Finlex adds tomorrow arrives correctly named rather than prettified from a slug.
    assert meta["author_name"] == "Helsingin hovioikeus"
    assert meta["legal_basis"] == ["concept_legal-basis.eu.gdpr"]
    assert meta["zones"] == ["introduction", "background", "motivation"]
    # The nesting is the outline: a heading inside a heading is a level deeper.
    levels = {s.label: s.level for s in parsed.segments if s.kind == "heading"}
    assert levels["Asian tausta ja käsittely"] == 0
    assert levels["Asian käsittely käräjäoikeudessa"] == 1
    assert levels["Asian tausta"] == 2


def test_a_metadata_only_akn_says_so_instead_of_looking_like_an_empty_judgment():
    xml = b"""<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
      <doc name="main"><meta><identification><FRBRWork>
        <FRBRuri value="/akn/fi/doc/government-proposal/2024/153"/></FRBRWork>
      </identification></meta>
      <preface><p>HE 153/2024</p></preface><mainBody/></doc></akomaNtoso>"""
    parsed = parse_finlex_akn(xml)
    # The preface is a title block, not the proposal. Reading it as text would store 80
    # characters and never go to main.pdf for the 150,000 that are the document.
    assert parsed.text is None
    assert parsed.metadata["pdf_only"] is True


def test_every_finnish_series_is_a_registered_source():
    assert set(SERIES) <= set(registry.ADAPTERS)
    assert set(SERIES) <= set(registry.SOURCE_INFO)


# ── Sweden ───────────────────────────────────────────────────────────────────
def test_a_cited_authority_may_be_a_json_array_pretending_to_be_a_string():
    assert _fritext({"fritext": "NJA 1970 s. 274 "}) == ["NJA 1970 s. 274"]
    assert _fritext({"fritext": '["NJA 1991 s. 277","NJA 1991:47"]'}) == [
        "NJA 1991 s. 277", "NJA 1991:47"]
    assert _fritext({"fritext": ""}) == []


def test_swedish_statutory_citations_arrive_already_parsed():
    edges = _lagrum_relations({"lagrumLista": [
        {"referens": "56 a § lagen (1960:729) om upphovsrätt", "sfsNummer": "1960:729"},
        {"referens": "2 kap. 3 § brottsbalken", "sfsNummer": "1962:700"},
        {"referens": "Artikel 11 i Haagprotokollet"},  # no SFS number — not Swedish law
    ]})
    assert {(e.dst_id, e.dst_anchor) for e in edges} == {
        ("se/sfs/1960/729", "56 a §"), ("se/sfs/1962/700", "2 kap. 3 §")}


def test_the_three_kinds_of_swedish_reference_each_resolve_differently():
    edges = _reference_relations({"hanvisadePubliceringarLista": [
        {"fritext": "NJA 1970 s. 274 "},
        {"fritext": "EU-domstolens avgörande W.J., C-644/20, EU:C:2022:371"},
        {"fritext": "EU-domstolens avgörande Cilfit, 283/81, ECLI:EU:C:1982:335"},
        {"fritext": "något annat", "gruppKorrelationsnummer": "446f9a2f"},
    ]})
    targets = {e.dst_id for e in edges}
    assert "se/nja/1970/274" in targets          # the report citation Sweden cites by
    assert "ECLI:EU:C:1982:335" in targets        # a CJEU ECLI printed in the free text
    assert "C-644/20" in targets                  # …and one that carries only the case no
    assert "se:grupp:446f9a2f" in targets         # another record of this same service


def test_domstolsverket_html_keeps_the_heading_level_and_the_paragraph_number():
    html = ("<h1>HÖGSTA FÖRVALTNINGSDOMSTOLENS DOM</h1>"
            "<h2>BAKGRUND</h2>"
            "<p>1.&nbsp;&nbsp;&nbsp;&nbsp;När en ny väg ska anläggas.</p>"
            "<p>2.&nbsp;&nbsp;&nbsp;&nbsp;Det är Trafikverket.</p>")
    parsed = parse_domstol_html(html)
    headings = [(s.label, s.level) for s in parsed.segments if s.kind == "heading"]
    assert headings == [("HÖGSTA FÖRVALTNINGSDOMSTOLENS DOM", 0), ("BAKGRUND", 1)]
    numbered = [s.label for s in parsed.segments if s.kind == "paragraph"]
    assert numbered == ["1.", "2."]
    # The number is padded with non-breaking spaces, which are not whitespace to
    # str.split — leaving them in meant no "1." prefix ever matched.
    assert " " not in parsed.text
    assert parsed.text.count("1.") == 0


# ── Estonia ──────────────────────────────────────────────────────────────────
def test_the_estonian_endpoint_answers_json_rpc_as_an_event_stream():
    body = ('event: message\n'
            'data: {"result": {"structuredContent": {"total": 3}}, "jsonrpc": "2.0"}\n')
    assert parse_sse(body)["result"]["structuredContent"]["total"] == 3
    # …and a plain JSON body still parses, in case the transport is ever changed.
    assert parse_sse('{"result": {}}') == {"result": {}}
    assert parse_sse("not json") is None


def test_estonian_provisions_arrive_resolved_to_an_act_and_a_loige():
    edges = _section_relations([
        {"act": "Halduskohtumenetluse seadustik", "paragrahv": "121", "lgs": ["1", "2"]},
        {"act": "RÕS", "paragrahv": "6", "lgs": []},
    ])
    assert {(e.dst_id, e.dst_anchor) for e in edges} == {
        # lahend.ee names the act sometimes by title and sometimes by abbreviation, in
        # the same list. Both have to land on the id the grammar mints.
        ("ee/seadus/hkms", "§ 121 lg 1"), ("ee/seadus/hkms", "§ 121 lg 2"),
        ("ee/seadus/ros", "§ 6")}


def test_an_estonian_judgment_joins_the_eu_corpus_without_a_grammar_pass():
    edges = _eu_relations([
        {"celex": "32016R0679", "kind": "act", "label": "IKÜM", "articles": ["6", "15"]},
        {"celex": "62020CJ0644", "kind": "case", "label": "W.J.", "articles": []},
    ])
    assert {(e.dst_id, e.dst_anchor, e.relationship_type) for e in edges} == {
        ("32016R0679", "Article 6", RelationshipType.INTERPRETS),
        ("32016R0679", "Article 15", RelationshipType.INTERPRETS),
        ("62020CJ0644", None, RelationshipType.CONSIDERS)}


def test_lahend_markdown_separates_the_service_header_from_the_court_text():
    md = """# Korrakaitse › Vanglad — 3-25-3458/5

- **Kohus:** Tartu Halduskohus Jõhvi kohtumaja
- **Kohtuasja number:** 3-25-3458/5
- **Asja liik:** Haldusasi
- **Kuulutamise aeg:** 31.01.2026

## Lahendi tekst

### RESOLUTSIOON

1. Tagastada Xi kaebus.

### KOHTU SEISUKOHT

Määruse kuupäev

31. jaanuar 2026

6. Kohus märgib esmalt.
"""
    parsed = parse_lahend_md(md)
    assert parsed.metadata["court"] == "Tartu Halduskohus Jõhvi kohtumaja"
    assert parsed.metadata["case_number"] == "3-25-3458/5"
    assert parsed.metadata["decided_at"] == "31.01.2026"
    # The bullets restate structured fields; in the body they would make a search for a
    # court name match every decision that prints one.
    assert "**Kohus:**" not in (parsed.text or "")
    assert "Tartu Halduskohus" not in (parsed.text or "")
    assert parsed.metadata["zones"] == ["RESOLUTSIOON", "KOHTU SEISUKOHT"]
    assert [s.label for s in parsed.segments if s.kind == "paragraph"] == ["1.", "6."]
    # …and an Estonian date is "31. jaanuar 2026" with a LOWERCASE month, so the day is
    # not a paragraph number. Without the capital-letter guard every ruling's own date
    # came out as "jaanuar 2026".
    assert "31. jaanuar 2026" in parsed.text


# ── the catalogue contract ───────────────────────────────────────────────────
NEW_SOURCES = [
    "at-justiz", "at-vwgh", "at-vfgh", "at-bvwg", "at-lvwg", "at-dsb", "at-gbk",
    "at-verg", "at-ris", "sk-ress", "se-domstol", "ee-lahend", *SERIES,
]


@pytest.mark.parametrize("key", NEW_SOURCES)
def test_every_new_source_is_in_the_catalogue_with_a_truthful_mode(key):
    row = next(r for r in registry.source_catalog() if r["key"] == key)
    assert row["jurisdiction"] in ("AT", "SK", "FI", "SE", "EE")
    assert row["group_label"] in ("Austria", "Slovakia", "Finland", "Sweden", "Estonia")
    assert row["kind"] in ("caselaw", "administrative", "legislation", "guidance",
                           "preparatory")
    # Every one of these has a moving feed, so every one must be pollable for new items.
    assert row["can_incremental"], key
    assert row["incremental_mode"] in ("server", "full-walk"), key
    assert row["description"] and len(row["description"]) > 120


@pytest.mark.parametrize("key", NEW_SOURCES)
def test_every_declared_option_is_accepted_by_the_constructor(key):
    """A SourceOption the constructor rejects is a form field that 500s on submit."""
    info = registry.SOURCE_INFO[key]
    kwargs = {opt.name: None for opt in info.options}
    adapter = registry.get_adapter(key, **kwargs)
    assert adapter.source == key
    assert adapter.min_interval > 0
