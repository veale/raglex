from __future__ import annotations

from datetime import date

import pytest

from raglex.core.models import DocType, Record
from raglex.storage import TextStore
from raglex.tagging import RuleEngine, seed
from raglex.tagging.predicates import DocView, pred_field, pred_grep_like, pred_literal, pred_regex
from raglex.tagging.tree import evaluate, root_method, validate_tree


# -- predicates -------------------------------------------------------------
def _view(text="", **fields):
    return DocView(row=fields, _load_text=lambda: text)


def test_literal_folds_case_and_accents():
    doc = _view("Concerne les données à caractère personnel")
    assert pred_literal(doc, {"value": "donnees a caractere personnel"}) is True


def test_regex_over_text():
    doc = _view("decided under Article 22 GDPR")
    assert pred_regex(doc, {"pattern": r"art(icle)?\.?\s*22", "flags": "i"}) is True


def test_grep_like_whole_word_and_proximity():
    doc = _view("the data subject made an access request today")
    assert pred_grep_like(doc, {"value": "access"}) is True
    assert pred_grep_like(doc, {"near": ["data", "request"], "within": 6}) is True
    assert pred_grep_like(doc, {"near": ["data", "request"], "within": 1}) is False


def test_field_predicate_ops():
    doc = _view(court="CJEU", decision_date="2020-07-16", doc_type="judgment")
    assert pred_field(doc, {"field": "court", "op": "eq", "value": "CJEU"}) is True
    assert pred_field(doc, {"field": "decision_date", "op": "gte", "value": "2018-05-25"}) is True
    assert pred_field(doc, {"field": "doc_type", "op": "in", "value": ["judgment", "decision"]})


# -- condition tree ---------------------------------------------------------
def test_tree_mixes_predicate_types_under_bool_ops():
    # field(court=CJEU) AND (literal 2016/679 OR literal GDPR) AND NOT field(doc_type=opinion)
    tree = {
        "op": "AND",
        "children": [
            {"predicate": "field", "args": {"field": "court", "op": "eq", "value": "CJEU"}},
            {"op": "OR", "children": [
                {"predicate": "literal", "args": {"value": "2016/679"}},
                {"predicate": "literal", "args": {"value": "GDPR"}},
            ]},
            {"op": "NOT", "children": [
                {"predicate": "field", "args": {"field": "doc_type", "op": "eq", "value": "opinion"}},
            ]},
        ],
    }
    hit = _view("cites Regulation 2016/679", court="CJEU", doc_type="judgment")
    miss = _view("cites Regulation 2016/679", court="CJEU", doc_type="opinion")
    assert evaluate(tree, hit) is True
    assert evaluate(tree, miss) is False


def test_root_method_single_vs_composite():
    assert root_method({"predicate": "literal", "args": {"value": "x"}}) == "literal"
    assert root_method({"op": "OR", "children": []}) == "rule"


def test_validate_tree_rejects_bad_regex_and_unknown_predicate():
    with pytest.raises(Exception):
        validate_tree({"predicate": "regex", "args": {"pattern": "("}})
    with pytest.raises(Exception):
        validate_tree({"predicate": "semantic", "args": {}})  # not yet supported


# -- engine -----------------------------------------------------------------
def _store_doc(catalogue, tmp_path, stable_id, text, **fields) -> None:
    ts = TextStore(tmp_path / "text")
    rec = Record(
        source=fields.get("source", "uk-grc"),
        stable_id=stable_id,
        doc_type=fields.get("doc_type", DocType.JUDGMENT),
        court=fields.get("court"),
        decision_date=fields.get("decision_date", date(2024, 1, 1)),
        text=text,
        raw_bytes=text.encode(),
    )
    rec.ensure_payload_hash()
    path = str(ts.put(rec.payload_hash, text))
    catalogue.upsert_document(rec, text_path=path)


def test_rule_run_tags_matching_docs_with_provenance(catalogue, tmp_path):
    _store_doc(catalogue, tmp_path, "a", "This is about the GDPR and 2016/679.")
    _store_doc(catalogue, tmp_path, "b", "An unrelated planning dispute.")
    engine = RuleEngine(catalogue)
    rid = engine.add_rule("gdpr", {"predicate": "literal", "args": {"value": "2016/679"}})

    result = engine.run_rule(rid)
    assert result.matched == 1 and result.written == 1

    tags_a = catalogue.tags_for("a")
    assert len(tags_a) == 1
    assert tags_a[0]["tag"] == "gdpr"
    assert tags_a[0]["assigned_by_rule_id"] == rid
    assert tags_a[0]["method"] == "literal"
    # denormalised cache refreshed for faceting
    assert "gdpr" in catalogue.get_document("a")["topic_tags"]
    assert catalogue.get_document("b")["topic_tags"] == "[]"


def test_preview_does_not_write(catalogue, tmp_path):
    _store_doc(catalogue, tmp_path, "a", "personal data and the GDPR")
    engine = RuleEngine(catalogue)
    res = engine.preview("gdpr", {"predicate": "literal", "args": {"value": "gdpr"}})
    assert res.matched == 1
    assert res.sample[0][0] == "a"
    assert catalogue.tags_for("a") == []  # nothing written


def test_manual_tag_is_never_overwritten_by_a_rule(catalogue, tmp_path):
    _store_doc(catalogue, tmp_path, "a", "mentions the gdpr")
    catalogue.upsert_document_tag("a", "gdpr", method="manual")
    engine = RuleEngine(catalogue)
    rid = engine.add_rule("gdpr", {"predicate": "literal", "args": {"value": "gdpr"}})
    result = engine.run_rule(rid)
    # rule matched but did not write over the human-curated tag (§4a)
    assert result.matched == 1 and result.written == 0
    methods = {t["method"] for t in catalogue.tags_for("a")}
    assert methods == {"manual"}


def test_rerun_is_a_reprojection(catalogue, tmp_path):
    _store_doc(catalogue, tmp_path, "a", "the gdpr applies")
    engine = RuleEngine(catalogue)
    rid = engine.add_rule("gdpr", {"predicate": "literal", "args": {"value": "gdpr"}})
    engine.run_rule(rid)
    engine.run_rule(rid)  # re-run clears prior then re-applies — no duplicate rows
    assert len([t for t in catalogue.tags_for("a") if t["tag"] == "gdpr"]) == 1


def test_seed_rules_tag_corpus(catalogue, tmp_path):
    _store_doc(catalogue, tmp_path, "a", "This decision concerns persoonsgegevens (AVG).")
    _store_doc(catalogue, tmp_path, "b", "freedom of information request refused.")
    engine = RuleEngine(catalogue)
    seed(engine)
    engine.run_all()
    assert "data_protection" in catalogue.get_document("a")["topic_tags"]
    assert "foi" in catalogue.get_document("b")["topic_tags"]
