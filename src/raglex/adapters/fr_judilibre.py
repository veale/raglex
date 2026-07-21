"""France — Judilibre (Cour de cassation) case-law adapter over the PISTE gateway.

The flagship French source and the closest analogue to ``nl-rechtspraak``: ECLI-native,
incremental, and it **ships typed edges for free** — the Court itself authors both the
*textes appliqués* (applied legislation) and the *rapprochements de jurisprudence*
(related decisions), so we mint high-confidence structured edges rather than re-deriving
them from prose (§1.3a).

Two-step contract, like Rechtspraak:
- ``discover(since)`` walks ``GET /export`` by **update date** (``date_type=update``,
  ``date_start=since``), paging on the returned ``next_batch`` cursor. ``/export`` exists
  precisely for third-party indexing and already returns whole decisions, so the payload
  is stashed on the stub to avoid a second round-trip; a targeted harvest by id still
  falls through to ``GET /decision``.
- ``fetch`` normalises one decision: **ECLI** is the primary key, the **zones**
  (*introduction, expose_du_litige, moyens, motivations, dispositif, moyens_annexes*) become
  native chunk ``Segment``s straight off the source's own offsets (§6b), and the *visa* /
  *rapprochements* become typed ``interprets`` / ``considers`` edges.

Auth is the shared :class:`PisteClient`. Without PISTE credentials the adapter yields
nothing (degrade safely, §5). The text is pseudonymised upstream — fine for RAG.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    Stub,
    TypedRelation,
)
from ._piste import PisteClient, piste_api_root
from ..citations.french import pourvoi_alias

_APP = "cassation/judilibre/v1.0"

# The `zones` object's keys (Judilibre `zone` schema), in the Court's layout order.
# Each maps to a list of `zoneSegment` {start,end} offsets into `text`.
_ZONE_ORDER = ("introduction", "expose", "moyens", "motivations", "dispositif", "annexes")

# A Légifrance article/text id inside a visa's URL → a resolvable destination.
_LEGIFRANCE_ID_RE = re.compile(r"(LEGIARTI\d+|LEGITEXT\d+|JORFARTI\d+|JORFTEXT\d+)")


@dataclass(slots=True)
class ParsedDecision:
    ecli: str | None = None
    number: str | None = None
    title: str | None = None
    jurisdiction: str | None = None
    chamber: str | None = None
    formation: str | None = None
    solution: str | None = None
    publication: list[str] = field(default_factory=list)
    nac: str | None = None
    decision_date: date | None = None
    text: str | None = None
    segments: list[Segment] = field(default_factory=list)
    relations: list[TypedRelation] = field(default_factory=list)


def _iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _zone_segments(text: str, zones: dict) -> list[Segment]:
    """Turn Judilibre's ``zones`` (name → [{start,end}] offsets into ``text``) into
    ``Segment``s, in the Court's layout order, so the chunker splits on the decision's
    own functional seams (the *motivations*, the *dispositif*) rather than re-guessing."""
    if not (text and isinstance(zones, dict)):
        return []
    ordered = [z for z in _ZONE_ORDER if z in zones] + [z for z in zones if z not in _ZONE_ORDER]
    segs: list[Segment] = []
    n = len(text)
    for name in ordered:
        spans = zones.get(name) or []
        if isinstance(spans, dict):
            spans = [spans]
        for span in spans:
            try:
                start, end = int(span["start"]), int(span["end"])
            except (KeyError, TypeError, ValueError):
                continue
            start, end = max(0, start), min(n, end)
            if end > start:
                segs.append(Segment(label=name, char_start=start, char_end=end, kind="zone"))
    segs.sort(key=lambda s: s.char_start)
    return segs


def _visa_relations(decision: dict) -> list[TypedRelation]:
    """*Textes appliqués* (``visa``: ``textLink`` {id, url, title}) → the case
    INTERPRETS the cited legislation. A Légifrance id lifted from the URL is a
    resolvable destination against fr-legislation (§5b); otherwise the edge dangles."""
    rels: list[TypedRelation] = []
    for visa in decision.get("visa") or []:
        if not isinstance(visa, dict):
            continue
        title = (visa.get("title") or "").strip()
        if not title:
            continue
        m = _LEGIFRANCE_ID_RE.search(visa.get("url") or "")
        rels.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=title,
            dst_id=m.group(1) if m else None,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return rels


def _rapprochement_relations(decision: dict) -> list[TypedRelation]:
    """*Rapprochements de jurisprudence* (``decisionLink``) → court-authored
    case-to-case edges. High confidence (structured), so ingested as ``considers``
    rather than re-derived. ``decisionLink`` carries no ECLI (only a Judilibre id +
    title + number), so the edge dangles on the title until the target is harvested."""
    rels: list[TypedRelation] = []
    for rap in decision.get("rapprochements") or []:
        if not isinstance(rap, dict):
            continue
        label = rap.get("title") or rap.get("number") or rap.get("id")
        if not label:
            continue
        # keep the Judilibre id + number in the raw string so a later resolver can
        # look the target up and back-fill its ECLI.
        raw = " | ".join(str(x) for x in (rap.get("title"), rap.get("number"),
                                          rap.get("jurisdiction"), rap.get("id")) if x)
        rels.append(TypedRelation(
            relationship_type=RelationshipType.CONSIDERS,
            raw_citation_string=raw or str(label),
            dst_id=None,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return rels


def parse_decision(decision: dict) -> ParsedDecision:
    """One ``/decision`` (or ``/export`` result) object → normalised fields + edges (pure)."""
    text = decision.get("text")
    number = decision.get("number") or (decision.get("numbers") or [None])[0]
    parsed = ParsedDecision(
        ecli=decision.get("ecli"),
        number=number,
        jurisdiction=decision.get("jurisdiction"),
        chamber=decision.get("chamber"),
        formation=decision.get("formation"),
        solution=decision.get("solution"),
        publication=list(decision.get("publication") or []),
        nac=decision.get("nac"),
        decision_date=_iso_date(decision.get("decision_date") or decision.get("decisionDate")),
        text=text,
        segments=_zone_segments(text, decision.get("zones") or {}),
    )
    # a readable case name: chamber + number ("Cour de cassation, Chambre civile 1, 21-00400")
    bits = [b for b in (decision.get("jurisdiction"), parsed.chamber, number) if b]
    parsed.title = ", ".join(str(b) for b in bits) or parsed.ecli
    parsed.relations = _visa_relations(decision) + _rapprochement_relations(decision)
    return parsed


class FrJudilibreAdapter(BaseAdapter):
    source = "fr-judilibre"
    min_interval = 0.3
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        ids: str | list[str] | None = None,
        batch_size: int = 100,
        client: PisteClient | None = None,
    ) -> None:
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = ids or []
        self.batch_size = min(batch_size, 100)  # /export caps at 100 per batch
        # Judilibre auth depends on the app's PISTE plan: KeyId (API-key plan) or Bearer
        # (OAuth plan). "auto" uses the KeyId when one is configured, else OAuth.
        self._client = client or PisteClient(self.source, auth="auto",
                                             min_interval=self.min_interval)

    def _get(self, path: str, params: dict) -> dict:
        resp = self._client.get(f"{piste_api_root()}/{_APP}/{path}", params=params,
                                headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            return {}
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return {}

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if not self._client.configured():
            return
        if self.ids:
            for ident in self.ids:
                yield Stub(stable_id=ident, hints={"id": ident})
            return
        batch = 0
        pages = 0
        while True:
            params = {"batch": batch, "batch_size": self.batch_size,
                      "date_type": "update", "order": "asc", "resolve_references": "true"}
            if since:
                params["date_start"] = since
            body = self._get("export", params)
            results = body.get("results") or []
            if not results:
                return
            for decision in results:
                ecli = decision.get("ecli")
                ident = decision.get("id")
                yield Stub(
                    stable_id=ecli or ident,
                    hint_date=_iso_date(decision.get("decision_date")),
                    # stash the whole exported decision so fetch needn't re-request it
                    hints={"id": ident, "decision": decision},
                )
            pages += 1
            # `next_batch` is a URL (null on the last batch) — advance the batch index
            # until it runs out (or the 10,000/window cap ends the results).
            if body.get("next_batch") is None or (max_pages is not None and pages >= max_pages):
                return
            batch += 1

    def fetch(self, stub: Stub) -> Record | None:
        decision = stub.hints.get("decision")
        if decision is None:
            ident = stub.hints.get("id") or stub.stable_id
            body = self._get("decision", {"id": ident, "resolve_references": "true"})
            # /decision returns the decision at the top level (or under "results")
            decision = body if body.get("text") is not None else (body.get("results") or [None])[0]
        if not decision:
            return None

        parsed = parse_decision(decision)
        ecli = parsed.ecli or stub.stable_id
        return Record(
            source=self.source,
            stable_id=ecli,
            ecli=ecli if str(ecli).startswith("ECLI:") else None,
            doc_type=DocType.JUDGMENT,
            title=parsed.title or stub.title,
            court=" / ".join(b for b in (parsed.jurisdiction, parsed.chamber) if b) or "Cour de cassation",
            decision_date=parsed.decision_date or stub.hint_date,
            language="fr",
            source_language="fr",
            landing_url=f"https://www.courdecassation.fr/decision/{decision.get('id', '')}",
            raw_bytes=json.dumps(decision, ensure_ascii=False).encode("utf-8"),
            raw_ext="json",
            text=parsed.text,
            segments=parsed.segments,
            relations=parsed.relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "number": parsed.number, "formation": parsed.formation,
                "solution": parsed.solution, "nac": parsed.nac,
                "publication": parsed.publication or None,
                "aliases": [x for x in (decision.get("id"),
                                         pourvoi_alias(parsed.number) if parsed.number else None)
                            if x],
            }.items() if v},
        )
