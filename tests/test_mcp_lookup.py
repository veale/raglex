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


def test_citing_documents_on_unheld_target_guides_to_lookup():
    f = _facade()
    r = f.citing_documents("[2099] UKSC 1")
    assert r["held"] is False and "lookup" in r["note"]


def test_find_is_citation_first_then_title_and_is_honest_about_semantic():
    f = _gdpr_corpus()
    # a citation query resolves straight to the authority
    byc = f.find("Article 15 GDPR")
    assert byc["citation_match"]["stable_id"] == "32016R0679"
    # a title query matches on the title, not on meaning
    byt = f.find("General Data Protection")
    assert any(x["stable_id"] == "32016R0679" for x in byt["results"])
    # honest about what search does: titles/citations, not a concept search
    assert "not by a legal question" in byt["how_search_works"].lower()
    # a natural-language legal question finds nothing (no concept search)
    q = f.find("what are the rules on the right of access")
    assert q["results"] == [] and "nothing_found" in q
