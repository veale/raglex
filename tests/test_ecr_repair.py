"""European Court Reports alias repair — re-chaining dead ``ECR → CELEX`` aliases to the
held judgment's ECLI, guarded by the report series so an ``ECR II-`` (General Court) can
never resolve to an ``ECLI:EU:C:`` (Court of Justice) decision."""

from __future__ import annotations

import pytest

from raglex.config import Config
from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
from raglex.facade import Facade, _ecr_series_ok


def test_ecr_series_guard():
    # II- is the General Court (EU:T) / Civil Service Tribunal (EU:F); I- and bare are EU:C
    assert _ecr_series_ok("[2000] ecr ii-491", "ECLI:EU:T:2000:77")
    assert not _ecr_series_ok("[2000] ecr ii-491", "ECLI:EU:C:1997:51")   # the mis-chain
    assert _ecr_series_ok("[2005] ecr i-7879", "ECLI:EU:C:2005:461")
    assert not _ecr_series_ok("[2005] ecr i-7879", "ECLI:EU:T:2005:1")
    assert _ecr_series_ok("[1974] ecr 837", "ECLI:EU:C:1974:114")         # no series → EU:C
    assert _ecr_series_ok("[2000] ecr ii-491", "61995TJ0025")             # non-ECLI target passes


@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        topic_threshold=3.0, embed_provider="local-hashing", embed_model=None,
    ))


def _held(cat, ecli: str):
    cat.upsert_document(Record(
        source="eu-cellar", stable_id=ecli, doc_type=DocType.JUDGMENT, title="held",
        raw_bytes=b"x", raw_ext="xml", payload_hash=ecli, text="body",
        extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER))


def test_repair_rechains_a_dead_ecr_alias_to_the_held_ecli(facade):
    with facade._open() as (cat, _rs, _ts):
        _held(cat, "ECLI:EU:T:2010:60")
        # a dead ECR alias pointing at a bare CELEX, plus the CELEX→ECLI hop
        cat.put_alias("[2010] ecr ii-491", "62008TJ0035", source="eu-report")
        cat.put_alias("62008tj0035", "ECLI:EU:T:2010:60", source="celex-ecli")
        cat.commit()

    dry = facade.repair_ecr_aliases(apply=False)
    assert {"alias": "[2010] ecr ii-491", "was": "62008TJ0035", "now": "ECLI:EU:T:2010:60"} in dry["changes"]

    facade.repair_ecr_aliases(apply=True)
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_alias("[2010] ecr ii-491") == "ECLI:EU:T:2010:60"


def test_repair_leaves_a_series_mismatch_dead(facade):
    # the cement-cartel shape: ECR II- chains (via a member case number) to an EU:C ECLI —
    # the guard must refuse to resolve it to that wrong decision.
    with facade._open() as (cat, _rs, _ts):
        _held(cat, "ECLI:EU:C:1997:51")
        cat.put_alias("[2000] ecr ii-491", "61995TJ0071", source="eu-report")
        cat.put_alias("61995tj0071", "ECLI:EU:C:1997:51", source="celex-ecli")
        cat.commit()

    res = facade.repair_ecr_aliases(apply=True)
    assert res["repaired"] == 0 and res["skipped_series"] == 1
    with facade._open() as (cat, _rs, _ts):
        assert cat.get_alias("[2000] ecr ii-491") == "61995TJ0071"   # left dead, not mis-resolved
