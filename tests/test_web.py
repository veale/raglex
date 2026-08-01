from __future__ import annotations

from datetime import date
import time

import pytest
from fastapi.testclient import TestClient

from raglex.config import Config
from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.embeddings import EmbedStage, HashingEmbeddingProvider
from raglex.resolve import Resolver
from raglex.storage import Catalogue, TextStore
from raglex.web import create_app


@pytest.fixture
def client(tmp_path):
    cat_path = tmp_path / "catalogue.sqlite"
    text_dir = tmp_path / "text"
    cat = Catalogue(cat_path)
    ts = TextStore(text_dir)

    def store(stable_id, text, *, court="Court of Justice", rels=None):
        rec = Record(
            source="eu-cellar", stable_id=stable_id, ecli=stable_id,
            doc_type=DocType.JUDGMENT, title=stable_id, court=court,
            decision_date=date(2024, 1, 1), language="en", source_language="en",
            text=text, raw_bytes=text.encode(), relations=rels or [],
            extracted_via=ExtractedVia.STRUCTURED,
        )
        rec.ensure_payload_hash()
        catalogue_path = str(ts.put(rec.payload_hash, text))
        cat.upsert_document(rec, text_path=catalogue_path)

    rel = TypedRelation(
        relationship_type=RelationshipType.APPLIES, raw_citation_string="ECLI:EU:C:2020:1",
        dst_id="ECLI:EU:C:2020:1", resolution_status=ResolutionStatus.PENDING,
    )
    store("ECLI:EU:C:2020:1", "The right to erasure of personal data under the GDPR.")
    store("ECLI:EU:C:2020:2", "Schrems II applies the data protection authority decision.", rels=[rel])
    Resolver(cat).run()
    EmbedStage(cat, HashingEmbeddingProvider(dimensions=512)).run()
    cat.close()

    config = Config(
        data_dir=tmp_path, catalogue_path=cat_path, raw_dir=tmp_path / "raw",
        text_dir=text_dir, settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )
    return TestClient(create_app(config))


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_stats_endpoint(client):
    data = client.get("/stats").json()
    assert data["total"] == 2
    assert data["by_doc_type"]["judgment"] == 2
    assert data["resolution"]["resolved"] == 1


def test_queues_endpoint(client):
    data = client.get("/queues").json()
    assert data["text_not_embedded"] == 0  # everything embedded in the fixture


def test_search_endpoint(client):
    hits = client.get("/search", params={"q": "right to erasure of personal data", "k": 3}).json()
    assert hits
    assert hits[0]["doc_id"] in {"ECLI:EU:C:2020:1", "ECLI:EU:C:2020:2"}
    assert "chunk_text" in hits[0]


def test_graph_endpoint_returns_typed_neighbourhood(client):
    data = client.get("/graph/ECLI:EU:C:2020:2").json()
    assert data["focus"] == "ECLI:EU:C:2020:2"
    out = [n for n in data["neighbours"] if n["direction"] == "out"]
    assert any(n["id"] == "ECLI:EU:C:2020:1" and n["relationship_type"] == "applies" for n in out)


def test_link_at_selection_anchors_and_renders(client):
    # highlight "data protection authority" in doc 2 and link it to doc 1
    r = client.post("/link-at-selection", json={
        "doc_id": "ECLI:EU:C:2020:2", "target_id": "ECLI:EU:C:2020:1",
        "selected_text": "data protection authority"}).json()
    assert "error" not in r, r
    assert r["char_start"] < r["char_end"]
    assert r["target_present"] is True
    # the reader body now carries an inline manual citation at that span, resolved to doc 1
    body = client.get("/document-body?id=ECLI:EU:C:2020:2").json()
    manual = [c for c in body["citations"] if c["method"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["candidate_id"] == "ECLI:EU:C:2020:1"
    assert manual[0]["resolved_id"] == "ECLI:EU:C:2020:1"
    assert body["text"][manual[0]["char_start"]:manual[0]["char_end"]] == "data protection authority"


def test_keep_current_overview_endpoint(client):
    data = client.get("/sources/keep-current").json()
    assert "overlap_default_days" in data
    modes = {s["key"]: s["incremental_mode"] for s in data["sources"]}
    assert modes.get("us-caselaw") == "server"
    assert modes.get("echr") == "targeted"
    assert modes.get("uk-caselaw") == "early-stop"


def test_document_endpoint(client):
    data = client.get("/documents/ECLI:EU:C:2020:1").json()
    assert data["document"]["stable_id"] == "ECLI:EU:C:2020:1"
    assert "relations" in data and "tags" in data and "versions" in data


def test_document_body_endpoint(client):
    body = client.get("/document-body", params={"id": "ECLI:EU:C:2020:1"}).json()
    assert "erasure" in (body["text"] or "")
    assert "segments" in body and body["doc_type"] == "judgment"
    assert body["oscola"]["text"]  # every body carries an OSCOLA citation


def test_static_document_export_endpoint(client):
    response = client.post(
        "/export/static-law", json={"id": "ECLI:EU:C:2020:1"})
    assert response.status_code == 200
    job_id = response.json().get("job_id")
    if job_id:
        for _ in range(100):
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.02)
        assert job["status"] == "done", job

    response = client.get(
        "/export/static-law.html", params={"id": "ECLI:EU:C:2020:1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-raglex-export-documents"] == "1"
    assert b"Document generated from a dataset held and maintained by" in response.content


def test_document_endpoint_carries_oscola_and_counts(client):
    data = client.get("/documents/ECLI:EU:C:2020:1").json()
    assert data["oscola"]["text"]
    assert "cases_cited_count" in data and "statute_cited_count" in data
    # the citing document's incoming row is OSCOLA-formatted too
    assert data["cited_by_count"] == 1
    assert data["incoming"][0]["src_oscola"]["text"]


def test_search_corpus_tokenised_query_and_facets(client):
    # tokenised AND: both words must hit the title/id, in any order → only …:2020:1
    r = client.get("/search-corpus", params={"query": "2020 1"}).json()
    ids = [it["stable_id"] for it in r["items"]]
    assert ids == ["ECLI:EU:C:2020:1"]
    # facets describe the WHOLE match set
    full = client.get("/search-corpus", params={"query": "2020"}).json()
    assert full["total"] == 2
    assert {f["key"]: f["n"] for f in full["facets"]["source"]}["eu-cellar"] == 2
    assert full["facets"]["year"]["2024"] == 2
    # results carry an OSCOLA citation
    assert full["items"][0]["oscola"]["text"]


def test_search_corpus_graph_filters(client):
    # cites: documents that cite …:2020:1  →  …:2020:2
    r = client.get("/search-corpus", params={"cites": "ECLI:EU:C:2020:1"}).json()
    assert [it["stable_id"] for it in r["items"]] == ["ECLI:EU:C:2020:2"]
    # cited_by: documents cited BY …:2020:2  →  …:2020:1
    r2 = client.get("/search-corpus", params={"cited_by": "ECLI:EU:C:2020:2"}).json()
    assert [it["stable_id"] for it in r2["items"]] == ["ECLI:EU:C:2020:1"]


def test_facet_values_endpoint(client):
    fv = client.get("/facet-values").json()
    assert any(s["key"] == "eu-cellar" for s in fv["sources"])
    assert any(d["key"] == "judgment" for d in fv["doc_types"])


def test_mentions_endpoint(client):
    data = client.get("/mentions", params={"id": "ECLI:EU:C:2020:1"}).json()
    assert data["target"] == "ECLI:EU:C:2020:1"
    assert data["total"] == 1
    assert data["groups"][0]["src_id"] == "ECLI:EU:C:2020:2"


def test_citations_out_endpoint(client):
    # sources the *extracted*-citation family split (populated by extraction in real flows);
    # here we assert the endpoint's shape for both families.
    data = client.get("/citations-out", params={"id": "ECLI:EU:C:2020:2", "family": "cases"}).json()
    assert data["family"] == "cases" and isinstance(data["items"], list) and "total" in data
    stat = client.get("/citations-out", params={"id": "ECLI:EU:C:2020:2", "family": "statute"}).json()
    assert stat["family"] == "statute" and isinstance(stat["items"], list)


def test_sources_and_alerts_endpoints(client):
    assert isinstance(client.get("/sources").json(), list)
    assert isinstance(client.get("/alerts").json(), list)


def test_import_note_and_link_via_api(client):
    r = client.post("/import/note", json={"text": "A summary.", "link_to": "ECLI:EU:C:2020:1"})
    note_id = r.json()["stable_id"]
    doc = client.get(f"/documents/{note_id}").json()
    assert doc["document"]["added_by"] == "user"
    assert doc["relations"][0]["dst_id"] == "ECLI:EU:C:2020:1"


def test_import_file_upload_via_api(client):
    files = {"file": ("note.html", b"<p>Imported commentary on erasure.</p>", "text/html")}
    r = client.post("/import/file", files=files, data={"doc_type": "commentary"})
    assert r.json()["chars"] > 0


def test_sources_list_and_embedding_health(client):
    srcs = client.get("/sources/list").json()
    assert "eu-cellar" in srcs and "uk-grc" in srcs and "uk-ico" in srcs  # incl. scrape recipe
    health = client.get("/health/embedding").json()
    assert health["provider"] == "local-hashing" and health["healthy"] is True


def test_harvest_unknown_source_returns_error(client):
    r = client.post("/harvest", json={"source": "does-not-exist"}).json()
    assert "error" in r  # endpoint wired; bad source handled without crashing


def test_backfill_source_job_requires_a_source_and_queues_uncapped(client):
    # no source → refused, not a crash
    assert "error" in client.post("/jobs/harvest-source", json={}).json()
    # A full backfill queues as a background job with NO page cap. Uses an unknown
    # source deliberately: the job still queues (proving the wiring + label), and the
    # unknown-source error surfaces inside the job instead of starting a real crawl
    # against a live register from the test suite.
    r = client.post("/jobs/harvest-source",
                    json={"source": "no-such-source", "max_pages": None}).json()
    assert "error" not in r
    jobs = {j["kind"]: j for j in client.get("/jobs").json()}
    assert "harvest-source" in jobs
    assert "everything" in jobs["harvest-source"]["label"]


def test_eu_digital_scope_rescans_every_jurisdiction(client):
    """``scope=eu-digital`` is the acquis citer worklist with no national filter — what a
    change to the citation grammars or the shorthand rules needs, since those apply
    corpus-wide rather than to one country's reports."""
    from raglex.facade import EU_DIGITAL_ACQUIS_IDS

    # the reviewed list spans the acquis and keeps the Data Protection Directive, which
    # predates it by 15 years but is still what the older case law cites
    assert "31995L0046" in EU_DIGITAL_ACQUIS_IDS
    assert {"32022R2065", "32016R0679", "32022R1925"} <= set(EU_DIGITAL_ACQUIS_IDS)

    r = client.post("/jobs/rescan-citations", json={"scope": "eu-digital"}).json()
    assert "error" not in r and r.get("job_id")
    for _ in range(200):
        job = client.get(f"/jobs/{r['job_id']}").json()
        if job["status"] in {"done", "error", "cancelled"}:
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    assert "all jurisdictions" in job["label"]


def test_settings_endpoint_masks_and_persists(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client.post("/settings", json={"OPENROUTER_API_KEY": "sk-abc-9999", "ZOTERO_LIBRARY_ID": "7"})
    rows = {s["key"]: s for s in client.get("/settings").json()["settings"]}
    assert rows["OPENROUTER_API_KEY"]["display"] == "••••9999"  # masked
    assert rows["ZOTERO_LIBRARY_ID"]["display"] == "7"


def test_system_storage_reports_database_size(client):
    r = client.get("/system/storage")
    assert r.status_code == 200
    body = r.json()
    assert body["database_bytes"] > 0
    assert isinstance(body["tables"], list)


# -- standalone import: the form's vocabularies, and a multi-file drop -------
def test_import_options_offers_live_vocabularies(client):
    """The dropdowns are built from the corpus, so they can never offer a value the
    rest of the app would not recognise."""
    data = client.get("/import/options").json()
    codes = [j["code"] for j in data["jurisdictions"]]
    assert "uk" in codes and "eu" in codes
    assert {"code": "uk", "label": "United Kingdom", "source": "uk-user-import",
            "documents": 0} in data["jurisdictions"]
    assert "judgment" in data["doc_types"] and "commentary" in data["doc_types"]
    assert [s["value"] for s in data["structures"]][0] == "auto"
    assert "en" in data["languages"]                      # the fixture's own documents
    # courts the corpus actually holds, under the names the reader already sees
    eu = data["courts_by_jurisdiction"].get("European Union") or []
    assert any(c["court"] == "Court of Justice" for c in eu)


def test_import_files_labels_each_item_in_the_drop(client):
    r = client.post(
        "/import/files",
        files=[("files", ("one.html", b"<p>The first note.</p>", "text/html")),
               ("files", ("two.html", b"<p>The second note.</p>", "text/html"))],
        data={"items": (
            '[{"title": "First paper", "doc_type": "article", "jurisdiction": "uk",'
            ' "court": "UKSC", "decision_date": "2024-03-01", "tags": ["seminar"]},'
            ' {"title": "Second paper", "doc_type": "commentary", "jurisdiction": "ie"}]')},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["imported"] == 2 and body["failed"] == 0
    first, second = body["documents"]
    assert (first["title"], first["doc_type"], first["source"]) == (
        "First paper", "article", "uk-user-import")
    assert first["jurisdiction"] == "uk" and first["tags"] == ["seminar"]
    assert (second["title"], second["source"]) == ("Second paper", "ie-user-import")
    # each row keeps its own identity in the corpus
    held = client.get(f"/documents/{first['stable_id']}").json()["document"]
    assert (held["court"], held["source"]) == ("UKSC", "uk-user-import")
    assert str(held["decision_date"])[:10] == "2024-03-01"


def test_import_files_reports_a_bad_row_without_losing_the_good_ones(client):
    """A drop of twenty PDFs must not be lost to one of them."""
    r = client.post(
        "/import/files",
        files=[("files", ("ok.html", b"<p>Fine.</p>", "text/html"))],
        data={"items": '[{"title": "Fine", "doc_type": "not-a-type"}]'},
    )
    body = r.json()
    # an unknown doc type falls back rather than failing the row
    assert body["imported"] == 1 and body["documents"][0]["doc_type"] == "commentary"


def test_single_file_import_still_takes_the_new_metadata(client):
    r = client.post(
        "/import/file",
        files={"file": ("j.html", b"<p>A judgment about things.</p>", "text/html")},
        data={"doc_type": "judgment", "title": "Smith v Jones", "jurisdiction": "us",
              "citation": "410 U.S. 113", "tags": "seminar,us"},
    )
    body = r.json()
    assert body["source"] == "us-user-import" and body["jurisdiction"] == "us"
    assert sorted(body["tags"]) == ["seminar", "us"]
    assert body["citation"] == "410 U.S. 113"


def test_metadata_edit_is_visible_immediately(client):
    """The document view is cached; a correction that isn't reflected on the next read
    looks exactly like a save that was ignored (which is how this was found)."""
    client.get("/documents/ECLI:EU:C:2020:1")           # prime the 120s cache
    r = client.post("/documents/ECLI:EU:C:2020:1/update",
                    json={"title": "Corrected Title"})
    assert r.json()["updated"] is True
    assert client.get("/documents/ECLI:EU:C:2020:1").json()["document"]["title"] == "Corrected Title"
