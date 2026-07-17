"""Watch system (saved harvest plans, §5a) — CRUD, source-capability catalog,
keyword seed filtering, and due-detection. Network-free (no real harvest)."""

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


def _doc(f, sid, text, source="uk-grc"):
    with f._open() as (cat, _rs, ts):
        r = Record(source=source, stable_id=sid, doc_type=DocType.JUDGMENT,
                   decision_date=date(2024, 1, 1), text=text, raw_bytes=text.encode(),
                   title=text[:40], extracted_via=ExtractedVia.STRUCTURED)
        r.ensure_payload_hash()
        cat.upsert_document(r, text_path=str(ts.put(r.payload_hash, text)))


def test_source_catalog_exposes_keyword_capability():
    cat = {s["key"]: s for s in _facade().source_catalog()}
    assert cat["uk-grc"]["keyword_search"] is True   # Find Case Law API search
    assert cat["nl-rechtspraak"]["keyword_search"] is False  # post-filter only
    assert any(o["name"] == "query" for o in cat["uk-grc"]["options"])


def test_source_catalog_exposes_capability_flags():
    cat = {s["key"]: s for s in _facade().source_catalog()}
    assert cat["uk-caselaw"]["can_gap_scan"] is True
    assert cat["uk-caselaw"]["can_discover_citing"] is True
    assert cat["uk-caselaw"]["can_incremental"] is True
    assert cat["eu-legislation"]["can_gap_scan"] is False      # by-id, not sequential
    assert cat["eu-legislation"]["can_incremental"] is False   # no moving feed


def test_watch_crud_roundtrip():
    f = _facade()
    w = f.create_watch(name="DP", spec={"source": "uk-grc", "keywords": ["data"], "degrees": 1},
                       cadence_minutes=120)
    wid = w["watch_id"]
    assert w["name"] == "DP" and w["spec"]["keywords"] == ["data"] and w["enabled"]
    f.update_watch(watch_id=wid, enabled=False, cadence_minutes=60)
    got = next(x for x in f.list_watches() if x["watch_id"] == wid)
    assert got["enabled"] is False and got["cadence_minutes"] == 60
    f.delete_watch(watch_id=wid)
    assert all(x["watch_id"] != wid for x in f.list_watches())


def test_keyword_seed_docs_filters_by_text():
    f = _facade()
    _doc(f, "g1", "An appeal about personal data and the GDPR")
    _doc(f, "g2", "A dispute about parking fines")
    hits = f._keyword_seed_docs("uk-grc", ["personal data"], limit=10)
    assert hits == ["g1"]
    # no keywords → all of the source's docs
    assert set(f._keyword_seed_docs("uk-grc", [], limit=10)) == {"g1", "g2"}


def test_discover_citing_picks_source_and_query(monkeypatch):
    """auto-routes a CELEX to CELLAR and a UK citation to Find Case Law search,
    deriving the search string; returns the newly-harvested ids (harvest mocked)."""
    f = _facade()
    calls = {}

    def fake_harvest(source, *, options=None, **kw):
        calls["source"] = source
        calls["options"] = options or {}
        # simulate the harvest landing one new doc from that source
        _doc(f, f"{source}-new", "judgment citing the target", source=source)
        return {"stored": 1}

    monkeypatch.setattr(f, "harvest", fake_harvest)
    # a UK landmark → its NEUTRAL CITATION becomes the FCL query (cases cite by citation,
    # not by name — searching the title would only find the case itself)
    _doc(f, "uksc/2014/38", "Kennedy v Charity Commission", source="uk-caselaw")
    r = f.discover_citing(target="uksc/2014/38")
    assert calls["source"] == "uk-caselaw"
    assert calls["options"]["query"] == "[2014] UKSC 38"
    assert "uk-caselaw-new" in r["discovered"] and r["count"] == 1

    # a CELEX routes to CELLAR with legislation_celex
    calls.clear()
    r2 = f.discover_citing(target="32016R0679")
    assert calls["source"] == "eu-cellar" and calls["options"]["legislation_celex"] == "32016R0679"
    assert r2["via"] == "eu-cellar"


def test_tick_runs_only_due_watches():
    f = _facade()
    # a watch with no source and a no-op seed rule (no network): degrees 0
    w = f.create_watch(name="noop", spec={"seed_rule": {"tag": "nope"}, "degrees": 0},
                       cadence_minutes=60)
    # last_run_at is None → due → runs once
    res = f.tick_watches()
    assert res["ran"] == 1
    # immediately after, not due again
    assert f.tick_watches()["ran"] == 0


def test_keyword_seed_docs_unquotes_phrase_keywords():
    """A phrase keyword quoted for the source API ('"data protection"') must still
    post-filter — the quote characters never appear in a document, so the quoted
    form used to match nothing and the watch silently seeded zero documents."""
    f = _facade()
    _doc(f, "g1", "An appeal about data protection and the GDPR")
    assert f._keyword_seed_docs("uk-grc", ['"data protection"'], limit=10) == ["g1"]


def test_run_watch_uses_per_watch_watermark(monkeypatch):
    """Each watch keeps its own feed cursor — two watches on one source with different
    queries must not share (and blind each other via) the source-wide watermark."""
    f = _facade()
    w = f.create_watch(name="DP", spec={"source": "uk-grc", "keywords": ["data"],
                                        "degrees": 0, "max_pages": 3})
    calls: list[dict] = []

    def fake_harvest(source, **kw):
        calls.append({"source": source, **kw})
        return {"stored": 0}

    monkeypatch.setattr(f, "harvest", fake_harvest)
    f.run_watch(watch_id=w["watch_id"])
    wm_key = f"watch:{w['watch_id']}:uk-grc"
    assert calls[0]["watermark_key"] == wm_key
    # no cursor yet → the first run is bounded by the watch's own max_pages
    assert calls[0]["max_pages"] == 3
    # once a cursor exists, the cursor bounds the crawl, not the page cap
    with f._open() as (cat, _rs, _ts):
        cat.set_watermark(wm_key, "2026-01-01T00:00:00+00:00")
    f.run_watch(watch_id=w["watch_id"])
    assert calls[1]["max_pages"] == 40
