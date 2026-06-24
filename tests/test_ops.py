from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from raglex.core.models import DocType, ExtractedVia, Record
from raglex.ops import (
    AlertThresholds,
    LogNotifier,
    check_alerts,
    corpus_stats,
    pipeline_queues,
    push_alerts,
    source_dashboard,
)


def _doc(catalogue, stable_id, *, source="uk-grc", dt=DocType.JUDGMENT,
         has_text=True, via=ExtractedVia.STRUCTURED):
    rec = Record(
        source=source, stable_id=stable_id, doc_type=dt, decision_date=date(2024, 1, 1),
        text="some text" if has_text else None, raw_bytes=stable_id.encode(),
        extracted_via=via,
    )
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path="/x" if has_text else None)


# -- views ------------------------------------------------------------------
def test_corpus_stats_breakdowns(catalogue):
    _doc(catalogue, "a", dt=DocType.JUDGMENT)
    _doc(catalogue, "b", dt=DocType.OPINION, source="eu-cellar")
    st = corpus_stats(catalogue)
    assert st.total == 2
    assert st.by_doc_type == {"judgment": 1, "opinion": 1}
    assert st.by_source["uk-grc"] == 1 and st.by_source["eu-cellar"] == 1


def test_pipeline_queue_depths(catalogue):
    _doc(catalogue, "a", has_text=True)   # text, not embedded
    _doc(catalogue, "b", has_text=False)  # fetched, no text
    q = pipeline_queues(catalogue)
    assert q["fetched_no_text"] == 1
    assert q["text_not_embedded"] == 1


def test_source_dashboard_counts_docs(catalogue):
    _doc(catalogue, "a")
    catalogue.record_run("uk-grc", yielded=True, failed=False)
    dash = {s.key: s for s in source_dashboard(catalogue)}
    assert dash["uk-grc"].documents == 1
    assert dash["uk-grc"].consecutive_failures == 0


# -- alerts -----------------------------------------------------------------
def test_alert_on_consecutive_failures(catalogue):
    for _ in range(3):
        catalogue.record_run("flaky", yielded=False, failed=True)
    alerts = check_alerts(catalogue)
    assert any(a.code == "adapter_failing" and a.subject == "flaky" for a in alerts)


def test_alert_on_stale_source(catalogue):
    _doc(catalogue, "a", source="quiet")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    catalogue.conn.execute(
        "INSERT OR REPLACE INTO sources (key, last_yield_at, consecutive_failures) VALUES ('quiet', ?, 0)",
        (old,),
    )
    catalogue.conn.commit()
    alerts = check_alerts(catalogue, AlertThresholds(stale_days=14))
    assert any(a.code == "no_new_documents" and a.subject == "quiet" for a in alerts)


def test_no_alerts_when_healthy(catalogue):
    _doc(catalogue, "a")
    catalogue.record_run("uk-grc", yielded=True, failed=False)
    assert check_alerts(catalogue) == []


def test_push_alerts_uses_notifier(catalogue):
    for _ in range(3):
        catalogue.record_run("flaky", yielded=False, failed=True)
    captured: list[str] = []
    pushed = push_alerts(catalogue, LogNotifier(sink=captured.append))
    assert pushed and captured
    assert any("adapter_failing" in line for line in captured)
