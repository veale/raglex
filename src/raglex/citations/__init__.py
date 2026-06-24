"""Citation extraction (§5) — recognise references in text as hanging edges that
the §5b resolver links later. Grammars are the extensibility surface."""

from .courts import KNOWN_COURTS, Court, lookup
from .extractor import CitationExtractor, extract_citations, grammar_citations
from .grammars import GRAMMARS, Grammar, register
from .llm_extractor import LLMCitationExtractor
from .models import Citation
from .snowball import Frontier, snowball
from .stage import ExtractStats, extract_corpus, extract_document

__all__ = [
    "extract_citations",
    "grammar_citations",
    "CitationExtractor",
    "LLMCitationExtractor",
    "KNOWN_COURTS",
    "Court",
    "lookup",
    "Frontier",
    "snowball",
    "GRAMMARS",
    "Grammar",
    "register",
    "Citation",
    "ExtractStats",
    "extract_corpus",
    "extract_document",
]
