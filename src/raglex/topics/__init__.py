"""Two-stage topic gate (§4) — the seed of the §4a rule engine."""

from .gate import TopicResult, cheap_match, confirm, fold, score_text

__all__ = ["TopicResult", "cheap_match", "confirm", "fold", "score_text"]
