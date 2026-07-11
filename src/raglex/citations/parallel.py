"""Mine parallel (co-referent) citations from judgment text (§5c).

No authoritative neutral-citation ↔ law-report table exists, so we recover it from the
corpus itself. Two textual signals, from strong to weak:

  1. **Adjacency** — within one judgment a case's parallel citations are printed together,
     separated only by ``;`` / ``,`` and pinpoints: "Pepper v Hart [1993] AC 593; [1992] 3
     WLR 1032; [1992] STC 898". Those are the *same case*, stated by the court.
  2. **Name + year coreference** — across judgments, two citations sharing a distinctive
     case name and the same/adjacent year are *putatively* the same case.

Adjacency runs are unioned into global clusters (transitively: "AC 593; WLR 1032" in one
judgment and "WLR 1032; All ER 42" in another give one cluster of three). Coreference adds
weaker links. Anchoring a cluster to a held document and aliasing every member to it means
a citation in *any* parallel form resolves to the one case.

The correctness invariant: **a cluster holds at most one distinct neutral citation** —
neutral citations are unique per judgment, so a merge that would put two in one cluster
(e.g. an appeal history "[2019] UKSC 1; [2019] EWCA Civ 5", which are *different*
documents) is vetoed. That single rule is what makes the putative rung safe to run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..resolve.matchers import first_candidate
from .report_match import surnames
from .reporters import is_report_citation


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One citation occurrence in a document's text (a row of the ``citations`` table).

    ``candidate`` is the case slug the extractor already resolved this occurrence to (from
    the ``citations`` table), when it is a neutral citation of a *case* — the authoritative
    signal, since the extractor's grammars recognise inline-division forms ("EWCA Civ 5")
    that the lighter :func:`first_candidate` does not."""

    raw: str
    char_start: int
    char_end: int
    candidate: str | None = None


def neutral_slug(raw: str) -> str | None:
    """The Find Case Law slug a citation string encodes if it's a *neutral* citation
    ("[2015] EWHC 100 (Ch)" → "ewhc/ch/2015/100"), else None. Law reports resolve to no
    slug. A string-only fallback for when the extractor's candidate isn't available."""
    c = first_candidate(raw)
    return c.value if (c and "/" in c.value) else None


def occ_neutral(o: Occurrence) -> str | None:
    """The neutral-citation slug for an occurrence — the extractor's stored ``candidate``
    when present, else derived from the raw string."""
    if o.candidate and "/" in o.candidate:
        return o.candidate
    return neutral_slug(o.raw)


# The text permitted *between* two parallel citations: separators (``;`` ``,``), a
# conjunction, and a pinpoint (page/paragraph numbers, "at 599", "pp 12-14"). Anything
# else — a new word, a name, "See", "In" — ends the run.
_ALLOWED_GAP_TOKEN = re.compile(r"\b(?:at|and|pp?|para|paras|paragraph|n)\b", re.IGNORECASE)


def _is_parallel_gap(gap: str) -> bool:
    """True when the text separating two adjacent citations is only punctuation, a
    conjunction and/or a pinpoint — i.e. they read as parallel citations of one case."""
    if len(gap) > 40:
        return False
    g = gap.strip()
    if not g or (";" not in g and "," not in g):
        return False  # parallels are list-separated; a bare space is just prose
    residue = _ALLOWED_GAP_TOKEN.sub(" ", g)
    residue = re.sub(r"[\d;,.\-–—()\[\]§¶\s]", " ", residue)
    return residue.strip() == ""


def _year_of(raw: str) -> int | None:
    m = re.search(r"[\[(](1[6-9]\d{2}|20\d{2})[\])]", raw or "")
    return int(m.group(1)) if m else None


def adjacency_groups(text: str, occs: list[Occurrence]) -> list[list[str]]:
    """Runs of parallel citations in one judgment's ``text``.

    ``occs`` need not be sorted. Returns each maximal adjacent run of ≥2 *distinct* raw
    citation strings whose separating text is a parallel gap. A run that names two or more
    different neutral citations is dropped (appeal history, not parallels — see the module
    invariant)."""
    ordered = sorted(occs, key=lambda o: (o.char_start if o.char_start is not None else 0))
    groups: list[list[str]] = []
    run: list[Occurrence] = []

    def flush() -> None:
        raws = list(dict.fromkeys(o.raw for o in run))
        neutrals = {n for o in run if (n := occ_neutral(o))}
        if len(raws) >= 2 and len(neutrals) <= 1:
            groups.append(raws)

    for o in ordered:
        if o.char_start is None or o.char_end is None:
            continue
        if run:
            prev = run[-1]
            gap = text[prev.char_end: o.char_start] if 0 <= prev.char_end <= o.char_start else "?"
            if _is_parallel_gap(gap):
                run.append(o)
                continue
            flush()
            run = []
        run = [o] if not run else run + [o]
    flush()
    return groups


class ClusterIndex:
    """Union-find over folded citation strings that enforces the one-neutral invariant:
    two clusters are merged only if they don't between them name two *different* neutral
    citations. ``union`` returns False when the merge is vetoed."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._neutral: dict[str, str | None] = {}

    def add(self, key: str, *, neutral: str | None = None) -> None:
        if key not in self._parent:
            self._parent[key] = key
            self._neutral[key] = neutral

    def _find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path-compress
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> bool:
        self.add(a)
        self.add(b)
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return True
        na, nb = self._neutral[ra], self._neutral[rb]
        if na and nb and na != nb:
            return False  # veto: two distinct neutral citations can't be one case
        self._parent[rb] = ra
        self._neutral[ra] = na or nb
        return True

    def clusters(self) -> list[list[str]]:
        """Members grouped by root, only clusters with ≥2 members."""
        by_root: dict[str, list[str]] = {}
        for key in self._parent:
            by_root.setdefault(self._find(key), []).append(key)
        return [members for members in by_root.values() if len(members) > 1]

    def neutral_of(self, key: str) -> str | None:
        return self._neutral[self._find(key)] if key in self._parent else None


def coref_key(name: str | None, raw: str) -> tuple[frozenset[str], int] | None:
    """The ``(distinctive-surname-tokens, year)`` key that links a citation across
    judgments on the weaker name+year rung — or None when it's too thin to be safe
    (fewer than two distinctive surname tokens, or no year). ``name`` is the case name the
    citing text printed beside ``raw``; ``raw`` supplies the year."""
    year = _year_of(raw)
    if year is None:
        return None
    toks = frozenset(surnames(name))
    if len(toks) < 2:
        return None
    return toks, year
