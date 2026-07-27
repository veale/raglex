"""The resolver's de-dotted report-citation pass.

``fold()`` keeps abbreviation dots (edge ``raw_fold`` = "[1996] 3 s.c.r. 458") while
``fold_citation()`` strips them (alias key = "[1996] 3 scr 458"). The literal
``a.alias = raw_fold`` join never matched, so a report citation whose case was already
HELD stayed pending forever and kept reappearing on the Westlaw retrieval export. The
de-dotted pass closes that gap.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date

from raglex.config import Config
from raglex.core.models import (DocType, ExtractedVia, Record, RelationshipType,
                                ResolutionStatus, TypedRelation)
from raglex.core.text import fold, fold_citation
from raglex.facade import Facade


def _facade() -> Facade:
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def test_dedotted_alias_resolves_a_dotted_pending_edge():
    f = _facade()
    with f._open() as (cat, _rs, ts):
        held = Record(source="ca-caselaw", stable_id="ca-case/scc/athey",
                      doc_type=DocType.JUDGMENT, decision_date=date(1996, 1, 1),
                      text="held", raw_bytes=b"h", extracted_via=ExtractedVia.STRUCTURED)
        held.ensure_payload_hash()
        cat.upsert_document(held, text_path=str(ts.put(held.payload_hash, "held")))
        citer = Record(source="x", stable_id="c/1", doc_type=DocType.JUDGMENT,
                       decision_date=date(2020, 1, 1), text="cites it", raw_bytes=b"t",
                       extracted_via=ExtractedVia.STRUCTURED,
                       relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                                  raw_citation_string="[1996] 3 S.C.R. 458", dst_id=None,
                                  resolution_status=ResolutionStatus.PENDING)])
        citer.ensure_payload_hash()
        cat.upsert_document(citer, text_path=str(ts.put(citer.payload_hash, "t")))
        # the edge's raw_fold keeps the dots; the alias is stored de-dotted (as CanLII /
        # report-alias minting does) — the two never matched before the de-dotted pass
        assert fold("[1996] 3 S.C.R. 458") != fold_citation("[1996] 3 S.C.R. 458")
        cat.put_alias(fold_citation("[1996] 3 S.C.R. 458"), "ca-case/scc/athey", source="report")
        cat.conn.commit()
    r = f.resolve()
    assert r["resolved"] == 1 and r["still_pending"] == 0


def test_dotted_alias_still_resolves_via_the_literal_pass():
    # the ordinary path (alias stored dotted, matching raw_fold) must keep working
    f = _facade()
    with f._open() as (cat, _rs, ts):
        held = Record(source="uk-caselaw", stable_id="ukhl/1932/100",
                      doc_type=DocType.JUDGMENT, decision_date=date(1932, 1, 1),
                      text="held", raw_bytes=b"h", extracted_via=ExtractedVia.STRUCTURED)
        held.ensure_payload_hash()
        cat.upsert_document(held, text_path=str(ts.put(held.payload_hash, "held")))
        citer = Record(source="x", stable_id="c/2", doc_type=DocType.JUDGMENT,
                       decision_date=date(2020, 1, 1), text="cites", raw_bytes=b"t",
                       extracted_via=ExtractedVia.STRUCTURED,
                       relations=[TypedRelation(relationship_type=RelationshipType.MENTIONS,
                                  raw_citation_string="[1932] AC 562", dst_id=None,
                                  resolution_status=ResolutionStatus.PENDING)])
        citer.ensure_payload_hash()
        cat.upsert_document(citer, text_path=str(ts.put(citer.payload_hash, "t")))
        cat.put_alias(fold("[1932] AC 562"), "ukhl/1932/100", source="report")
        cat.conn.commit()
    assert f.resolve()["resolved"] == 1
