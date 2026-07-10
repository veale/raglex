"""Match reporter-only citations to harvested cases (§5b).

A pre-neutral-citation case is cited by law report ("[1998] AC 1") with no fetchable id.
But when we've *harvested* the case another way — the House of Lords scraper, an uploaded
judgment — we can link the report citation to it, so a "[1998] AC 1" resolves like any
other edge. The signal a citing document gives is the **case name** next to the report:
"Pepper v Hart [1993] AC 593". So the match is:

  1. the citation is in a report a House of Lords case would appear in (AC / WLR / All ER
     / Cr App R …) — a tribunal-only reporter rules a HoL match out;
  2. the harvested case's decision year is the report year or a year or two earlier
     (reports lag the judgment);
  3. the harvested case's title shares the distinctive surnames of the cited name;
  4. (confirmation) those surnames also appear in the judgment's opening paragraphs.

Each rung narrows the field; a match needs the name overlap AND the year AND a plausible
reporter. Ambiguity (two equally-good candidates) yields no match — a wrong link is worse
than a pending one.
"""

from __future__ import annotations

import re

# Reporters in which a House of Lords / senior-court decision actually appears. A citation
# in a tribunal- or county-court-only series is not a HoL case, so it can't match one.
HOL_PLAUSIBLE_SERIES = frozenset({
    "AC", "App Cas", "WLR", "All ER", "All ER (Comm)", "AC ", "HL Cas",
    "Cr App R", "Cr App R (S)", "Lloyd's Rep", "ICR", "IRLR", "STC", "TC",
    "P & CR", "FLR", "FCR", "BCLC", "BCC", "Fam", "QB", "KB", "Ch", "SC", "SC (HL)",
    "ER", "LR",  # nominate / English Reports reprints of very old HL cases
})

# Party-role and procedural noise that isn't part of a case's distinctive name.
_STOPWORDS = frozenset({
    "v", "and", "another", "others", "or", "the", "of", "in", "re", "ex", "parte", "on",
    "appeal", "from", "appellant", "appellants", "respondent", "respondents", "fc",
    "secretary", "state", "for", "home", "department", "commissioners", "commissioner",
    "regina", "queen", "king", "crown", "council", "borough", "city", "county",
    "attorney", "general", "reference", "no", "application", "by", "an", "a",
    "his", "her", "majesty", "revenue", "customs", "ex parte", "r", "practice", "note",
    "limited", "ltd", "plc", "llp", "co", "company", "inc", "corporation", "corp",
})

# "Pepper v Hart", "Smith and another v Jones", "R v Brown", "Austin v Commissioner of
# Police of the Metropolis" — the name run ending just before the citation. Permissive:
# each side is a capitalised head word then any run of name-ish words (lower-case
# connectors allowed); surnames() strips the role/procedural noise afterwards.
_NAME_BEFORE = re.compile(
    r"(?P<a>(?:R|Reg|Regina|In re|In the matter of)\b|[A-Z][A-Za-z'’.\-]+"
    r"(?:\s+[A-Za-z'’.&()\-]+){0,8})"
    r"\s+v\.?\s+"
    r"(?P<b>[A-Z][A-Za-z'’.\-]+(?:\s+[A-Za-z'’.&()\-]+){0,8})"
    r"\s*$"
)


def surnames(text: str | None) -> set[str]:
    """The distinctive lower-cased tokens of a case name/title — surnames and unusual
    words, with party roles, procedural words and common terms stripped. This is what two
    citations of the same case share even when the surrounding role text differs
    ("Austin v Commissioner of Police" vs "Austin (FC) (Appellant) v Commissioner")."""
    out: set[str] = set()
    for tok in re.findall(r"[A-Za-z][\w'’\-]+", text or ""):
        low = tok.lower()
        if low in _STOPWORDS or len(low) <= 2:
            continue
        out.add(low)
    return out


def extract_preceding_name(context_before: str) -> str | None:
    """The "X v Y" case name immediately before a report citation, or None. ``context_before``
    is the ~120 chars of text ending right where the citation starts."""
    tail = (context_before or "")[-160:]
    m = _NAME_BEFORE.search(tail)
    if not m:
        return None
    return f"{m.group('a').strip()} v {m.group('b').strip()}"


def _series_of(raw: str) -> str | None:
    from .reporters import report_series

    return report_series(raw)


def _report_year(raw: str) -> int | None:
    m = re.search(r"[\[(](1[6-9]\d{2}|20\d{2})[\])]", raw or "")
    return int(m.group(1)) if m else None


def score_candidate(name_tokens: set[str], case_title: str, case_year: int | None,
                    report_year: int | None) -> float:
    """How well a harvested case matches a cited name+year. 0 = no match. The name overlap
    dominates; the year must be within the reporting lag window or it's disqualified."""
    if report_year is not None and case_year is not None:
        # a report is published the judgment year or up to two years later
        if not (report_year - 2 <= case_year <= report_year + 1):
            return 0.0
    title_tokens = surnames(case_title)
    if not name_tokens or not title_tokens:
        return 0.0
    shared = name_tokens & title_tokens
    if len(shared) < min(2, len(name_tokens)):
        return 0.0  # need both sides' surnames (or all, for a one-name reference)
    # Jaccard over the distinctive tokens, nudged by how many name tokens were covered.
    jaccard = len(shared) / len(name_tokens | title_tokens)
    coverage = len(shared) / len(name_tokens)
    return round(0.5 * jaccard + 0.5 * coverage, 3)


def match_report(raw: str, name: str | None, cases: list, *,
                 min_score: float = 0.5, confirm_text: bool = True) -> tuple[str, float] | None:
    """Best harvested case for a report citation, or None if none is confident/unambiguous.

    ``cases`` is a list of objects/dicts with ``stable_id``, ``title``, ``year`` and
    optionally ``opening`` (the judgment's first paragraphs, for the confirmation rung).
    Returns ``(stable_id, score)``. Refuses to guess when the top two candidates are close
    — a wrong resolution is worse than leaving it pending."""
    series = _series_of(raw)
    if series is None or series not in HOL_PLAUSIBLE_SERIES:
        return None
    if not name:
        return None
    ry = _report_year(raw)
    name_tokens = surnames(name)
    if len(name_tokens) < 2:  # need both parties to disambiguate
        return None

    scored: list[tuple[float, object]] = []
    for c in cases:
        title = _attr(c, "title")
        year = _attr(c, "year")
        s = score_candidate(name_tokens, title or "", _as_int(year), ry)
        if s >= min_score:
            scored.append((s, c))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]
    # ambiguity guard: a clear leader, or the runner-up is materially worse
    if len(scored) > 1 and scored[1][0] >= best_score - 0.08:
        # tie-break on the confirmation rung (surnames in the judgment opening) if we have it
        opening = _attr(best, "opening")
        rn_opening = _attr(scored[1][1], "opening")
        if not (confirm_text and opening and name_tokens <= surnames(opening)
                and not (rn_opening and name_tokens <= surnames(rn_opening))):
            return None  # genuinely ambiguous → don't guess
    # confirmation: the cited surnames should appear in the judgment's opening prose
    opening = _attr(best, "opening")
    if confirm_text and opening:
        if not (name_tokens & surnames(opening)):
            return None
    return _attr(best, "stable_id"), best_score


def _attr(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_int(v):
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None
