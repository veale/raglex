"""Manual resolution of hanging references (§5b) — list them, then satisfy them by
linking to an existing item, supplying the missing identifier, or uploading bytes."""

from __future__ import annotations

import base64
import tempfile
from datetime import date

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade


def _facade() -> Facade:
    import os

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _doc(f: Facade, stable_id: str, text: str, **kw) -> None:
    with f._open() as (cat, _rs, ts):
        rec = Record(source="x", stable_id=stable_id, ecli=kw.get("ecli"),
                     doc_type=kw.get("doc_type", DocType.JUDGMENT), decision_date=date(2024, 1, 1),
                     text=text, raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def test_unresolved_lists_hanging_references_with_routing():
    f = _facade()
    _doc(f, "case-1", "following Case C-311/18 and applying Article 17 of Regulation (EU) 2016/679")
    f.extract_citations(stable_id="case-1")

    refs = {r["ref"]: r for r in f.unresolved_references()}
    assert "62018CJ0311" in refs  # CJEU case — routed to eu-cellar
    assert refs["62018CJ0311"]["suggested_adapter"] == "eu-cellar"
    assert refs["32016R0679"]["suggested_adapter"] == "eu-legislation"
    assert refs["62018CJ0311"]["citing_count"] == 1


def test_resolve_reference_to_existing_item():
    f = _facade()
    _doc(f, "case-1", "the court followed Case C-311/18")
    f.extract_citations(stable_id="case-1")
    # the judgment is already in the corpus under its ECLI; link the hanging ref to it
    _doc(f, "ECLI:EU:C:2020:559", "judgment text", ecli="ECLI:EU:C:2020:559")

    res = f.resolve_reference(ref="62018CJ0311", existing_id="ECLI:EU:C:2020:559")
    assert res["resolved"] and res["resolved_edges"] >= 1
    with f._open() as (cat, _rs, _ts):
        edge = next(e for e in cat.relations_for("case-1") if e["raw_citation_string"].startswith("Case C-311"))
        assert edge["resolution_status"] == "resolved" and edge["dst_id"] == "ECLI:EU:C:2020:559"


def test_unresolved_normalises_urls_merges_and_drops_junk():
    """Adapter-supplied raw URLs collapse to their candidate id (and merge with the
    same case's neutral-citation form); junk anchors like '#' are dropped."""
    from raglex.core.models import RelationshipType, ResolutionStatus, TypedRelation

    f = _facade()
    _doc(f, "case-1", "body cites [2011] EWCA Civ 31")
    f.extract_citations(stable_id="case-1")  # grammar → candidate ewca/civ/2011/31
    with f._open() as (cat, _rs, _ts):
        cat.add_relations("case-1", [
            # same case, but as a raw Find Case Law URL (no candidate) — must merge
            TypedRelation(relationship_type=RelationshipType.MENTIONS,
                          raw_citation_string="https://caselaw.nationalarchives.gov.uk/ewca/civ/2011/31",
                          dst_id=None, extracted_via=ExtractedVia.STRUCTURED,
                          resolution_status=ResolutionStatus.PENDING),
            # a legislation URL with a section — collapses to the Act id, routable
            TypedRelation(relationship_type=RelationshipType.MENTIONS,
                          raw_citation_string="http://www.legislation.gov.uk/id/ukpga/1988/52/section/131",
                          dst_id=None, extracted_via=ExtractedVia.STRUCTURED,
                          resolution_status=ResolutionStatus.PENDING),
            # junk anchor — must be dropped
            TypedRelation(relationship_type=RelationshipType.MENTIONS, raw_citation_string="#",
                          dst_id=None, extracted_via=ExtractedVia.STRUCTURED,
                          resolution_status=ResolutionStatus.PENDING),
        ])
    refs = {r["ref"]: r for r in f.unresolved_references()}
    assert "#" not in refs  # junk dropped
    # the URL merged into the neutral-citation candidate (one row, routable)
    assert refs["ewca/civ/2011/31"]["suggested_adapter"] == "uk-caselaw"
    assert refs["ewca/civ/2011/31"]["citing_count"] == 1
    # legislation URL → Act id, routed to uk-legislation
    assert refs["ukpga/1988/52"]["form"] == "UK legislation"
    assert refs["ukpga/1988/52"]["suggested_adapter"] == "uk-legislation"


def test_harvest_reference_routes_or_errors_cleanly():
    f = _facade()
    # a form with no targeted adapter returns a clean, actionable error (no crash)
    res = f.harvest_reference(ref="ECLI:FR:CC:2020:1")
    assert "error" in res and "upload" in res["error"]


def test_targeted_harvest_covers_cjeu_and_nl_ecli():
    """The targeted-harvest registry now builds fetchers for CJEU case CELEX and
    Dutch ECLI (network-free: just check a real adapter is constructed)."""
    from raglex.facade import _TARGETED_HARVEST
    from raglex.adapters.eu_cellar import CJEUCaseAdapter

    cjeu = _TARGETED_HARVEST["eu-cellar"]("62018CJ0511")
    assert isinstance(cjeu, CJEUCaseAdapter) and cjeu.celex == "62018CJ0511"
    nl = _TARGETED_HARVEST["nl-rechtspraak"]("ECLI:NL:HR:2021:1234")
    assert nl is not None and nl.source == "nl-rechtspraak"
    # a non-EU, non-CELEX candidate isn't targetable via eu-cellar → None (no crash)
    assert _TARGETED_HARVEST["eu-cellar"]("not-a-celex") is None


def test_unresolved_normalises_url_dst_id():
    """A citation stored with a full URL as its dst_id still shows as a routable slug."""
    from raglex.core.models import RelationshipType, ResolutionStatus, TypedRelation

    f = _facade()
    _doc(f, "case-1", "body")
    with f._open() as (cat, _rs, _ts):
        cat.add_relations("case-1", [TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string="https://caselaw.nationalarchives.gov.uk/ukut/aac/2012/440",
            dst_id="https://caselaw.nationalarchives.gov.uk/ukut/aac/2012/440",
            extracted_via=ExtractedVia.STRUCTURED, resolution_status=ResolutionStatus.PENDING)])
    refs = {r["ref"]: r for r in f.unresolved_references()}
    assert "ukut/aac/2012/440" in refs and refs["ukut/aac/2012/440"]["suggested_adapter"] == "uk-caselaw"


def test_resolve_reference_by_supplying_identifier():
    """A reference recognised by name only (no candidate) gets its identifier from
    the user; supplying a neutral citation re-keys the edge and it resolves once the
    target is present."""
    from raglex.core.models import RelationshipType, ResolutionStatus, TypedRelation

    f = _facade()
    _doc(f, "case-1", "see the Divisional Court's judgment in the Miller litigation")
    # a name-only hanging edge (what the LLM extractor leaves): no candidate id
    with f._open() as (cat, _rs, _ts):
        cat.add_relations("case-1", [TypedRelation(
            relationship_type=RelationshipType.MENTIONS, raw_citation_string="the Miller litigation",
            dst_id=None, extracted_via=ExtractedVia.REGEX, resolution_status=ResolutionStatus.PENDING)])

    refs = {r["ref"]: r for r in f.unresolved_references()}
    assert refs["the Miller litigation"]["needs_identifier"] is True

    # user provides the neutral citation; the target is already in the corpus
    _doc(f, "ewhc/2016/2768", "Miller v SoS judgment")
    res = f.resolve_reference(ref="the Miller litigation", identifier="[2016] EWHC 2768")
    assert res["canonical"] == "ewhc/2016/2768" and res["resolved"]


def test_resolve_reference_by_upload():
    f = _facade()
    _doc(f, "case-1", "the court followed Case C-311/18")
    f.extract_citations(stable_id="case-1")
    payload = base64.b64encode(b"Judgment of the Court in Case C-311/18.").decode()

    res = f.resolve_reference(ref="62018CJ0311", content_base64=payload,
                              filename="judgment.txt", title="C-311/18 judgment")
    assert res["target"] and res["resolved"]
    with f._open() as (cat, _rs, _ts):
        edge = next(e for e in cat.relations_for("case-1") if "C-311" in (e["raw_citation_string"] or ""))
        assert edge["resolution_status"] == "resolved" and edge["dst_id"] == res["target"]


def test_act_level_keeps_regnal_chapter():
    from raglex.facade import _act_level
    # a regnal Act is 4 segments (type/monarch/session/number) — don't drop the chapter
    assert _act_level("ukpga/Eliz2/9-10/18/section/5") == "ukpga/Eliz2/9-10/18"
    assert _act_level("ukpga/Eliz2/9-10/18") == "ukpga/Eliz2/9-10/18"
    # modern Acts still collapse section → Act (3 segments)
    assert _act_level("ukpga/2000/36/section/14") == "ukpga/2000/36"


def test_carry_forward_inferred_edges_excluded_from_worklist():
    from raglex.citations import extract_document
    from raglex.storage import TextStore
    import tempfile, pathlib
    f = _facade()
    # a doc naming an Act then a bare provision → carry-forward (inferred) edge
    _doc(f, "case-cf", "Under the Freedom of Information Act 2000 the request failed. "
                        "Section 9999 was also engaged.")
    with f._open() as (cat, _rs, ts):
        extract_document(cat, ts, "case-cf")
    refs = f.unresolved_references(limit=500)
    raws = [r["raw"] for r in refs]
    # the inferred "Section 9999" guess must NOT appear as a harvest target
    assert not any("9999" in (r or "") for r in raws)
