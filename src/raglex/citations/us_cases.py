"""US case-citation recognition via eyecite (Free Law Project).

RagLex's own grammars cover the systems it harvests (UK/EU/Commonwealth). US
reporter citations — "135 S. Ct. 2401", "325 U.S. 410 (1945)", "519 U.S. 452" —
are a long tail it doesn't hold, but Canadian and UK-apex judgments cite them
heavily, and without recognising the span the section grammar and carry-forward
misread "S. Ct." / "U.S." as statutory material ("processed as sections", per the
refinement queue). eyecite carries the whole reporter gazetteer, so this defers the
breadth to a maintained library rather than reimplementing it.

**Gated.** eyecite is only run on text that actually looks American — a cheap
reporter-shaped pre-check — so the ~1% of documents that cite US cases pay the cost
and the rest don't. This also means it fires wherever US cites appear (a UK Supreme
Court judgment reaching for a US authority), not just in a named jurisdiction.

The candidate id is a normalised ``us/<reporter>/<vol>/<page>`` slug: the corpus
holds no US cases, so the reference is deliberately unfetchable, but a stable id
still clusters every reference to one authority and keeps it out of the statutory
worklist. Parallel citations to the same case in different reporters stay distinct
(the corpus's usual treatment of report citations).
"""

from __future__ import annotations

import re

from .models import Citation

# A cheap gate: a volume-reporter-page shape using a distinctively-American
# reporter. Runs on every document, so it must be fast and specific — a plain
# regex over the reporter abbreviations eyecite would recognise, enough to decide
# whether the (heavier) eyecite pass is worth running at all.
_US_REPORTER_HINT = re.compile(
    r"\b\d{1,4}\s+(?:"
    r"U\.?\s?S\.?|S\.?\s?Ct\.?|L\.?\s?Ed\.?(?:\s?2d)?|"        # SCOTUS
    r"F\.\s?(?:2d|3d|4th|Supp\.?(?:\s?2d|\s?3d)?|App'?x)|"      # federal
    r"A\.\s?(?:2d|3d)|P\.\s?(?:2d|3d)|N\.[EW]\.\s?(?:2d|3d)|"  # regional
    r"S\.[EW]\.\s?(?:2d|3d)|So\.\s?(?:2d|3d)|Cal\.\s?(?:2d|3d|4th|5th)"
    r")\.?\s+\d{1,5}\b"          # a reporter may end in a dot, then the page number
)

_REPORTER_SLUG = re.compile(r"[^a-z0-9]+")


def looks_american(text: str) -> bool:
    """Cheap pre-gate: does the text carry a US reporter citation at all?"""
    return bool(text) and _US_REPORTER_HINT.search(text) is not None


def _slug(reporter: str) -> str:
    return _REPORTER_SLUG.sub("", (reporter or "").lower())


def us_case_citations(text: str) -> list[Citation]:
    """US case citations in ``text`` as RagLex ``Citation``s. Empty when the text
    isn't American (the gate) or eyecite isn't installed (graceful degradation —
    the rest of extraction is unaffected)."""
    if not looks_american(text):
        return []
    try:
        from eyecite import get_citations
        from eyecite.models import CaseCitation
    except Exception:  # noqa: BLE001 — a missing optional dep must not break extraction
        return []

    out: list[Citation] = []
    try:
        found = get_citations(text)
    except Exception:  # noqa: BLE001 — eyecite must never sink the extraction pass
        return []
    for c in found:
        if not isinstance(c, CaseCitation):
            continue
        g = c.groups or {}
        vol, rep, page = g.get("volume"), g.get("reporter"), g.get("page")
        if not (vol and rep and page):
            continue
        span = c.span()
        cand = f"us/{_slug(rep)}/{vol}/{page}"
        md = c.metadata
        pin = getattr(md, "pin_cite", None)
        out.append(Citation(
            raw=text[span[0]:span[1]],
            entity_kind="case",
            candidate_id=cand,
            pinpoint=f"p. {pin}" if pin else None,
            char_start=span[0],
            char_end=span[1],
            method="us_reporter",
            confidence=0.85,
        ))
    return out
