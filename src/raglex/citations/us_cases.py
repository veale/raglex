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

The candidate id is a normalised ``us/<reporter>/<vol>/<page>`` slug. The
``us-caselaw`` adapter (CourtListener) mints its documents under exactly this slug,
so every citation recognised here resolves to the held case once that case is
harvested — and stays a clustered, jurisdiction-filterable frontier node until then.
``us_candidate_id`` is the shared constructor: extractor and adapter must agree on
the slug or nothing ever joins.
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


def reporter_slug(reporter: str) -> str:
    """The canonical slug token for a reporter abbreviation, however it is spaced or
    dotted ("S.Ct." / "S. Ct." → ``sct``).

    Public because the CourtListener adapter has to mint the *same* slug from the
    reporter strings the API hands back ("U.S.", "F.3d") that this module mints from
    the ones it finds in prose. A reporter outside the table degrades to its
    stripped-alphanumeric form on both sides, so an unlisted series still clusters
    with itself — it just isn't one of the curated names.
    """
    return _canonical_reporter(reporter)


# slug token → the canonical abbreviation, derived from the table above rather than
# written out twice. The first spelling of each slug wins, which is why _REPORTERS
# lists the canonical form first ("U.S." before "U. S.").
_SLUG_TO_REPORTER: dict[str, str] = {}
for _rep, _slug in _REPORTERS.items():
    _SLUG_TO_REPORTER.setdefault(_slug, _rep)


def reporter_name(slug: str) -> str:
    """``"sct"`` → ``"S. Ct."`` — the inverse of ``reporter_slug``.

    Needed wherever a slug has to become a real citation again: the CourtListener
    adapter sends the abbreviation (the API won't recognise our canonical token), and
    the Corpus Map uses it as a sub-type label.
    """
    return _SLUG_TO_REPORTER.get(slug.lower(), slug.upper())


def us_candidate_id(volume: str | int, reporter: str, page: str | int) -> str:
    """``us/<reporter>/<vol>/<page>`` — the shared identity for a US case.

    The single constructor for the slug, used by the extractor when it recognises a
    citation in prose and by the ``us-caselaw`` adapter when it stores the case. They
    must not drift: the slug IS the join key between a pending citation and the held
    document.
    """
    return f"us/{reporter_slug(reporter)}/{volume}/{page}"


def us_case_citations(text: str) -> list[Citation]:
    """US case citations in ``text`` as RagLex ``Citation``s. Empty when the text
    isn't American (the gate)."""
    if not looks_american(text):
        return []
    out: list[Citation] = []
    for m in _US_CITE_RE.finditer(text):
        cand = us_candidate_id(m.group("vol"), m.group("rep"), m.group("page"))
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


# CourtListener court-id slugs → natural-language names. Not citation court codes (US
# cases cite by reporter, not neutral citation), so these live here rather than in the
# neutral-citation registry, and court_label consults this map for us-… sources. Covers
# the default seed set (SCOTUS + the federal circuits, FEDERAL_APPELLATE); an unlisted
# slug (a district court, a state court) falls through to us_court_name's derivation.
US_COURT_NAMES: dict[str, str] = {
    "scotus": "Supreme Court of the United States",
    "ca1": "U.S. Court of Appeals, First Circuit",
    "ca2": "U.S. Court of Appeals, Second Circuit",
    "ca3": "U.S. Court of Appeals, Third Circuit",
    "ca4": "U.S. Court of Appeals, Fourth Circuit",
    "ca5": "U.S. Court of Appeals, Fifth Circuit",
    "ca6": "U.S. Court of Appeals, Sixth Circuit",
    "ca7": "U.S. Court of Appeals, Seventh Circuit",
    "ca8": "U.S. Court of Appeals, Eighth Circuit",
    "ca9": "U.S. Court of Appeals, Ninth Circuit",
    "ca10": "U.S. Court of Appeals, Tenth Circuit",
    "ca11": "U.S. Court of Appeals, Eleventh Circuit",
    "cadc": "U.S. Court of Appeals, D.C. Circuit",
    "cafc": "U.S. Court of Appeals, Federal Circuit",
    "cc": "U.S. Court of Claims",
    "uscfc": "U.S. Court of Federal Claims",
    "cavc": "U.S. Court of Appeals for Veterans Claims",
    "tax": "U.S. Tax Court",
    "bap1": "Bankruptcy Appellate Panel, First Circuit",
    "bap9": "Bankruptcy Appellate Panel, Ninth Circuit",
}


def us_court_name(slug: str | None) -> str | None:
    """Natural-language name for a CourtListener court-id ``slug`` ('scotus' → 'Supreme
    Court of the United States', 'ca9' → '…Ninth Circuit'). Derives the common families
    the explicit map doesn't enumerate — the federal district courts ('cand' → 'U.S.
    District Court, N.D. Cal.') — and returns ``None`` when it can't, so the caller keeps
    its own prettified fallback rather than inventing a court name."""
    if not slug:
        return None
    low = slug.strip().lower()
    if low in US_COURT_NAMES:
        return US_COURT_NAMES[low]
    # Federal district courts: <region><state>d, e.g. "cand" (N.D. Cal.), "nysd" (S.D.N.Y.).
    _REGION = {"c": "Central", "e": "Eastern", "m": "Middle", "n": "Northern",
               "s": "Southern", "w": "Western"}
    _STATE = {
        "al": "Ala.", "ak": "Alaska", "az": "Ariz.", "ar": "Ark.", "ca": "Cal.",
        "co": "Colo.", "ct": "Conn.", "de": "Del.", "fl": "Fla.", "ga": "Ga.",
        "hi": "Haw.", "id": "Idaho", "il": "Ill.", "in": "Ind.", "ia": "Iowa",
        "ks": "Kan.", "ky": "Ky.", "la": "La.", "me": "Me.", "md": "Md.",
        "ma": "Mass.", "mi": "Mich.", "mn": "Minn.", "ms": "Miss.", "mo": "Mo.",
        "mt": "Mont.", "ne": "Neb.", "nv": "Nev.", "nh": "N.H.", "nj": "N.J.",
        "nm": "N.M.", "ny": "N.Y.", "nc": "N.C.", "nd": "N.D.", "oh": "Ohio",
        "ok": "Okla.", "or": "Or.", "pa": "Pa.", "ri": "R.I.", "sc": "S.C.",
        "sd": "S.D.", "tn": "Tenn.", "tx": "Tex.", "ut": "Utah", "vt": "Vt.",
        "va": "Va.", "wa": "Wash.", "wv": "W. Va.", "wi": "Wis.", "wy": "Wyo.",
    }
    # CourtListener district slug = <state><region?>d, state first: "cand" = ca+n+d =
    # N.D. Cal.; "mdd" = md+''+d = D. Md.; "nysd" = ny+s+d = S.D.N.Y.
    if low.endswith("d") and len(low) >= 3:
        body = low[:-1]
        st = _STATE.get(body[:2])
        if st:
            region = _REGION.get(body[2:])
            prefix = f"{region[0]}.D." if region else "D."
            return f"U.S. District Court, {prefix} {st}"
    return None
