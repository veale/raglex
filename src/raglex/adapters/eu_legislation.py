"""EU legislation adapter — CELLAR Formex (the published machine-readable format).

AKN4EU is the EU's interinstitutional drafting/exchange standard, but CELLAR does
not currently publish an ``akn`` manifestation for acts (verified: even the 2024
AI Act exposes only ``fmx4`` / ``xhtml`` / ``pdf``), so the reliable structured
route is **Formex 4** — its ``<ACT>`` content member carries the full ``<ARTICLE>``
hierarchy (99 articles for the GDPR). The stable_id is the CELEX, so harvesting
the GDPR resolves every "interprets 32016R0679" edge the CELLAR case adapter
emitted (§5b). When CELLAR starts publishing an AKN4EU manifestation, it's a new
``format`` parser — the adapter is unchanged.

Default targets are the core EU data-protection instruments; override with
``-o celex=32016R0679,32016L0680``.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats import parse

CELEX_BASE = "https://publications.europa.eu/resource/celex"

# Sector-1 (primary law) descriptors → the instrument the article belongs to.
_TREATY = {"E": "TFEU", "M": "TEU", "F": "TEU (pre-Lisbon)", "C": "EC Treaty",
           "A": "Euratom Treaty", "P": "Charter of Fundamental Rights", "D": "EEA Agreement"}
_DESC_KIND = {"R": "Regulation", "L": "Directive", "D": "Decision",
              "Q": "Institutional act", "M": "Other act"}
# Titles the EUR-Lex HTML gives that are NOT real titles (just the CELEX, an OJ
# filename, or a stray heading like "ANNEX").
_GENERIC_TITLE = re.compile(r"^\s*(?:EUR-Lex\b.*|ANNEX|[A-Z]_\d.*\.xml|)\s*$", re.IGNORECASE)


def celex_title(celex: str) -> str | None:
    """A human title derived from a CELEX when the source gives none: treaty/Charter
    articles → "Article 267 TFEU"; secondary law → "Regulation 2016/679"."""
    m = re.match(r"^(?P<sector>[1-9])(?P<year>\d{4})(?P<desc>[A-Z]{1,2})(?P<num>\d+)", celex.upper())
    if not m:
        return None
    sector, year, desc, num = m.group("sector"), m.group("year"), m.group("desc"), m.group("num")
    if sector == "1":  # primary law: the number is the article number
        inst = _TREATY.get(desc[0], "EU primary law")
        return f"Article {int(num)} {inst}"
    kind = _DESC_KIND.get(desc[0])
    if kind:
        # directives are cited year/number, regulations number/year (pre-2015) — show
        # the colloquial order so it reads like a normal citation
        a, b = (year, str(int(num))) if (desc[0] == "L" or int(year) >= 2015) else (str(int(num)), year)
        return f"{kind} {a}/{b}"
    return None


def _is_generic_title(t: str | None) -> bool:
    return not t or bool(_GENERIC_TITLE.match(t))

DEFAULT_CELEX = (
    "32016R0679",  # GDPR
    "32016L0680",  # Law Enforcement Directive
    "32018R1725",  # EUDPR (EU institutions)
    "32002L0058",  # ePrivacy Directive
)


class EULegislationAdapter(BaseAdapter):
    source = "eu-legislation"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, celex: str | tuple[str, ...] | None = None, client: RateLimitedClient | None = None) -> None:
        if isinstance(celex, str):
            celex = tuple(c.strip() for c in celex.split(",") if c.strip())
        self.celex_list = tuple(celex) if celex else DEFAULT_CELEX
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        for celex in self.celex_list:
            yield Stub(
                stable_id=celex,
                landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                raw_url=f"{CELEX_BASE}/{celex}",
                court=None,
            )

    def fetch(self, stub: Stub) -> Record | None:
        # Try Formex; a 404/FetchError (no Formex rendition) is NOT fatal — fall
        # through to the EUR-Lex HTML below rather than giving up.
        raw = None
        try:
            resp = self._client.get(
                stub.raw_url,
                headers={"Accept": "application/zip;mtype=fmx4", "Accept-Language": "eng"},
            )
            if getattr(resp, "status_code", 200) < 400:
                raw = resp.content
        except FetchError:
            raw = None
        parsed = parse("formex-legislation", raw) if raw else None
        if parsed and parsed.text:
            return Record(
                source=self.source,
                stable_id=stub.stable_id,  # CELEX — the resolution target (§5b)
                doc_type=DocType.LEGISLATION,
                title=parsed.title or stub.stable_id,
                language="en", source_language="en",
                landing_url=stub.landing_url,
                raw_bytes=raw, raw_ext="zip",
                text=parsed.text, segments=parsed.segments, relations=parsed.relations,
                extracted_via=ExtractedVia.STRUCTURED,
                extra={"format": "formex-legislation", "celex": stub.stable_id},
            )
        # No Formex rendition (common for old instruments like Directive 95/46) — fall
        # back to the EUR-Lex HTML, which exists even when Formex doesn't.
        html = self._fetch_html(stub.stable_id)
        hp = parse("eurlex-html", html) if html else None
        if hp and hp.text:
            # the EUR-Lex HTML <title> is often generic ("EUR-Lex - 12008E267 - EN")
            # or a stray heading ("ANNEX") — derive a real title from the CELEX then.
            title = celex_title(stub.stable_id) if _is_generic_title(hp.title) else hp.title
            return Record(
                source=self.source, stable_id=stub.stable_id, doc_type=DocType.LEGISLATION,
                title=title or stub.stable_id, language="en", source_language="en",
                landing_url=stub.landing_url, raw_bytes=html, raw_ext="html",
                text=hp.text, segments=hp.segments, relations=hp.relations,
                extracted_via=ExtractedVia.STRUCTURED,
                extra={"format": "eurlex-html", "celex": stub.stable_id},
            )
        # Neither Formex nor HTML parsed — register a metadata stub so the (often
        # heavily-cited) instrument is still a real, clickable node and its citations
        # resolve (§5b); text can be backfilled later.
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.LEGISLATION,
            title=stub.stable_id, language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=stub.stable_id.encode(), raw_ext="txt",
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"celex": stub.stable_id, "metadata_only": True},
        )

    def _fetch_html(self, celex: str) -> bytes | None:
        """The EUR-Lex rendered HTML for a CELEX (the fallback when no Formex)."""
        url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:{celex}"
        try:
            r = self._client.get(url, headers={"Accept-Language": "eng"})
        except FetchError:
            return None
        return r.content if getattr(r, "status_code", 200) < 400 else None
