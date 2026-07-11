from __future__ import annotations

import base64

import pytest

from raglex.config import Config
from raglex.facade import Facade
from raglex.mcp_server import build_server


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", topic_threshold=3.0, embed_provider="local-hashing",
        embed_model=None,
    )


# -- facade: the agent augment workflow ------------------------------------
def test_facade_import_link_tag_and_read(config):
    f = Facade(config)
    # a "law section" the agent will augment
    sec = f.import_bytes(data=b"<p>Article 17 GDPR: right to erasure.</p>",
                         filename="art17.html", doc_type="legislation", title="Art 17 GDPR")
    section_id = sec["stable_id"]

    # agent posts secondary material in three ways and links it
    by_b64 = f.import_base64(
        content_base64=base64.b64encode(b"<p>Commentary on erasure.</p>").decode(),
        filename="c.html", doc_type="commentary", link_to=section_id, relationship="analyses",
    )
    note = f.add_note(text="Erasure is not absolute.", link_to=section_id)
    f.tag(doc_id=section_id, tag="gdpr")

    got = f.get_document(section_id)
    assert got["document"]["stable_id"] == section_id
    assert any(t["tag"] == "gdpr" for t in got["tags"])

    # the commentary's edge points at the section
    com = f.get_document(by_b64["stable_id"])
    assert com["relations"][0]["dst_id"] == section_id
    assert note["relationship"] == "summarises"

    # graph view from the section sees the incoming commentary/note
    g = f.graph(section_id)
    incoming = [n for n in g["neighbours"] if n["direction"] == "in"]
    assert len(incoming) >= 2


def test_facade_list_documents_for_iteration(config):
    f = Facade(config)
    for i in range(3):
        f.import_bytes(data=f"<p>section {i}</p>".encode(), filename=f"s{i}.html",
                       doc_type="legislation")
    docs = f.list_documents(doc_type="legislation")
    assert len(docs) == 3


def test_facade_document_mentions_grouped_and_ranked(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>1. The rule. 2. The exception.</p>", filename="t.html",
                            doc_type="judgment", title="Target v Authority")["stable_id"]
    a = f.import_bytes(data=b"<p>citing a</p>", filename="a.html", doc_type="judgment", title="A v B")["stable_id"]
    b = f.import_bytes(data=b"<p>citing b</p>", filename="b.html", doc_type="judgment", title="C v D")["stable_id"]
    f.link(src_id=a, dst_id=target, relationship="applies", src_anchor="12", dst_anchor="1.")
    f.link(src_id=b, dst_id=target, relationship="considers", src_anchor="4", dst_anchor="2.")

    m = f.document_mentions(target)
    assert m["total"] == 2
    assert {g["src_id"] for g in m["groups"]} == {a, b}
    # the per-paragraph roll-up keys off this document's paragraph labels
    assert set(m["by_anchor"]) == {"1.", "2."}
    assert m["by_anchor"]["1."][0]["src_id"] == a
    # filtering to one paragraph narrows the groups
    only1 = f.document_mentions(target, anchor="1.")
    assert {g["src_id"] for g in only1["groups"]} == {a}


def test_facade_list_documents_query_is_case_insensitive(config):
    f = Facade(config)
    f.import_bytes(data=b"<p>right to erasure</p>", filename="e.html",
                   doc_type="legislation", title="Data Protection Act 2018")
    # the query filter must match regardless of case (Postgres LIKE is case-sensitive)
    assert len(f.list_documents(query="data protection")) == 1
    assert len(f.list_documents(query="DATA PROTECTION")) == 1
    assert len(f.list_documents(query="Data Protection")) == 1


def test_facade_embed_and_search(config):
    f = Facade(config)
    f.import_bytes(data=b"<p>The right to erasure of personal data under the GDPR.</p>",
                   filename="a.html", doc_type="commentary")
    f.import_bytes(data=b"<p>Merger control and competition remedies.</p>",
                   filename="b.html", doc_type="commentary")
    f.embed()
    hits = f.search("right to erasure of personal data", k=2)
    assert hits and "erasure" in hits[0]["chunk_text"].lower()


# -- MCP server: same operations as tools ----------------------------------
def test_mcp_exposes_full_toolset(config):
    server = build_server(config)
    import asyncio
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    # read + write + ops parity with the API
    for expected in {
        "search", "list_documents", "get_document", "graph_neighbours", "corpus_stats",
        "dashboard", "harvest_worklist", "import_pdf_url", "import_pdf_base64", "add_note",
        "attach_file_base64", "link_documents", "tag_document", "import_zotero",
        "embed_pending", "resolve_citations",
    }:
        assert expected in names, f"missing MCP tool {expected}"


def test_mcp_tool_call_roundtrips(config):
    """A tool call writes through the same Facade the API uses."""
    import asyncio

    server = build_server(config)
    b64 = base64.b64encode(b"<p>An imported note about Article 22.</p>").decode()
    result = asyncio.run(server.call_tool(
        "import_pdf_base64", {"content_base64": b64, "filename": "n.html", "doc_type": "commentary"}
    ))
    # FastMCP returns (content, structured) — the structured payload carries our dict
    structured = result[1] if isinstance(result, tuple) else result
    assert "stable_id" in str(structured)

    stats = asyncio.run(server.call_tool("corpus_stats", {}))
    assert "total" in str(stats)


# -- affecting-side change propagation (§0) ---------------------------------
def test_propagate_changes_flags_held_affected_acts(config, monkeypatch):
    """A new amending act pushes its changes to the held acts it affects: an unapplied
    change flags that act for re-pull; a not-held act is skipped; applied changes still
    become amends edges."""
    from datetime import date
    import raglex.adapters.uk_legislation as ukl
    from raglex.adapters.leg_effects import ChangeEffect
    from raglex.core.models import DocType, ExtractedVia, Record

    f = Facade(config)
    # we already HOLD the FOI Act; we do NOT hold the 1953 Act
    with f._open() as (cat, _rs, _ts):
        rec = Record(source="uk-legislation", stable_id="ukpga/2000/36",
                     doc_type=DocType.LEGISLATION, title="Freedom of Information Act 2000",
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec)

    monkeypatch.setattr(ukl.UKLegislationAdapter, "changes_affecting",
        lambda self, base, **kw: [
            ChangeEffect("ukpga/2000/36", "ukpga/2018/12", "s. 5 inserted", False, "s. 5", None, "FOIA"),
            ChangeEffect("ukpga/Eliz2/1-2/37", "ukpga/2018/12", "words substituted", True, "s. 19", None, "RSA 1953"),
        ])

    res = f.propagate_changes_from(stable_id="ukpga/2018/12")
    assert res["affected_held"] == 1 and res["edges"] == 1      # only the held FOI Act
    assert res["flagged_for_repull"] == 1                        # its change is unapplied

    # the held, affected act is now queued due-now for re-pull
    with f._open() as (cat, _rs, _ts):
        due = [r["stable_id"] for r in cat.due_effects_refresh(limit=10)]
    assert "ukpga/2000/36" in due
    # and the amending act now "describes what it changes" via the amends edge
    changes = f.effects_caused_by(stable_id="ukpga/2018/12")
    assert any(c["affected_id"] == "ukpga/2000/36" for c in changes)


def test_detect_citations_from_pasted_text(config):
    f = Facade(config)
    text = ("In Case C-311/18, ECLI:EU:C:2020:559 the Court considered Article 46 GDPR. "
            "See [2021] UKSC 12 and the Data Protection Act 2018.")
    r = f.detect_citations(text=text)
    cands = {c["candidate"]: c for c in r["citations"]}
    assert "32016R0679" in cands and cands["32016R0679"]["adapter"] == "eu-legislation"
    assert "uksc/2021/12" in cands and cands["uksc/2021/12"]["adapter"] == "uk-caselaw"
    assert "ukpga/2018/12" in cands  # the Act, by name
    assert "ECLI:EU:C:2020:559" in cands
    assert all("in_corpus" in c for c in r["citations"])
