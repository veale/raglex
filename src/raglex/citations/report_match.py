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

# Reporters in which a senior-court decision actually appears. A citation in a
# tribunal- or county-court-only series is not a senior-court case, so it can't match
# one. (Named for its original House of Lords use; it now also gates the Irish series —
# the candidate pool is ALL held judgments, so imported Irish cases match too.)
HOL_PLAUSIBLE_SERIES = frozenset({
    "AC", "App Cas", "WLR", "All ER", "All ER (Comm)", "AC ", "HL Cas",
    "Cr App R", "Cr App R (S)", "Lloyd's Rep", "ICR", "IRLR", "STC", "TC",
    "P & CR", "FLR", "FCR", "BCLC", "BCC", "Fam", "QB", "KB", "Ch", "SC", "SC (HL)",
    "ER", "LR",  # nominate / English Reports reprints of very old HL cases
    # Irish senior courts publish in these (imported via the BAILII zip/file paths)
    "IR", "ILRM", "ILTR", "Ir Jur Rep", "Ir Jur", "LR Ir", "Frewen",
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
    r"(?P<a>(?:R|Reg|Regina)\b(?:\s*\([^()]{2,40}\))?|(?:In re|In the matter of)\b|"
    r"[A-Z][A-Za-z'’.\-]+"
    r"(?:\s+[A-Za-z'’.&()\-]+){0,8})"
    r"\s+v\.?\s+"
    r"(?P<b>[A-Z][A-Za-z'’.\-]+(?:\s+[A-Za-z'’.&()\-]+){0,8})"
    r"\s*$"
)


def surnames(text: str | None) -> set[str]:
    """The distinctive lower-cased tokens of a case name/title — surnames and unusual
    words, with party roles, procedural words and common terms stripped. This is what two
    citations of the same case share even when the surrounding role text differs
    ("Austin v Commissioner of Police" vs "Austin (FC) (Appellant) v Commissioner").

    Law-report abbreviations are canonicalised first (``normalise_abbrev``) so "A-G" and
    "Attorney General", "Ltd" and "Limited" tokenise to the same token — otherwise the two
    forms of one name share nothing distinctive."""
    from ..core.text import fold
    from .name_variants import normalise_abbrev

    # accent-fold so "Confédération" matches "Confederation" and "Öztürk" matches "Ozturk"
    # — ECtHR party names in particular carry accents inconsistently across citing texts.
    folded = fold(normalise_abbrev(text or ""))
    out: set[str] = set()
    for tok in re.findall(r"[a-z][\w'’\-]+", folded):
        if tok in _STOPWORDS or len(tok) <= 2:
            continue
        out.add(tok)
    return out


def extract_preceding_name(context_before: str) -> str | None:
    """The "X v Y" case name immediately before a report citation, or None. ``context_before``
    is the ~120 chars of text ending right where the citation starts."""
    tail = (context_before or "")[-160:]
    m = _NAME_BEFORE.search(tail)
    if not m:
        return None
    return f"{m.group('a').strip()} v {m.group('b').strip()}"


# Sentence prose swept into the first party ("The claimants rely on Investors
# Compensation Scheme Ltd v …") pollutes the name tokens. The tight form of a side is
# its LAST run of capitalised words (connectors like "of/and/the" allowed inside):
# "…rely on Investors Compensation Scheme Ltd" → "Investors Compensation Scheme Ltd".
_CAP_RUN = re.compile(
    r"(?:R|Reg|Regina)\b(?:\s*\([^)]{2,40}\))?$|"
    r"[A-Z][\w'’.\-]*(?:\s+(?:of|and|the|for|de|d'|la|le|van|von|den|der|&|"
    r"[A-Z][\w'’.\-]*|\([^()]{1,30}\)))*$"
)
# "Re X" / "In re X" / "In the matter of X" — no "v", one subject.
_RE_FORM = re.compile(
    r"\b(?:In\s+)?[Rr]e\s+(?P<x>[A-Z][\w'’.\-]*(?:\s+[\w'’.&()\-]+){0,6})\s*$|"
    r"\bIn\s+the\s+matter\s+of\s+(?P<y>[A-Z][\w'’.\-]*(?:\s+[\w'’.&()\-]+){0,6})\s*$"
)


def _tight_side(side: str) -> str:
    m = _CAP_RUN.search(side.strip())
    return m.group(0).strip() if m else side.strip()


# A citation immediately before the one being examined — "Pinnock [2010] UKSC 45,
# [2011] 2 AC 104 and Powell" reads name → neutral → report; the name sits BEFORE the
# earlier citations, so strip them off the tail one at a time and look again.
_TRAILING_CITE = re.compile(
    r"(?:,|;|\band)?\s*[\[(](?:19|20)\d{2}[\])]\s*(?:\d{1,2}\s+)?"
    r"[A-Z][A-Za-z.'&\- ]{0,16}?\s*\d{1,5}(?:\s*\([A-Za-z]{2,12}\))?\s*$"
)


def extract_name_candidates(context_before: str) -> list[str]:
    """Plausible case-name strings ending right before a report citation, best first.

    Multiple candidates because the heuristics are imperfect: sentence-initial prose
    gets swept into the first party, abbreviation/period conventions vary, and some
    references are "Re X" or single-party. Downstream token matching tolerates noise,
    so the permissive form comes first; the capitalisation-tightened form catches the
    cases where the permissive one grabbed prose."""
    tail = re.sub(r"\s+", " ", (context_before or "")[-200:])
    # strip trailing punctuation/brackets the citation hangs off ("Pepper v Hart, [1993]…")
    tail = re.sub(r"[\s(\[«\"'‘“,;:–—-]+$", "", tail)
    # …then any earlier citations of the same case sitting between the name and this one
    for _ in range(3):
        m = _TRAILING_CITE.search(tail)
        if not m:
            break
        tail = re.sub(r"[\s,;:–—-]+$", "", tail[: m.start()])
    out: list[str] = []

    def _add(name: str | None) -> None:
        name = re.sub(r"\s+", " ", (name or "").strip(" .,;:"))
        if name and len(name) > 2 and name not in out:
            out.append(name)

    m = _NAME_BEFORE.search(tail)
    if m:
        a, b = m.group("a").strip(), m.group("b").strip()
        _add(f"{a} v {b}")
        ta, tb = _tight_side(a), _tight_side(b)
        if (ta, tb) != (a, b):
            _add(f"{ta} v {tb}")
    r = _RE_FORM.search(tail)
    if r:
        _add(f"Re {r.group('x') or r.group('y')}")
    if not out:
        # bare trailing capitalised run ("…as held in Pepper [1993] AC 593") — a weak,
        # single-party candidate; useful for human-confirmed suggestions only.
        m2 = _CAP_RUN.search(tail)
        if m2 and len(surnames(m2.group(0))) >= 1:
            _add(m2.group(0))
    return out


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
                 min_score: float = 0.5, confirm_text: bool = True,
                 allow_single: bool = True) -> tuple[str, float, str] | None:
    """Best harvested case for a report citation, or None if none is confident/unambiguous.

    ``cases`` is a list of objects/dicts with ``stable_id``, ``title``, ``year`` and
    optionally ``opening`` (the judgment's first paragraphs, for the confirmation rung).
    Returns ``(stable_id, score, kind)`` where ``kind`` is ``"exact"`` (both parties
    matched), ``"abbrev"`` (matched only after abbreviation normalisation), or ``"single"``
    (one-party fallback). Refuses to guess when the top two candidates are close — a wrong
    resolution is worse than leaving it pending."""
    series = _series_of(raw)
    if series is None or series not in HOL_PLAUSIBLE_SERIES:
        return None
    if not name:
        return None
    ry = _report_year(raw)
    name_tokens = surnames(name)
    if len(name_tokens) < 2:
        # single-party fallback: the citing text names only one party ("Pepper [1993] AC
        # 593"). Far riskier, so gate it hard — exactly one pool case in the year window
        # carries the (single, distinctive) surname AND the confirmation rung passes.
        if not (allow_single and len(name_tokens) == 1):
            return None
        return _match_single(name_tokens, cases, ry, confirm_text)

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
        runner = scored[1][1]
        if _same_case(best, runner):
            # duplicate holdings of ONE case (a HoL scrape + the Find Case Law copy) tie
            # every report match to that case forever — same distinctive name and year is
            # one case, so prefer its neutral-citation-shaped id rather than refusing.
            best = _prefer_canonical(best, runner)
        else:
            # tie-break on the confirmation rung (surnames in the judgment opening) if we have it
            opening = _attr(best, "opening")
            rn_opening = _attr(runner, "opening")
            if not (confirm_text and opening and name_tokens <= surnames(opening)
                    and not (rn_opening and name_tokens <= surnames(rn_opening))):
                return None  # genuinely ambiguous → don't guess
    # confirmation: the cited surnames should appear in the judgment's opening prose
    opening = _attr(best, "opening")
    if confirm_text and opening:
        if not (name_tokens & surnames(opening)):
            return None
    # "abbrev" when the match leaned on the abbreviation table — i.e. the un-normalised
    # tokens don't overlap enough on their own but the normalised ones do.
    raw_shared = _raw_tokens(name) & _raw_tokens(_attr(best, "title"))
    kind = "exact" if len(raw_shared) >= 2 else "abbrev"
    return _attr(best, "stable_id"), best_score, kind


_NEUTRAL_SLUG = re.compile(r"^[a-z]+(?:/[a-z]+)*/\d{4}/\d+$")
_SERIES_NO = re.compile(r"\bno\.?\s*(\d+)\b", re.IGNORECASE)


def _same_case(a, b) -> bool:
    """Two pool entries that are duplicate holdings of one case: identical distinctive
    name tokens and the same decision year — but "Factortame (No 2)" and "(No 3)" are
    different cases whose distinctive tokens coincide, so a differing series number
    keeps them apart."""
    ta, tb = _attr(a, "title") or "", _attr(b, "title") or ""
    na, nb = _SERIES_NO.search(ta), _SERIES_NO.search(tb)
    if (na.group(1) if na else None) != (nb.group(1) if nb else None):
        return False
    return (surnames(ta) == surnames(tb)
            and _as_int(_attr(a, "year")) is not None
            and _as_int(_attr(a, "year")) == _as_int(_attr(b, "year")))


def _prefer_canonical(a, b):
    """Of two duplicate holdings, the one whose stable_id is a neutral-citation slug
    (ukhl/1997/28 beats hol/ld199798/invest01)."""
    return a if _NEUTRAL_SLUG.match(str(_attr(a, "stable_id") or "")) \
        or not _NEUTRAL_SLUG.match(str(_attr(b, "stable_id") or "")) else b


def _raw_tokens(text: str | None) -> set[str]:
    """Distinctive tokens WITHOUT abbreviation normalisation — used only to tell whether a
    match leaned on the abbreviation table (for the ``abbrev`` annotation)."""
    return {t.lower() for t in re.findall(r"[A-Za-z][\w'’\-]+", text or "")
            if t.lower() not in _STOPWORDS and len(t) > 2}


def _match_single(name_tokens: set[str], cases: list, ry: int | None,
                  confirm_text: bool) -> tuple[str, float, str] | None:
    """One-party report match: the single distinctive surname must land on exactly one
    pool case in the reporting-lag year window, confirmed in that judgment's opening."""
    hits = []
    for c in cases:
        year = _as_int(_attr(c, "year"))
        if ry is not None and year is not None and not (ry - 2 <= year <= ry + 1):
            continue
        if name_tokens <= surnames(_attr(c, "title")):
            hits.append(c)
    if len(hits) != 1:
        return None  # zero, or ambiguous → don't guess
    best = hits[0]
    opening = _attr(best, "opening")
    if confirm_text and opening and not (name_tokens <= surnames(opening)):
        return None
    return _attr(best, "stable_id"), 0.5, "single"


def _attr(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_int(v):
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None
