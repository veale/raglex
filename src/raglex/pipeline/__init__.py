"""The shared, jurisdiction-agnostic ingest pipeline (§5)."""

from .runner import Pipeline, RunStats

__all__ = ["Pipeline", "RunStats"]
