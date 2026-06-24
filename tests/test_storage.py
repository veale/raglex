from __future__ import annotations

from raglex.core.models import UpstreamStatus
from tests.conftest import make_record


def test_rawstore_roundtrip_and_dedup(rawstore):
    h1 = rawstore.put(b"abc", ext="xml")
    h2 = rawstore.put(b"abc", ext="xml")  # identical bytes -> same hash, no dup
    assert h1 == h2
    assert rawstore.exists(h1, "xml")
    assert rawstore.get(h1, "xml") == b"abc"


def test_catalogue_upsert_and_relations(catalogue):
    rec = make_record()
    catalogue.upsert_document(rec, raw_path="/tmp/x.xml")
    row = catalogue.get_document(rec.stable_id)
    assert row is not None
    assert row["title"] == "Doe v Data Controller"
    assert row["upstream_status"] == "live"

    rels = catalogue.relations_for(rec.stable_id)
    assert len(rels) == 1
    assert rels[0]["raw_citation_string"] == "Case C-311/18 (Schrems II)"
    assert rels[0]["resolution_status"] == "pending"  # dangling edge until §5b resolves


def test_payload_hash_dedup(catalogue):
    rec = make_record()
    catalogue.upsert_document(rec)
    assert catalogue.payload_hash_seen(rec.payload_hash) is True
    assert catalogue.payload_hash_seen("deadbeef") is False


def test_version_bumps_on_changed_payload(catalogue):
    rec = make_record()
    catalogue.upsert_document(rec)
    assert catalogue.get_document(rec.stable_id)["version"] == 1

    rec2 = make_record(raw_bytes=b"<akomaNtoso>revised data protection text</akomaNtoso>")
    rec2.ensure_payload_hash()
    catalogue.upsert_document(rec2)
    assert catalogue.get_document(rec.stable_id)["version"] == 2


def test_changed_payload_archives_prior_version(catalogue):
    """A document is a series of versions; the prior one is retained (§1.4)."""
    rec = make_record(title="v1")
    catalogue.upsert_document(rec)
    assert catalogue.list_versions(rec.stable_id) == []  # nothing archived yet

    rec2 = make_record(title="v2", raw_bytes=b"<akomaNtoso>revised text</akomaNtoso>")
    rec2.ensure_payload_hash()
    catalogue.upsert_document(rec2)

    archived = catalogue.list_versions(rec.stable_id)
    assert len(archived) == 1
    assert archived[0]["version"] == 1 and archived[0]["title"] == "v1"
    assert archived[0]["payload_hash"] == rec.payload_hash  # old bytes recoverable
    assert catalogue.get_document(rec.stable_id)["title"] == "v2"  # latest


def test_unchanged_payload_does_not_archive(catalogue):
    rec = make_record()
    catalogue.upsert_document(rec)
    catalogue.upsert_document(rec)  # same bytes → no new version
    assert catalogue.list_versions(rec.stable_id) == []


def test_upstream_status_never_deletes(catalogue):
    rec = make_record()
    catalogue.upsert_document(rec)
    catalogue.mark_upstream_status(rec.stable_id, UpstreamStatus.GONE_404)
    row = catalogue.get_document(rec.stable_id)
    assert row is not None  # row survives — append-only (§1.4a)
    assert row["upstream_status"] == "gone_404"


def test_watermark_roundtrip(catalogue):
    assert catalogue.get_watermark("uk-caselaw") is None
    catalogue.set_watermark("uk-caselaw", "2024-01-15")
    assert catalogue.get_watermark("uk-caselaw") == "2024-01-15"
