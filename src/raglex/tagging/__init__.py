"""Rule-based tagging engine (§4a) — conditions → tags, as editable data."""

from .engine import PreviewResult, RuleEngine, RunResult
from .seed import seed
from .tree import evaluate, root_method, validate_tree

__all__ = [
    "PreviewResult",
    "RuleEngine",
    "RunResult",
    "seed",
    "evaluate",
    "root_method",
    "validate_tree",
]
