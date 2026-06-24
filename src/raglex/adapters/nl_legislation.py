"""Netherlands legislation adapter — wetten.overheid.nl BWB "toestand" XML.

The NL consolidated corpus (Basiswettenbestand) is native **BWB XML**, not Akoma
Ntoso (AKN is only used for the Omgevingswet under the STOP standard). The
consolidated text for a regulation's in-force version is at
``/{BWBID}/{geldigheidsdatum}/0/xml``; we resolve the current in-force date from
the work's landing page, then parse via the ``bwb`` format. stable_id is the
**BWB-id** (e.g. ``BWBR0040940`` = Uitvoeringswet AVG, the GDPR implementation
act).

Discovery here is a configured BWB-id list (default: the DP instruments). KOOP's
**SRU** service (``x-connection=BWB``, CQL over ``dcterms.identifier`` /
``overheidbwb.rechtsgebied`` / ``dcterms.modified``) is the documented way to
discover by topic or sync deltas — a drop-in for ``discover`` when wanted.
Fragment-level citation into NL law uses the **JuriConnect** standard (cf. the
pinpoint links §1.9 supports).
"""

from __future__ import annotations

import re
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import element_text, localname
from ..formats import parse

BASE_URL = "https://wetten.overheid.nl"
SRU_URL = "https://zoekservice.overheid.nl/sru/Search"  # KOOP SRU (x-connection=BWB)

DEFAULT_IDS = (
    "BWBR0040940",  # Uitvoeringswet AVG (GDPR implementation)
    "BWBR0045754",  # Wet open overheid (Woo) — NL FOI act
)


class NLLegislationAdapter(BaseAdapter):
    source = "nl-legislation"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        ids: str | tuple[str, ...] | None = None,
        rechtsgebied: str | None = None,
        use_sru: bool = True,
        client: RateLimitedClient | None = None,
    ) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else (DEFAULT_IDS if not rechtsgebied else ())
        # rechtsgebied (e.g. 'staats- en bestuursrecht') enables topic discovery;
        # otherwise a configured BWB-id list. SRU also drives delta sync.
        self.rechtsgebied = rechtsgebied
        self.use_sru = use_sru
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    # -- SRU discovery (KOOP, x-connection=BWB) ----------------------------
    def _sru_query(self, cql: str, *, max_records: int = 50) -> list[dict]:
        params = {
            "operation": "searchRetrieve", "version": "1.2", "x-connection": "BWB",
            "query": cql, "maximumRecords": max_records,
        }
        try:
            resp = self._client.get(SRU_URL, params=params)
        except FetchError:
            return []
        return _parse_sru(resp.content)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # 1) SRU path (topic discovery + delta sync via dcterms.modified).
        if self.use_sru:
            clauses = []
            if self.ids:
                clauses.append("(" + " or ".join(f"dcterms.identifier=={i}" for i in self.ids) + ")")
            if self.rechtsgebied:
                clauses.append(f"overheidbwb.rechtsgebied=={self.rechtsgebied}")
            if since:
                clauses.append(f"dcterms.modified>={since}")  # incremental cursor
            cql = " and ".join(clauses) if clauses else "dcterms.modified>=2024-01-01"
            records = self._sru_query(cql)
            if records:
                # SRU returns one record per *toestand* (version); keep the latest
                # per BWB-id (the in-force consolidated text).
                latest: dict[str, dict] = {}
                for rec in records:
                    cur = latest.get(rec["identifier"])
                    if cur is None or (rec.get("modified") or "") >= (cur.get("modified") or ""):
                        latest[rec["identifier"]] = rec
                for rec in latest.values():
                    yield Stub(
                        stable_id=rec["identifier"],
                        landing_url=f"{BASE_URL}/{rec['identifier']}",
                        title=rec.get("title"),
                        hint_date=rec.get("modified"),  # watermark on modified
                        hints={"geldig": rec.get("geldigheidsdatum")},
                    )
                return
        # 2) Fallback: the configured id list, date resolved at fetch.
        for bwbid in self.ids:
            yield Stub(stable_id=bwbid, landing_url=f"{BASE_URL}/{bwbid}")

    def _resolve_date(self, bwbid: str) -> str | None:
        """Find the current in-force *toestand* date from the work landing page
        (the BWB XML path requires an exact toestand date, not an arbitrary one)."""
        try:
            html = self._client.get(f"{BASE_URL}/{bwbid}").text
        except FetchError:
            return None
        m = re.search(rf"{re.escape(bwbid)}/(\d{{4}}-\d{{2}}-\d{{2}})/0", html)
        return m.group(1) if m else None

    def fetch(self, stub: Stub) -> Record | None:
        bwbid = stub.stable_id
        # prefer the in-force date SRU already gave us; else resolve from the page
        date = stub.hints.get("geldig") or self._resolve_date(bwbid)
        if not date:
            return None
        try:
            resp = self._client.get(f"{BASE_URL}/{bwbid}/{date}/0/xml")
        except FetchError:
            return None
        raw = resp.content
        parsed = parse("bwb", raw)
        if not parsed.text:
            return None
        return Record(
            source=self.source,
            stable_id=bwbid,
            doc_type=DocType.LEGISLATION,
            title=parsed.title or bwbid,
            language="nl",
            source_language="nl",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            relations=parsed.relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"format": "bwb", "geldigheidsdatum": date},
        )


def _parse_sru(xml_bytes: bytes) -> list[dict]:
    """Parse a KOOP SRU BWB response into {identifier, title, modified,
    geldigheidsdatum} per record (namespace-agnostic by local-name)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[dict] = []
    for record in (e for e in root.iter() if localname(e.tag) == "record"):
        fields: dict[str, str] = {}
        for el in record.iter():
            name = localname(el.tag)
            if name in ("identifier", "title", "modified", "geldigheidsdatum"):
                val = " ".join(element_text(el).split())
                if val and name not in fields:
                    fields[name] = val
        ident = fields.get("identifier", "")
        m = re.search(r"BWBR\d+", ident)
        if m:
            fields["identifier"] = m.group(0)
            out.append(fields)
    return out
