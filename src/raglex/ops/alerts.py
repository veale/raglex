"""Alerting (§8) — push, not pull: the real safeguard for a solo operator.

A dashboard only helps when you're looking at it; for a one-person system the
dominant failure mode is **silent** failure. This computes alerts and pushes them
to a pluggable notifier (Slack/Discord/email later; a logging notifier now) on:

- an adapter failing 3 times consecutively;
- a source yielding no new documents in X days — the key signal that an
  HTML-fallback site changed structure and the parser is silently returning
  nothing (per-source X, since "genuinely quiet" sources differ);
- a processing queue backing up past a threshold (ingestion outrunning
  processing);
- an LLM-extraction spike for a source that normally parses structurally
  (upstream format drift).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..storage.catalogue import Catalogue
from .views import pipeline_queues, source_dashboard

log = logging.getLogger("raglex.ops.alerts")

# severities
WARNING = "warning"
CRITICAL = "critical"


@dataclass(slots=True)
class Alert:
    code: str
    severity: str
    subject: str  # the source key or queue name
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "subject": self.subject, "message": self.message}


@dataclass(slots=True)
class AlertThresholds:
    consecutive_failures: int = 3
    stale_days: int = 14  # default "no new docs in X days"; tune per source
    queue_backlog: int = 1000
    llm_extract_ratio: float = 0.5


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def check_alerts(catalogue: Catalogue, thresholds: AlertThresholds | None = None) -> list[Alert]:
    t = thresholds or AlertThresholds()
    alerts: list[Alert] = []

    for sh in source_dashboard(catalogue):
        if sh.consecutive_failures >= t.consecutive_failures:
            alerts.append(Alert(
                "adapter_failing", CRITICAL, sh.key,
                f"{sh.key} has failed {sh.consecutive_failures} runs in a row",
            ))
        # only flag staleness for sources that have produced before
        if sh.documents > 0:
            stale = _days_since(sh.last_yield_at)
            if stale is not None and stale >= t.stale_days:
                alerts.append(Alert(
                    "no_new_documents", WARNING, sh.key,
                    f"{sh.key} has yielded no new documents in {stale:.0f} days "
                    f"(possible silent parser break)",
                ))
        if sh.llm_extracted_ratio >= t.llm_extract_ratio and sh.documents >= 10:
            alerts.append(Alert(
                "llm_extract_spike", WARNING, sh.key,
                f"{sh.key} is extracting {sh.llm_extracted_ratio:.0%} of docs via LLM "
                f"(possible upstream format drift)",
            ))

    queues = pipeline_queues(catalogue)
    for name, depth in queues.items():
        if depth >= t.queue_backlog:
            alerts.append(Alert(
                "queue_backlog", WARNING, name,
                f"queue {name!r} has {depth} items pending (ingestion outrunning processing)",
            ))
    return alerts


@runtime_checkable
class Notifier(Protocol):
    def notify(self, alert: Alert) -> None: ...


class LogNotifier:
    """Default notifier — logs/prints. A Slack/Discord/email webhook notifier
    implements the same one-method interface and drops in (§8)."""

    def __init__(self, sink=None) -> None:
        self._sink = sink  # callable(str) for tests; defaults to the logger

    def notify(self, alert: Alert) -> None:
        line = f"[{alert.severity.upper()}] {alert.code} ({alert.subject}): {alert.message}"
        if self._sink is not None:
            self._sink(line)
        else:
            log.warning(line)


def push_alerts(
    catalogue: Catalogue,
    notifier: Notifier | None = None,
    *,
    thresholds: AlertThresholds | None = None,
) -> list[Alert]:
    """Compute alerts and push each to the notifier. Returns them for the dashboard."""
    notifier = notifier or LogNotifier()
    alerts = check_alerts(catalogue, thresholds)
    for alert in alerts:
        notifier.notify(alert)
    return alerts
