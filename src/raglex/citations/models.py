"""The unit of citation extraction (§5, §5b).

A ``Citation`` is a *recognised reference* found in a document's text — not yet a
graph edge. It carries everything needed to (a) make a hanging edge now and
(b) resolve it later: the literal text, the **entity kind** (case / act /
regulation / …), a **candidate id** in resolvable form (ECLI / CELEX / legislation
URI), an optional **pinpoint** (which article/section — becomes the edge's
``dst_anchor``), the char span (for treatment classification over the surrounding
text, §1.3a), and provenance (which grammar, confidence).

Extraction and resolution are deliberately separate concerns: extraction
*recognises* references and proposes candidates; resolution (§5b) *identifies*
which corpus node a candidate is, retrying hanging edges until the target lands.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Citation:
    raw: str  # the literal matched text
    entity_kind: str  # case | regulation | directive | decision | act | treaty | opinion | eu_instrument
    candidate_id: str | None  # normalised, resolvable form (ECLI / CELEX / leg URI), or None
    pinpoint: str | None  # "Article 17" / "s. 14" → the edge dst_anchor
    char_start: int
    char_end: int
    method: str  # the grammar name that produced it
    confidence: float = 1.0
