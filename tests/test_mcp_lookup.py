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
            "related_documents", "get_provision"} <= names
    assert "maintenance" in names
    # ~60 mutation ops must NOT each be a top-level tool
    for gated in ("harvest", "import_pdf_url", "create_watch", "set_settings",
                  "resolve_reference", "harvest_all_references"):
        assert gated not in names, f"{gated} leaked as a top-level tool"
    assert len(names) < 20, f"core surface too large: {sorted(names)}"
