"""Manual corrections — fix misclassification the academic spots (§1.3a/§4a):
reclassify a document, correct/suppress/re-point a citation edge, un-tag."""

from __future__ import annotations

import os
import tempfile
from datetime import date

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade


def _facade() -> Facade:
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _doc(f: Facade, sid: str, text: str, **kw) -> None:
    with f._open() as (cat, _rs, ts):
        rec = Record(source="x", stable_id=sid, doc_type=kw.get("doc_type", DocType.JUDGMENT),
                     decision_date=date(2024, 1, 1), text=text, raw_bytes=text.encode(),
                     court=kw.get("court"), title=kw.get("title"),
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def test_update_document_metadata():
    f = _facade()
    _doc(f, "c1", "text", court="wrong", title="bad", doc_type=DocType.JUDGMENT)
    res = f.update_document(stable_id="c1", doc_type="decision", court="UKSC", title="Good")
    assert res["updated"]
    d = res["document"]
    assert d["doc_type"] == "decision" and d["court"] == "UKSC" and d["title"] == "Good"
    assert d["added_by"] == "user"  # recorded as human curation


def test_update_document_rejects_bad_doc_type():
    f = _facade()
    _doc(f, "c1", "text")
    res = f.update_document(stable_id="c1", doc_type="nonsense")
    assert "error" in res


def test_reclassify_citation_treatment_sticks_through_reextraction():
    f = _facade()
    _doc(f, "c1", "the court mentioned Case C-311/18 in passing")
    f.extract_citations(stable_id="c1")
    rid = f.get_document("c1")["relations"][0]["relation_id"]
    # academic corrects: it's actually distinguishing, not the heuristic's guess
    f.correct_citation(relation_id=rid, treatment="distinguishes")
    # re-extraction must not clobber a manual correction (extracted_via=manual)
    f.extract_citations(stable_id="c1")
    rels = f.get_document("c1")["relations"]
    edge = next(r for r in rels if "C-311" in (r["raw_citation_string"] or ""))
    assert edge["relationship_type"] == "distinguishes" and edge["extracted_via"] == "manual"


def test_suppress_citation_false_positive_does_not_return():
    f = _facade()
    # "2024 GDPR 5" trips the bracketless neutral-citation grammar — a false positive
    _doc(f, "c1", "see Case C-311/18 and the stray 2024 GDPR 5 string")
    f.extract_citations(stable_id="c1")
    doc = f.get_document("c1")
    spur = next(r for r in doc["relations"] if (r["dst_id"] or "").startswith("gdpr/"))
    f.correct_citation(relation_id=spur["relation_id"], suppress=True)

    doc2 = f.get_document("c1")
    assert doc2["suppressed_count"] == 1
    assert not any((r["dst_id"] or "").startswith("gdpr/") for r in doc2["relations"])
    # the veto survives a re-extraction
    f.extract_citations(stable_id="c1")
    doc3 = f.get_document("c1")
    assert not any((r["dst_id"] or "").startswith("gdpr/") for r in doc3["relations"])


def test_repoint_citation_to_correct_document():
    f = _facade()
    _doc(f, "c1", "applying Case C-311/18")
    f.extract_citations(stable_id="c1")
    _doc(f, "right-doc", "the actual judgment")
    rid = f.get_document("c1")["relations"][0]["relation_id"]
    res = f.correct_citation(relation_id=rid, dst_id="right-doc")
    assert res["action"] == "repointed"
    edge = f.get_document("c1")["relations"][0]
    assert edge["dst_id"] == "right-doc" and edge["resolution_status"] == "resolved"


def test_untag_and_bulk_tag():
    f = _facade()
    _doc(f, "a", "x")
    _doc(f, "b", "y")
    f.tag_many(doc_ids=["a", "b"], tag="my-collection")
    assert any(t["tag"] == "my-collection" for t in f.get_document("a")["tags"])
    f.untag(doc_id="a", tag="my-collection")
    assert not any(t["tag"] == "my-collection" for t in f.get_document("a")["tags"])
    assert any(t["tag"] == "my-collection" for t in f.get_document("b")["tags"])  # b untouched
