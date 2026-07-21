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
from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    Stub,
    TypedRelation,
)
from ..formats.legifrance_json import parse_legifrance_obj
from ._piste import PisteClient, piste_api_root

# lf-engine-app service path on the PISTE root.
_APP = "dila/legifrance/lf-engine-app"

# Fund → (DILA `fond` code, DocType). CNIL deliberations and Conseil constitutionnel
# decisions are DECISION/GUIDANCE, not LEGISLATION.
_FUNDS = {
    "LEGI": ("LEGI", DocType.LEGISLATION),
    "JORF": ("JORF", DocType.LEGISLATION),
    "CNIL": ("CNIL", DocType.DECISION),
    "CONSTIT": ("CONSTIT", DocType.DECISION),
}


def _text_kind(text_id: str) -> str:
    """Which consult endpoint an id wants, from its prefix."""
    tid = (text_id or "").upper()
    if tid.startswith("LEGIARTI") or "ARTI" in tid[:8]:
        return "article"
    if tid.startswith("JORFTEXT") or tid.startswith("JORF"):
        return "jorf"
    return "legipart"  # LEGITEXT, CNILTEXT, CONSTEXT … — consolidated text


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
                    "sort": "PUBLICATION_DATE_DESC",
                    "typePagination": "DEFAUT",
                    "operateur": "ET",
                    "champs": [{"typeChamp": "ALL", "criteres": [
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
                d = str(hit.get("datePublication") or hit.get("date") or "")
                if since and d and d < since:
                    stop = True
                    continue
                yield Stub(
                    stable_id=text_id,
                    title=hit.get("titre") or hit.get("title"),
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
        else:
            body = self._post("consult/legiPart", {"textId": text_id, "date": today})
        if not body:
            return None

        doc = parse_legifrance_obj(body)
        _fond, doc_type = _FUNDS.get(fond, ("LEGI", DocType.LEGISLATION))
        stable_id = doc.eli or f"fr/{fond.lower()}/{doc.cid or text_id}"

        relations: list[TypedRelation] = []
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
        if versions_meta:
            extra["article_versions"] = versions_meta
        if doc.text is None:
            # older JO has no HTML text before June 2004 — flag for the OCR/import worklist
            extra["has_text"] = False

        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=doc_type,
            title=doc.title or stub.title,
            decision_date=doc.date_debut or stub.hint_date,
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


def _hit_id(hit: dict) -> str | None:
    """A search hit's text id — the shape varies by fund; read defensively."""
    for key in ("id", "cid", "titreId", "textId"):
        if hit.get(key):
            return hit[key]
    titles = hit.get("titles") or hit.get("titres")
    if isinstance(titles, list) and titles:
        return titles[0].get("id") or titles[0].get("cid")
    return None
