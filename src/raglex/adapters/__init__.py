"""Source adapters. One adapter per jurisdiction/source (§1.5)."""

from .eu_cellar import EUCellarAdapter
from .nl_rechtspraak import NLRechtspraakAdapter
from .registry import ADAPTERS, get_adapter
from .uk_caselaw import UKCaseLawAdapter

__all__ = [
    "ADAPTERS",
    "get_adapter",
    "EUCellarAdapter",
    "NLRechtspraakAdapter",
    "UKCaseLawAdapter",
]
