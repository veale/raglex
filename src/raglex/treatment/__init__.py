"""Treatment classification (§1.3a): reclassify bare `mentions` citation edges
into how one case treats another, from the prose around the citation."""

from .classifier import (
    HeuristicTreatmentClassifier,
    LLMTreatmentClassifier,
    TreatmentClassifier,
)
from .stage import TreatmentStats, classify_corpus, classify_document

__all__ = [
    "HeuristicTreatmentClassifier",
    "LLMTreatmentClassifier",
    "TreatmentClassifier",
    "TreatmentStats",
    "classify_corpus",
    "classify_document",
]
