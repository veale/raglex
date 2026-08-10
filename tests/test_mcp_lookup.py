"""The MCP retrieval front door (`Facade.lookup`) and the gated maintenance surface.

`lookup` is the workhorse an agent calls with a citation: it resolves, returns a
token-cheap preview (or a pincited passage, or a capped full read), and folds fetching in
as a silent fallback — an unheld-but-routable citation is fetched, an unfetchable one comes
back as an external URL. The MCP server keeps this and a handful of navigation tools
first-class, and hides ~60 mutation ops behind one `maintenance` dispatcher.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade


def _facade() -> Facade:
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _doc(f: Facade, stable_id: str, text: str, title: str) -> None:
    with f._open() as (cat, _rs, ts):
        rec = Record(source="uk-caselaw", stable_id=stable_id, doc_type=DocType.JUDGMENT,
                     title=title, decision_date=date(2016, 1, 1), text=text,
                     raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


# -- lookup: held ------------------------------------------------------------

def test_lookup_by_citation_returns_preview_not_the_whole_body():
    f = _facade()
    body = "This is the opinion of the court. " * 400   # long enough to be truncated
    _doc(f, "ewhc/admin/2016/2768", body, "R (Smith) v Home Secretary")
    r = f.lookup(citation="[2016] EWHC 2768 (Admin)", autofetch=False)
    assert r["held"] is True and r["stable_id"] == "ewhc/admin/2016/2768"
    assert r["title"] == "R (Smith) v Home Secretary"
    # token discipline: a preview, NOT the full body, and no `text` key by default
    assert "text_preview" in r and "text" not in r
    assert len(r["text_preview"]) <= Facade._LOOKUP_PREVIEW_CHARS
    assert r["preview_truncated"] is True
    assert "how_to_read" in r


def test_lookup_full_returns_capped_text():
    f = _facade()
    _doc(f, "ewhc/admin/2016/2768", "short judgment body", "A v B")
    r = f.lookup(citation="ewhc/admin/2016/2768", full=True, autofetch=False)
    assert r["held"] is True
    assert r["text"] == "short judgment body"          # under the cap → whole text
    assert not r.get("text_truncated")


def test_lookup_pincite_returns_a_passage_not_the_body():
    f = _facade()
    _doc(f, "ewhc/admin/2016/2768", "[1] First para.\n[2] Second para.\n[3] Third para.",
         "A v B")
    r = f.lookup(citation="[2016] EWHC 2768 (Admin)", pincite="[2]", context=0,
                 autofetch=False)
    assert r["held"] is True and r["pincite"] == "[2]"
    assert "passage" in r and "text_preview" not in r


def test_search_within_document_bypasses_index_and_recovers_paragraph_anchor():
    f = _facade()
    body = ("1. First paragraph.\n\n2. Middle paragraph.\n\n"
            "3. The data must not be denuded of its proper context.\n\n"
            "4. Fourth paragraph.\n\n5. Fifth paragraph.")
    _doc(f, "ewhc/kb/2025/134", body, "Ashley v HMRC")
    r = f.search_within_document(
        "ewhc/kb/2025/134", '"denuded of its proper context"')
    assert r["matched"] is True and r["total"] == 1
    assert r["matches"][0]["anchor"] == "3."
    assert r["search_route"] == "complete served body (index bypassed)"
    assert r["index_coverage"]["complete"] is False


# -- lookup: not held --------------------------------------------------------

def test_lookup_unheld_returns_external_links():
    f = _facade()
    r = f.lookup(citation="[2016] EWHC 2768 (Admin)", autofetch=False)
    assert r["held"] is False
    assert r["candidate"] == "ewhc/admin/2016/2768"
    assert r["routable"] is True
    assert any("bailii.org" in l["url"] for l in r["external_links"])


def test_lookup_empty_is_handled():
    assert "error" in _facade().lookup(citation="   ")


def test_duplicate_ecli_renditions_share_one_citator_identity():
    """A GDPRhub copy and an official ECLI node are one judgment for cited-by purposes."""
    f = _facade()
    with f._open() as (cat, _rs, ts):
        ecli = "ECLI:DE:BFH:2025:U.140125.IXR25.22.0"
        for sid, source in (("gdprhub/bfh-ix-r-25-22", "gdprhub"), (ecli, "de-rii")):
            rec = Record(source=source, stable_id=sid, ecli=ecli,
                         doc_type=DocType.JUDGMENT, title="BFH IX R 25/22",
                         text="judgment", raw_bytes=sid.encode(),
                         extracted_via=ExtractedVia.STRUCTURED)
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        _doc_rec = Record(source="de-rii", stable_id="citer/1", doc_type=DocType.JUDGMENT,
                          title="Later BFH case", text="cites it", raw_bytes=b"citer",
                          extracted_via=ExtractedVia.STRUCTURED)
        _doc_rec.ensure_payload_hash()
        cat.upsert_document(_doc_rec, text_path=str(ts.put(_doc_rec.payload_hash, _doc_rec.text)))
        cat.conn.execute(
            "INSERT INTO relations (src_id,dst_id,candidate_id,raw_citation_string,"
            "resolution_status,relationship_type,extracted_via) VALUES (?,?,?,?,?,?,?)",
            ("citer/1", ecli, ecli, ecli, "resolved", "mentions", "regex"))
        cat.commit()

    summary = f.get_document("gdprhub/bfh-ix-r-25-22")
    citers = f.citing_documents("gdprhub/bfh-ix-r-25-22")
    assert summary["cited_by_count"] == citers["total"] == 1
    assert citers["results"][0]["stable_id"] == "citer/1"


# -- overview / jurisdictions ------------------------------------------------

def test_holdings_overview_shape():
    f = _facade()
    _doc(f, "ewhc/admin/2016/2768", "body", "A v B")
    ov = f.holdings_overview()
    assert "jurisdictions" in ov and "total_documents" in ov
    uk = [j for j in ov["jurisdictions"] if j["jurisdiction"] == "United Kingdom"]
    assert uk and uk[0]["held"]["cases"] >= 1
    # fetch-on-demand names the live adapters for the jurisdiction
    assert "uk-caselaw" in uk[0]["fetch_on_demand"]


# -- the MCP server surface --------------------------------------------------

def _tool_names(mcp) -> set[str]:
    loop = asyncio.new_event_loop()
    try:
        return {t.name for t in loop.run_until_complete(mcp.list_tools())}
    finally:
        loop.close()


def test_core_tools_are_first_class_and_maintenance_is_gated():
    from raglex.mcp_server import build_server

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    names = _tool_names(build_server(Config.from_env()))
    # the research surface is small and workflow-shaped
    assert {"search", "lookup", "overview", "jurisdictions", "citator",
            "related_documents", "get_provision", "citing_documents"} <= names
    assert "maintenance" in names
    # ~60 mutation ops must NOT each be a top-level tool
    for gated in ("harvest", "import_pdf_url", "create_watch", "set_settings",
                  "resolve_reference", "harvest_all_references"):
        assert gated not in names, f"{gated} leaked as a top-level tool"
    assert len(names) < 20, f"core surface too large: {sorted(names)}"


# -- provision-scoped citers: the "who cites Article 15" workflow -------------

def _legislation(f: Facade, stable_id: str, text: str, title: str, source: str) -> None:
    with f._open() as (cat, _rs, ts):
        rec = Record(source=source, stable_id=stable_id, doc_type=DocType.LEGISLATION,
                     title=title, decision_date=date(2016, 1, 1), text=text,
                     raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def _citer(f: Facade, stable_id: str, title: str, *, source: str, court, dd: date,
           dst: str, anchor: str) -> None:
    from raglex.core.models import (RelationshipType, ResolutionStatus, TypedRelation)
    with f._open() as (cat, _rs, ts):
        rec = Record(source=source, stable_id=stable_id, doc_type=DocType.JUDGMENT,
                     title=title, decision_date=dd, text=f"This cites {anchor}.",
                     raw_bytes=b"x", extracted_via=ExtractedVia.STRUCTURED, court=court)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        cat.add_relations(stable_id, [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS, raw_citation_string=anchor,
            dst_id=dst, resolution_status=ResolutionStatus.RESOLVED,
            extracted_via=ExtractedVia.STRUCTURED, dst_anchor=anchor,
            context_start=10, context_end=10 + len(anchor))])
        cat.conn.commit()


def _gdpr_corpus() -> Facade:
    f = _facade()
    _legislation(f, "32016R0679",
                 "Article 15\nRight of access.\n\nArticle 17\nRight to erasure.",
                 "General Data Protection Regulation", source="eu-legislation")
    _citer(f, "caseUK", "Case A", source="uk-caselaw", court=None,
           dd=date(2019, 1, 1), dst="32016R0679", anchor="Article 15")
    _citer(f, "decFR", "DPA decision", source="edpb-oss", court="dpa-fr",
           dd=date(2022, 1, 1), dst="32016R0679", anchor="Article 15")
    _citer(f, "caseErasure", "Case C", source="uk-caselaw", court=None,
           dd=date(2021, 1, 1), dst="32016R0679", anchor="Article 17")
    return f


def test_lookup_infers_pincite_from_the_citation_and_scopes_citers_to_that_article():
    f = _gdpr_corpus()
    r = f.lookup(citation="Article 15 GDPR", autofetch=False)
    assert r["held"] is True and r["stable_id"] == "32016R0679"
    # the pinpoint is taken from the citation string itself
    assert r["pincite"] == "Article 15" and r.get("pincite_inferred") is True
    citing = r["citing"]
    # ONLY the Article 15 citers, never the Article 17 one
    ids = {row["stable_id"] for row in citing["top"]}
    assert ids == {"caseUK", "decFR"}
    assert citing["total"] == 2
    # facets tell the agent what it can narrow to
    juris = {row["jurisdiction"] for row in citing["facets"]["jurisdiction"]}
    assert {"United Kingdom", "France"} <= juris
    assert citing["browse"]["args"] == {"target": "32016R0679", "anchor": "Article 15"}


def test_citing_documents_filters_by_iso_jurisdiction_and_sorts():
    f = _gdpr_corpus()
    # ISO code narrows to the French DPA decision only
    fr = f.citing_documents("32016R0679", anchor="Article 15", jurisdiction="fr")
    assert [x["stable_id"] for x in fr["results"]] == ["decFR"]
    assert fr["results"][0]["kind"] == "administrative"
    # kind filter: only the UK court case
    cases = f.citing_documents("32016R0679", anchor="Article 15", kind="cases")
    assert [x["stable_id"] for x in cases["results"]] == ["caseUK"]
    # newest-first ordering across both Article 15 citers
    newest = f.citing_documents("32016R0679", anchor="Article 15", sort="newest")
    assert [x["stable_id"] for x in newest["results"]] == ["decFR", "caseUK"]
    # a browsable, re-callable list with concrete navigation hints
    assert newest["how_to_browse"] and any("sort=" in h for h in newest["how_to_browse"])
    assert newest["is_floor"] is True and "minimum" in newest["count_note"]


def test_gdprhub_court_bucket_uses_the_decision_jurisdiction():
    f = _facade()
    assert f._doc_bucket("gdprhub", "court-nl") == "Netherlands"
    assert f._doc_bucket("gdprhub", "court-de") == "Germany"


def test_citing_documents_on_unheld_target_guides_to_lookup():
    f = _facade()
    r = f.citing_documents("[2099] UKSC 1")
    assert r["held"] is False and "lookup" in r["note"]


def test_citing_documents_intersects_multiple_authorities():
    f = _facade()
    with f._open() as (cat, _rs, ts):
        for sid in ("authority/A", "authority/B", "citer/both", "citer/one"):
            rec = Record(source="uk-caselaw", stable_id=sid, doc_type=DocType.JUDGMENT,
                         title=sid, text="body", raw_bytes=sid.encode(),
                         extracted_via=ExtractedVia.STRUCTURED)
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        for src, dst in (("citer/both", "authority/A"), ("citer/both", "authority/B"),
                         ("citer/one", "authority/A")):
            cat.conn.execute(
                "INSERT INTO relations (src_id,dst_id,candidate_id,raw_citation_string,"
                "resolution_status,relationship_type,extracted_via) VALUES (?,?,?,?,?,?,?)",
                (src, dst, dst, dst, "resolved", "mentions", "regex"))
        cat.commit()
    found = f.citing_documents(["authority/A", "authority/B"], mode="intersection")
    assert found["total"] == 1
    assert [r["stable_id"] for r in found["results"]] == ["citer/both"]


def test_find_is_citation_first_then_title_and_is_honest_about_semantic():
    f = _gdpr_corpus()
    # a citation query resolves straight to the authority
    byc = f.find("Article 15 GDPR")
    assert byc["citation_match"]["stable_id"] == "32016R0679"
    # a title query matches on the title, not on meaning
    byt = f.find("General Data Protection")
    assert any(x["stable_id"] == "32016R0679" for x in byt["results"])
    # honest about the title-first route and lexical body fallback
    assert "indexed document body" in byt["how_search_works"].lower()
    # a natural-language legal question finds nothing (no concept search)
    q = f.find("what are the rules on the right of access")
    assert q["results"] == [] and "nothing_found" in q


def test_find_relaxes_descriptive_party_query_without_widening_questions():
    assert Facade._relaxed_find_query(
        "Uber Ola driver Article 15 automated decision-making Amsterdam"
    ) == "Uber OR Ola OR driver OR automated OR decision OR making OR Amsterdam"
    assert Facade._relaxed_find_query("what are the rules on the right of access") is None
    assert Facade._relaxed_find_query("Uber AND Ola") is None


def test_mentions_facets_describe_the_whole_set_not_the_loaded_page():
    """The reader's mentions tray shows one PAGE of citers but chips that claim to
    summarise all of them. Counting the page's own rows made the chips describe 40
    documents under a header saying 912 — and any jurisdiction sorting below the first
    page vanished. The crossed facet therefore comes from the server, over the whole
    anchor-scoped set, and is unaffected by paging or by a filter being applied."""
    f = _gdpr_corpus()

    page = f.document_mentions("32016R0679", anchor="Article 15", limit=1)
    assert len(page["groups"]) == 1 and page["total"] == 2 and page["has_more"] is True
    crossed = {(row["jurisdiction"], row["kind"]): row["documents"]
               for row in page["facets"]["jurisdiction_kind"]}
    # both citers are counted, though only one of them is on this page
    assert crossed == {("United Kingdom", "cases"): 1,
                       ("France", "administrative"): 1}

    # …and selecting a chip narrows at the server, keeping the same whole-set facets
    fr = f.document_mentions("32016R0679", anchor="Article 15",
                             jurisdiction="fr", kind="administrative")
    assert [g["src_id"] for g in fr["groups"]] == ["decFR"]
    assert fr["total"] == 1 and fr["has_more"] is False
    assert {(row["jurisdiction"], row["kind"]): row["documents"]
            for row in fr["facets"]["jurisdiction_kind"]} == crossed


# -- lookup: two jurisdictions, one statute name -----------------------------

def _two_data_protection_acts() -> Facade:
    """The corpus as it really is: a UK Act held under its own id, and an Irish Act of
    the same name and year held ONLY as the Law Reform Commission's revised text."""
    f = _facade()
    with f._open() as (cat, _rs, ts):
        for sid, source, title in (
            ("ukpga/2018/12", "uk-legislation", "Data Protection Act 2018"),
            ("ie/2018/act/7@2026-06-25", "ie-revised",
             "Data Protection Act 2018 (revised to 2026-06-25)"),
        ):
            body = f"{title}. Section 1. This Act may be cited as {title}."
            rec = Record(source=source, stable_id=sid, doc_type=DocType.LEGISLATION,
                         title=title, text=body, raw_bytes=body.encode(),
                         extracted_via=ExtractedVia.STRUCTURED)
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, body)))
    return f


def test_a_named_jurisdiction_picks_the_right_countrys_act():
    """Ireland and the UK both have a Data Protection Act 2018, both from May 2018,
    both implementing the GDPR. The bare title belongs to the gazetteer's UK Act; a
    caller who writes the country must get that country's law."""
    f = _two_data_protection_acts()

    assert f.lookup(citation="Data Protection Act 2018",
                    autofetch=False)["stable_id"] == "ukpga/2018/12"
    for spelling in ("Data Protection Act 2018 (Ireland)",
                     "Data Protection Act 2018 (IE)",
                     "Irish Data Protection Act 2018"):
        r = f.lookup(citation=spelling, autofetch=False)
        assert r["stable_id"] == "ie/2018/act/7@2026-06-25", spelling
        assert r["jurisdiction"] == "Ireland"


def test_an_unheld_base_act_opens_the_revised_text_that_stands_for_it():
    """Ireland publishes no consolidated base text, so ``ie/2018/act/7`` — the id every
    citation of the Act resolves to — is not itself a node. It must still open the Act."""
    f = _two_data_protection_acts()
    with f._open() as (cat, _rs, _ts):
        cat.refresh_version_aliases()

    r = f.lookup(citation="ie/2018/act/7", autofetch=False)
    assert r["held"] is True and r["stable_id"] == "ie/2018/act/7@2026-06-25"


def test_a_jurisdiction_scoped_find_does_not_answer_with_another_countrys_act():
    f = _two_data_protection_acts()

    ie = f.find("Data Protection Act 2018", jurisdiction="ie")
    assert ie["citation_match"]["stable_id"] == "ie/2018/act/7@2026-06-25"
    assert all(r["stable_id"] != "ukpga/2018/12" for r in ie["results"])

    uk = f.find("Data Protection Act 2018", jurisdiction="United Kingdom")
    assert uk["citation_match"]["stable_id"] == "ukpga/2018/12"


def test_a_statute_name_search_reaches_every_jurisdictions_act_of_that_name():
    """Searching the corpus for "Data Protection Act 2018" resolved the words as a
    CITATION through the UK gazetteer and then replaced the query with that one id — so
    the Irish Act, held and titled with those exact words, could not be found at all.
    A name is not a key; only an identifier may short-circuit the text search."""
    f = _two_data_protection_acts()

    hit = f.search_corpus(query="Data Protection Act 2018", doc_type="legislation")
    assert {r["stable_id"] for r in hit["items"]} == {
        "ukpga/2018/12", "ie/2018/act/7@2026-06-25"}
    # …and the instrument the gazetteer knows by that name still ranks first
    assert hit["items"][0]["stable_id"] == "ukpga/2018/12"

    # An IDENTIFIER still matches by primary key, which is the whole point of the hop.
    exact = f.search_corpus(query="ukpga/2018/12")
    assert [r["stable_id"] for r in exact["items"]] == ["ukpga/2018/12"]


def test_a_new_consolidation_takes_the_edges_that_named_the_base_act_with_it():
    """``ie/2018/act/7`` means "the Data Protection Act 2018", so it means whichever
    consolidation is current. An edge that resolved BEFORE the alias existed went
    wherever the raw title took it — for Ireland, the UK Act of the same name."""
    from raglex.core.models import RelationshipType, ResolutionStatus, TypedRelation

    f = _two_data_protection_acts()
    with f._open() as (cat, _rs, _ts):
        _doc(f, "iehc/2024/9", "A judgment.", "A v B")
        cat.add_relations("iehc/2024/9", [TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string="the Data Protection Act 2018",
            dst_id="ukpga/2018/12",            # what the global title alias gave it
            resolution_status=ResolutionStatus.RESOLVED,
        )])
        cat.conn.execute(
            "UPDATE relations SET candidate_id = ? WHERE src_id = ?",
            ("ie/2018/act/7", "iehc/2024/9"))
        cat.commit()

        assert cat.refresh_version_aliases() == 1

        edge = cat.relations_for("iehc/2024/9")[0]
        assert edge["dst_id"] == "ie/2018/act/7@2026-06-25"
