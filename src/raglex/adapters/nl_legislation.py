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
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET
import zipfile

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import element_text, localname
from ..citations.dutch import law_name_alias
from ..formats import parse

BASE_URL = "https://wetten.overheid.nl"
SRU_URL = "https://zoekservice.overheid.nl/sru/Search"  # KOOP SRU (x-connection=BWB)

DEFAULT_IDS = (
    "BWBR0040940",  # Uitvoeringswet AVG (GDPR implementation)
    "BWBR0045754",  # Wet open overheid (Woo) — NL FOI act
)


def _date(value: str | None) -> date | None:
    if not value:
        return None


def _bulk_identity(data: bytes, name: str = "") -> tuple[str, str] | None:
    """Read the BWB work id and validity date from an official bulk XML member."""
    sample = data[:300_000].decode("utf-8", "ignore")
    bwb = re.search(r"\b(BWB[RV]\d{7})\b", name + " " + sample, re.I)
    if not bwb:
        return None
    # KOOP dumps vary by generation; accept the standard element/attribute names and
    # finally a date carried in the member name.
    dm = re.search(
        r"(?:geldigheidsdatum|geldig-van|geldig_van|inwerkingtreding)[^>0-9]{0,80}"
        r"(?:>|[=\"'])\s*(\d{4}-\d{2}-\d{2})", sample, re.I)
    if not dm:
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return (bwb.group(1).upper(), dm.group(1) if dm else "0001-01-01")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


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
        all_records: bool = False,
        version_date: str | None = None,
        path: str | None = None,
        use_sru: bool = True,
        client: RateLimitedClient | None = None,
    ) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.all_records = bool(all_records)
        self.ids = tuple(ids) if ids else (DEFAULT_IDS if not rechtsgebied and not all_records else ())
        # rechtsgebied (e.g. 'staats- en bestuursrecht') enables topic discovery;
        # otherwise a configured BWB-id list. SRU also drives delta sync.
        self.rechtsgebied = rechtsgebied
        self.version_date = version_date
        self.path = Path(path) if path else None
        self.use_sru = use_sru
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    # -- SRU discovery (KOOP, x-connection=BWB) ----------------------------
    def _sru_query(self, cql: str, *, start_record: int = 1,
                   max_records: int = 500) -> tuple[list[dict], int | None]:
        params = {
            "operation": "searchRetrieve", "version": "1.2", "x-connection": "BWB",
            "query": cql, "startRecord": start_record, "maximumRecords": max_records,
        }
        try:
            resp = self._client.get(SRU_URL, params=params)
        except FetchError:
            return [], None
        return _parse_sru_page(resp.content)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is not None:
            yield from self._discover_bulk()
            return
        # 1) SRU path (topic discovery + delta sync via dcterms.modified).
        if self.use_sru:
            clauses = []
            if self.ids:
                clauses.append("(" + " or ".join(f"dcterms.identifier=={i}" for i in self.ids) + ")")
            if self.rechtsgebied:
                clauses.append(f"overheidbwb.rechtsgebied=={self.rechtsgebied}")
            if since:
                clauses.append(f"dcterms.modified>={since}")  # incremental cursor
            cql = " and ".join(clauses) if clauses else "dcterms.modified>=1900-01-01"
            latest: dict[str, dict] = {}
            start, pages = 1, 0
            while True:
                records, next_record = self._sru_query(cql, start_record=start)
                for rec in records:
                    cur = latest.get(rec["identifier"])
                    if cur is None or (rec.get("modified") or "") >= (cur.get("modified") or ""):
                        latest[rec["identifier"]] = rec
                pages += 1
                if not next_record or not records or (max_pages is not None and pages >= max_pages):
                    break
                start = next_record
            if latest:
                for rec in latest.values():
                    date = self.version_date or rec.get("geldigheidsdatum")
                    sid = f"{rec['identifier']}@{self.version_date}" if self.version_date else rec["identifier"]
                    yield Stub(
                        stable_id=sid,
                        landing_url=f"{BASE_URL}/{rec['identifier']}",
                        title=rec.get("title"),
                        hint_date=_date(rec.get("modified")),  # watermark on modified
                        hints={"geldig": date, "bwbid": rec["identifier"]},
                    )
                return
        # 2) Fallback: the configured id list, date resolved at fetch.
        for bwbid in self.ids:
            sid = f"{bwbid}@{self.version_date}" if self.version_date else bwbid
            yield Stub(stable_id=sid, landing_url=f"{BASE_URL}/{bwbid}",
                       hints={"bwbid": bwbid, "geldig": self.version_date})

    def _discover_bulk(self) -> Iterator[Stub]:
        """Enumerate every toestand in a KOOP bulk zip/folder, retaining history."""
        entries: list[tuple[str, str, dict]] = []
        archives = [self.path] if self.path.is_file() and self.path.suffix.lower() == ".zip" else (
            sorted(self.path.rglob("*.zip")) if self.path.is_dir() else [])
        for archive in archives:
            try:
                with zipfile.ZipFile(archive) as zf:
                    for member in zf.namelist():
                        if not member.lower().endswith(".xml"):
                            continue
                        raw = zf.read(member)
                        ident = _bulk_identity(raw, member)
                        if ident:
                            entries.append((*ident, {"archive": str(archive), "member": member}))
            except (OSError, zipfile.BadZipFile):
                continue
        if self.path.is_dir():
            for xml in sorted(self.path.rglob("*.xml")):
                try:
                    ident = _bulk_identity(xml.read_bytes(), str(xml))
                except OSError:
                    continue
                if ident:
                    entries.append((*ident, {"file": str(xml)}))
        latest = {}
        for bwb, valid, _ in entries:
            latest[bwb] = max(valid, latest.get(bwb, ""))
        for bwb, valid, hints in entries:
            # The latest toestand is the undated Work node; every earlier toestand is
            # separately addressable for time-correct Juriconnect edges.
            sid = bwb if valid == latest[bwb] else f"{bwb}@{valid}"
            yield Stub(stable_id=sid, landing_url=f"{BASE_URL}/{bwb}",
                       hint_date=_date(valid), hints={**hints, "bwbid": bwb, "geldig": valid})

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
        bwbid = stub.hints.get("bwbid") or stub.stable_id.split("@", 1)[0]
        if stub.hints.get("archive"):
            try:
                with zipfile.ZipFile(stub.hints["archive"]) as zf:
                    raw = zf.read(stub.hints["member"])
            except (OSError, KeyError, zipfile.BadZipFile):
                return None
        elif stub.hints.get("file"):
            try:
                raw = Path(stub.hints["file"]).read_bytes()
            except OSError:
                return None
        else:
            raw = None
        # prefer the in-force date SRU/bulk metadata already gave us
        date = stub.hints.get("geldig") or self._resolve_date(bwbid)
        if not date:
            return None
        if raw is None:
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
            stable_id=stub.stable_id,
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
            extra={k: v for k, v in {
                "format": "bwb", "geldigheidsdatum": date, "bwb_id": bwbid,
                "point_in_time": date if "@" in stub.stable_id else None,
                # A bare Juriconnect pointer means current law and must never be
                # redirected to an historical copy. Dated copies get dated aliases.
                "aliases": ([f"jci1.3:c:{bwbid}&g={date}", f"{bwbid}@{date}"]
                            if "@" in stub.stable_id else
                            [f"jci1.3:c:{bwbid}", law_name_alias(parsed.title or bwbid)]),
            }.items() if v},
        )


def _parse_sru_page(xml_bytes: bytes) -> tuple[list[dict], int | None]:
    """Parse a KOOP SRU BWB response into {identifier, title, modified,
    geldigheidsdatum} per record (namespace-agnostic by local-name)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return [], None
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
    nxt = next((element_text(e).strip() for e in root.iter()
                if localname(e.tag) == "nextRecordPosition" and element_text(e).strip()), None)
    return out, int(nxt) if nxt and nxt.isdigit() else None


def _parse_sru(xml_bytes: bytes) -> list[dict]:
    """Backward-compatible page parser used by existing callers/tests."""
    return _parse_sru_page(xml_bytes)[0]
