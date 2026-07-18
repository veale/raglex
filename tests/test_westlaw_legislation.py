"""Westlaw legislation export → a clean, section-segmented Act keyed by its
legislation.gov.uk id, so hanging statute references (incl. pinpoints) resolve to it.

The fixture reproduces the Westlaw shape: one item per provision, each wrapped in the same
running-header + copyright + OGL boilerplate, repeated for every section — the "gunk" the
parser strips."""

from __future__ import annotations

import base64

import pytest

from raglex.adapters.westlaw_legislation import parse_westlaw_legislation
from raglex.config import Config
from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
from raglex.facade import Facade


def _rtf(body: str) -> bytes:
    doc = (r"{\rtf1\ansi\ansicpg1252"
           r"{\colortbl;\red0\green0\blue0;}"
           r"{\fonttbl{\f0 Arial;}}"
           r"{\*\generator Apache XML Graphics RTF Library;}"
           r"{\b0\f0\fs20 " + body + r"}}")
    return doc.encode("latin-1")


HEADER = r"Test Interpretation Act 1889 c. 5"


def _item(*lines: str) -> str:
    """One Westlaw item: the running header, then its lines, each ending a paragraph."""
    return r"\par ".join((HEADER, "© 2026 Thomson Reuters.", "For educational use only", *lines)) + r"\par "


def _act_rtf() -> bytes:
    body = (
        _item("Preamble", "As Originally Enacted",
              "The text of this legislation is as originally enacted.",
              "An Act to test the parser.", "[30th August 1889]",
              "Contains public sector information licensed under the Open Government Licence v3.0.")
        + _item("s. 1 Short rule.", "As Originally Enacted",
                "The text of this legislation is as originally enacted.",
                r"1.\'97 Short rule.", "(1.) The first rule applies.",
                "Rules. > s. 1 Short rule.",
                "Contains public sector information licensed under the Open Government Licence v3.0.")
        + _item("s. 38 Effect of repeal in future Acts.", "As Originally Enacted",
                "The text of this legislation is as originally enacted.",
                r"38.\'97 Effect of repeal in future Acts.",
                "(1.) Where this Act repeals and re-enacts a provision, references are construed accordingly.",
                "Repeals. > s. 38 Effect of repeal in future Acts.",
                "Contains public sector information licensed under the Open Government Licence v3.0.")
    )
    return _rtf(body)


def test_parse_produces_a_ukpga_id_segmented_by_section():
    p = parse_westlaw_legislation(_act_rtf())
    assert p is not None
    assert p.title == "Test Interpretation Act 1889" and p.chapter == "5" and p.year == 1889
    assert p.stable_id == "ukpga/1889/5"                     # legislation.gov.uk id
    assert p.version_note == "As Originally Enacted"
    assert p.long_title == "An Act to test the parser."
    assert p.enacted_date is not None and p.enacted_date.month == 8
    assert [s.label for s in p.segments] == [
        "Preamble", "s. 1 Short rule.", "s. 38 Effect of repeal in future Acts."]
    # boilerplate is stripped from the body
    assert "Thomson Reuters" not in p.text and "Open Government Licence" not in p.text
    assert "For educational use only" not in p.text
    # the provision's duplicated "38.— …" opener is dropped (the label carries it)
    s38 = p.text[[s for s in p.segments if s.label.startswith("s. 38")][0].char_start:]
    assert s38.startswith("s. 38 Effect of repeal") and "references are construed" in s38
    assert p.crossheadings.get("s. 1 Short rule.") == "Rules."


def test_a_case_export_is_not_misrouted_as_legislation():
    # a judgment export carries "Judicial Consideration" / "Where Reported", not OGL
    from tests.test_westlaw_rtf import _digest_case
    assert parse_westlaw_legislation(_digest_case()) is None


def test_rejects_non_rtf():
    assert parse_westlaw_legislation(b"%PDF-1.7 not rtf") is None


@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        topic_threshold=3.0, embed_provider="local-hashing", embed_model=None,
    ))


def test_import_resolves_a_hanging_pinpoint_statute_reference(facade):
    body = "The appeal turns on section 38 of the Test Interpretation Act 1889."
    with facade._open() as (cat, _rs, ts):
        ph = "cafe01"
        cat.upsert_document(Record(
            source="uk-caselaw", stable_id="ewhc/ch/1990/1", doc_type=DocType.JUDGMENT,
            title="Citing", text=body, raw_bytes=body.encode(), raw_ext="txt", payload_hash=ph,
            extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER),
            text_path=str(ts.put(ph, body)))
        cat.commit()
    facade.extract_citations(stable_id="ewhc/ch/1990/1")

    # the hanging-edge upload path: satisfy the reference with the .doc export
    res = facade.resolve_reference(
        ref="the Test Interpretation Act 1889", filename="act.doc",
        content_base64=base64.b64encode(_act_rtf()).decode())
    assert res["target"] == "ukpga/1889/5"

    with facade._open() as (cat, _rs, _ts):
        doc = cat.get_document("ukpga/1889/5")
        assert doc is not None and doc["doc_type"] == "legislation" and doc["source"] == "uk-legislation"
        rels = [dict(r) for r in cat.relations_for("ewhc/ch/1990/1")]
        edge = next(r for r in rels if "section 38" in r["raw_citation_string"])
        assert edge["dst_id"] == "ukpga/1889/5" and edge["resolution_status"] == "resolved"
        assert edge["dst_anchor"] == "s. 38"                 # pinpoint preserved
