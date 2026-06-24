"""Snowball / radiate seed-resolution (the network-free parts): seeds by rule —
cites-X (one and two hops), tag, and explicit ids."""

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


def _doc(f, sid, text, **kw):
    with f._open() as (cat, _rs, ts):
        r = Record(source="x", stable_id=sid, doc_type=kw.get("doc_type", DocType.JUDGMENT),
                   decision_date=date(2024, 1, 1), text=text, raw_bytes=text.encode(),
                   extracted_via=ExtractedVia.STRUCTURED)
        r.ensure_payload_hash()
        cat.upsert_document(r, text_path=str(ts.put(r.payload_hash, text)))


def test_seed_rule_cites_target():
    f = _facade()
    _doc(f, "32016R0679", "gdpr", doc_type=DocType.LEGISLATION)
    _doc(f, "caseA", "applying Article 17 of Regulation (EU) 2016/679")
    _doc(f, "caseB", "a contract dispute")  # cites nothing relevant
    f.extract_citations()
    res = f.radiate(seed_rule={"cites": "32016R0679"}, dry_run=True)
    assert res["seeds"] == ["caseA"] and res["seed_count"] == 1


def test_seed_rule_cites_two_hops():
    """'cases citing any case which cites the GDPR' — caseB cites caseA cites GDPR."""
    f = _facade()
    _doc(f, "32016R0679", "gdpr", doc_type=DocType.LEGISLATION)
    _doc(f, "ECLI:XX:A:2020:1", "applying Article 17 of Regulation (EU) 2016/679",
         doc_type=DocType.JUDGMENT)
    _doc(f, "caseB", "following ECLI:XX:A:2020:1")
    f.extract_citations()
    one = set(f.radiate(seed_rule={"cites": "32016R0679"}, dry_run=True)["seeds"])
    two = set(f.radiate(seed_rule={"cites": "32016R0679", "hops": 2}, dry_run=True)["seeds"])
    assert "ECLI:XX:A:2020:1" in one and "caseB" not in one  # 1 hop: direct citers
    assert "caseB" in two  # 2 hops: citers of citers


def test_seed_rule_tag_and_explicit():
    f = _facade()
    _doc(f, "d1", "x")
    _doc(f, "d2", "y")
    f.tag(doc_id="d1", tag="my-area")
    assert set(f.radiate(seed_rule={"tag": "my-area"}, dry_run=True)["seeds"]) == {"d1"}
    assert set(f.radiate(seeds=["d2"], dry_run=True)["seeds"]) == {"d2"}
