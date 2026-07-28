"""Shared text-normalisation helpers."""

from __future__ import annotations

import re
import unicodedata

# A full stop closing a letter-abbreviation ("K.B.", "A.C.", "Ch."), as opposed to a
# decimal point in a pinpoint ("para 5.2") — hence the "not followed by a digit" guard.
_ABBREV_DOT_RE = re.compile(r"(?<=[a-z])\.(?!\d)")


_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_SURROGATE_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")


def scrub_surrogates(text: str, *, join_pairs: bool = True) -> str:
    """Remove the unpaired surrogates a broken PDF ``ToUnicode`` CMap yields.

    A str holding a lone surrogate cannot be encoded as UTF-8 at all, so it blows up
    at the *last* step — writing the text file, or binding a psycopg parameter —
    with "surrogates not allowed", aborting the whole harvest rather than the one
    document (same failure shape as the NUL bytes stripped in the chunk writer).

    A well-formed pair is the astral character the CMap meant, so it is joined back
    up; anything left over is genuinely undecodable and becomes U+FFFD. Pass
    ``join_pairs=False`` where char offsets are already fixed (segments, citation
    spans) — the replacement is then strictly 1:1 and cannot shift them.
    """
    if not _SURROGATE_RE.search(text):
        return text
    if join_pairs:
        text = _SURROGATE_PAIR_RE.sub(
            lambda m: chr(
                0x10000 + ((ord(m[0][0]) - 0xD800) << 10) + (ord(m[0][1]) - 0xDC00)
            ),
            text,
        )
    return _SURROGATE_RE.sub("�", text)


# Windows-1252 bytes decoded as ISO-8859-1: the punctuation a legal text is full of —
# en dashes, curly quotes, ellipses — lands in the C1 control block instead, where a
# browser draws it as an empty rectangle. A single Court of Appeal judgment carried 74 of
# them ("Home Park House ▯ a fortiori" should read "Home Park House – a fortiori").
# The mapping is 1:1, so it can be applied to text whose citation offsets are already
# stored without moving a single character. The five slots cp1252 leaves undefined become
# a space rather than vanishing, for the same reason.
_CP1252_C1 = {
    i: (bytes([i]).decode("cp1252") if bytes([i]).decode("cp1252", "ignore") else " ")
    for i in range(0x80, 0xA0)
}


def fix_cp1252_c1(text: str) -> str:
    """Repair Windows-1252 punctuation mis-decoded into the C1 control block."""
    return text.translate(_CP1252_C1) if text else text


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
