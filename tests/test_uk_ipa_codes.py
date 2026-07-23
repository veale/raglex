"""UK IPA 2016 codes of practice — provision-edge extraction and record building.
Network-free (a fake fetcher stands in for gov.uk)."""

from __future__ import annotations

from raglex.adapters.uk_ipa_codes import (
    CODES,
    IPA_2016,
    UKIPACodesAdapter,
    ipa_provision_relations,
    _slug,
)
from raglex.core.models import DocType, ExtractedVia, RelationshipType


def _anchors(text):
    return [r.dst_anchor for r in ipa_provision_relations(text)]


def test_bare_sections_and_schedules_link_to_ipa():
    rels = ipa_provision_relations("A warrant under section 87 and Schedule 7 is needed.")
    assert {r.dst_anchor for r in rels} == {"section 87", "Schedule 7"}
    assert all(r.dst_id == IPA_2016 for r in rels)
    assert all(r.relationship_type is RelationshipType.INTERPRETS for r in rels)
    assert all(r.extracted_via is ExtractedVia.REGEX for r in rels)


def test_of_the_act_is_the_ipa():
    assert _anchors("As set out in section 2 of the Act.") == ["section 2"]
    assert _anchors("Under section 6 of the Investigatory Powers Act 2016.") == ["section 6"]


def test_a_different_named_act_is_excluded():
    assert _anchors("This is governed by section 6 of the Human Rights Act 1998.") == []
    assert _anchors("See regulation 5 of the Data Protection Regulations 2019 too.") == []


def test_pinpoint_subsections_preserved():
    assert _anchors("A warrant under section 61(7)(b) applies.") == ["section 61(7)(b)"]


def test_ranges_and_lists_expand():
    assert _anchors("See sections 138 to 140.") == ["section 138", "section 139", "section 140"]
    assert _anchors("sections 5, 7 to 9 apply") == [
        "section 5", "section 7", "section 8", "section 9"]
    assert _anchors("sections 61 and 62") == ["section 61", "section 62"]


def test_short_forms():
    assert _anchors("see s. 12 and Sch. 3") == ["section 12", "Schedule 3"]


def test_duplicate_anchors_deduped():
    rels = ipa_provision_relations("section 5 here, and section 5 again later.")
    assert [r.dst_anchor for r in rels] == ["section 5"]


class _FakePage:
    def __init__(self, html):
        self.html = html


class _FakeFetcher:
    def __init__(self, html):
        self.html = html

    def fetch(self, url, *, headers=None):
        return _FakePage(self.html)

    def close(self):
        pass


def test_record_is_home_office_guidance_with_edges():
    html = ("<html><body><main><h1>Interception code</h1>"
            "<p>Under section 138 to 140 the agency acts. Schedule 7 applies.</p>"
            "</main></body></html>")
    adapter = UKIPACodesAdapter(fetcher=_FakeFetcher(html))
    stubs = list(adapter.discover(None))
    assert len(stubs) == len(CODES) == 9
    rec = adapter.fetch(stubs[0])
    assert rec.doc_type is DocType.GUIDANCE
    assert rec.court == "Home Office"
    assert rec.stable_id.startswith("uk-ipa-code/")
    assert rec.extra["issuer"] == "Home Office"
    assert {r.dst_anchor for r in rec.relations} >= {"section 138", "section 139", "section 140", "Schedule 7"}
    assert all(r.dst_id == IPA_2016 for r in rec.relations)


def test_slug_strips_accessible_suffix():
    assert _slug("https://www.gov.uk/x/notices-regime-code-of-practice-accessible") == \
        "notices-regime-code-of-practice"
    assert _slug("https://www.gov.uk/x/communications-data-code-of-practice-accessible--2") == \
        "communications-data-code-of-practice"
