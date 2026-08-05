"""Which jurisdiction a source belongs to — one table, read by both layers.

The facade buckets documents by source key for the Explore view, and the catalogue has
to answer the same question in SQL for a jurisdiction-locked provision mapping. Two
copies of that table would drift the moment a source was added, and the drift would be
silent: a new EU source would simply stop passing through a lock that was supposed to
admit it.
"""

from __future__ import annotations

#: Source-key prefix → jurisdiction bucket. Order matters (first match wins); anything
#: unmatched is "Other".
JURISDICTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("uk-", "bailii", "westlaw", "ofcom", "ico", "hol"), "United Kingdom"),
    (("eu-", "edpb", "a29wp", "dma", "cellar", "eur-lex"), "European Union"),
    (("echr",), "Council of Europe"),
    (("fr-",), "France"),
    (("de-",), "Germany"),
    (("nl-",), "Netherlands"),
    (("it-",), "Italy"),
    (("ie-", "eisb"), "Ireland"),
    (("au-",), "Australia"),
    (("ca-",), "Canada"),
    (("nz-",), "New Zealand"),
    (("sg-",), "Singapore"),
    (("hk-",), "Hong Kong"),
    (("in-",), "India"),
    (("us-",), "United States"),
)

#: The short codes a caller may name a bucket by (SourceInfo uses these), so a mapping
#: can be locked with ``source_jurisdiction="EU"`` as readily as the display name.
JURISDICTION_CODES: dict[str, str] = {
    "gb": "United Kingdom", "uk": "United Kingdom",
    "eu": "European Union",
    "coe": "Council of Europe",
    "fr": "France", "de": "Germany", "nl": "Netherlands", "it": "Italy",
    "ie": "Ireland", "au": "Australia", "ca": "Canada", "nz": "New Zealand",
    "sg": "Singapore", "hk": "Hong Kong", "in": "India", "us": "United States",
}


def canonical_jurisdiction(value: str | None) -> str | None:
    """A caller's jurisdiction name or code → the bucket name used everywhere else."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in {name for _prefixes, name in JURISDICTIONS}:
        return raw
    return JURISDICTION_CODES.get(raw.lower())


def prefixes_for(jurisdiction: str | None) -> tuple[str, ...]:
    """The source-key prefixes that belong to a bucket, or () for an unknown one."""
    name = canonical_jurisdiction(jurisdiction)
    for prefixes, bucket in JURISDICTIONS:
        if bucket == name:
            return prefixes
    return ()


def sql_lock_match(source_col: str, lock_col: str) -> tuple[str, list[str]]:
    """A SQL predicate for "this document satisfies that row's jurisdiction lock".

    The lock is a COLUMN value, not a constant, so the predicate has to cover every
    bucket at once — generated from the table above rather than written out, so adding a
    source to a jurisdiction cannot leave the lock behind. A lock naming a bucket that
    does not exist matches nothing, which is the safe direction: a typo hides citers
    instead of admitting the whole corpus.
    """
    arms: list[str] = []
    params: list[str] = []
    for prefixes, bucket in JURISDICTIONS:
        likes = " OR ".join(f"{source_col} LIKE ?" for _ in prefixes)
        arms.append(f"({lock_col} = ? AND ({likes}))")
        params.append(bucket)
        params.extend(f"{p}%" for p in prefixes)
    return "(" + " OR ".join(arms) + ")", params


def sql_source_match(column: str, jurisdiction: str | None) -> tuple[str, list[str]]:
    """A SQL predicate for "this source belongs to that jurisdiction", and its params.

    Prefix matching, because that is how the buckets are defined — ``eu-cellar``,
    ``eu-legislation`` and ``edpb`` are all European Union. An unknown jurisdiction
    yields a predicate that matches nothing, so a mistyped lock hides the mapping's
    citers rather than silently admitting the whole corpus.
    """
    prefixes = prefixes_for(jurisdiction)
    if not prefixes:
        return "1 = 0", []
    return ("(" + " OR ".join(f"{column} LIKE ?" for _ in prefixes) + ")",
            [f"{p}%" for p in prefixes])
