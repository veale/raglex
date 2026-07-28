"""Free-text search over the corpus — lexical, gated by jurisdiction, with literal
quotation support.

Deliberately independent of the embedding stage. Until now the full-text index
(``tsv``) lived on the ``embeddings`` table, so lexical search required the embed
pass to have run — and it never had, which is why free-text search returned nothing
at all. A lexical index needs text and a tsvector; it needs no model, no GPU and no
HPC queue, and separating the two means this feature can never be blocked behind
one.
"""

from .query import ParsedQuery, Phrase, QueryError, Term, parse, to_tsquery

__all__ = ["ParsedQuery", "Phrase", "QueryError", "Term", "parse", "to_tsquery"]
