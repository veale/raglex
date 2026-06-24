"""LLM citation extractor (§5) — the recall layer over the deterministic grammars.

Grammars catch *well-formed* references (an ECLI, a CELEX, "[2024] UKSC 1",
"Article 17 GDPR"). They cannot catch references carried in prose: "the Court's
earlier data-retention judgment", "the Grand Chamber's ruling in the Spanish
right-to-be-forgotten case", "the Act's predecessor". This optional pass asks an
LLM to surface those, normalising to the same resolvable candidate forms the
grammars produce (ECLI / CELEX / legislation id) so resolution (§5b) treats them
identically.

It is additive and resilient: it runs *after* the grammars, only its findings
that don't overlap an existing span are kept, and if the model is unavailable it
contributes nothing (the grammar output stands). The model is told to return the
char-anchoring snippet so we can locate the span; we never trust offsets it
invents.
"""

from __future__ import annotations

from typing import Iterable

from .models import Citation

_SYSTEM = (
    "You extract legal citations from text. Find references to court cases, "
    "legislation (acts, regulations, directives), and legal instruments — "
    "INCLUDING ones written in prose without a formal citation (e.g. 'the Court's "
    "data-retention ruling', 'the predecessor Directive'). For each, give: the "
    "exact substring quoted from the text ('quote'); the entity kind (one of: "
    "case, act, regulation, directive, decision, treaty, opinion); and, when you "
    "are confident, a normalised resolvable id ('candidate') — an ECLI for an EU/"
    "national case, a CELEX for an EU instrument, or a legislation id like "
    "ukpga/2000/36 — plus an optional 'pinpoint' (e.g. 'Article 17', 's. 14'). "
    "Omit 'candidate' rather than guess. Do NOT invent citations not supported by "
    "the text."
)

_VALID_KINDS = {"case", "act", "regulation", "directive", "decision", "treaty", "opinion"}


class LLMCitationExtractor:
    """Narrative-citation pass behind the same shape as the grammar extractor.
    ``extract(text)`` returns ``Citation`` objects with real char spans located by
    finding each model-quoted snippet back in the source text."""

    def __init__(self, client=None) -> None:
        from ..llm import get_llm_client

        self._client = client or get_llm_client()

    def available(self) -> bool:
        return self._client.available()

    def extract(self, text: str) -> list[Citation]:
        if not text or not self._client.available():
            return []
        # Chunk long documents so each request stays within context; the client
        # batches the chunks into as few calls as practical.
        chunks = list(_windows(text))
        results = self._client.json_batch(
            _SYSTEM, [c.text for c in chunks],
            instruction=(
                'For each text item return {"index": i, "citations": [{"quote": ..., '
                '"kind": ..., "candidate": ..., "pinpoint": ...}]}.'
            ),
        )
        out: list[Citation] = []
        for chunk, entry in zip(chunks, results):
            if not isinstance(entry, dict):
                continue
            for c in entry.get("citations") or []:
                cit = _to_citation(c, text, chunk.start)
                if cit is not None:
                    out.append(cit)
        return out


class _Window:
    __slots__ = ("text", "start")

    def __init__(self, text: str, start: int) -> None:
        self.text = text
        self.start = start


def _windows(text: str, size: int = 6000, overlap: int = 400) -> Iterable[_Window]:
    if len(text) <= size:
        yield _Window(text, 0)
        return
    pos = 0
    while pos < len(text):
        yield _Window(text[pos : pos + size], pos)
        pos += size - overlap


def _to_citation(raw: dict, full_text: str, offset: int) -> Citation | None:
    """Validate one model citation and locate its span in the source. Anything we
    can't anchor to actual text is dropped (no hallucinated spans)."""
    if not isinstance(raw, dict):
        return None
    quote = (raw.get("quote") or "").strip()
    kind = str(raw.get("kind") or "").strip().lower()
    if not quote or kind not in _VALID_KINDS:
        return None
    # find the quote near its reported chunk (search the whole text as a fallback)
    idx = full_text.find(quote, offset)
    if idx == -1:
        idx = full_text.find(quote)
    if idx == -1:
        return None
    candidate = raw.get("candidate")
    candidate = str(candidate).strip() if candidate else None
    pinpoint = raw.get("pinpoint")
    pinpoint = str(pinpoint).strip() if pinpoint else None
    return Citation(
        raw=quote,
        entity_kind=kind,
        candidate_id=candidate or None,
        pinpoint=pinpoint or None,
        char_start=idx,
        char_end=idx + len(quote),
        method="llm",
        confidence=0.6,  # softer than a grammar match; resolution still gates it
    )
