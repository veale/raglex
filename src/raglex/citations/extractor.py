"""Citation extraction over free text (§5).

Runs every registered grammar over the text, then resolves overlaps so a single
reference yields one citation (the most specific match wins — "Article 17 of
Regulation (EU) 2016/679" beats the bare "2016/679" inside it). Output is a list
of ``Citation`` with char spans; the stage turns each into a hanging typed edge
(§5b) that resolution links later.

An ``llm`` extractor — for narrative citations a grammar can't catch ("the Court's
earlier data-retention ruling") — slots in behind the same ``extract`` signature
and is batched (§5); grammars stay the cheap, deterministic first pass.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

from .grammars import DROP, GRAMMARS
from .models import Citation

# A pinpoint into a *cited case*: the paragraph number that trails the citation —
# "… at [57]", "(per Mostyn J at 57 and 62/63)", "[5]", "at para 131". Three precise
# forms so it can't grab the next citation's year: an at/para cue + number, or a
# fully-bracketed 1–3 digit "[57]". Years (19xx/20xx) are excluded.
_CASE_PINPOINT = re.compile(
    r"^[\s,;]*\(?\s*(?:per\b[^.;)\n]{0,40}?\s)?(?:"
    r"(?:at|in)\s+(?:paras?\.?\s*|paragraphs?\s+|§§?\s*)?\[?(?P<a>\d{1,4})\]?"
    r"|(?:paras?\.?\s*|paragraphs?\s+)\[?(?P<b>\d{1,4})\]?"
    r"|§§?\s*(?P<d>\d{1,4})"  # ECHR uses § for paragraphs ("Golder v UK, § 35")
    r"|\[(?P<c>\d{1,3})\]"
    r")",
    re.IGNORECASE,
)


def _attach_case_pinpoints(text: str, cites: list[Citation]) -> list[Citation]:
    """For case citations with no pinpoint, look just after the citation for a
    paragraph reference ("at [57]", "[5]") and attach it — JADE-style pinpoint links
    into the cited judgment."""
    out: list[Citation] = []
    for c in cites:
        if c.pinpoint or c.entity_kind not in ("case", "opinion"):
            out.append(c)
            continue
        m = _CASE_PINPOINT.match(text[c.char_end: c.char_end + 40])
        num = m and (m.group("a") or m.group("b") or m.group("c") or m.group("d"))
        if num and not re.fullmatch(r"(?:19|20)\d{2}", num):  # not a year
            out.append(replace(c, pinpoint=f"para {num}"))
        else:
            out.append(c)
    return out


# A *bare* provision reference with no statute named alongside it — "section 5",
# "Article 6", "regulation 3", "paragraph 12 of Schedule 1". On its own it doesn't
# say which instrument; the carry-forward pass attaches it to the last-named one.
_BARE_PROVISION = re.compile(
    r"\b(?P<cue>section|sections|sub-?section|s|ss|article|articles|art|arts|"
    r"recital|recitals|"
    r"regulation|regulations|reg|regs|paragraph|paragraphs|para|paras|schedule|sch)\.?\s*"
    r"(?P<num>\d+[A-Z]?(?:\(\d+[A-Z]?\))*)\b",
    re.IGNORECASE,
)
# carry-forward only attaches a bare provision to a *legislation* antecedent — a
# bare "section 5" never means a paragraph of a cited case.
_LEG_KINDS = {"act", "regulation", "directive", "decision", "treaty", "eu_instrument", "named"}

# EU instruments are divided into *Articles*, UK Acts/SIs into *sections* and *schedules*.
# So the cue word disambiguates the antecedent: a bare "section 66" can't belong to an EU
# directive, and a bare "Article 6" can't belong to a UK Act. This stops a "section N" from
# carrying forward onto a nearer-but-wrong EU instrument (e.g. Directive 2003/4 in an
# Environmental-Information case where the Communications Act is the real host).
_EU_KINDS = {"directive", "decision", "treaty", "eu_instrument"}


def _cue_allows(cue: str, kind: str) -> bool:
    """Whether a bare-provision ``cue`` ("section", "Article", …) can attach to an
    antecedent of this ``entity_kind``."""
    c = cue.lower().rstrip(".")
    if c.startswith(("section", "sub", "ss", "schedule", "sch")) or c == "s":
        return kind not in _EU_KINDS          # UK statutory provision → not an EU instrument
    if c.startswith(("article", "art")):
        return kind in _EU_KINDS              # Article → EU instrument / treaty, not a UK Act
    if c.startswith("recital"):
        # Recitals belong to EU instruments (regulations included — the GDPR is one) and
        # never to a UK Act, which has no recitals.
        return kind in _EU_KINDS or kind in {"regulation", "named"}
    return True                                # regulation / paragraph — leave to nearest


def _bare_pinpoint(cue: str, num: str) -> str:
    c = cue.lower().rstrip(".")
    if c.startswith("recital"):
        return f"Recital {num}"
    if c.startswith(("article", "art")):
        return f"Article {num}"
    if c.startswith(("regulation", "reg")):
        return f"reg. {num}"
    if c.startswith(("paragraph", "para")):
        return f"para {num}"
    if c.startswith(("schedule", "sch")):
        return f"Sch. {num}"
    return f"s. {num}"


def _attach_carry_forward(text: str, kept: list[Citation]) -> list[Citation]:
    """Heuristic (§5): a bare "section 5" / "Article 6" with no statute named in the
    same breath is taken to refer to the **most recently mentioned legislation**, even
    several paragraphs earlier. Emits a low-confidence ``carry_forward`` citation so
    the resulting edge is flagged uncertain (provenance ``inferred``) for human review.
    Skips any bare reference already inside a fuller, literal citation."""
    occupied = sorted((c.char_start, c.char_end) for c in kept)
    # every citation in document order — used to find what a bare reference FOLLOWS
    all_sorted = sorted(kept, key=lambda c: c.char_start)
    # legislation antecedents in document order, with their candidate + kind
    antecedents = sorted(
        (c for c in kept if c.candidate_id and c.entity_kind in _LEG_KINDS),
        key=lambda c: c.char_start,
    )
    if not antecedents:
        return kept
    out = list(kept)
    for m in _BARE_PROVISION.finditer(text):
        s, e = m.start(), m.end()
        if any(os <= s and e <= oe for os, oe in occupied):
            continue  # already part of a literal citation ("s.5 of the FOIA 2000")
        cue = m.group("cue").lower().rstrip(".")
        # A "paragraph N" whose nearest preceding citation is a CASE is that
        # judgment's pinpoint, not a provision of whatever instrument was last
        # named — the CJEU's own citation form ends every case reference with
        # ", C-597/19, EU:C:2021:492, paragraph 107". Attaching those to the
        # last-named directive minted a phantom legislation edge per case cite
        # (the 2026-07 C-604/22 bug). Paragraph cues defer to a nearby case.
        if cue.startswith("para"):
            prev = [c for c in all_sorted if c.char_end <= s and s - c.char_end <= 80]
            if prev and prev[-1].entity_kind in ("case", "opinion"):
                continue
        prior = [a for a in antecedents if a.char_end <= s
                 and _cue_allows(m.group("cue"), a.entity_kind)]
        if not prior:
            continue
        host = prior[-1]  # nearest preceding named instrument of a compatible kind
        out.append(Citation(
            raw=m.group(0), entity_kind=host.entity_kind, candidate_id=host.candidate_id,
            pinpoint=_bare_pinpoint(m.group("cue"), m.group("num")),
            char_start=s, char_end=e, method="carry_forward", confidence=0.4,
        ))
    return out


class CitationExtractor(Protocol):
    def extract(self, text: str) -> list[Citation]:
        ...


def grammar_citations(text: str) -> list[Citation]:
    """The deterministic first pass: every registered grammar over the text."""
    found: list[Citation] = []
    for g in GRAMMARS.values():
        for m in g.pattern.finditer(text):
            candidate, pinpoint, kind_override = g.normalize(m)
            if kind_override is DROP:
                continue  # normaliser rejected it as non-citation noise (currency/ISBN/…)
            found.append(
                Citation(
                    raw=m.group(0).strip(),
                    entity_kind=kind_override or g.entity_kind,
                    candidate_id=candidate,
                    pinpoint=pinpoint,
                    char_start=m.start(),
                    char_end=m.end(),
                    method=g.name,
                )
            )
    return found


def alias_citations(text: str, aliases: dict[str, str]) -> list[Citation]:
    """Citations from user-defined shorthand *rules* ("UK GDPR" → a document id):
    every occurrence of a phrase becomes a link to its target, so the rule propagates
    across the corpus. Word-boundary, case-insensitive; longer phrases win overlaps."""
    found: list[Citation] = []
    for phrase, target in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        if not phrase or not target:
            continue
        # \b only guards against mid-word matches when the adjacent phrase character
        # is itself a word character. On a non-word edge — e.g. an alias like "(UK)
        # GDPR" — a bare \b demands a boundary that never exists there, so the phrase
        # silently never matches. Apply the boundary per edge only when it helps.
        lb = r"\b" if phrase[0].isalnum() or phrase[0] == "_" else ""
        rb = r"\b" if phrase[-1].isalnum() or phrase[-1] == "_" else ""
        for m in re.finditer(rf"{lb}{re.escape(phrase)}{rb}", text, re.IGNORECASE):
            found.append(Citation(raw=m.group(0), entity_kind="named", candidate_id=target,
                                  pinpoint=None, char_start=m.start(), char_end=m.end(),
                                  method="named_alias"))
    return found


def extract_citations(text: str, *, llm: CitationExtractor | None = None,
                      aliases: dict[str, str] | None = None) -> list[Citation]:
    """Recognise citations in ``text``. Grammars run first (deterministic, cheap),
    then user-defined shorthand rules (``aliases``), then an optional ``llm`` pass for
    narrative citations. More specific / earlier matches win an overlap."""
    if not text:
        return []
    # User shorthand rules take precedence over the built-in grammars on an overlap: a
    # person who defines "UK GDPR" → X means it, over any generic grammar. They lead the
    # list so the stable longest-match dedupe keeps them on a span tie.
    cites = alias_citations(text, aliases) if aliases else []
    cites += grammar_citations(text)
    grammar = _dedupe_overlaps(cites)
    if llm is None:
        return _attach_carry_forward(text, _attach_case_pinpoints(text, grammar))
    extra = [c for c in llm.extract(text) if not _overlaps_any(c, grammar)]
    merged = _attach_case_pinpoints(text, _dedupe_overlaps(grammar + extra))
    return _attach_carry_forward(text, merged)


def _overlaps_any(c: Citation, kept: list[Citation]) -> bool:
    return any(c.char_start < k.char_end and k.char_start < c.char_end for k in kept)


def _dedupe_overlaps(cites: list[Citation]) -> list[Citation]:
    """Keep the longest match at each location; drop spans contained in a kept one
    (so the article-scoped citation wins over the bare instrument number)."""
    ordered = sorted(cites, key=lambda c: (c.char_start, -(c.char_end - c.char_start)))
    kept: list[Citation] = []
    occupied: list[tuple[int, int]] = []
    for c in ordered:
        if any(s <= c.char_start and c.char_end <= e for s, e in occupied):
            continue
        kept.append(c)
        occupied.append((c.char_start, c.char_end))
    return kept
