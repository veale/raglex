"""Hong Kong legislation — the e-Legislation bulk XML corpus.

Hong Kong's Department of Justice publishes the whole consolidated statute book as a
bulk XML drop (one directory per chapter, HKLM schema). That drop is the **only**
sanctioned machine channel, and the reason is worth stating because it constrains the
whole design:

    ``www.elegislation.gov.hk/robots.txt`` is ``User-agent: * / Disallow: /`` with
    ``Allow: /sitemap`` (and a ``Googlebot`` exception). Every content page — including
    the per-chapter XML — is off-limits to a crawler.

So this adapter **never fetches document content over HTTP**. Content comes from the
bulk drop on disk; the network is used only for the one path robots permits, the
sitemap, and only to answer "has a chapter appeared that my drop doesn't have?".
That is the same discipline the New Zealand adapter follows for a different reason
(a bot wall rather than a robots rule) — where the publisher offers a data channel,
use it, and don't drive the website.

**Change detection without a feed.** The drop encodes each chapter's consolidation
point in its filename — ``cap_486_20221001000000_en_c.xml`` is Cap. 486 as at
2022-10-01. That timestamp is the version signal: re-pointing the adapter at a refreshed
drop re-imports exactly the chapters whose timestamp moved, and ``since`` filters on it.
Chapters present in the sitemap but absent from the drop are surfaced as a
``sitemap_only`` gap list rather than scraped, so a stale drop is visible instead of
silently incomplete.

Identity is the chapter number (``hk/cap/486``, ``hk/cap/132ci`` for subsidiary
legislation, ``hk/instrument/a101`` for the Basic Law and its companions) — the register's
own key, and what the XML's cross-references address, so edges resolve by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)
from ..formats.hklm_xml import hk_id, parse_hklm_xml

__all__ = ["hk_id", "HKLegislationAdapter", "BulkFile", "scan_bulk_dir", "sitemap_caps"]

SITE = "https://www.elegislation.gov.hk"
SITEMAP = f"{SITE}/sitemap.xml"

# cap_486_20221001000000_en_c.xml  ·  cap_132CI_20200730000000_en_c.xml
# A101_--------------_en_c.xml     (instruments carry no timestamp — dashes stand in)
_FILE_RE = re.compile(
    r"^(?P<key>[A-Za-z0-9]+(?:_[A-Za-z0-9]+)??)_(?P<stamp>\d{14}|-+)_(?P<lang>[a-z]{2})_c$")
# the directory is the more reliable key: cap_486_en_c · A101_en_c · cap_132CI_en_c
_DIR_RE = re.compile(r"^(?:cap_(?P<cap>[0-9]+[A-Za-z]*)|(?P<inst>A[0-9]+[A-Za-z]*))"
                     r"_(?P<lang>[a-z]{2})_c$")
_SITEMAP_CAP = re.compile(r"/hk/cap([0-9]+[A-Za-z]*)\b", re.I)


@dataclass(frozen=True, slots=True)
class BulkFile:
    """One chapter's XML in the bulk drop."""
    path: Path
    kind: str          # cap | instrument
    number: str        # "486" | "132CI" | "A101"
    language: str      # en
    version: date | None   # the consolidation point encoded in the filename

    @property
    def stable_id(self) -> str:
        return hk_id(self.kind, self.number, self.language)


def _stamp_date(raw: str | None) -> date | None:
    """``20221001000000`` → 2022-10-01. Instruments use a dash run instead of a
    timestamp (they are not consolidated), which yields None rather than an error."""
    digits = (raw or "").strip()
    if not digits.isdigit() or len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def scan_bulk_dir(path: Path, *, language: str = "en") -> list[BulkFile]:
    """Enumerate the bulk drop. The directory name carries the chapter key and the
    filename the consolidation timestamp, so no XML is opened during discovery — a
    3,000-chapter corpus enumerates instantly."""
    out: list[BulkFile] = []
    if not path.is_dir():
        return out
    for folder in sorted(path.iterdir()):
        if not folder.is_dir():
            continue
        m = _DIR_RE.match(folder.name)
        if not m or m.group("lang") != language:
            continue
        kind = "cap" if m.group("cap") else "instrument"
        number = m.group("cap") or m.group("inst")
        for file in sorted(folder.glob("*.xml")):
            fm = _FILE_RE.match(file.stem)
            out.append(BulkFile(path=file, kind=kind, number=number,
                                language=m.group("lang"),
                                version=_stamp_date(fm.group("stamp") if fm else None)))
    return out


def sitemap_caps(xml: bytes) -> set[str]:
    """The chapter numbers the register's sitemap lists — the enumeration of what
    *exists*, used to spot chapters missing from a stale drop. The sitemap carries no
    ``lastmod``, so it can answer "what exists" but never "what changed"."""
    return {m.group(1).lower() for m in _SITEMAP_CAP.finditer(xml.decode("utf-8", "replace"))}


class HKLegislationAdapter(BaseAdapter):
    """Hong Kong e-Legislation, imported from the bulk XML drop.

    ``path`` points at the unpacked drop (a directory of ``cap_{n}_en_c/`` folders).
    ``since`` filters on each chapter's consolidation date, so re-pointing at a refreshed
    drop imports only what was re-consolidated. Set ``check_sitemap=true`` to additionally
    fetch the register's sitemap (the one robots-permitted path) and record which
    chapters exist upstream but are absent from the drop.
    """

    source = "hk-legislation"
    min_interval = 2.0        # only ever used for the single sitemap request
    requires_js = False
    requires_proxy = False

    def __init__(self, *, path: str | Path | None = None, language: str = "en",
                 ids: str | tuple[str, ...] | None = None,
                 include_repealed: bool = True,
                 check_sitemap: bool | str = False,
                 client: RateLimitedClient | None = None) -> None:
        self.path = Path(path).expanduser() if path else None
        self.language = (language or "en").lower()
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        # Repealed chapters stay addressable by default: a judgment from 2003 cites the
        # law as it then stood, and dropping them breaks those citations.
        self.include_repealed = _flag(include_repealed)
        self.check_sitemap = _flag(check_sitemap)
        self._client = client
        # chapters the register lists but the drop lacks — surfaced, never scraped
        self.sitemap_only: set[str] = set()

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is None or not self.path.exists():
            return
        files = scan_bulk_dir(self.path, language=self.language)
        wanted = {i.strip().lower().replace("cap.", "").replace(" ", "") for i in self.ids}

        held = {f.number.lower() for f in files if f.kind == "cap"}
        if self.check_sitemap:
            self.sitemap_only = self._sitemap_gap(held)

        count = 0
        for file in files:
            if wanted and not ({file.number.lower(), file.stable_id.lower()} & wanted):
                continue
            stamp = file.version.isoformat() if file.version else None
            if since and stamp and stamp <= since[:10]:
                continue
            yield Stub(
                stable_id=file.stable_id,
                landing_url=f"{SITE}/hk/cap{file.number}",
                raw_url=str(file.path),
                hint_date=file.version,
                hints={"path": str(file.path), "kind": file.kind, "number": file.number,
                       "language": file.language, "watermark": stamp},
            )
            count += 1
            if max_pages is not None and count >= max_pages * 100:
                return

    def _sitemap_gap(self, held: set[str]) -> set[str]:
        """Chapters listed upstream but missing locally. Best-effort: the drop is
        perfectly usable without this, so a failed request must not abort the run."""
        client = self._client or RateLimitedClient(self.source, min_interval=self.min_interval)
        try:
            resp = client.get(SITEMAP)
        except FetchError:
            return set()
        return {c for c in sitemap_caps(resp.content or b"") if c not in held}

    def fetch(self, stub: Stub) -> Record | None:
        file = Path(stub.hints["path"])
        try:
            data = file.read_bytes()
        except OSError:
            return None
        doc = parse_hklm_xml(data)
        if not doc.text:
            return None
        meta = doc.metadata
        if meta.get("repealed") and not self.include_repealed:
            return None

        relations = [r for r in doc.relations if r.dst_id != stub.stable_id]

        # Amendment provenance: the source notes name the amending instruments (Legal
        # Notices, Ordinances of a year, Editorial Records). The consolidated corpus
        # doesn't hold those as documents, so these are recorded as citation strings
        # WITHOUT a dst_id — a real, auditable edge that the resolver may later match,
        # rather than a fabricated id that could never resolve.
        seen: set[tuple] = set()
        for note in meta.get("source_notes") or []:
            for instrument in note.instruments:
                key = (instrument, note.provision)
                if key in seen:
                    continue
                seen.add(key)
                relations.append(TypedRelation(
                    relationship_type=RelationshipType.AMENDED_BY,
                    raw_citation_string=instrument, dst_id=None,
                    src_anchor=note.provision,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING,
                ))

        version = meta.get("version_date")
        extra = {
            "jurisdiction": "hk",
            "kind": meta.get("kind"),
            "number": meta.get("number"),
            "format": "hklm-xml",
            "doc_name": meta.get("doc_name"),          # "Cap. 486"
            "doc_status": meta.get("doc_status"),
            "identifier": meta.get("identifier"),
            "is_authoritative": True,   # the DoJ drop is the official consolidated text
            "point_in_time": version.isoformat() if version else None,
            "repealed": meta.get("repealed"),
            "in_effect": meta.get("in_effect"),
            "source_note_count": len(meta.get("source_notes") or []),
        }

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=doc.title or meta.get("doc_name") or stub.stable_id,
            language=self.language, source_language=self.language,
            decision_date=doc.decision_date,
            landing_url=stub.landing_url,
            raw_bytes=data, raw_ext="xml",
            text=doc.text, segments=doc.segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v is not None},
        )


def _flag(value: bool | str) -> bool:
    return str(value).strip().lower() not in ("false", "0", "no", "", "none")
