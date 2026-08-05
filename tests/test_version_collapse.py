"""Searching a versioned corpus: collapsing an instrument's point-in-time expressions to
the one a reader wants, ranking a query by relevance rather than by date, and re-pointing
a published edition onto a newer consolidation. Network-free."""

from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade
from raglex.storage import Catalogue


# ── the id → (instrument, version) split ─────────────────────────────────────
def test_version_base_and_date_reads_both_series_shapes():
    """Two conventions in one corpus: ``@YYYY-MM-DD`` (Dutch, assimilated UK) and
    CELLAR's ``-YYYYMMDD``."""
    assert Catalogue.version_base_and_date("BWBR0006622@2013-08-31") == (
        "BWBR0006622", "2013-08-31")
    assert Catalogue.version_base_and_date("02002L0058-20091219") == (
        "02002L0058", "2009-12-19")
    assert Catalogue.version_base_and_date("ukpga/2017/30") == ("ukpga/2017/30", None)
    # a bare year-like tail is not a version — "ukpga/2017/30" must not lose its number
    assert Catalogue.version_base_and_date("uksi/2018/342") == ("uksi/2018/342", None)


class _Row(dict):
    def keys(self):  # noqa: D102 — sqlite3.Row-alike for the helper's has_text probe
        return dict.keys(self)


def _v(stable_id, has_text=1):
    return _Row(stable_id=stable_id, has_text=has_text)


# ── the collapse ─────────────────────────────────────────────────────────────
def test_collapse_keeps_the_newest_readable_version_in_force():
    """A search for "Wegenverkeerswet 1994" returned eight rows with identical titles —
    eight of that law's 182 held snapshots — and never the law itself."""
    rows = [_v("BWBR0006622@2002-01-01"), _v("BWBR0006622@2013-08-31"),
            _v("BWBR0006622@2005-03-01")]
    got = Catalogue.collapse_version_rows(rows, on_date="2026-08-05")
    assert [r["stable_id"] for r in got] == ["BWBR0006622@2013-08-31"]


def test_collapse_skips_a_textless_snapshot():
    """A version can be held as a metadata record with no text at all — the DSA's sole
    consolidation exists in eight languages, none of them English. Returning it would
    send every read to a blank page."""
    rows = [_v("x/1@2020-01-01"), _v("x/1@2024-01-01", has_text=0)]
    got = Catalogue.collapse_version_rows(rows, on_date="2026-08-05")
    assert [r["stable_id"] for r in got] == ["x/1@2020-01-01"]


def test_collapse_does_not_jump_to_a_future_consolidation():
    rows = [_v("x/1@2024-01-01"), _v("x/1@2027-01-01")]
    got = Catalogue.collapse_version_rows(rows, on_date="2026-08-05")
    assert [r["stable_id"] for r in got] == ["x/1@2024-01-01"]


def test_collapse_falls_back_to_the_base_act():
    rows = [_v("ukpga/2017/30"), _v("ukpga/2017/30@2030-01-01")]
    got = Catalogue.collapse_version_rows(rows, on_date="2026-08-05")
    assert [r["stable_id"] for r in got] == ["ukpga/2017/30"]


def test_collapse_keeps_distinct_instruments_and_their_order():
    rows = [_v("a/1@2020-01-01"), _v("b/2"), _v("a/1@2024-01-01"), _v("c/3")]
    got = [r["stable_id"] for r in Catalogue.collapse_version_rows(rows)]
    # one row per instrument, each in the position of its best-ranked member
    assert got == ["a/1@2024-01-01", "b/2", "c/3"]


# ── ranking ──────────────────────────────────────────────────────────────────
def _facade() -> Facade:
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _law(f: Facade, stable_id: str, title: str, when: date, *, cited: int = 0) -> None:
    rec = Record(source="uk-legislation", stable_id=stable_id,
                 doc_type=DocType.LEGISLATION, title=title, decision_date=when,
                 language="en", text=f"{title} body text", raw_bytes=stable_id.encode(),
                 raw_ext="xml", extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    with f._open() as (cat, _rs, ts):
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        if cited:
            cat.conn.execute(
                "INSERT INTO citation_counts (candidate_id, entity_kind, occurrences, "
                "documents, rebuilt_at) VALUES (?, 'act', ?, ?, '2026-01-01')",
                (stable_id, cited, cited))
            cat.conn.commit()


def test_the_act_outranks_the_commencement_orders_made_under_it():
    """Date order put ten commencement orders above the Act, so at the eight rows an
    autocomplete shows, "Digital Economy Act 2017" — an exact title match — did not
    appear at all."""
    f = _facade()
    _law(f, "ukpga/2017/30", "Digital Economy Act 2017", date(2017, 4, 27))
    for n, (sid, year) in enumerate([
            ("uksi/2026/126", 2026), ("uksi/2021/1170", 2021), ("uksi/2020/70", 2020),
            ("uksi/2018/382", 2018), ("uksi/2018/624", 2018), ("uksi/2018/690", 2018),
            ("uksi/2017/1136", 2017), ("uksi/2018/342", 2018), ("wsi/2018/342", 2018)]):
        _law(f, sid, f"The Digital Economy Act 2017 (Commencement No. {n + 1}) "
                     f"Regulations {year}", date(year, 6, 1))

    hits = f.list_documents(query="Digital Economy Act 2017", limit=8)
    assert hits[0]["stable_id"] == "ukpga/2017/30"


def test_a_tie_breaks_on_the_cached_citation_count():
    """Where relevance cannot separate two documents, how much the corpus cites them
    does — read from the citation_counts roll-up, never counted live."""
    f = _facade()
    _law(f, "ukpga/1998/29", "Data Protection Act", date(1998, 7, 16), cited=40)
    _law(f, "ukpga/2018/12", "Data Protection Act", date(2018, 5, 23), cited=9000)
    hits = f.list_documents(query="Data Protection Act", limit=5)
    assert [h["stable_id"] for h in hits] == ["ukpga/2018/12", "ukpga/1998/29"]


def test_browsing_without_a_query_keeps_date_order_and_every_version():
    """The Corpus browser is how the versions themselves are reached, and its ordering
    is served straight off an index at 5M rows — the ranking must not touch it."""
    f = _facade()
    _law(f, "x/1@2020-01-01", "A law", date(2020, 1, 1))
    _law(f, "x/1@2024-01-01", "A law", date(2024, 1, 1))
    rows = f.list_documents(source="uk-legislation", limit=10)
    assert [r["stable_id"] for r in rows] == ["x/1@2024-01-01", "x/1@2020-01-01"]


def test_searching_collapses_the_versions_browsing_shows():
    f = _facade()
    _law(f, "x/1@2020-01-01", "Wegenverkeerswet 1994", date(2020, 1, 1))
    _law(f, "x/1@2024-01-01", "Wegenverkeerswet 1994", date(2024, 1, 1))
    hits = f.list_documents(query="Wegenverkeerswet", limit=8)
    assert [h["stable_id"] for h in hits] == ["x/1@2024-01-01"]


# ── keeping a published edition current ──────────────────────────────────────
def test_latest_readable_version_follows_an_id_shaped_series():
    """Only the CELLAR consolidations carry ``consolidates`` edges; the Dutch and
    assimilated ``@date`` series carry none, so an edge-only lookup reported "already
    current" for every one of them."""
    f = _facade()
    _law(f, "european/regulation/2016/0679", "UK GDPR", date(2021, 1, 1))
    _law(f, "european/regulation/2016/0679@2024-01-01", "UK GDPR", date(2024, 1, 1))
    with f._open() as (cat, _rs, _ts):
        assert cat.latest_readable_version("european/regulation/2016/0679") == (
            "european/regulation/2016/0679@2024-01-01")
        # already current → nothing to do
        assert cat.latest_readable_version(
            "european/regulation/2016/0679@2024-01-01") is None


def test_a_build_repoints_an_edition_onto_the_newer_consolidation():
    """An edition names one expression of a law, and laws are consolidated again. Left
    alone it publishes the same superseded text for ever."""
    from raglex.static_bundle import _repoint_to_current_versions

    f = _facade()
    _law(f, "x/1@2020-01-01", "A law (2020 text)", date(2020, 1, 1))
    _law(f, "x/1@2024-01-01", "A law (2024 text)", date(2024, 1, 1))
    items = [{"stable_id": "x/1@2020-01-01", "title": "A law (2020 text)",
              "short": "AL", "note": "mine"}]
    moves = _repoint_to_current_versions(f, items)
    assert moves == [{"from": "x/1@2020-01-01", "to": "x/1@2024-01-01",
                      "title": "A law (2020 text)"}]
    assert items[0]["stable_id"] == "x/1@2024-01-01"
    assert items[0]["title"] == "A law (2024 text)"   # the date is in the name
    assert items[0]["short"] == "AL" and items[0]["note"] == "mine"   # the operator's
    # and it is idempotent — a second build moves nothing
    assert _repoint_to_current_versions(f, items) == []


# ── assimilated law held twice ───────────────────────────────────────────────
def test_merge_assimilated_duplicates_folds_the_serving_form_onto_the_canonical_one():
    """legislation.gov.uk serves an assimilated instrument on two paths and the corpus
    took both as identities: 4,171 instruments stored twice, the UK GDPR among them,
    with 40,042 citations on the copy nothing else pointed at."""
    f = _facade()
    _law(f, "eur/2016/679", "Assimilated Regulation (EU) 2016/679", date(2021, 1, 1))
    _law(f, "european/regulation/2016/0679", "Assimilated Regulation (EU) 2016/679",
         date(2021, 1, 1))
    _law(f, "eur/2008/1272", "Assimilated Regulation (EC) 1272/2008", date(2021, 1, 1))

    plan = f.merge_assimilated_duplicates()
    kinds = {c["old"]: c["kind"] for c in plan["changes"]}
    assert kinds["eur/2016/679"] == "merge"      # the canonical node already exists
    assert kinds["eur/2008/1272"] == "rename"    # it does not — so it just moves

    st = f.merge_assimilated_duplicates(apply=True)
    assert st["merged"] == 1 and st["renamed"] == 1
    with f._open() as (cat, _rs, _ts):
        assert cat.get_document("eur/2016/679") is None
        assert cat.get_document("european/regulation/2016/0679") is not None
        assert cat.get_document("european/regulation/2008/1272") is not None
    assert f.merge_assimilated_duplicates(apply=True)["changes"] == []   # idempotent


def test_a_dated_assimilated_copy_keeps_its_date_when_merged():
    f = _facade()
    _law(f, "eur/2016/679@2024-01-01", "Assimilated Regulation", date(2024, 1, 1))
    f.merge_assimilated_duplicates(apply=True)
    with f._open() as (cat, _rs, _ts):
        assert cat.get_document(
            "european/regulation/2016/0679@2024-01-01") is not None


def test_the_harvester_no_longer_mints_the_serving_form():
    from raglex.adapters.uk_legislation import _canonical_leg_id

    assert _canonical_leg_id("eur/2016/679") == "european/regulation/2016/0679"
    assert _canonical_leg_id("eudr/2000/60") == "european/directive/2000/0060"
    assert _canonical_leg_id("ukpga/2018/12") == "ukpga/2018/12"   # untouched


def test_a_retired_id_follows_its_merge_to_the_survivor():
    """Merging the assimilated duplicates left every stored reference to eur/2016/679
    pointing at a document that no longer exists — including a configured static
    edition, which failed its build with "document not found" instead of following the
    move the merge had already recorded as an alias."""
    from raglex.core.text import fold

    f = _facade()
    _law(f, "european/regulation/2016/0679", "UK GDPR", date(2021, 1, 1))
    _law(f, "european/regulation/2016/0679@2024-01-01", "UK GDPR", date(2024, 1, 1))
    with f._open() as (cat, _rs, _ts):
        cat.put_alias(fold("eur/2016/679"), "european/regulation/2016/0679",
                      source="assimilated-merge")
        # the retired id resolves, and lands on the version a reader would open
        assert cat.latest_readable_version("eur/2016/679") == (
            "european/regulation/2016/0679@2024-01-01")
        # an id that was never held and has no alias is still unknown
        assert cat.latest_readable_version("eur/1999/1") is None


def test_a_build_repoints_an_edition_off_a_retired_id():
    from raglex.core.text import fold
    from raglex.static_bundle import _repoint_to_current_versions

    f = _facade()
    _law(f, "european/regulation/2016/0679", "Assimilated Regulation (EU) 2016/679",
         date(2021, 1, 1))
    with f._open() as (cat, _rs, _ts):
        cat.put_alias(fold("eur/2016/679"), "european/regulation/2016/0679",
                      source="assimilated-merge")
    items = [{"stable_id": "eur/2016/679", "title": "UK GDPR", "short": "UK GDPR",
              "note": ""}]
    moves = _repoint_to_current_versions(f, items)
    assert moves and moves[0]["to"] == "european/regulation/2016/0679"
    assert items[0]["stable_id"] == "european/regulation/2016/0679"
    assert items[0]["short"] == "UK GDPR"     # the operator's own naming is kept
