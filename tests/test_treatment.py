from __future__ import annotations

from datetime import date

from raglex.citations import extract_document
from raglex.core.models import DocType, ExtractedVia, Record, RelationshipType
from raglex.storage import TextStore
from raglex.treatment import HeuristicTreatmentClassifier, classify_document


def test_heuristic_classifier_reads_cues():
    clf = HeuristicTreatmentClassifier()
    assert clf.classify("the court distinguished ECLI:EU:C:2020:559 on the facts", entity_kind="case") == RelationshipType.DISTINGUISHES
    assert clf.classify("we follow [2024] UKSC 12", entity_kind="case") == RelationshipType.FOLLOWS
    assert clf.classify("hereby overruling the earlier decision", entity_kind="case") == RelationshipType.OVERRULES
    assert clf.classify("merely cited in passing", entity_kind="case") == RelationshipType.CONSIDERS
    # a statute citation gets no treatment
    assert clf.classify("applied Article 17 of Regulation (EU) 2016/679", entity_kind="regulation") is None


def _doc(catalogue, ts, stable_id, text, dt=DocType.JUDGMENT):
    rec = Record(source="x", stable_id=stable_id, doc_type=dt, decision_date=date(2024, 1, 1),
                 text=text, raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def test_extraction_writes_citations_table_and_deduped_edges(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    # the same case cited twice → 2 citation rows, 1 edge
    _doc(catalogue, ts, "case-1",
         "We follow ECLI:EU:C:2020:559. Later, ECLI:EU:C:2020:559 is applied again.")
    extract_document(catalogue, ts, "case-1")

    cites = catalogue.citations_for("case-1")
    ecli_cites = [c for c in cites if c["candidate_id"] == "ECLI:EU:C:2020:559"]
    assert len(ecli_cites) == 2 and ecli_cites[0]["entity_kind"] == "case"
    edges = [r for r in catalogue.relations_for("case-1") if r["dst_id"] == "ECLI:EU:C:2020:559"]
    assert len(edges) == 1  # deduped
    assert edges[0]["context_start"] is not None  # span stored for treatment


def test_treatment_reclassifies_mentions_to_followed(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "case-1", "In this matter the court expressly followed ECLI:EU:C:2020:559.")
    extract_document(catalogue, ts, "case-1")
    # before: a bare mentions edge
    edge = [r for r in catalogue.relations_for("case-1") if r["dst_id"] == "ECLI:EU:C:2020:559"][0]
    assert edge["relationship_type"] == "mentions"

    n = classify_document(catalogue, ts, "case-1")
    assert n == 1
    edge = [r for r in catalogue.relations_for("case-1") if r["dst_id"] == "ECLI:EU:C:2020:559"][0]
    assert edge["relationship_type"] == "follows"  # mentions → follows (§1.3a)


def test_treatment_leaves_authoritative_typed_edges_untouched(catalogue, tmp_path):
    """An adapter's typed edge (e.g. NL 'applies') must not be downgraded."""
    from raglex.core.models import ResolutionStatus, TypedRelation

    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "src", "the court distinguished the earlier ruling")
    catalogue.add_relation("src", TypedRelation(
        relationship_type=RelationshipType.APPLIES, raw_citation_string="ECLI:NL:HR:2021:1",
        dst_id="ECLI:NL:HR:2021:1", extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.RESOLVED, context_start=0, context_end=10))
    classify_document(catalogue, ts, "src")
    edge = catalogue.relations_for("src")[0]
    assert edge["relationship_type"] == "applies"  # untouched (not 'mentions')
