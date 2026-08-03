"""Regressions from the live passage-refinement queue (Ashley v HMRC and neighbours)."""

from collections import defaultdict

from raglex.citations.extractor import (
    attach_stored_shorthands,
    extract_citations,
    valid_shorthand,
)
from raglex.citations.models import Citation
from raglex.citations.stage import _guard_cites


def test_existing_global_shorthand_guard_does_not_link_bare_litigation_terms():
    stored = [
        ("SAR", "ukpga/1998/29", "act", True),
        ("HMRC", "ukpga/2000/36", "act", True),
        ("LPP", "ukpga/2000/36", "act", True),
        ("Issue 3", "ukpga/2000/36", "act", True),
    ]
    text = "The SAR left HMRC to address LPP under Issue 3."
    assert attach_stored_shorthands(text, [], stored) == []
    assert all(not valid_shorthand(name) for name, *_ in stored)


def test_tax_year_possessive_is_not_inferred_as_section_2011():
    text = "all data held in relation to your client's 2011/12 tax return"
    assert not [c for c in extract_citations(text) if c.raw == "s 2011"]


def test_nearby_explicit_article_supplies_host_for_quoted_article_range():
    text = (
        "Article 12 of the UK GDPR provides: “The controller shall provide information "
        "under Articles 15 to 22 and 34 to the data subject.”"
    )
    cites = extract_citations(text)
    pins = {c.pinpoint for c in cites if c.candidate_id == "european/regulation/2016/0679"}
    assert {f"Article {n}" for n in range(15, 23)} | {"Article 34"} <= pins


def test_judgment_drops_bare_schedule_carry_forward_but_keeps_literal_citation():
    inferred = Citation(
        raw="Schedule 1", entity_kind="regulation", candidate_id="uk/cpr/part/8",
        pinpoint="Sch. 1", char_start=10, char_end=20, method="carry_forward",
    )
    literal = Citation(
        raw="Schedule 1 to the Data Protection Act 2018", entity_kind="act",
        candidate_id="ukpga/2018/12", pinpoint="Sch. 1", char_start=30,
        char_end=75, method="uk_act_schedule",
    )
    doc = defaultdict(lambda: None, {
        "doc_type": "judgment", "source": "uk-caselaw", "court": "ewhc",
        "stable_id": "ewhc/kb/2025/134", "meta_json": None,
    })
    kept = _guard_cites(None, doc, [inferred, literal], stable_id=doc["stable_id"])
    assert kept == [literal]
