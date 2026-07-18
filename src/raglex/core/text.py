"""Shared text-normalisation helpers."""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """Case-fold and accent-fold so 'données' matches 'donnees' and 'DSGVO' matches
    'dsgvo'. Used wherever literal matching should ignore case and diacritics — tag
    predicates, citation matching, dedup keys."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()
