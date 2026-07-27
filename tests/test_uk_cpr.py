from __future__ import annotations

from raglex.adapters.registry import source_catalog
from raglex.adapters.uk_cpr import (
    CPR_ROOT_ID,
    UKCivilProcedureRulesAdapter,
    page_identity,
    parse_index,
)
from raglex.citations.extractor import extract_citations


INDEX = """
<article><div class="rich-text">
  <a href="#parts-1-10">1-10</a>
  <a href="/courts/procedure-rules/civil/rules/part03">Part 3 – Case Management</a>
  <a href="/courts/procedure-rules/civil/rules/part03/practice-direction-3e-costs-management">
    Practice Direction 3D – Costs Management
  </a>
  <a href="/courts/procedure-rules/civil/rules/pd-pre-action">
    Practice Direction – Pre-Action Conduct and Protocols
  </a>
  <a href="#Back-to-top">To the top</a>
</div></article>
"""

PART = """
<article>
 <h1>PART 3 – THE COURT'S CASE MANAGEMENT POWERS</h1>
 <div class="two-sidebars__article-content">
  <div class="rich-text">
   <p>Contents of this Part</p><figure><table><tr><td>Rule 3.1</td></tr></table></figure>
   <h2>I CASE MANAGEMENT</h2>
   <h3>The court's general powers of management</h3>
   <p>3.1</p><p>(1) The court may manage the case.</p>
   <h3>Relief from sanctions</h3>
   <p>3.9</p><p>(1) The court will consider all the circumstances.</p>
  </div>
  <div class="updated-date"><p>Updated: Tuesday, 5 September 2023</p></div>
 </div>
</article>
"""

PD = """
<article>
 <h1>PRACTICE DIRECTION 3D – COSTS MANAGEMENT</h1>
 <div class="one-sidebar__article-content">
  <div class="rich-text">
   <h2>This Practice Direction supplements Part 3</h2>
   <p>1. A costs budget must be filed.</p>
   <p>2.1 The parties must cooperate.</p>
  </div>
  <div class="updated-date"><p>Updated: Thursday, 24 November 2022</p></div>
 </div>
</article>
"""


class Response:
    def __init__(self, text: str):
        self.content = text.encode()


class Client:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url.rstrip("/").endswith("/rules"):
            return Response(INDEX)
        if url.endswith("part03"):
            return Response(PART)
        if "practice-direction-3e" in url:
            return Response(PD)
        return Response("<article><h1>PRACTICE DIRECTION – PRE-ACTION CONDUCT AND "
                        "PROTOCOLS</h1><div class='rich-text'><p>1. Before proceedings.</p>"
                        "</div></article>")


def _cite(text: str):
    return [(c.candidate_id, c.pinpoint, c.method) for c in extract_citations(text)
            if c.candidate_id and c.candidate_id.startswith("uk/cpr")]


def test_index_and_identity_use_printed_title_not_legacy_url():
    links = parse_index(INDEX)
    assert len(links) == 3
    pd = page_identity(links[1][0], links[1][1])
    assert pd.stable_id == "uk/cpr/pd/3d"
    assert page_identity("Practice Direction 32", "/legacy/pd_part32").stable_id == "uk/cpr/pd/32"
    assert page_identity("Part 57A – BPC", "/legacy/part57a").stable_id == "uk/cpr/part/57a"


def test_targeted_rule_fetches_owning_part_and_mints_rule_aliases():
    client = Client()
    adapter = UKCivilProcedureRulesAdapter(ids="uk/cpr/rule/3.9", client=client)
    stubs = list(adapter.discover(None))
    assert [s.stable_id for s in stubs] == ["uk/cpr/part/3"]
    # Targeted mode reads the index and requested Part, not every PD.
    assert len(client.urls) == 2
    record = adapter.fetch(stubs[0])
    assert record.stable_id == "uk/cpr/part/3"
    assert record.extra["updated_at_source"] == "2023-09-05"
    assert "uk/cpr/rule/3.9" in record.extra["aliases"]
    assert "Rule 3.1" not in record.text  # contents table removed
    assert any(s.label == "rule 3.9" for s in record.segments)
    assert record.relations[0].dst_id == "uksi/1998/3132"


def test_practice_direction_is_structured_and_related_to_cpr_root():
    adapter = UKCivilProcedureRulesAdapter(ids="uk/cpr/pd/3d", client=Client())
    record = adapter.fetch(next(adapter.discover(None)))
    assert record.stable_id == "uk/cpr/pd/3d"
    assert record.doc_type.value == "guidance"
    assert [s.label for s in record.segments if s.kind == "paragraph"] == [
        "paragraph 1", "paragraph 2.1"]
    assert record.relations[0].dst_id == CPR_ROOT_ID


def test_cpr_rule_part_and_direction_grammars():
    assert _cite("CPR 3.9(1)(a)") == [
        ("uk/cpr/rule/3.9", "rule 3.9(1)(a)", "uk_cpr_rule_prefix")]
    assert _cite("rule 3.9 under the Civil Procedure Rules") == [
        ("uk/cpr/rule/3.9", "rule 3.9", "uk_cpr_rule_suffix")]
    assert _cite("CPR Part 36") == [
        ("uk/cpr/part/36", "Part 36", "uk_cpr_part_prefix")]
    assert _cite("Pt 52 of the CPR") == [
        ("uk/cpr/part/52", "Part 52", "uk_cpr_part_suffix")]
    assert _cite("Practice Direction 3D para 5.2") == [
        ("uk/cpr/pd/3d", "paragraph 5.2", "uk_cpr_practice_direction")]
    assert _cite("paragraph 18.1 of PD 32") == [
        ("uk/cpr/pd/32", "paragraph 18.1", "uk_cpr_practice_direction_suffix")]
    assert _cite("[2.2] of CPR Practice Direction 22") == [
        ("uk/cpr/pd/22", "paragraph 2.2", "uk_cpr_practice_direction_suffix")]
    assert _cite("paragraph 3.1, PD 3C") == [
        ("uk/cpr/pd/3c", "paragraph 3.1", "uk_cpr_practice_direction_suffix")]
    assert _cite("Practice Direction on Pre-Action Conduct and Protocols") == [
        ("uk/cpr/pd/pre-action-conduct-and-protocols", None,
         "uk_cpr_pre_action_direction")]
    assert _cite("Practice Direction: Insolvency Proceedings") == [
        ("uk/cpr/pd/insolvency-proceedings", None, "uk_cpr_insolvency_direction")]
    assert _cite("Civil Procedure Rules 1998") == [
        ("uk/cpr", None, "uk_cpr_instrument")]


def test_cpr_source_is_a_full_walk_currency_source():
    row = next(r for r in source_catalog() if r["key"] == "uk-cpr")
    assert row["incremental_mode"] == "full-walk"
    assert row["can_incremental"] is True


def test_cpr_rule_and_pd_lists_emit_every_pinpoint():
    cites = [c for c in extract_citations("CPR Rules 6.13 and 6.25")
             if c.method == "uk_cpr_rule_list"]
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("uk/cpr/rule/6.13", "rule 6.13"),
        ("uk/cpr/rule/6.25", "rule 6.25"),
    ]
    cites = [c for c in extract_citations("paragraphs 3.1 and 3.2, PD 3C")
             if c.method == "uk_cpr_pd_paragraph_list"]
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("uk/cpr/pd/3c", "paragraph 3.1"),
        ("uk/cpr/pd/3c", "paragraph 3.2"),
    ]
