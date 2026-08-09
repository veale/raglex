"""France — Légifrance (DILA) legislation adapter over the PISTE gateway.

Consolidated French statute law: the 70-odd codes plus laws and decrees (fund
**LEGI**), and — through the *same* client — the CNIL's deliberations (fund **CNIL**,
directly relevant to the data-protection focus) and the Conseil constitutionnel's
decisions (fund **CONSTIT**). Légifrance is ELI-native, so France slots in beside
legislation.gov.uk, EUR-Lex and Ireland as another ELI resolution target rather than a
bespoke silo — which is what turns every ``textes appliqués`` edge Judilibre mints into
a live link (§5b).

Auth is shared with ``fr-judilibre`` via :class:`PisteClient` (one PISTE app subscribes
to both). With no PISTE credentials the adapter yields nothing, degrading safely (§5).

Discovery has two shapes:
- **Codes** (LEGI): ``POST /list/code`` enumerates every consolidated code with its
  ``lastUpdate`` — the natural watermark. Each code is fetched whole via
  ``/consult/legiPart`` and its articles become native chunk units (§6b).
- **CNIL / CONSTIT / JORF funds**: ``POST /search`` newest-first, watermarked on the
  document date; each hit is consulted by its id.

Named ``ids`` (LEGITEXT…/LEGIARTI…/ELI) fetch specific instruments directly.

Endpoints follow the documented ``lf-engine-app`` shapes but MUST be re-verified live
before a real backfill — the response bodies here are read defensively by
``formats/legifrance_json.py``.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    RelationshipType,
    ExtractedVia,
    Record,
    Stub,
    TypedRelation,
)
from ..formats.legifrance_json import _epoch_ms_to_date as _epoch_ms_date
from ..formats.legifrance_json import parse_legifrance_obj
from ._piste import PisteClient, piste_api_root

# lf-engine-app service path on the PISTE root.
_APP = "dila/legifrance/lf-engine-app"

# The newest-first sort each fund actually implements (DILA's own
# "description des tris et filtres de l'API"). The vocabulary is NOT shared: a
# deliberation is sorted by DATE_DECISION_DESC, a Conseil constitutionnel decision by
# DATE_DESC, a Journal officiel text by PUBLICATION_DATE_DESC.
#
# Getting this wrong is silent. An unknown sort is not rejected — the API answers 200 and
# quietly orders by something else, so asking CNIL for PUBLICATION_DATE_DESC returns 2019
# at the top of a fund whose newest deliberation is 2026-07-24. Every incremental run
# here depends on newest-first, so a wrong sort means the cursor is set from a stale
# slice and everything after it is skipped for good.
_SORT_BY_FOND = {
    "CNIL": "DATE_DECISION_DESC",
    "CONSTIT": "DATE_DESC",
    "JURI": "DATE_DESC",
    "CETAT": "DATE_DESC",
    "JORF": "PUBLICATION_DATE_DESC",
    "LODA": "PUBLICATION_DATE_DESC",
}

# French long-form dates, for the funds whose search hits carry no date field at all.
_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12, "decembre": 12,
}
_FR_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:er)?\s+(" + "|".join(_FR_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE)


def _date_in_title(title: str | None) -> str | None:
    """The decision date written into a Conseil constitutionnel title.

    Every date field on a CONSTIT search hit is null — ``date``, ``datePublication``,
    ``dateSignature``, ``dateDiffusion``, all of them — so the only date the search gives
    back is the one inside the title: "Décision 2026-1214/1215 QPC - 31 juillet 2026 -
    Société Airbnb …". Without it the fund has no cursor at all and every run re-walks
    the whole thing."""
    m = _FR_DATE_RE.search(title or "")
    if not m:
        return None
    return f"{int(m.group(3)):04d}-{_FR_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"

# Fund → (DILA `fond` code, DocType). CNIL deliberations and Conseil constitutionnel
# decisions are DECISION/GUIDANCE, not LEGISLATION.
_FUNDS = {
    "LEGI": ("LEGI", DocType.LEGISLATION),
    "JORF": ("JORF", DocType.LEGISLATION),
    "CNIL": ("CNIL", DocType.DECISION),
    "CONSTIT": ("CONSTIT", DocType.DECISION),
}

# Fund → the registry key it is reached by, which is also the provenance a document
# should carry. One adapter class serves three registry keys, and a class-level
# ``source`` made all three store as "fr-legislation": a CNIL deliberation and a Conseil
# constitutionnel decision arrived indistinguishable from a consolidated code, so the
# keep-current view could not say whether either had gone stale and the per-source health
# counters were the sum of three unrelated registers.
_SOURCE_BY_FOND = {
    "LEGI": "fr-legislation",
    "JORF": "fr-legislation",
    "CNIL": "fr-cnil",
    "CONSTIT": "fr-constit",
}


def _text_kind(text_id: str) -> str:
    """Which consult endpoint an id wants, from its prefix.

    Each fund has its OWN consult route and they are not interchangeable: a CNIL
    deliberation is ``consult/cnil``, a Conseil constitutionnel decision is
    ``consult/juri`` (it is case law, not a consolidated text), and only LEGITEXT goes to
    ``consult/legiPart``. Sending a CNILTEXT id to legiPart earns "L'expression à valider
    est fausse", and sending it to consult/cnil under any key but ``textId`` — ``cid``,
    ``textCid`` — earns a 500. All four routes were checked against production."""
    tid = (text_id or "").upper()
    if tid.startswith("LEGIARTI") or "ARTI" in tid[:8]:
        return "article"
    if tid.startswith("JORFTEXT") or tid.startswith("JORF"):
        return "jorf"
    if tid.startswith("CNILTEXT"):
        return "cnil"
    if tid.startswith("CONSTEXT"):
        return "juri"
    return "legipart"  # LEGITEXT … — consolidated text


class FrLegislationAdapter(BaseAdapter):
    source = "fr-legislation"
    # PISTE publishes generous limits; pace politely (§1.8).
    min_interval = 0.3
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        fond: str = "LEGI",
        ids: str | list[str] | None = None,
        client: PisteClient | None = None,
    ) -> None:
        self.fond = (fond or "LEGI").upper()
        # Provenance follows the fund, not the class (see _SOURCE_BY_FOND). Set before
        # the client is built so rate-limiting and health are booked per register too.
        self.source = _SOURCE_BY_FOND.get(self.fond, "fr-legislation")
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = ids or []
        # Légifrance uses OAuth2 client-credentials.
        self._client = client or PisteClient(self.source, auth="oauth",
                                             min_interval=self.min_interval)

    # -- HTTP --------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        resp = self._client.post(f"{piste_api_root()}/{_APP}/{path}",
                                 json=payload,
                                 headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            return {}
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return {}

    # -- discover ----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if not self._client.configured():
            return  # degrade safely without credentials
        if self.ids:
            yield from self._discover_ids()
            return
        if self.fond == "LEGI":
            yield from self._discover_codes(since)
        else:
            yield from self._discover_search(since, max_pages=max_pages)

    def _discover_ids(self) -> Iterator[Stub]:
        for ident in self.ids:
            yield Stub(stable_id=ident, hints={"text_id": ident, "fond": self.fond})

    def _discover_codes(self, since: str | None) -> Iterator[Stub]:
        """Every consolidated code via ``/list/code``; lastUpdate is the watermark."""
        body = self._post("list/code", {"pageNumber": 1, "pageSize": 300,
                                         "states": ["VIGUEUR"]})
        for code in body.get("results") or body.get("codes") or []:
            text_id = code.get("id") or code.get("cid")
            if not text_id:
                continue
            last = str(code.get("lastUpdate") or code.get("dateModif") or "")
            if since and last and last < since:
                continue
            yield Stub(
                stable_id=text_id,
                title=code.get("titre") or code.get("title"),
                hint_date=_iso_date(last),
                hints={"text_id": text_id, "fond": "LEGI", "kind": "legipart"},
            )

    def _discover_search(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        """Newest-first search within a non-LEGI fund (CNIL, CONSTIT, JORF)."""
        page = 1
        while True:
            recherche = {
                "fond": self.fond,
                "recherche": {
                    "pageNumber": page,
                    "pageSize": 100,
                    "sort": _SORT_BY_FOND.get(self.fond, "PUBLICATION_DATE_DESC"),
                    "typePagination": "DEFAUT",
                    "operateur": "ET",
                    # ``operateur`` is required on the CHAMP as well as on the criteria
                    # inside it and on the search as a whole. Omit it and Légifrance does
                    # not answer 400 — it answers 500, "une exception non gérée est
                    # survenue", which reads like an outage at the far end rather than a
                    # malformed body. It is the only difference between no French
                    # administrative case law at all and 26,806 CNIL deliberations.
                    "filtres": [],
                    "champs": [{"typeChamp": "ALL", "operateur": "ET", "criteres": [
                        {"typeRecherche": "TOUS_LES_MOTS_DANS_UN_CHAMP",
                         "valeur": "*", "operateur": "ET"}]}],
                },
            }
            body = self._post("search", recherche)
            results = body.get("results") or []
            if not results:
                return
            stop = False
            for hit in results:
                text_id = _hit_id(hit)
                if not text_id:
                    continue
                title = _hit_title(hit)
                # CONSTIT answers with every date field null, so fall back to the date
                # written into the title. Without a date there is no cursor, and a
                # "server-side incremental" fund silently becomes a full re-walk.
                d = str(hit.get("datePublication") or hit.get("date") or "")[:10]
                if not d:
                    d = _date_in_title(title) or ""
                if since and d and d < since[:10]:
                    stop = True
                    continue
                yield Stub(
                    stable_id=text_id,
                    title=title,
                    hint_date=_iso_date(d),
                    hints={"text_id": text_id, "fond": self.fond},
                )
            page += 1
            if stop or (max_pages is not None and page > max_pages):
                return

    # -- fetch -------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        text_id = stub.hints.get("text_id") or stub.stable_id
        fond = stub.hints.get("fond", self.fond)
        kind = stub.hints.get("kind") or _text_kind(text_id)
        today = date.today().isoformat()

        if kind == "article":
            body = self._post("consult/getArticle", {"id": text_id})
        elif kind == "jorf":
            body = self._post("consult/jorf", {"textCid": text_id, "searchedString": ""})
        elif kind in ("cnil", "juri"):
            body = self._post(f"consult/{kind}", {"textId": text_id})
        else:
            body = self._post("consult/legiPart", {"textId": text_id, "date": today})
        if not body:
            return None

        doc = parse_legifrance_obj(body)
        _fond, doc_type = _FUNDS.get(fond, ("LEGI", DocType.LEGISLATION))
        # IDENTITY, and the whole reason this source can be switched on safely: the DILA
        # bulk already seeded these funds and PISTE is only filling in the tail, so the
        # live increment MUST land on the ids the backfill used or the French corpus
        # forks the way the German one did. The bulk keys a Conseil constitutionnel
        # decision by its ECLI (7,112 of them held as ECLI:FR:CC:…) and a CNIL
        # deliberation as fr/cnil/<CNILTEXT…> (26,367 held). Légifrance answers `cid:
        # null` for both funds, so the CNIL branch falls through to the text id — which
        # is the same string the bulk used — and CONSTIT has to be keyed on the ECLI it
        # does return, not on fr/constit/<CONSTEXT…>, which nothing else in the corpus
        # knows about.
        stable_id = doc.ecli or doc.eli or f"fr/{fond.lower()}/{doc.cid or text_id}"

        relations: list[TypedRelation] = []
        raw_text = body.get("text") if isinstance(body.get("text"), dict) else {}
        # The API states the publication link itself — a CONSTIT decision names the
        # Journal officiel text that promulgated it (idTexteJo) — so the edge is
        # structured fact, not something the citation grammar has to find in prose.
        jo_id = raw_text.get("idTexteJo")
        if jo_id:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.RELATED_TO,
                raw_citation_string=" | ".join(
                    str(x) for x in (raw_text.get("titreJo"), jo_id) if x),
                dst_id=None,
            ))
        # record the version series as point-in-time metadata; the pipeline maps these
        # onto document_versions (§6b — "what did the article say in 1992?").
        versions_meta = [
            {"id": v.version_id, "etat": v.etat,
             "date_debut": v.date_debut.isoformat() if v.date_debut else None,
             "date_fin": v.date_fin.isoformat() if v.date_fin else None}
            for v in doc.versions
        ]

        extra = {"legifrance_id": text_id, "fond": fond}
        if doc.cid:
            extra["cid"] = doc.cid
        if doc.eli:
            extra["eli"] = doc.eli
        if doc.ecli:
            extra["ecli"] = doc.ecli
        # What the fund actually knows about the decision. These are the facets the
        # Court and the CNIL publish alongside the text — the outcome, the kind of
        # review, the official reference and the Journal officiel it appeared in — and
        # they are the difference between a searchable decision and a wall of French.
        for key, field in (
            ("nature", "nature"), ("solution", "solution"), ("nor", "nor"),
            ("numero", "num"), ("juridiction", "juridiction"),
            ("type_decision", "typeDecision"),
            ("type_controle", "typeControleNormes"),
            ("nature_qualifiee", "natureQualifiee"),
            ("demandeur", "demandeur"), ("jo_reference", "titreJo"),
            ("jo_text_id", "idTexteJo"), ("url_officielle", "urlCC"),
        ):
            value = raw_text.get(field)
            if value not in (None, "", [], {}):
                extra[key] = value
        if versions_meta:
            extra["article_versions"] = versions_meta
        # Unified legislative currency (§CUR): map the French état vocabulary onto the canonical
        # in-force/amended/repealed model so the status banner + MCP treat FR law like any other
        # jurisdiction. Only for consolidated legislation (LEGI) — not CNIL/CONSTIT decisions.
        if doc_type == DocType.LEGISLATION:
            from ..leg_currency import currency_from_french_versions
            cur = currency_from_french_versions(doc.etat, doc.article_states)
            cur.in_force_from = doc.date_debut.isoformat() if doc.date_debut else None
            cur.in_force_to = doc.date_fin.isoformat() if doc.date_fin else None
            meta = cur.to_meta()
            if meta:
                extra["currency"] = meta
        if doc.text is None:
            # older JO has no HTML text before June 2004 — flag for the OCR/import worklist
            extra["has_text"] = False

        return Record(
            source=self.source,
            stable_id=stable_id,
            ecli=doc.ecli,
            doc_type=doc_type,
            title=doc.title or stub.title,
            # A decision's date is its decision date (dateTexte), not the dateDebut of a
            # consolidated version — and for CONSTIT the stub's date, parsed out of the
            # title, is the only one the search gave us.
            decision_date=(_epoch_ms_date(raw_text.get("dateTexte"))
                           if doc_type is not DocType.LEGISLATION else None)
                          or doc.date_debut or stub.hint_date,
            language="fr",
            source_language="fr",
            landing_url=f"https://www.legifrance.gouv.fr/{'eli/' + doc.eli if doc.eli else 'search/all'}",
            raw_bytes=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            raw_ext="json",
            text=doc.text,
            segments=doc.segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra=extra,
        )


def _iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _hit_title(hit: dict) -> str | None:
    """A search hit's title — nested in ``titles[0]`` for the funds whose top-level
    ``titre`` is null (CONSTIT, CNIL), which is also where the only date lives."""
    for key in ("titre", "title"):
        if hit.get(key):
            return hit[key]
    titles = hit.get("titles") or hit.get("titres")
    if isinstance(titles, list) and titles:
        return titles[0].get("title") or titles[0].get("titre")
    return None


def _hit_id(hit: dict) -> str | None:
    """A search hit's text id — the shape varies by fund; read defensively."""
    for key in ("id", "cid", "titreId", "textId"):
        if hit.get(key):
            return hit[key]
    titles = hit.get("titles") or hit.get("titres")
    if isinstance(titles, list) and titles:
        return titles[0].get("id") or titles[0].get("cid")
    return None
