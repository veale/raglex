"""Germany — Open Legal Data (``de-openlegaldata``), the Länder case-law corpus.

`Open Legal Data <https://de.openlegaldata.io>`_ republishes German court decisions from
the federal and — this is the point — the **Länder** registers: 424k decisions from 918
courts, where the corpus's existing German case law (``de-rii``, ``de-neuris``) is the
seven federal courts and nothing else. Oberverwaltungsgerichte, Landgerichte,
Landesarbeits- and Landessozialgerichte, Verwaltungsgerichte, Amtsgerichte — the courts
where German data-protection, telecoms and platform litigation actually starts.

Two shapes, one record:

- **Bulk** (``path=``): the HuggingFace dump (``openlegaldata/court-decisions-germany``),
  54 parquet shards. Streamed with ``iter_batches`` and the row carried on the stub, so
  one decision is in memory at a time whatever the shard size.
- **API** (no ``path``): ``/api/cases/`` newest-first, which is how the corpus stays
  current after the bulk seed. The API serves the same fields minus ``markdown_content``
  and ``reference_markers``, so the HTML body is what both paths actually parse (see
  ``formats.olg_html``) and nothing about a record depends on which route fetched it.

## Dedup against the federal case law already held

A decision is keyed by its **ECLI** where it has one (75% do), which is the same key
``de-rii`` and ``de-neuris`` use — so a BGH judgment held from the official
rechtsprechung-im-internet bulk is recognised by the pipeline's held-prefilter and never
re-stored, and the official rendition (whose juris XML segments better) is never
overwritten by this one. The remaining quarter — overwhelmingly Länder decisions no
federal source publishes — is keyed ``de/openlegaldata/<slug>`` and additionally mints
the ``de:case:<COURT>:<AZ>`` alias, so a "OVG Münster, Beschl. v. …, 13 A 1234/20"
citation resolves to it and a later ECLI-bearing rendition of the same decision merges
rather than duplicating.

## The CJEU decisions in the dump are not ingested

3,852 rows are Luxembourg, not German — ``ECLI:EU:C``/``ECLI:EU:T`` judgments and
Advocate-General opinions the register mirrors in German. The corpus holds those from
CELLAR under the same ECLIs, in the EU jurisdiction, with their preparatory and
transposition apparatus attached; storing a German mirror under a German source would
file EU case law as German and duplicate what is already held. They are skipped by
default (``include_eu`` opts in for the German-language text).

## What comes across as structured edges

``reference_markers`` carries upstream-extracted references. The **law** markers are
authoritative — a book slug and a section number (``gvg`` § 171b), which is exactly the
``de/gesetz/<abk>`` id the German grammar mints — and become ``INTERPRETS`` edges with
the § as ``dst_anchor``, so the statute graph is populated at import time rather than
waiting for the text pass. The **case** markers are not: they are mostly law-report
references (``NStZ 2013, 466``) that name no court, and where they do name one the
attribution is a guess that is sometimes wrong (a BGH ``VIII ZB`` docket filed under
BVerfG). Those are left to ``citations.german.case_citations``, whose court window
already refuses to read a docket across another court's name.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..citations.de_courts import court_name
from ..citations.german import case_alias, law_id
from ..core.adapter import BaseAdapter, option_flag, option_int
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
from ..formats.olg_html import parse_olg

API = "https://de.openlegaldata.io/api"
SITE = "https://de.openlegaldata.io"

#: The columns the bulk shards carry. Named explicitly so a shard that gains a column
#: doesn't silently change what a stub weighs.
_COLUMNS = ("id", "slug", "court", "file_number", "date", "created_date", "updated_date",
            "type", "ecli", "content", "markdown_content", "reference_markers")

#: German decision types that are not a judgment of a German court. ``Schlussantrag des
#: Generalanwalts`` is an Advocate General's Opinion — an EU document, and typed as an
#: opinion rather than a judgment wherever one is kept.
_OPINION_TYPES = {"schlussantrag des generalanwalts", "schlussanträge des generalanwalts"}


def _iso(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _is_eu(ecli: str, court: dict) -> bool:
    """A Luxembourg decision mirrored in the German register — see the module docstring."""
    if (ecli or "").upper().startswith(("ECLI:EU:", "ECLI:CE:")):
        return True
    return (court or {}).get("slug", "") in ("eugh", "eug")


def _court_display(court: dict, ecli: str) -> str | None:
    """The court's canonical German name.

    The register's own ``name`` is used where it has one, normalised through the court
    table so every source spells one court one way. 8,433 rows carry the placeholder
    court "Unknown court" — for those the ECLI's court token says which court it really
    was (2,053 BGH, 1,335 BVerwG…), and reading it there is the difference between a
    facet row called "Unknown court" and the judgment appearing under its own court.
    """
    raw = _clean((court or {}).get("name"))
    slug = (court or {}).get("slug") or ""
    if raw and raw.lower() not in ("unknown court", "unknown", ""):
        return court_name(slug) or court_name(raw) or raw
    return court_name(ecli) or None


#: "§§ 611 ff. BGB" — a reference to a section AND those that follow it. Upstream folds
#: the range marker into the section slug, yielding "611f", which is a different (and
#: usually non-existent) provision: § 611a BGB is real, so "§ 611f" reads as one of that
#: lettered family rather than as "§ 611 and following". The anchor keeps the section the
#: range STARTS at, which is the provision the reference is actually about.
_RANGE_TAIL = re.compile(r"(?i)\bff?\.?(?:\s|$)")


def _section_anchor(section: str, raw: str) -> str:
    if _RANGE_TAIL.search(raw or ""):
        stripped = re.sub(r"(?i)ff?$", "", section)
        if stripped and stripped != section and stripped[-1:].isdigit():
            return stripped
    return section


def _law_relations(markers: str | None, *, text_len: int) -> list[TypedRelation]:
    """The upstream law markers → ``INTERPRETS`` edges (see the module docstring).

    Offsets are carried through as the citation context, but only when they fall inside
    the text we actually derived: the marker offsets index the register's own rendition
    of the body, which is the HTML we parse but not character-for-character the flat text
    we store, so an offset past the end would anchor a citation to nothing.
    """
    if not markers:
        return []
    try:
        parsed = json.loads(markers)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for marker in parsed if isinstance(parsed, list) else []:
        if not isinstance(marker, dict):
            continue
        raw = _clean(marker.get("text"))
        start, end = marker.get("start"), marker.get("end")
        for ref in marker.get("references") or []:
            if not isinstance(ref, dict) or ref.get("ref_type") != "RefType.LAW":
                continue
            book = _clean(ref.get("book"))
            if not book:
                continue
            dst = law_id(book)
            section = _section_anchor(_clean(ref.get("section")), raw)
            anchor = f"§ {section}" if section else None
            if (dst, anchor or "") in seen:
                continue
            seen.add((dst, anchor or ""))
            inside = (isinstance(start, int) and isinstance(end, int)
                      and 0 <= start < end <= text_len)
            out.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=raw or f"{book} {section}".strip(),
                dst_id=dst, dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
                context_start=start if inside else None,
                context_end=end if inside else None,
            ))
    return out


class DeOpenLegalDataAdapter(BaseAdapter):
    source = "de-openlegaldata"
    # The public API starts returning 429s during a few hundred detail fetches at
    # 2.5 requests/second. A weekly watch can afford the conservative public-service
    # rate; its steady-state delta is small, and the historical path is local parquet.
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        path: str | None = None,
        ids: str | list[str] | None = None,
        courts: str | list[str] | None = None,
        min_year: int | str | None = None,
        include_eu: bool | str | None = None,
        page_size: int | str | None = None,
        start_offset: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = list(ids or [])
        if isinstance(courts, str):
            courts = [c.strip().lower() for c in courts.split(",") if c.strip()]
        self.courts = {c.lower() for c in (courts or [])}
        self.min_year = option_int(min_year, 0)
        self.include_eu = option_flag(include_eu, False)
        self.page_size = max(1, min(100, option_int(page_size, 100)))
        # Every emitted stub carries resume_offset, so interrupted bulk jobs restore
        # it as start_offset. Keep the cursor in the adapter's filtered-stub space
        # (after EU/court/year exclusions), exactly matching the emitted offsets.
        self.start_offset = max(0, option_int(start_offset, 0))
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            yield from self._discover_ids()
        elif self.path:
            yield from self._discover_bulk(since, max_pages=max_pages)
        else:
            yield from self._discover_api(since, max_pages=max_pages)

    def _shards(self) -> list[Path]:
        base = self.path
        if base is None:
            return []
        if base.is_file():
            return [base]
        files = sorted(base.rglob("*.parquet"))
        return files

    def _discover_bulk(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        import pyarrow.parquet as pq

        cutoff = (since or "")[:10]
        seen = 0
        emitted = 0
        shards = self._shards()
        total = None
        for shard in shards:
            handle = pq.ParquetFile(shard)
            if total is None:
                # One cheap metadata read per shard would be exact; the first shard's
                # row count times the shard count is close enough to draw a progress bar
                # and costs nothing.
                total = handle.metadata.num_rows * len(shards)
            for batch in handle.iter_batches(batch_size=200, columns=list(_COLUMNS)):
                for row in batch.to_pylist():
                    stub = self._stub(row, cutoff, feed_total=total, offset=seen)
                    if stub is None:
                        continue
                    if seen < self.start_offset:
                        seen += 1
                        continue
                    yield stub
                    seen += 1
                    emitted += 1
                    if max_pages is not None and emitted >= max_pages * 500:
                        return

    def _discover_api(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        """Newest-first over ``/api/cases/``, stopping at the watermark.

        The feed's cursor is ``created_date`` — when Open Legal Data ingested the
        decision — not the decision date: Länder registers publish months late and often
        in batches, so a decision-date cursor strands everything that arrives behind the
        front. Deep paging is refused upstream (page 500 is a 404), which is exactly
        right for a watch: a weekly run reads the first pages and stops.
        """
        cutoff = (since or "")[:19]
        page, seen = 1, 0
        while page <= (max_pages or 200):
            payload = self._get(f"{API}/cases/", {
                "ordering": "-created_date", "page": page, "page_size": self.page_size})
            results = (payload or {}).get("results") or []
            if not results:
                return
            for row in results:
                created = _clean(row.get("created_date"))[:19]
                if cutoff and created and created <= cutoff:
                    return
                stub = self._stub(row, "", feed_total=(payload or {}).get("count"),
                                  offset=seen, watermark=created)
                if stub is not None:
                    if seen < self.start_offset:
                        seen += 1
                        continue
                    yield stub
                    seen += 1
            if not (payload or {}).get("next"):
                return
            page += 1

    def _discover_ids(self) -> Iterator[Stub]:
        """A targeted pull: numeric case ids, slugs, or ECLIs."""
        for ident in self.ids:
            ident = ident.strip()
            if not ident:
                continue
            if ident.isdigit():
                row = self._get(f"{API}/cases/{ident}/")
            else:
                key = "ecli" if ident.upper().startswith("ECLI:") else "slug"
                payload = self._get(f"{API}/cases/", {key: ident, "page_size": 1})
                results = (payload or {}).get("results") or []
                row = self._get(f"{API}/cases/{results[0]['id']}/") if results else None
            stub = self._stub(row or {}, "") if row else None
            if stub is not None:
                yield stub

    def _stub(self, row: dict, cutoff: str, *, feed_total=None, offset: int | None = None,
              watermark: str | None = None) -> Stub | None:
        court = row.get("court") or {}
        ecli = _clean(row.get("ecli"))
        if not self.include_eu and _is_eu(ecli, court):
            return None
        if self.courts and (court.get("slug") or "").lower() not in self.courts:
            return None
        decided = _iso(row.get("date"))
        if self.min_year and decided and decided.year < self.min_year:
            return None
        stamp = decided.isoformat() if decided else None
        if cutoff and stamp and stamp <= cutoff:
            return None
        slug = _clean(row.get("slug"))
        if not slug and not ecli:
            return None
        # ONE cursor space per source, and it is the API's ``created_date`` — when the
        # register ingested the decision, which is what ``_discover_api`` compares
        # against. The bulk path deliberately reports NO watermark: its stamp would be a
        # DECISION date, and leaving that as the source cursor would have the weekly
        # watch comparing a date against a datetime from a different clock. A local walk
        # does not need a date cursor anyway — the pipeline's backfill frontier resumes
        # it, and the held-prefilter skips what it already stored.
        hints = {"row": row, "watermark": watermark}
        if feed_total:
            hints["feed_total"] = int(feed_total)
        if offset is not None:
            hints["resume_offset"] = offset
        return Stub(
            stable_id=stable_id_for(ecli, slug),
            landing_url=f"{SITE}/case/{slug}" if slug else None,
            title=_clean(row.get("file_number")) or slug or ecli,
            court=(court.get("slug") or "").lower() or None,
            hint_date=decided,
            hints=hints,
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        row = dict(stub.hints.get("row") or {})
        if not row:
            return None
        # The list endpoint omits the body; the bulk shard carries it. Only pay for the
        # detail request when the body is actually missing FROM AN API STUB. A handful
        # of parquet rows have neither content rendition; falling back to REST for those
        # turned an offline 424k-row seed into a rate-limited network crawl and parked
        # the whole worker inside a long Retry-After sleep. Bodyless bulk rows are not
        # ingestible documents and must be skipped locally.
        if not _clean(row.get("content")) and not _clean(row.get("markdown_content")):
            if self.path:
                return None
            detail = self._get(f"{API}/cases/{row.get('id')}/") if row.get("id") else None
            if detail:
                row.update(detail)
        html = row.get("content") or ""
        markdown = row.get("markdown_content") or ""
        parsed = parse_olg(html, markdown=markdown)
        if not parsed.text:
            return None

        court = row.get("court") or {}
        ecli = _clean(row.get("ecli"))
        slug = _clean(row.get("slug"))
        docket = _clean(row.get("file_number"))
        display = _court_display(court, ecli)
        decision_type = _clean(row.get("type"))
        title = ", ".join(x for x in (display, decision_type, docket) if x) or slug or ecli

        aliases = [a for a in (
            case_alias(display, docket) if display and docket else None,
            f"de/openlegaldata/{slug}" if slug and ecli else None,
        ) if a]
        # The link a reader (and a static export) follows to the original. It has to be
        # minted here, from the slug, because there is no working resolver on the other
        # side: an ECLI resolver answers for the federal courts and not for the Länder
        # ones, which is most of this corpus — ECLI:DE:OVGNRW:… resolves nowhere. The
        # register's own case page always exists for a record it published, so the join
        # is local and total. Where the API also gives the ORIGINAL court-database URL
        # (justiz.nrw.de, rechtsprechung.niedersachsen.de…) that is kept alongside; the
        # bulk dump does not carry it, and new records carry a placeholder instead.
        source_url = _clean(row.get("source_url"))
        if not re.match(r"https?://(?!example\.com)\S+$", source_url):
            source_url = ""
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=ecli if ecli.upper().startswith("ECLI:") else None,
            doc_type=(DocType.OPINION if decision_type.lower() in _OPINION_TYPES
                      else DocType.JUDGMENT),
            title=title,
            court=display,
            decision_date=_iso(row.get("date")),
            language="de",
            source_language="de",
            landing_url=stub.landing_url,
            raw_bytes=(html or markdown).encode("utf-8"),
            raw_ext="html" if html else "md",
            text=parsed.text,
            segments=parsed.segments,
            relations=_law_relations(row.get("reference_markers"),
                                     text_len=len(parsed.text)),
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "aktenzeichen": docket,
                "document_type": decision_type,
                "court_slug": (court.get("slug") or "").lower() or None,
                "court_jurisdiction": _clean(court.get("jurisdiction")) or None,
                "court_level": _clean(court.get("level_of_appeal")) or None,
                "openlegaldata_id": row.get("id"),
                "court_source_url": source_url or None,
                "zones": parsed.metadata.get("zones") or None,
                "aliases": aliases or None,
            }.items() if v},
        )

    # -- http ----------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> dict | None:
        resp = self._client.get(url, params=params or {},
                                headers={"Accept": "application/json"},
                                raise_for_4xx=False)
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return None


def stable_id_for(ecli: str | None, slug: str | None) -> str:
    """The id a decision is held under — its ECLI, else a slug-derived surrogate.

    Keying on the ECLI is what makes this source dedup against ``de-rii`` and
    ``de-neuris`` rather than duplicate them (see the module docstring)."""
    ecli = _clean(ecli)
    if ecli.upper().startswith("ECLI:"):
        return ecli
    return f"de/openlegaldata/{_clean(slug)}"
