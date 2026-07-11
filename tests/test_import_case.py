"""Robust judgment import (§5b/§1.9) — key by the case's own neutral citation, alias every
form it's cited by, extract clean text. The failure it prevents: a manual upload becoming
an opaque, unlinked commentary blob with raw RTF markup as its "text"."""

from __future__ import annotations

import tempfile

from raglex.config import Config
from raglex.core.models import (
    DocType, ExtractedVia, Record, RelationshipType, ResolutionStatus, TypedRelation,
)
from raglex.extraction import extract_bytes
from raglex.facade import Facade


def _facade() -> Facade:
    import os
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def test_rtf_is_de_rtfed_not_stored_as_markup():
    rtf = rb"{\rtf1\ansi\ansicpg1252 The court held \b clearly\b0  in favour.}"
    out = extract_bytes(rtf, ext="rtf")
    assert out.engine == "striprtf"
    assert "rtf1" not in out.text and "held" in out.text and "\\b" not in out.text


HEADER = (
    "James Killock and Michael Veale v ICO\n[2021] UKUT 299 (AAC)\n\n"
    "IN THE UPPER TRIBUNAL\nBefore: Mrs Justice Farbey\n"
    "This appeal concerns the balancing of privacy... " + "text " * 50
)


def _txt(text: str) -> bytes:
    return text.encode()


def test_import_case_keys_by_detected_neutral_citation_and_aliases_report():
    f = _facade()
    # a document already citing the case by its report + chamber-less forms
    with f._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(source="uk-caselaw", stable_id="citing-1", doc_type=DocType.JUDGMENT,
            relations=[
                TypedRelation(relationship_type=RelationshipType.MENTIONS, raw_citation_string="[2022] 1 WLR 2241",
                              extracted_via=ExtractedVia.REGEX, resolution_status=ResolutionStatus.PENDING),
                TypedRelation(relationship_type=RelationshipType.MENTIONS, raw_citation_string="[2021] UKUT 299 (AAC)",
                              extracted_via=ExtractedVia.REGEX, resolution_status=ResolutionStatus.PENDING),
            ]))

    res = f.import_case(data=_txt(HEADER), filename="299.txt", ref="[2022] 1 WLR 2241")
    assert res["stable_id"] == "ukut/aac/2021/299"
    assert res["detected_citation"] == "ukut/aac/2021/299"

    with f._open() as (cat, _rs, _ts):
        doc = cat.get_document("ukut/aac/2021/299")
        assert doc and doc["doc_type"] == "judgment" and doc["added_by"] == "user"
        assert cat.get_alias("[2022] 1 wlr 2241") == "ukut/aac/2021/299"     # report form
        assert cat.get_alias("ukut/2021/299") == "ukut/aac/2021/299"          # chamber-less
        # both pending citations to the case now resolve to the imported judgment
        rels = cat.relations_for("citing-1")
        assert all(r["resolution_status"] == "resolved" for r in rels)
        assert all(r["dst_id"] == "ukut/aac/2021/299" for r in rels)


def test_import_case_falls_back_when_no_neutral_citation_detected():
    f = _facade()
    res = f.import_case(data=_txt("Some commentary about privacy law. " * 20),
                        filename="note.txt", ref="[2022] 1 WLR 2241")
    # no neutral citation in the text → keyed by the ref's candidate (still linked)
    assert res["detected_citation"] is None and res["stable_id"]
