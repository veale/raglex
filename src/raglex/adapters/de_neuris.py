"""Germany — NeuRIS / rechtsinformationen.bund.de adapter (legislation + case law).

The official German open-data portal (DigitalService for the BMJ + Federal Office of
Justice), in public **beta** ("Testphase"). No auth, open API, **ELI + ECLI native**.
Two document classes that match RagLex's split cleanly and share one client:

- **Case law** (``mode="caselaw"``, default): the federal courts — BVerfG, BGH, BAG,
  BFH, BSG, BVerwG, BPatG — from 2010 on, anonymised, keyed by **ECLI**. Returned as
  JSON (``CaseLawSchema``) whose text is already split into functional fields
  (Leitsatz, Tenor, Tatbestand, Entscheidungsgründe…), which become native chunk
  segments (§6b).
- **Legislation** (``mode="legislation"``): federal laws + ordinances (BGB, SGB, GG,
  BDSG…), consolidated, keyed by **ELI**. The machine base is **LDML.de** (the German
  AKN profile), fetched via the expression's ``encoding`` link and parsed by
  ``formats/ldml_de.py`` — §, Abs., Satz become chunk units.

The API is JSON-LD (Hydra): a collection is ``{member: [{item: …}], view: {next}}``.
Beta means endpoints/fields may change, so everything is read defensively by alias and
pinned by fixture tests; a **daily** watermark is right. Historical legislation versions
and amendment cross-indexing are known beta gaps — the ``leg_effects`` extractor carries
more weight on the German amendment language as a result (§2.3).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import assemble
from ..formats.ldml_de import parse_ldml_de

BASE = "https://testphase.rechtsinformationen.bund.de/v1"

# CaseLawSchema text fields in the order a German judgment lays them out → segments.
_CASELAW_ZONES = (
    ("guidingPrinciple", "Leitsatz"),
    ("headnote", "Orientierungssatz"),
    ("otherHeadnote", "Sonstiger Orientierungssatz"),
    ("outline", "Gliederung"),
    ("tenor", "Tenor"),
    ("caseFacts", "Tatbestand"),
    ("decisionGrounds", "Entscheidungsgründe"),
    ("grounds", "Gründe"),
    ("otherLongText", "Sonstiger Langtext"),
    ("dissentingOpinion", "Abweichende Meinung"),
)


def _iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _field(obj: dict, *keys):
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", []):
            return v
    return None


def _members(body: dict) -> list[dict]:
    """The Hydra collection members, each unwrapped from its ``{item: …}`` envelope."""
    out = []
    for m in body.get("member") or []:
        out.append(m.get("item") if isinstance(m, dict) and "item" in m else m)
    return out


def _xml_content_url(expression: dict) -> str | None:
    """The LDML.de XML manifestation's ``contentUrl`` from a legislation expression's
    ``encoding`` list (the entry whose ``encodingFormat`` is XML)."""
    for enc in expression.get("encoding") or []:
        if not isinstance(enc, dict):
            continue
        fmt = (enc.get("encodingFormat") or "").lower()
        url = enc.get("contentUrl") or enc.get("@id")
        if url and ("xml" in fmt or str(url).endswith(".xml")):
            return url
    return None


def parse_caselaw(obj: dict) -> Record | None:
    """One NeuRIS ``CaseLawSchema`` (JSON) → a Record (pure). ECLI is the primary key;
    the functional text fields become citable segments."""
    ecli = _field(obj, "ecli", "ECLI")
    doc_no = _field(obj, "documentNumber", "@id")
    if not (ecli or doc_no):
        return None
    court = _field(obj, "courtName", "courtType", "judicialBody") or "Bundesgericht"
    file_numbers = _field(obj, "fileNumbers", "fileNumber")

    blocks: list[tuple[str, str, str]] = []
    for key, label in _CASELAW_ZONES:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            blocks.append((label, "zone", val.strip()))
    text, segments = assemble(blocks)

    stable_id = ecli or f"de/{doc_no}"
    headline = _field(obj, "headline", "titleLine")
    title = headline or ", ".join(
        str(b) for b in (court, file_numbers if isinstance(file_numbers, str)
                         else (file_numbers or [None])[0]) if b) or ecli
    return Record(
        source="de-neuris",
        stable_id=stable_id,
        ecli=ecli if (ecli and str(ecli).startswith("ECLI:")) else None,
        doc_type=DocType.JUDGMENT,
        title=title,
        court=str(court),
        decision_date=_iso_date(_field(obj, "decisionDate", "date")),
        language=_field(obj, "inLanguage") or "de",
        source_language="de",
        landing_url=f"https://testphase.rechtsinformationen.bund.de/case-law/{doc_no or stable_id}",
        raw_bytes=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        raw_ext="json",
        text=text or None,
        segments=segments,
        extracted_via=ExtractedVia.STRUCTURED,
        extra={k: v for k, v in {
            "document_number": doc_no if str(doc_no).find("/") == -1 else None,
            "file_numbers": file_numbers,
            "document_type": _field(obj, "documentType"),
            "court_type": _field(obj, "courtType"),
        }.items() if v},
    )


class DeNeurisAdapter(BaseAdapter):
    source = "de-neuris"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        mode: str = "caselaw",
        ids: str | list[str] | None = None,
        per_page: int = 100,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.mode = mode if mode in ("caselaw", "legislation") else "caselaw"
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = ids or []
        self.per_page = per_page
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        full = url if url.startswith("http") else f"{BASE}/{url.lstrip('/')}"
        resp = self._client.get(full, params=params or {},
                                headers={"Accept": "application/json"}, raise_for_4xx=False)
        if resp.status_code >= 400:
            return {}
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return {}

    def _get_bytes(self, url: str, *, accept: str) -> bytes | None:
        full = url if url.startswith("http") else urljoin(f"{BASE}/", url.lstrip("/"))
        resp = self._client.get(full, headers={"Accept": accept}, raise_for_4xx=False)
        if resp.status_code >= 400 or not resp.content:
            return None
        return resp.content

    # -- discover ----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for ident in self.ids:
                yield Stub(stable_id=ident, hints={"id": ident})
            return
        collection = "case-law" if self.mode == "caselaw" else "legislation"
        page = 0
        pages = 0
        while True:
            params = {"size": self.per_page, "pageIndex": page}
            if since:
                params["dateFrom"] = since
            body = self._get_json(collection, params)
            members = _members(body)
            if not members:
                return
            for item in members:
                if self.mode == "caselaw":
                    sid = _field(item, "ecli", "documentNumber", "@id")
                    ident = _field(item, "documentNumber", "@id")
                    d = _iso_date(_field(item, "decisionDate", "date"))
                    hints = {"id": ident}
                else:
                    sid = _field(item, "legislationIdentifier", "@id")
                    d = _iso_date(_field(item, "temporalCoverage", "legislationDate", "datePublished"))
                    hints = {"id": sid, "at_id": _field(item, "@id"),
                             "abbreviation": _field(item, "abbreviation")}
                if not sid:
                    continue
                yield Stub(stable_id=sid, hint_date=d, hints=hints)
            page += 1
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            # stop when the Hydra view exposes no further page
            if not (body.get("view") or {}).get("next"):
                return

    # -- fetch -------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        if self.mode == "legislation":
            return self._fetch_legislation(stub)
        return self._fetch_caselaw(stub)

    def _fetch_caselaw(self, stub: Stub) -> Record | None:
        ident = stub.hints.get("id") or stub.stable_id
        obj = self._get_json(f"case-law/{ident}")
        if not obj:
            return None
        doc = obj if _field(obj, "ecli", "documentNumber") else (_members(obj) or [None])[0]
        return parse_caselaw(doc) if doc else None

    def _fetch_legislation(self, stub: Stub) -> Record | None:
        at_id = stub.hints.get("at_id")
        eli = stub.hints.get("id") or stub.stable_id
        xml: bytes | None = None
        # the expression JSON carries the LDML.de XML manifestation's contentUrl
        if at_id:
            expr = self._get_json(at_id)
            xml_url = _xml_content_url(expr) if expr else None
            if xml_url:
                xml = self._get_bytes(xml_url, accept="application/xml")
        if xml is None and at_id:
            xml = self._get_bytes(f"{at_id.rstrip('/')}.xml", accept="application/xml")
        if xml is None:
            return None

        parsed = parse_ldml_de(xml)
        eli_id = parsed.metadata.get("eli") or eli
        jurabk = parsed.metadata.get("jurabk") or stub.hints.get("abbreviation")
        return Record(
            source=self.source,
            stable_id=eli_id,
            doc_type=DocType.LEGISLATION,
            title=parsed.title or stub.title,
            decision_date=parsed.decision_date or stub.hint_date,
            language="de",
            source_language="de",
            landing_url=f"https://testphase.rechtsinformationen.bund.de/norms/{eli_id}",
            raw_bytes=xml,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            relations=parsed.relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {"eli": eli_id, "jurabk": jurabk}.items() if v},
        )
