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
from ..citations.french import pourvoi_alias, rg_alias

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


def _number_alias(decision: dict, number: str | None) -> str | None:
    """The key this decision can be cited by — and NOT a bare pourvoi key unless it
    really is one.

    A Cour de cassation number is a pourvoi number, unique nationally, and
    ``fr:pourvoi:21-00400`` is a sound key. A cour d'appel or tribunal judiciaire number
    is an RG number, unique only within the court that issued it: "24/00002" is a live
    docket at Nîmes, at Amiens and at three dozen other courts on the same day. Minting
    ``fr:pourvoi:24/00002`` for 1.3 million of those would not merely be noisy — the
    pipeline folds an ECLI-less record whose declared alias names a held document from
    another source into that document, and neither ca nor tj carries an ECLI. Unrelated
    judgments from different cities would have been merged into one node, and into the
    73,046 cours d'appel decisions the DILA bulk already holds under the same numbering.

    So the RG number is scoped by the court that issued it, which ``location`` gives."""
    if not number:
        return None
    juris = (decision.get("jurisdiction") or "").casefold()
    if "cassation" in juris or not juris:
        return pourvoi_alias(number)
    return rg_alias(decision.get("location") or juris, number)


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
        since_date: str | None = None,
        jurisdiction: str | None = None,
        client: PisteClient | None = None,
    ) -> None:
        # Judilibre is three registers behind one endpoint, and asking for none of them
        # gets you the first: cc (Cour de cassation, 566,124), ca (cours d'appel,
        # 626,374), tj (tribunaux judiciaires, 697,807).
        self.jurisdiction = (jurisdiction or "cc").strip().lower()
        if self.jurisdiction not in ("cc", "ca", "tj"):
            self.jurisdiction = "cc"
        if self.jurisdiction != "cc":
            self.source = f"fr-judilibre-{self.jurisdiction}"
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = ids or []
        # Where the offline bulk already covers, so a descending seed stops there.
        self.floor = (since_date or "").strip() or None
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
        # WHICH END TO START FROM. Both directions are a sweep over the same changes
        # feed, and the choice only matters when the walk is cut short — which it always
        # is, because a query is capped at 10,000 and the register holds 566,124.
        #
        #   incremental (a cursor exists) → ASCENDING from it. That is what a changes
        #   feed is for: small, ordered, resumable, and it cannot skip an update.
        #
        #   seeding (no cursor) → DESCENDING. Ascending from the beginning spends every
        #   window it is given on the 1860s; the first real backfill covered 10,000
        #   decisions, stopped in July 1971 and reported success. Whatever budget a run
        #   gets should buy the most recent law first and work back towards the point the
        #   DILA bulk already covers, so a truncated run leaves the OLDEST hole, not the
        #   newest.
        #
        # ``floor`` (source option ``since_date``) is where the bulk takes over — set it
        # to the newest decision DILA holds and a descending sweep stops there instead of
        # re-deriving two centuries the corpus already has.
        descending = not since
        batch = 0
        pages = 0
        cursor = since                      # ascending: lower bound, moves up
        edge: str | None = None             # descending: upper bound, moves down
        newest_seen: str | None = None
        oldest_seen: str | None = None
        while True:
            params = {"batch": batch, "batch_size": self.batch_size,
                      "date_type": "update", "resolve_references": "true",
                      "jurisdiction": self.jurisdiction,
                      "order": "desc" if descending else "asc"}
            if descending:
                if edge:
                    params["date_end"] = edge
                if self.floor:
                    params["date_start"] = self.floor
            elif cursor:
                params["date_start"] = cursor
            body = self._get("export", params)
            results = body.get("results") or []
            if not results:
                return
            feed_total = body.get("total")
            for decision in results:
                ecli = decision.get("ecli")
                ident = decision.get("id")
                update_date = decision.get("update_date")
                if update_date:
                    if newest_seen is None or update_date > newest_seen:
                        newest_seen = update_date
                    if oldest_seen is None or update_date < oldest_seen:
                        oldest_seen = update_date
                yield Stub(
                    stable_id=ecli or ident,
                    hint_date=_iso_date(decision.get("decision_date")),
                    # stash the whole exported decision so fetch needn't re-request it
                    hints={"id": ident, "decision": decision,
                           # The walk is a CHANGES FEED — ordered by update date
                           # (date_type=update, order=asc) and resumed with date_start —
                           # so the cursor has to be the update date too. It rode on the
                           # DECISION date, which is a different clock entirely: the
                           # first run of this watch ended up with a watermark of
                           # 1965-03-18 after reading 2,000 decisions, and a later batch
                           # whose newest decision happened to be recent would have
                           # jumped the cursor forward over every pending update before
                           # it. Same mistake as de-rii's ToC, same fix.
                           "watermark": decision.get("update_date"),
                           "feed_total": feed_total},
                )
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            # `next_batch` is a URL, null on the last batch of THIS QUERY — and a query
            # is capped at 10,000 results however many the register holds. Judilibre
            # holds 566,124, so treating that null as the end of the walk ended the
            # backfill after 10,000 with a cursor in 1971 and called it a success: at one
            # window per run a weekly watch reaches the present in about fifty years.
            #
            # The window is exhausted, not the register. Open the next one at the newest
            # update date this window returned and keep sweeping. Items sharing that
            # exact date are re-offered and dedup by id; if a single date fills a whole
            # window the cursor cannot advance and we stop rather than spin.
            if body.get("next_batch") is not None:
                batch += 1
                continue
            if descending:
                # step the upper bound down to the oldest update this window returned
                if oldest_seen and oldest_seen != edge:
                    edge, batch = oldest_seen, 0
                    continue
                return
            if newest_seen and newest_seen != cursor:
                cursor, batch = newest_seen, 0
                continue
            return

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
                "location": decision.get("location"),
                "jurisdiction": decision.get("jurisdiction"),
                "aliases": [x for x in (decision.get("id"),
                                         _number_alias(decision, parsed.number))
                            if x],
            }.items() if v},
        )
