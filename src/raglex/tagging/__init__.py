"""Rule-based tagging engine (§4a) — conditions → tags, as editable data."""

from .engine import PreviewResult, RuleEngine, RunResult
from .tree import evaluate, root_method, validate_tree

__all__ = [
    "PreviewResult",
    "RuleEngine",
    "RunResult",
    "evaluate",
    "root_method",
    "validate_tree",
]
