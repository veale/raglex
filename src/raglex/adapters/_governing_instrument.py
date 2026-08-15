"""What a guidance document is *about*, for ``citation_default_instrument``.

A single-subject register — the AEPD's resoluciones, AESIA's AI Act guides, the
Garante's provvedimenti — states its governing instrument once, in a *visto* or a
cover paragraph, and then writes "el artículo 9" for the rest of the document. The
carry-forward pass can only tether those to an instrument the text actually named, so a
guide whose PDF text begins after the cover page tethers nothing at all.

``citation_default_instrument`` is how an adapter supplies that missing antecedent (see
``docs/adapter-authoring.md``). Two rules, in this order:

1. **The document's own title wins.** An AESIA register is about the AI Act, but guide
   07 is *"Guía de datos y gobernanza de datos"* and an AEPD guide may be about the
   LSSI; where the title names exactly one instrument, that instrument governs that
   document, not the register's default.
2. **Otherwise the register's regime applies** — the GDPR for a data-protection
   authority, the AI Act for AESIA.

A title naming *two* instruments falls back to the default rather than guessing, which
is the same rule ``docs/adapter-authoring.md`` states for mixed registers: only set the
field when exactly one instrument is identified.
"""

from __future__ import annotations

#: The citation kinds that can be a document's governing instrument. A case or a
#: guidance series is never one, however often the title names it.
_LEGISLATIVE_KINDS = frozenset({
    "act", "regulation", "directive", "decision", "treaty", "eu_instrument", "named",
})


def title_instrument(title: str | None) -> dict | None:
    """The single instrument a title names, or ``None``.

    Every language grammar is run, not only the register's own: the Garante titles a
    measure in Italian and the AEPD in Spanish, but both cite ``Regolamento``/
    ``Reglamento (UE) 2016/679`` in a form the EU grammar also reads, and a Spanish
    register occasionally publishes an English-titled joint statement.
    """
    from ..citations.extractor import all_grammar_citations

    hits = [c for c in all_grammar_citations(str(title or ""))
            if c.candidate_id and c.entity_kind in _LEGISLATIVE_KINDS]
    unique = list(dict.fromkeys(c.candidate_id for c in hits))
    if len(unique) != 1:
        return None
    host = next(c for c in hits if c.candidate_id == unique[0])
    return {"id": unique[0], "kind": host.entity_kind or "named"}


def default_instrument(title: str | None, fallback: dict | None) -> dict | None:
    """The ``citation_default_instrument`` for one document of a single-subject register.

    ``fallback`` is the register's regime — pass ``None`` for a register that has no
    single one, and a title-less document there will correctly declare nothing.
    """
    return title_instrument(title) or (dict(fallback) if fallback else None)


#: The two regimes this repository's Spanish and Italian registers run on.
GDPR = {"id": "32016R0679", "kind": "regulation"}
AI_ACT = {"id": "32024R1689", "kind": "regulation"}
