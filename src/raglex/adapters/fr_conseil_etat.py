"""France — administrative order (Conseil d'État & below) via the open-data platform.

France has *two* court orders; Judilibre covers only the judicial one. The
administrative order (Conseil d'État, cours administratives d'appel, tribunaux
administratifs) is where most **data-protection, public-law and CNIL-appeal**
litigation sits — high value for the DP/FOI focus.

Source: ``opendata.justice-administrative.fr`` — the *complete* administrative-decision
set, backed by the Elasticsearch endpoint the public search UI calls
(``/recherche/api/elastic/decisions``). Decisions are **ECLI-native** (``ECLI:FR:CE:…``),
so they slot onto the same ECLI spine as everything else. No PISTE auth.

The ES endpoint is **undocumented and could change**, so it is wrapped defensively:
parsing is pure and reads every field by several aliases, and a ``ScrapeRecipe``-style
HTML fallback is the intended escape hatch (RagLex's scrape path exists for exactly
this). ArianeWeb's curated *analyses* / *conclusions du rapporteur public* are a later
secondary-document enrichment (COMMENTARY/OPINION pinned to the decision) — not built
here.

Every endpoint MUST be re-verified live before a real backfill.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.errors import FetchError
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import synthesise_numbered_segments

ES_ENDPOINT = "https://opendata.justice-administrative.fr/recherche/api/elastic/decisions"


def _iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _field(src: dict, *keys):
    for k in keys:
        v = src.get(k)
        if v not in (None, "", []):
            return v
    return None


def parse_hit(src: dict) -> Record | None:
    """One ES ``_source`` document → a Record (pure). Field names are read by alias
    because the index schema is undocumented."""
    ecli = _field(src, "ecli", "ECLI")
    numero = _field(src, "numero", "num", "numero_dossier")
    if not (ecli or numero):
        return None
    text = _field(src, "texte_integral", "texteIntegral", "texte", "content")
    juridiction = _field(src, "juridiction", "nom_juridiction", "jurisdiction") or "Conseil d'État"
    d = _iso_date(_field(src, "date_lecture", "dateLecture", "date_decision", "date"))
    stable_id = ecli or f"fr/ta/{numero}"
    # numbered administrative-decision points ("1. Considérant que…") → citable segments
    segments = synthesise_numbered_segments(text or "")
    return Record(
        source="fr-conseil-etat",
        stable_id=stable_id,
        ecli=ecli if (ecli and str(ecli).startswith("ECLI:")) else None,
        doc_type=DocType.JUDGMENT,
        title=", ".join(str(b) for b in (juridiction, numero) if b) or ecli,
        court=str(juridiction),
        decision_date=d,
        language="fr",
        source_language="fr",
        landing_url=_field(src, "url", "lien") or f"https://opendata.justice-administrative.fr/{stable_id}",
        raw_bytes=json.dumps(src, ensure_ascii=False).encode("utf-8"),
        raw_ext="json",
        text=text,
        segments=segments,
        extracted_via=ExtractedVia.STRUCTURED,
        extra={k: v for k, v in {
            "numero": numero, "formation": _field(src, "formation"),
            "solution": _field(src, "solution", "type_solution"),
        }.items() if v},
    )


_ARIANE_ECLI = re.compile(r"^ECLI:FR:CE[A-Z]*:(\d{4}):(\d+)\.(\d{8})$", re.I)


def ariane_url(ecli):
    match = _ARIANE_ECLI.fullmatch(ecli)
    if not match:
        raise ValueError("ArianeWeb targeting needs a Conseil d'État ECLI with a dated docket")
    day = date.fromisoformat(f"{match[3][:4]}-{match[3][4:6]}-{match[3][6:]}")
    if str(day.year) != match[1]:
        raise ValueError("ECLI year and decision date differ")
    return f"https://www.conseil-etat.fr/fr/arianeweb/CE/decision/{day.isoformat()}/{match[2]}"


def parse_ariane(raw, expected_ecli):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "html.parser")
    body = soup.select_one(".article__main .ezrichtext-field")
    if body is None:
        raise FetchError("ArianeWeb returned no decision body")
    identifiers = set(re.findall(r"ECLI:FR:CE[A-Z]*:\d{4}:\d+\.\d{8}", body.get_text(), re.I))
    if identifiers != {expected_ecli}:
        raise FetchError("ArianeWeb decision identity differs from the requested ECLI", transient=False)
    for br in body.find_all("br"):
        br.replace_with("\n")
    text = body.get_text("", strip=False)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    match = _ARIANE_ECLI.fullmatch(expected_ecli)
    record = parse_hit({"ecli":expected_ecli, "numero":match[2], "texte_integral":text,
                        "date_lecture":f"{match[3][:4]}-{match[3][4:6]}-{match[3][6:]}",
                        "url":ariane_url(expected_ecli)})
    record.raw_bytes, record.raw_ext = raw, "html"
    record.extra["format"] = "arianeweb"
    return record


class FrConseilEtatAdapter(BaseAdapter):
    source = "fr-conseil-etat"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, per_page: int = 50, ids: str | list[str] | None = None, client: RateLimitedClient | None = None) -> None:
        self.per_page = per_page
        self.ids = [x.strip().upper() for x in ids.split(",") if x.strip()] if isinstance(ids, str) else [x.upper() for x in (ids or [])]
        for identifier in self.ids:
            ariane_url(identifier)
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _search(self, since: str | None, offset: int) -> dict:
        """One ES page, newest-first, optionally filtered to decisions read since
        ``since``. The query DSL is kept minimal and defensive."""
        query: dict = {"match_all": {}}
        if since:
            query = {"range": {"date_lecture": {"gte": since}}}
        body = {
            "from": offset,
            "size": self.per_page,
            "sort": [{"date_lecture": {"order": "desc"}}],
            "query": query,
        }
        resp = self._client.request("POST", ES_ENDPOINT, json=body,
                                    headers={"Accept": "application/json"},
                                    raise_for_4xx=False)
        if resp.status_code >= 400:
            raise FetchError(f"{self.source}: search HTTP {resp.status_code}", status_code=resp.status_code)
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise FetchError(f"{self.source}: search returned invalid JSON") from exc

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for identifier in self.ids:
                yield Stub(identifier, landing_url=ariane_url(identifier), raw_url=ariane_url(identifier),
                           hints={"ariane_ecli":identifier})
            return
        offset = 0
        pages = 0
        while True:
            body = self._search(since, offset)
            hits = (body.get("hits") or {}).get("hits") or []
            if not hits:
                return
            for hit in hits:
                src = hit.get("_source") or {}
                ecli = _field(src, "ecli", "ECLI")
                numero = _field(src, "numero", "num")
                sid = ecli or (f"fr/ta/{numero}" if numero else hit.get("_id"))
                if not sid:
                    continue
                yield Stub(
                    stable_id=sid,
                    hint_date=_iso_date(_field(src, "date_lecture", "dateLecture", "date")),
                    hints={"source_doc": src},  # ES already carried the whole document
                )
            offset += len(hits)
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return

    def fetch(self, stub: Stub) -> Record | None:
        if stub.hints.get("ariane_ecli"):
            return parse_ariane(self._client.get(stub.raw_url).content, stub.hints["ariane_ecli"])
        src = stub.hints.get("source_doc")
        if src is None:
            # targeted harvest by id — query the one document
            body = self._search(None, 0)  # fallback; a real by-id endpoint is TODO
            hits = (body.get("hits") or {}).get("hits") or []
            src = next((h.get("_source") for h in hits
                        if _field(h.get("_source") or {}, "ecli") == stub.stable_id), None)
        if not src:
            return None
        return parse_hit(src)
