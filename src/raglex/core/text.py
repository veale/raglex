"""Shared text-normalisation helpers."""

from __future__ import annotations

import re
import unicodedata

# A full stop closing a letter-abbreviation ("K.B.", "A.C.", "Ch."), as opposed to a
# decimal point in a pinpoint ("para 5.2") — hence the "not followed by a digit" guard.
_ABBREV_DOT_RE = re.compile(r"(?<=[a-z])\.(?!\d)")


def fold(text: str) -> str:
    """Case-fold and accent-fold so 'données' matches 'donnees' and 'DSGVO' matches
    'dsgvo'. Used wherever literal matching should ignore case and diacritics — tag
    predicates, citation matching, dedup keys."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def fold_citation(text: str) -> str:
    """``fold`` plus the punctuation a law report is cited with inconsistently.

    Reporters get abbreviated both ways in the wild — "[1948] 1 KB 223" and
    "[1948] 1 K.B. 223" are the same report, but plain ``fold`` keeps the stops, so
    they land on different alias keys and one of them silently fails to resolve.
    (Real case: Wednesbury is held under "(1948) 1 kb 223", so every dotted citation
    of it went unlinked.) Whitespace is collapsed for the same reason — an alias
    minted across a line break carries a newline into the key.

    Bracket style is deliberately *not* normalised: "[1948]" and "(1948)" mean
    different things in citation convention, and both forms are minted as aliases
    anyway, so folding them together would buy nothing and lose a real distinction.
    """
    return " ".join(_ABBREV_DOT_RE.sub("", fold(text)).split())
