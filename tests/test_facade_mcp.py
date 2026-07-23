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
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
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


def test_preparatory_mentions_are_a_separate_conditional_section_and_flag(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>Act</p>", filename="act.html",
                            doc_type="legislation", title="Final Act")["stable_id"]
    prep = f.import_bytes(data=b"<p>Impact assessment</p>", filename="ia.html",
                          doc_type="preparatory", title="Impact assessment")["stable_id"]
    f.link(src_id=prep, dst_id=target, relationship="adopted_as")
    mentions = f.document_mentions(target)
    assert mentions["groups"] == []
    assert mentions["preparatory_count"] == 1
    assert mentions["preparatory_groups"][0]["src_id"] == prep
    assert mentions["preparatory_note"].startswith("Preparatory documents exist")
    flag = f.get_document(target)["preparatory_documents"]
    assert flag == {"available": True, "count": 1,
                    "message": "Preparatory documents exist for this item — 1 available.",
                    "retrieve_with": "document_mentions"}


def test_mentions_of_parent_article_include_subarticles(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>Article 22 Automated decisions</p>", filename="gdpr.html",
                            doc_type="legislation", title="GDPR")["stable_id"]
    srcs = []
    for i, anchor in enumerate(("Article 22", "Article 22(1)", "Article 22(3)")):
        src = f.import_bytes(data=f"<p>cite {i}</p>".encode(), filename=f"c{i}.html",
                             doc_type="judgment", title=f"C{i} v D")["stable_id"]
        f.link(src_id=src, dst_id=target, relationship="mentions", dst_anchor=anchor)
        srcs.append(src)
    got = f.document_mentions(target, anchor="Article 22")
    assert {g["src_id"] for g in got["groups"]} == set(srcs)


def test_mentions_accept_the_full_segment_label_as_anchor(config):
    """The reader's "See all mentions" sends the SEGMENT LABEL — "Article 17 Right to
    erasure (right to be forgotten)" — while edges pin to the bare unit ("Article 17",
    "Article 17(2)"). The tray used to answer "Nothing mentions this yet" for a
    provision cited thousands of times; the canonical-anchor-key fallback fixes it
    without loosening the match (Article 170 and Recital 17 stay distinct)."""
    f = Facade(config)
    target = f.import_bytes(data=b"<p>Article 17 Right to erasure</p>", filename="gdpr17.html",
                            doc_type="legislation", title="GDPR")["stable_id"]
    srcs = []
    for i, anchor in enumerate(("Article 17", "Article 17(2)")):
        src = f.import_bytes(data=f"<p>cite {i}</p>".encode(), filename=f"e{i}.html",
                             doc_type="judgment", title=f"E{i} v F")["stable_id"]
        f.link(src_id=src, dst_id=target, relationship="mentions", dst_anchor=anchor)
        srcs.append(src)
    # near-misses that must NOT be swept in by the fallback
    for anchor, name in (("Article 170", "far"), ("Recital 17", "rec")):
        src = f.import_bytes(data=f"<p>{name}</p>".encode(), filename=f"{name}.html",
                             doc_type="judgment", title=f"{name} v X")["stable_id"]
        f.link(src_id=src, dst_id=target, relationship="mentions", dst_anchor=anchor)

    got = f.document_mentions(
        target, anchor="Article 17 Right to erasure ( right to be forgotten )")
    assert {g["src_id"] for g in got["groups"]} == set(srcs)


def test_corpus_map_cites_translates_country_category_to_storage_source(config, monkeypatch):
    from contextlib import contextmanager

    f = Facade(config)
    seen = []

    class Cat:
        def document_subtype_counts(self):
            return [{"source": "fr-dila", "doc_type": "judgment",
                     "court": "Cour de cassation", "prefix": "ECLI:FR:CCASS", "n": 1}]

        def outgoing_citation_targets_for(self, pairs):
            seen.extend(pairs)
            return [{"dst_id": "32016R0679", "raw": "règlement (UE) 2016/679"}]

    @contextmanager
    def opened():
        yield Cat(), None, None

    monkeypatch.setattr(f, "_open", opened)
    got = f._corpus_map_cites_uncached("fr-caselaw")
    assert seen == [("fr-dila", "judgment")]
    assert got["targets"][0]["category"] == "eu-legislation"


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


# -- MCP server: lean retrieval surface, mutations gated -------------------
def test_mcp_core_tools_first_class_mutations_gated(config):
    """The research surface is first-class and small; the ~60 mutation/admin ops are NOT
    each a top-level tool — they live behind the one ``maintenance`` dispatcher, whose
    ``help`` still lists them all (so nothing is lost, only removed from the hot context)."""
    import asyncio

    server = build_server(config)
    names = {t.name for t in asyncio.run(server.list_tools())}
    for core in {"search", "lookup", "overview", "jurisdictions", "list_documents",
                 "get_document", "graph_neighbours", "citator", "related_documents"}:
        assert core in names, f"missing core tool {core}"
    assert "maintenance" in names
    assert len(names) < 20, f"core surface unexpectedly large: {sorted(names)}"
    # the mutation ops are gated, not top-level…
    for gated in {"import_pdf_url", "import_pdf_base64", "add_note", "harvest_worklist",
                  "import_zotero", "embed_pending", "resolve_citations", "corpus_stats"}:
        assert gated not in names, f"{gated} leaked as a top-level tool"
    # …but every one is still reachable and documented via maintenance('help')
    import json
    help_res = asyncio.run(server.call_tool("maintenance", {"op": "help"}))
    if isinstance(help_res, tuple):
        help_res = help_res[1] if isinstance(help_res[1], dict) else help_res[0]
    if isinstance(help_res, list):
        help_res = json.loads(help_res[0].text)
    ops = help_res["ops"]
    for gated in {"import_pdf_base64", "add_note", "corpus_stats", "resolve_citations"}:
        assert gated in ops


def test_mcp_tool_call_roundtrips_through_maintenance(config):
    """A gated op dispatched through ``maintenance`` writes through the same Facade."""
    import asyncio

    server = build_server(config)
    b64 = base64.b64encode(b"<p>An imported note about Article 22.</p>").decode()
    result = asyncio.run(server.call_tool("maintenance", {
        "op": "import_pdf_base64",
        "args": {"content_base64": b64, "filename": "n.html", "doc_type": "commentary"}}))
    structured = result[1] if isinstance(result, tuple) else result
    assert "stable_id" in str(structured)

    stats = asyncio.run(server.call_tool("maintenance", {"op": "corpus_stats"}))
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


# -- outbound link labels ---------------------------------------------------
# An external link is labelled by where it POINTS, not by the adapter that
# ingested the document. The two diverge for most of the corpus: 272k judgments
# carry source "uk-caselaw" (the Find Case Law adapter) but a landing_url on
# bailii.org, because FCL holds no copy and the adapter fell back to BAILII.
def test_link_label_follows_the_host_not_the_source(config):
    f = Facade(config)

    # the big one: a Find Case Law-sourced doc whose URL is really BAILII
    assert f.link_label("https://www.bailii.org/ew/cases/EWCA/Civ/2002/1642.html",
                        "uk-caselaw") == "BAILII"
    # ...and a genuine TNA scrape keeps the National Archives label
    assert f.link_label("https://caselaw.nationalarchives.gov.uk/ewca/civ/2002/1642",
                        "uk-caselaw") == "National Archives"


def test_link_label_names_each_lii_and_tolerates_subdomains(config):
    f = Facade(config)

    assert f.link_label("http://www.austlii.edu.au/au/cases/cth/HCA/2019/1.html") == "AustLII"
    assert f.link_label("https://www.canlii.org/en/ca/scc/doc/2019/2019scc1/", "ca-caselaw") == "CanLII"
    # other adapters that fell back to BAILII are labelled BAILII too
    assert f.link_label("https://www.bailii.org/eu/cases/EUECJ/1994/C35992.html", "eu-cellar") == "BAILII"
    assert f.link_label("https://www.bailii.org/ie/cases/IEHC/2019/H1.html", "ie-caselaw") == "BAILII"


def test_link_label_falls_back_to_source_without_a_url(config):
    f = Facade(config)

    assert f.link_label(None, "uk-legislation") == "legislation.gov.uk"
    assert f.link_label("", "edpb") == "EDPB"
    # an unmapped host is reported honestly as itself, never guessed
    assert f.link_label("https://example.gov/x", "uk-caselaw") == "example.gov"


def test_mention_snippets_anchor_the_citing_document_not_the_cited(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>1. The rule. 2. The exception.</p>", filename="t2.html",
                            doc_type="judgment", title="Target v Authority")["stable_id"]
    a = f.import_bytes(data=b"<p>citing a</p>", filename="a2.html",
                       doc_type="judgment", title="A v B")["stable_id"]
    f.link(src_id=a, dst_id=target, relationship="applies", src_anchor="12", dst_anchor="1.")

    # the user reached this tray by clicking paragraph "1." of the TARGET, so
    # labelling the snippet "1." tells them only what they already know — the
    # useful anchor is where the passage sits in the CITING judgment ("12")
    g = f.document_mentions(target, anchor="1.")["groups"][0]
    for s in g["snippets"]:
        assert s["anchor"] != "1."
        assert s["anchor"] in (None, "12")


def test_mention_groups_name_the_citing_court_and_jurisdiction(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>1. The rule.</p>", filename="t3.html",
                            doc_type="judgment", title="Target")["stable_id"]
    a = f.import_bytes(data=b"<p>citing</p>", filename="a3.html",
                       doc_type="judgment", title="A v B")["stable_id"]
    f.link(src_id=a, dst_id=target, relationship="applies")

    g = f.document_mentions(target)["groups"][0]
    # the tray renders names, never raw slugs — both keys must be present even
    # when the court is unknown, so the UI never falls back to showing "ewca"
    assert "src_court_label" in g and "src_jurisdiction" in g


def test_mention_sort_modes_are_offered_and_validated(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>1. The rule.</p>", filename="t4.html",
                            doc_type="judgment", title="Target")["stable_id"]
    old = f.import_bytes(data=b"<p>old</p>", filename="o.html", doc_type="judgment",
                         title="Old v Case")["stable_id"]
    new = f.import_bytes(data=b"<p>new</p>", filename="n.html", doc_type="judgment",
                         title="New v Case")["stable_id"]
    # decision_date is deliberately outside update_document_fields' allowlist
    # (it isn't user-correctable metadata), so set it directly for the fixture
    with f._open() as (cat, _rs, _ts):
        for sid, when in ((old, "1990-01-01"), (new, "2020-01-01")):
            cat.conn.execute("UPDATE documents SET decision_date = ? WHERE stable_id = ?",
                             (when, sid))
        cat.conn.commit()
    f.link(src_id=old, dst_id=target, relationship="applies")
    f.link(src_id=new, dst_id=target, relationship="applies")

    m = f.document_mentions(target)
    assert m["sort"] == "pagerank"                 # authority, not raw popularity
    assert set(m["sorts"]) >= {"pagerank", "cited", "newest", "oldest", "passages"}

    assert [g["src_id"] for g in f.document_mentions(target, sort="newest")["groups"]] == [new, old]
    assert [g["src_id"] for g in f.document_mentions(target, sort="oldest")["groups"]] == [old, new]
    # an unrecognised sort falls back to the default rather than erroring
    assert f.document_mentions(target, sort="nonsense")["sort"] == "pagerank"


def test_mention_snippets_mark_the_citation_that_made_the_edge(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>1. The rule.</p>", filename="t5.html",
                            doc_type="legislation", title="Arbitration Act 1996")["stable_id"]
    citing = f.import_bytes(
        data=b"<p>The tribunal considered whether the Arbitration Act s 7 applied "
             b"to the dispute, and concluded that it did not.</p>",
        filename="c5.html", doc_type="judgment", title="A v B")["stable_id"]

    with f._open() as (cat, _rs, ts):
        text = ts.get(cat.get_document(citing)["payload_hash"])
    lo = text.index("Arbitration Act s 7")
    hi = lo + len("Arbitration Act s 7")
    f.link(src_id=citing, dst_id=target, relationship="mentions")
    with f._open() as (cat, _rs, _ts):
        cat.conn.execute(
            "UPDATE relations SET context_start = ?, context_end = ?, "
            "raw_citation_string = ? WHERE src_id = ? AND dst_id = ?",
            (lo, hi, "Arbitration Act s 7", citing, target))
        cat.conn.commit()

    snip = f.document_mentions(target)["groups"][0]["snippets"][0]
    ms, me = snip["mark"]
    # the marked span must land exactly on the citation, even though the snippet
    # was windowed out of the middle of the text and then stripped
    assert snip["text"][ms:me] == "Arbitration Act s 7"
    assert snip["raw"] == "Arbitration Act s 7"


def test_incoming_edges_carry_jurisdiction_and_kind_for_faceting(config):
    f = Facade(config)
    target = f.import_bytes(data=b"<p>1. The rule.</p>", filename="t6.html",
                            doc_type="legislation", title="Target Act")["stable_id"]
    citer = f.import_bytes(data=b"<p>citing</p>", filename="c6.html",
                           doc_type="judgment", title="A v B")["stable_id"]
    f.link(src_id=citer, dst_id=target, relationship="mentions")

    inc = f.get_document(target)["incoming"]
    assert inc, "expected the citing judgment on the incoming edge list"
    # the cited-by panel slices on jurisdiction × kind ("UK cases 7"), so both
    # must ride along on every incoming row
    row = inc[0]
    assert row["src_kind"] == "cases"
    assert row["src_jurisdiction"]
