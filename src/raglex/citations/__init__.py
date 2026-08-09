"""Citation extraction (§5) — recognise references in text as hanging edges that
the §5b resolver links later. Grammars are the extensibility surface."""

from .courts import KNOWN_COURTS, Court, lookup
from .extractor import (
    CitationExtractor,
    all_grammar_citations,
    declared_instrument_host,
    extract_citations,
    grammar_citations,
)
from .grammars import GRAMMARS, Grammar, register
from .llm_extractor import LLMCitationExtractor
from .models import Citation
from .snowball import Frontier
from .snowball import snowball as citation_frontier
from .stage import (ExtractStats, extract_corpus, extract_document,
                    extract_documents_parallel)

__all__ = [
    "extract_citations",
    "grammar_citations",
    "all_grammar_citations",
    "declared_instrument_host",
    "CitationExtractor",
    "LLMCitationExtractor",
    "KNOWN_COURTS",
    "Court",
    "lookup",
    "Frontier",
    "citation_frontier",
    "GRAMMARS",
    "Grammar",
    "register",
    "Citation",
    "ExtractStats",
    "extract_corpus",
    "extract_document",
    "extract_documents_parallel",
]
