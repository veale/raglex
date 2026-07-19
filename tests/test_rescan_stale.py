"""Staleness-scoped rescan: the ``stale_days`` filter that skips documents extracted in
the last N days, so restarting the server doesn't re-scan the whole corpus. Freshness is
read from the durable ``last_extracted_at`` stamp AND (retroactively) the newest citation
timestamp, so it works against an in-flight rescan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import make_record


def _text_doc(cat, rawstore, stable_id):
    rec = make_record(stable_id=stable_id, text="some text with the GDPR in it")
    cat.upsert_document(rec, raw_path=None, text_path="x")
    # upsert marks has_text from the record; ensure the flag is set for the scan
    cat.conn.execute("UPDATE documents SET has_text = 1 WHERE stable_id = ?", (stable_id,))
    cat.conn.commit()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_stale_days_uses_last_extracted_at_stamp(catalogue, rawstore):
    _text_doc(catalogue, rawstore, "a")
    _text_doc(catalogue, rawstore, "b")
    # a was extracted just now; b was never extracted
    catalogue.mark_extracted("a")
    stale = catalogue.text_document_ids(stale_days=7)
    assert stale == ["b"]                       # a is fresh (skipped), b is stale
    # with no window, both are candidates
    assert set(catalogue.text_document_ids()) == {"a", "b"}


def test_stale_days_is_retroactive_via_citation_timestamps(catalogue, rawstore):
    # No last_extracted_at stamp at all (pre-migration / in-flight run): freshness must
    # still be read from citations.created_at, which the running rescan is populating.
    _text_doc(catalogue, rawstore, "recent")
    _text_doc(catalogue, rawstore, "old")
    # a citation created just now → "recent" counts as freshly scanned
    catalogue.add_citations("recent", [{"raw": "GDPR", "method": "eu_named"}])
    # an OLD citation row (30 days ago) → "old" is stale
    catalogue.conn.execute(
        "INSERT INTO citations (src_id, raw, method, created_at) VALUES (?,?,?,?)",
        ("old", "GDPR", "eu_named", _iso_days_ago(30)))
    catalogue.conn.commit()

    stale = catalogue.text_document_ids(stale_days=7)
    assert stale == ["old"]                     # recent citation → skipped; old → rescanned


def test_stale_days_stamp_and_citation_signals_combine(catalogue, rawstore):
    # A doc fresh by EITHER signal is skipped.
    _text_doc(catalogue, rawstore, "stamped")
    _text_doc(catalogue, rawstore, "cited")
    _text_doc(catalogue, rawstore, "cold")
    catalogue.mark_extracted("stamped")                       # fresh by stamp
    catalogue.add_citations("cited", [{"raw": "GDPR"}])       # fresh by citation
    # "cold" has neither → stale
    assert catalogue.text_document_ids(stale_days=7) == ["cold"]


def test_mark_extracted_sets_the_column(catalogue, rawstore):
    _text_doc(catalogue, rawstore, "a")
    catalogue.mark_extracted("a")
    row = catalogue.get_document("a")
    assert row["last_extracted_at"] is not None


def test_never_extracted_documents_are_ordered_first(catalogue, rawstore):
    # "all jobs should start scanning with stuff that has never scanned" — the id
    # stream must put never-extracted docs ahead of already-extracted ones, so a
    # time-boxed or interrupted run always makes progress on the backlog first.
    for sid in ("z-old", "m-older", "a-never"):
        _text_doc(catalogue, rawstore, sid)
    # a-never has no stamp; the other two were extracted, at different times
    catalogue.mark_extracted("z-old")
    catalogue.conn.execute(
        "UPDATE documents SET last_extracted_at = ? WHERE stable_id = 'm-older'",
        (_iso_days_ago(30),))
    catalogue.conn.commit()

    ids = catalogue.text_document_ids()
    assert ids[0] == "a-never"                    # never-scanned leads
    assert ids.index("m-older") < ids.index("z-old")  # then least-recently-scanned
