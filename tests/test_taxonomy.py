"""Corpus taxonomy — held documents and pending candidates land in the same
category/sub-type buckets, so the Corpus Map's held-vs-pending lines up."""

from raglex.citations.taxonomy import classify_candidate, classify_document


def test_held_documents_classify_to_category_and_subtype():
    cj = classify_document(source="eu-cellar", doc_type="judgment",
                           court="Court of Justice", stable_id="ECLI:EU:C:2019:1")
    assert (cj.category, cj.subtype) == ("eu-cellar", "cj")
    assert classify_document(source="eu-cellar", doc_type="opinion",
                             court="Advocate General", stable_id="ECLI:EU:C:2019:1").subtype == "ag"
    assert classify_document(source="eu-cellar", doc_type="judgment",
                             court="General Court", stable_id="ECLI:EU:T:2019:1").subtype == "gc"
    # UK legislation: bare slug-head (held GROUP BY) → kind + nation
    assert classify_document(source="uk-legislation", stable_id="uksi").subtype == "secondary:UK-wide"
    assert classify_document(source="uk-legislation", stable_id="asp").subtype == "primary:Scotland"
    assert classify_document(source="uk-legislation", stable_id="european").subtype == "assimilated"
    assert classify_document(source="uk-caselaw", court="ewhc", stable_id="ewhc").category == "uk-caselaw"
    assert classify_document(source="echr", stable_id="echr").subtype == "convention"
    assert classify_document(source="echr", stable_id="ECLI:CE:ECHR:2019:1").subtype == "case"


def test_pending_candidates_classify_consistently():
    assert classify_candidate("62018CJ0311").category == "eu-cellar"
    assert classify_candidate("62018CJ0311").subtype == "cj"
    assert classify_candidate("32016R0679").category == "eu-legislation"
    assert classify_candidate("32016R0679").subtype == "reg"
    assert classify_candidate("uksi/2016/413").subtype == "secondary:UK-wide"
    assert classify_candidate("ukpga/1998/42").subtype == "primary:UK-wide"
    assert classify_candidate("ewhc/2020/1").category == "uk-caselaw"
    assert classify_candidate("4451/70").category == "echr"
