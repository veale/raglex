"""US case-citation recognition — self-contained (no external dependency).

RagLex's own grammars cover the systems it harvests (UK/EU/Commonwealth). US
reporter citations — "135 S. Ct. 2401", "325 U.S. 410 (1945)", "519 U.S. 452" —
are a long tail it doesn't hold, but Canadian and UK-apex judgments cite them
heavily, and without recognising the span the section grammar and carry-forward
misread "S. Ct." / "U.S." as statutory material ("processed as sections", per the
refinement queue).

eyecite (Free Law Project) is the reference implementation, but it pulls a Rust
extension (fast-diff-match-patch) that won't build on the deployment's arm64 image,
and its breadth is overkill here: a compact table of the major reporters — the ones
that actually turn up in Commonwealth judgments — plus the standard
``volume reporter page`` shape covers the flagged citations without a dependency.
The reporter abbreviations and their canonical forms follow reporters-db.

**Gated.** The volume-reporter-page pre-check is cheap and runs on every document;
the fuller scan only fires when the text actually looks American, so it also catches
US cites wherever they appear (a UK Supreme Court judgment reaching for a US
authority), not just in a named jurisdiction.

The candidate id is a normalised ``us/<reporter>/<vol>/<page>`` slug: the corpus
holds no US case law and there is no adapter, so the reference is deliberately
unfetchable but clusters to one node and reads as a US case in the frontier (where
it can be filtered by jurisdiction for a later bulk import).
"""

from __future__ import annotations

import re

from .models import Citation

# The major US reporters, mapped to a canonical slug token. Ordered longest-first in
# the alternation so "S. Ct." wins over a bare "S." Grouped by series; this is the
# subset that actually appears in Commonwealth/UK judgments citing US authority.
_REPORTERS: dict[str, str] = {
    # Supreme Court
    "U.S.": "us", "U. S.": "us",
    "S. Ct.": "sct", "S.Ct.": "sct",
    "L. Ed. 2d": "led2d", "L.Ed.2d": "led2d", "L. Ed.": "led", "L.Ed.": "led",
    # Federal
    "F.4th": "f4th", "F. 4th": "f4th",
    "F.3d": "f3d", "F. 3d": "f3d", "F.2d": "f2d", "F. 2d": "f2d",
    "F. Supp. 3d": "fsupp3d", "F.Supp.3d": "fsupp3d",
    "F. Supp. 2d": "fsupp2d", "F.Supp.2d": "fsupp2d",
    "F. Supp.": "fsupp", "F.Supp.": "fsupp",
    "F.": "f", "Fed. Appx.": "fedappx", "F. App'x": "fedappx",
    # Regional reporters (National Reporter System)
    "A.3d": "a3d", "A.2d": "a2d", "A.": "a",
    "P.3d": "p3d", "P.2d": "p2d", "P.": "p",
    "N.E.3d": "ne3d", "N.E.2d": "ne2d", "N.E.": "ne",
    "N.W.2d": "nw2d", "N.W.": "nw",
    "S.E.2d": "se2d", "S.E.": "se",
    "S.W.3d": "sw3d", "S.W.2d": "sw2d", "S.W.": "sw",
    "So. 3d": "so3d", "So. 2d": "so2d", "So.": "so",
    "Cal. Rptr. 3d": "calrptr3d", "Cal. Rptr. 2d": "calrptr2d", "Cal. Rptr.": "calrptr",
    "N.Y.S.3d": "nys3d", "N.Y.S.2d": "nys2d",
}

# a dot/space-tolerant alternation over the reporter abbreviations, longest first
_REP_ALT = "|".join(
    re.escape(rep).replace(r"\ ", r"\ ?").replace(r"\.", r"\.\ ?")
    for rep in sorted(_REPORTERS, key=len, reverse=True)
)
# vol REPORTER page. A trailing ", N" pin page is deliberately NOT captured: it is
# ambiguous with a parallel citation ("519 U.S. 452, 117 S. Ct. 905"), where the
# "117" opens the next reporter, not a pin — so each reporter stays its own citation.
_US_CITE_RE = re.compile(
    rf"\b(?P<vol>\d{{1,4}})\s+(?P<rep>{_REP_ALT})\s+(?P<page>\d{{1,5}})\b",
    re.IGNORECASE,
)
# the same shape, minimal, for the cheap pre-gate
_US_HINT_RE = re.compile(rf"\b\d{{1,4}}\s+(?:{_REP_ALT})\s+\d{{1,5}}\b", re.IGNORECASE)


def looks_american(text: str) -> bool:
    """Cheap pre-gate: does the text carry a US reporter citation at all?"""
    return bool(text) and _US_HINT_RE.search(text) is not None


def _canonical_reporter(matched: str) -> str:
    """The slug token for a matched reporter, however it was spaced/dotted."""
    key = re.sub(r"\s+", " ", matched).strip()
    if key in _REPORTERS:
        return _REPORTERS[key]
    # normalise spacing around dots and retry ("S.Ct." vs "S. Ct.")
    for rep, slug in _REPORTERS.items():
        if re.sub(r"[.\s]", "", rep).lower() == re.sub(r"[.\s]", "", key).lower():
            return slug
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def us_case_citations(text: str) -> list[Citation]:
    """US case citations in ``text`` as RagLex ``Citation``s. Empty when the text
    isn't American (the gate)."""
    if not looks_american(text):
        return []
    out: list[Citation] = []
    for m in _US_CITE_RE.finditer(text):
        rep = _canonical_reporter(m.group("rep"))
        cand = f"us/{rep}/{m.group('vol')}/{m.group('page')}"
        out.append(Citation(
            raw=m.group(0),
            entity_kind="case",
            candidate_id=cand,
            pinpoint=None,
            char_start=m.start(),
            char_end=m.end(),
            method="us_reporter",
            confidence=0.85,
        ))
    return out
