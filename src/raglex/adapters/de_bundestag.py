"""German Bundestag legislative history and research papers.

Two publication surfaces share the Bundestag as publisher but not an access contract:

``de-bt-drucksachen`` reads official Drucksachen through the DIP API.  Discovery uses
the metadata endpoint (100 rows/page), filters on *last update*, and fetches full text
only for selected records.  The API's cursor is exhausted when it stops changing.

``de-bt-wd`` reads the public Wissenschaftliche Dienste/Fachbereich Europa listing.
That listing is an HTML fragment endpoint, not an API; its raw PDF is therefore the
durable source record and its numbered outline supplies structural segments.

Drucksachen are official works under § 5(1) UrhG.  WD papers are deliberately tagged
with the Bundestag's distinct reserved-rights notice instead of inheriting that status.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urlparse

from ..citations.german import law_citations
from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.http import RateLimitedClient
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
from ..extraction import text_or_ocr

DIP_BASE = "https://search.dip.bundestag.de/api/v1"
BT_BASE = "https://www.bundestag.de"
WD_LANDING = f"{BT_BASE}/dokumente/analysen"
WD_FRAGMENT = f"{BT_BASE}/ajax/filterlist/de/dokumente/analysen/474644-474644"

# Values observed in DIP and named by the Bundestag's own vocabulary.  Callers can
# override this list as the register adds another class without a code release.
DEFAULT_DRUCKSACHE_TYPES = (
    "Gesetzentwurf",
    "Beschlussempfehlung und Bericht",
    "Antwort",
)

_DRS_NUMBER = re.compile(r"^(?:BT-?Drs\.?\s*)?(\d{1,2})\s*/\s*(\d{1,5})$", re.I)
_WD_URL = re.compile(r'href="(https://www\.bundestag\.de/resource/blob/(\d+)/[^"?#]+\.pdf)"', re.I)
_WD_ROW = re.compile(
    r'<tr\b[^>]*class="[^"]*m-documents__tableRow[^"]*"[^>]*>(.*?)</tr>', re.I | re.S)
_CELL = re.compile(r'<td\b[^>]*class="[^"]*m-documents__tableData[^"]*"[^>]*>(.*?)</td>', re.I | re.S)

# Old papers use ``WD 3 - 3000 - 045/21``; the current register also publishes the
# shorter ``WD 5 - 077/26`` and ``EU 6 - 085/26`` forms.  PE remains in the historical
# archive.  The matcher is intentionally bounded to citation-looking digits.
WD_ID_RE = re.compile(
    r"\b(?P<series>WD|PE|EU)\s*(?P<section>\d{1,2})\s*-\s*"
    r"(?:(?P<office>3000|30000)\s*-\s*)?(?P<number>\d{1,4})\s*/\s*(?P<year>\d{2,4})\b",
    re.I,
)

_BT_DATE_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}

_REASON_HEADING = re.compile(
    r"(?im)^[ \t]*(?P<label>Zu\s+(?P<kind>Artikel|Nummer|Buchstabe|"
    r"Doppelbuchstabe|Absatz|Satz|§)\s*(?P<value>[0-9]+[a-z]?|[a-z]{1,2})\b[^\n]*)$"
)
_PART_HEADING = re.compile(
    r"(?im)^[ \t]*(?P<label>(?:[AB]\.\s*)?(?:Allgemeiner|Besonderer)\s+Teil)\s*$"
)
_EU_HEADING = re.compile(
    r"(?im)^[ \t]*(?P<label>Vereinbarkeit\s+mit\s+dem\s+Recht\s+der\s+"
    r"Europäischen\s+Union)\s*$"
)
_CORRELATION_HEADING = re.compile(
    r"(?im)^[ \t]*(?P<label>(?:Entsprechungs|Zuordnungs|Korrelations)tabelle)\s*$"
)
_REASON_RANK = {
    "artikel": 0, "nummer": 1, "§": 1, "buchstabe": 2, "absatz": 2,
    "doppelbuchstabe": 3, "satz": 3,
}
_OUTLINE = re.compile(
    r"(?m)^[ \t]*(?P<number>\d{1,2}(?:\.\d{1,2}){0,4})\.?(?:[ \t]+)"
    r"(?P<title>[^\n]{3,160})$"
)


def drucksache_id(value: str) -> str | None:
    """Canonical RagLex id for ``20/5548`` (or a common printed spelling)."""
    match = _DRS_NUMBER.match((value or "").strip())
    if not match:
        return None
    return f"de/bt-drs/{int(match.group(1))}/{int(match.group(2))}"


def drucksache_aliases(value: str) -> list[str]:
    match = _DRS_NUMBER.match((value or "").strip())
    if not match:
        return []
    wp, number = str(int(match.group(1))), str(int(match.group(2)))
    return [
        f"BT-Drs {wp}/{number}", f"BT-Drs. {wp}/{number}",
        f"BT-Drucksache {wp}/{number}", f"Drucksache {wp}/{number}",
        f"BTDrs {wp}/{number}", f"Bundestagsdrucksache {wp}/{number}",
    ]


def canonical_wd_id(value: str) -> str | None:
    match = WD_ID_RE.search(value or "")
    if not match:
        return None
    series = match.group("series").upper()
    section = str(int(match.group("section")))
    number = str(int(match.group("number"))).zfill(3)
    year = match.group("year")[-2:]
    office = "3000" if match.group("office") else None
    return f"{series} {section} - {office + ' - ' if office else ''}{number}/{year}"


def wd_aliases(value: str) -> list[str]:
    canonical = canonical_wd_id(value)
    if not canonical:
        return []
    match = WD_ID_RE.search(canonical)
    assert match is not None
    series, section = match.group("series").upper(), str(int(match.group("section")))
    number, year = str(int(match.group("number"))).zfill(3), match.group("year")[-2:]
    aliases = [canonical, canonical.replace(" - ", "-"), f"{series}{section}/{number}/{year}"]
    # The short register spelling and the older office-number spelling both resolve.
    aliases.extend((f"{series} {section} - {number}/{year}",
                    f"{series} {section} - 3000 - {number}/{year}"))
    return list(dict.fromkeys(aliases))


def _iso_date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat((value or "")[:10]).date()
    except ValueError:
        return None


def _german_date(value: str) -> date | None:
    match = re.search(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})", value or "")
    if not match:
        return None
    month = _BT_DATE_MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _clean_html(value: str) -> str:
    value = re.sub(r"(?is)<(?:script|style|svg)\b.*?</(?:script|style|svg)>", " ", value or "")
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def parse_wd_fragment(raw: bytes, *, offset: int = 0) -> list[Stub]:
    """Parse only the fragment's table template; its list template duplicates every row."""
    body = raw.decode("utf-8", "ignore")
    table = body.split('<template data-js-document-results="list">', 1)[0]
    out: list[Stub] = []
    for index, row in enumerate(_WD_ROW.findall(table)):
        cells = _CELL.findall(row)
        asset = _WD_URL.search(row)
        if len(cells) < 2 or not asset:
            continue
        title = _clean_html(cells[1])
        # The link's visible text is followed by "PDF | 123 KB" in the same cell.
        title = re.sub(r"\s+PDF\s*\|.*$", "", title, flags=re.I).strip()
        canonical = canonical_wd_id(title) or canonical_wd_id(asset.group(1))
        stable = ("de/bt-wd/" + re.sub(r"[^a-z0-9]+", "-", canonical.casefold()).strip("-")
                  if canonical else f"de/bt-wd/blob-{asset.group(2)}")
        out.append(Stub(
            stable_id=stable,
            landing_url=asset.group(1),
            raw_url=asset.group(1),
            title=title or None,
            hint_date=_german_date(_clean_html(cells[0])),
            hints={"blob_id": asset.group(2), "document_number": canonical,
                   "resume_offset": offset + index},
        ))
    return out


def _heading_marks(text: str) -> list[tuple[int, int, str, str, int]]:
    """All explanatory-memorandum headings in document order."""
    marks: list[tuple[int, int, str, str, int]] = []
    for match in _PART_HEADING.finditer(text):
        label = " ".join(match.group("label").split())
        marks.append((match.start(), match.end(), label, "section", 0))
    for match in _EU_HEADING.finditer(text):
        label = " ".join(match.group("label").split())
        marks.append((match.start(), match.end(), label, "eu-compatibility", 1))
    for match in _CORRELATION_HEADING.finditer(text):
        label = " ".join(match.group("label").split())
        marks.append((match.start(), match.end(), label, "correlation-table", 1))
    for match in _REASON_HEADING.finditer(text):
        label = " ".join(match.group("label").split())
        rank = _REASON_RANK[match.group("kind").casefold()]
        marks.append((match.start(), match.end(), label, "provision-commentary", rank + 1))
    return sorted(marks, key=lambda item: (item[0], item[1]))


def segment_drucksache(text: str) -> tuple[list[Segment], list[TypedRelation]]:
    """Recover Begründung hierarchy and exact provision-commentary relations.

    Segments are non-overlapping blocks (heading through the next heading); ``level`` and
    the stack-composed label preserve the nested ``Artikel › Nummer › Buchstabe`` path.
    Only an explicit German law citation in a heading becomes ``INTERPRETS``.  Missing
    targets remain useful document context and are never guessed from nearby prose.
    """
    marks = _heading_marks(text)
    if not marks:
        return [], []
    segments: list[Segment] = []
    relations: list[TypedRelation] = []
    stack: list[tuple[int, str]] = []
    for index, (start, _heading_end, label, kind, level) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        if kind == "provision-commentary":
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, label))
            full_label = " › ".join(item[1] for item in stack)
        else:
            stack.clear()
            full_label = label
        segments.append(Segment(label=full_label, char_start=start, char_end=end,
                                kind=kind, level=level))
        if kind != "provision-commentary":
            continue
        # Parentheses in the heading are the authoritative amended-law target.  Running
        # over the whole commentary would confuse statutes merely discussed with the
        # provision the heading actually explains.
        targets = []
        for parenthetical in re.findall(r"\(([^()]{3,180})\)", label):
            targets.extend(law_citations(parenthetical))
        # ``Zu § 45b BDSG`` is itself an explicit target even without parentheses.
        # Dedupe below because a parenthetical is also part of ``label``.
        targets.extend(law_citations(label))
        unique_targets = {
            (citation.candidate_id, citation.pinpoint, citation.raw): citation
            for citation in targets
        }.values()
        for citation in unique_targets:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=citation.raw,
                dst_id=citation.candidate_id,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
                src_anchor=full_label,
                dst_anchor=citation.pinpoint,
                context_start=start,
                context_end=end,
            ))
    return segments, relations


def segment_wd(text: str) -> list[Segment]:
    marks = list(_OUTLINE.finditer(text or ""))
    if len(marks) < 2:
        return []
    segments: list[Segment] = []
    for index, match in enumerate(marks):
        start = match.start()
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        number = match.group("number")
        title = " ".join(match.group("title").split())
        # A table/list item is not an outline heading.  Headings are short and do not end
        # as a sentence; this deliberately prefers missing a seam to inventing one.
        if title.endswith((".", ";", ",")):
            continue
        segments.append(Segment(label=f"{number} {title}", char_start=start, char_end=end,
                                kind="section", level=number.count(".")))
    return segments if len(segments) >= 2 else []


class BundestagDrucksachenAdapter(BaseAdapter):
    source = "de-bt-drucksachen"
    min_interval = 0.12  # comfortably below the documented 25-concurrent ceiling

    def __init__(self, *, api_key: str | None = None, ids: str | None = None,
                 document_numbers: str | None = None, types: str | None = None,
                 prefer_pdf_tables: object = None,
                 start_offset: int = 0,
                 client: RateLimitedClient | None = None) -> None:
        self.api_key = api_key or os.environ.get("BUNDESTAG_DIP_API_KEY")
        requested = document_numbers or ids
        self.document_numbers = tuple(
            item.strip() for item in str(requested).split(",") if item.strip()
        ) if requested else ()
        self.types = tuple(
            item.strip() for item in str(types).split(",") if item.strip()
        ) if types else DEFAULT_DRUCKSACHE_TYPES
        self.prefer_pdf_tables = option_flag(prefer_pdf_tables, True)
        # Discovery checkpoints use one absolute cursor across all requested document
        # classes. Deploys restore it as start_offset; replaying the short prefix is
        # harmless and keeps the cursor correct even when the per-class API cursors are
        # opaque. The held prefilter still makes that replay download-free.
        self.start_offset = max(0, option_int(start_offset, 0))
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval,
                                                   timeout=120)

    @property
    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("de-bt-drucksachen requires BUNDESTAG_DIP_API_KEY or api_key=")
        return {"Authorization": f"ApiKey {self.api_key}"}

    @staticmethod
    def _stub(item: dict, *, resume_offset: int = 0, total: int | None = None) -> Stub | None:
        number = str(item.get("dokumentnummer") or "")
        stable = drucksache_id(number)
        if not stable or not item.get("id"):
            return None
        hints = {
            "dip_id": str(item["id"]), "document_number": number,
            "drucksachetyp": item.get("drucksachetyp"), "updated": item.get("aktualisiert"),
            "watermark": item.get("aktualisiert"), "pdf_hash": item.get("pdf_hash"),
            "contenthash": item.get("pdf_hash"),
            "publisher": item.get("herausgeber"), "resume_offset": resume_offset,
            # An item returned by the last-modified delta is an upstream revision even
            # where its stable id is already held; payload hashing makes this idempotent.
            "revision": not bool(item.get("pdf_hash")),
        }
        if total is not None:
            hints["feed_total"] = total
        return Stub(stable_id=stable, title=item.get("titel"), hint_date=_iso_date(item.get("datum")),
                    landing_url=_pdf_url(number), hints=hints)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.document_numbers:
            for offset, number in enumerate(self.document_numbers):
                response = self._client.get(
                    f"{DIP_BASE}/drucksache", params={"f.dokumentnummer": number},
                    headers=self._headers, raise_for_4xx=False)
                if response.status_code >= 400:
                    continue
                payload = response.json()
                for item in payload.get("documents") or []:
                    stub = self._stub(item, resume_offset=offset,
                                      total=len(self.document_numbers))
                    if stub:
                        yield stub
            return

        absolute = 0
        for drs_type in self.types:
            pages = 0
            cursor: str | None = None
            while True:
                params = {"f.drucksachetyp": drs_type}
                if since:
                    params["f.aktualisiert.start"] = since
                if cursor:
                    params["cursor"] = cursor
                response = self._client.get(f"{DIP_BASE}/drucksache", params=params,
                                            headers=self._headers, raise_for_4xx=False)
                if response.status_code >= 400:
                    break
                payload = response.json()
                total = payload.get("numFound")
                rows = payload.get("documents") or []
                for item in rows:
                    stub = self._stub(item, resume_offset=absolute, total=total)
                    absolute += 1
                    if stub and absolute > self.start_offset:
                        yield stub
                pages += 1
                next_cursor = payload.get("cursor")
                if not rows or not next_cursor or next_cursor == cursor:
                    break
                if max_pages is not None and pages >= max_pages:
                    break
                cursor = next_cursor

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(f"{DIP_BASE}/drucksache-text/{stub.hints['dip_id']}",
                                    headers=self._headers, raise_for_4xx=False)
        if response.status_code >= 400:
            return None
        item = response.json()
        text = str(item.get("text") or "").strip()
        if not text:
            return None
        raw = json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
        number = str(item.get("dokumentnummer") or stub.hints.get("document_number") or "")
        canonical = drucksache_id(number) or stub.stable_id
        citation = f"BT-Drs {number}"
        title = str(item.get("titel") or stub.title or "").strip()
        raw_ext = "json"
        extraction_engine = None
        # DIP's text is deliberately flat.  For implementation/transposition bills the
        # official PDF is the only rendition that retains enough row layout for a later
        # correlation-table pass, so preserve and parse that rendition instead.
        if self.prefer_pdf_tables and re.search(
                r"\b(?:Umsetzung|Durchführung)\b.*\b(?:Richtlinie|EU)\b", title, re.I):
            pdf_url = _pdf_url(number)
            pdf = self._client.get(pdf_url, raise_for_4xx=False) if pdf_url else None
            if pdf is not None and pdf.status_code < 400 and pdf.content.startswith(b"%PDF"):
                pdf_text, needs_ocr, page_spans, extraction_engine = text_or_ocr(pdf.content)
                if len((pdf_text or "").strip()) >= 500:
                    text, raw, raw_ext = pdf_text.strip(), pdf.content, "pdf"
        segments, relations = segment_drucksache(text)
        if not segments and raw_ext == "pdf":
            segments = [Segment(label=f"p. {page}", char_start=start, char_end=end, kind="page")
                        for page, start, end in page_spans]
        if citation.casefold() not in title.casefold():
            title = f"{citation} — {title}" if title else citation
        return Record(
            source=self.source, stable_id=canonical, doc_type=DocType.PREPARATORY,
            title=title, decision_date=_iso_date(item.get("datum")) or stub.hint_date,
            language="de", source_language="de", landing_url=_pdf_url(number),
            raw_bytes=raw, raw_ext=raw_ext, text=text, segments=segments,
            relations=relations, extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "aliases": drucksache_aliases(number),
                "document_number": citation,
                "drucksachetyp": item.get("drucksachetyp"),
                "wahlperiode": item.get("wahlperiode"),
                "dip_id": str(item.get("id") or stub.hints.get("dip_id")),
                "dip_updated": item.get("aktualisiert"),
                "pdf_hash_md5": item.get("pdf_hash"),
                "contenthash": item.get("pdf_hash"),
                "rights_status": "official-work-public-domain",
                "rights_basis": "§ 5(1) UrhG (amtliches Werk)",
                "segmentation_method": "bundestag-begruendung-stack-v1" if segments else None,
                "extraction_engine": extraction_engine,
            },
        )


class BundestagWDAdapter(BaseAdapter):
    source = "de-bt-wd"
    min_interval = 1.0

    def __init__(self, *, ids: str | None = None, start_offset: int = 0,
                 limit: int = 50, client: RateLimitedClient | None = None) -> None:
        self.ids = tuple(item.strip() for item in str(ids).split(",") if item.strip()) if ids else ()
        self.start_offset = max(0, option_int(start_offset, 0))
        self.limit = max(10, option_int(limit, 50))
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval,
                                                   timeout=120)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for index, value in enumerate(self.ids):
                if value.startswith("http"):
                    match = re.search(r"/resource/blob/(\d+)/", value)
                    yield Stub(stable_id=f"de/bt-wd/blob-{match.group(1) if match else index}",
                               landing_url=value, raw_url=value,
                               hints={"blob_id": match.group(1) if match else None,
                                      "resume_offset": index})
            return
        offset, pages = self.start_offset, 0
        while True:
            response = self._client.get(
                WD_FRAGMENT,
                params={"limit": self.limit, "noFilterSet": "true", "offset": offset},
                headers={"Accept": "text/html", "User-Agent": "RagLex legal-research corpus"},
                raise_for_4xx=False,
            )
            if response.status_code >= 400:
                return
            rows = parse_wd_fragment(response.content, offset=offset)
            if not rows:
                return
            for stub in rows:
                if since and stub.hint_date and stub.hint_date.isoformat() <= since[:10]:
                    return
                yield stub
            offset += len(rows)
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url or stub.landing_url, raise_for_4xx=False)
        if response.status_code >= 400 or not response.content.startswith(b"%PDF"):
            return None
        text, needs_ocr, page_spans, engine = text_or_ocr(response.content)
        text = (text or "").strip()
        if not text:
            return None
        canonical = (canonical_wd_id(text[:5000]) or canonical_wd_id(stub.title or "")
                     or stub.hints.get("document_number"))
        stable = ("de/bt-wd/" + re.sub(r"[^a-z0-9]+", "-", canonical.casefold()).strip("-")
                  if canonical else stub.stable_id)
        title = (stub.title or "").strip() or canonical or stable
        if canonical and canonical.casefold() not in title.casefold():
            title = f"{canonical} — {title}"
        segments = segment_wd(text)
        if not segments:
            segments = [Segment(label=f"p. {page}", char_start=start, char_end=end, kind="page")
                        for page, start, end in page_spans]
        return Record(
            source=self.source, stable_id=stable, doc_type=DocType.GUIDANCE,
            title=title, decision_date=stub.hint_date, language="de", source_language="de",
            landing_url=stub.landing_url, raw_bytes=response.content, raw_ext="pdf", text=text,
            segments=segments, extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "aliases": wd_aliases(canonical or ""),
                "document_number": canonical,
                "fachbereich": (canonical.split(" - ", 1)[0] if canonical else None),
                "blob_id": stub.hints.get("blob_id"),
                "needs_ocr": needs_ocr,
                "extraction_engine": engine,
                "rights_status": "bundestag-reserved-rights",
                "rights_notice": ("Publication/distribution rights reserved by the Deutscher "
                                  "Bundestag; source attribution required and intended onward "
                                  "publication must be notified to the relevant Fachbereich."),
                "redistribution": "attributed-excerpts-only",
            },
        )


def _pdf_url(number: str) -> str | None:
    match = _DRS_NUMBER.match(number or "")
    if not match:
        return None
    wp, nr = int(match.group(1)), int(match.group(2))
    padded = f"{nr:05d}"
    return f"https://dserver.bundestag.de/btd/{wp}/{padded[:3]}/{wp}{padded}.pdf"
