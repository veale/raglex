"""Sweden — the archived *Sök rättspraxis* snapshot (``se-domstol-bulk``).

An offline seed read from a local parquet corpus of Domstolsverket publications
(``nexoneAB/swedish-legal-decisions-raw-v1``, 17,228 distinct publications harvested from
the same REST service ``se-domstol`` reads live). Its README counts 55,096 records; that
is the same 17,228 publications rendered three ways — ``pretrain``, ``instruct`` and
``structured`` — and each of the three is also written twice, once as
``train-00000-of-00001.parquet`` and once as ``train/data.parquet``. This adapter reads
the ``structured`` rendition, ignores the duplicates, and dedupes on ``pub_id``.

## What it is for, which is not what a bulk seed is usually for

It is **not** a faster route to the corpus. Compared against the live service, the
snapshot has no text this corpus lacks: across a 400-record sample the text already held
was comparable or longer in every single case, never shorter, because ``se-domstol``
reads the publisher's own HTML and PDF while the snapshot holds a cleaned rendering.
Importing it wholesale would archive 17,000 good documents and replace them with worse
ones, since a differing payload advances the stored version.

What it holds that nothing else does is the publications **Domstolsverket has since
withdrawn**. Of the 432 ids in the snapshot that the paged list no longer returns, 391 are
judgments the list merely hides — ``se-domstol`` reaches those by expanding the
publication group — and **41 are gone**: the detail route answers HTTP 200 with an empty
body, and no route reaches them at all.

Forty of the forty-one are ``PROVNINGSTILLSTAND`` notices from Högsta domstolen and
Högsta förvaltningsdomstolen, granted between October 2024 and March 2026. That is a
category with a life cycle: the court publishes the question it has agreed to hear, and
takes the notice down once it has answered it. The statement of what the Supreme Court
thought worth deciding, in its own words, is therefore evidence that exists only until it
matters most. This adapter is how the corpus keeps it.

So ``mode`` defaults to ``withdrawn``: walk the live service, and import from the snapshot
only what the service can no longer supply. ``mode=all`` exists for a cold start with no
network, and should not be run against a corpus already harvested live.

## Deciding what is withdrawn costs about six hundred requests

Not 17,228. The paged list settles the great majority in 174 requests — an id the list
returns is live, no probe needed — and only the remainder is probed one by one. The list
is deterministic (two full walks returned identical id sets), so this is a sound test
rather than a sampling.

If the service cannot be reached, ``withdrawn`` mode raises rather than falling through to
importing everything: "the network is down" and "the publisher withdrew it" must not
produce the same import.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Iterator

from ..citations.swedish import ACT_ABBREVS, ACTS, _fold, act_id
from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor
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
from ..formats.se_dom_pdf import parse_se_judgment_pdf
from .se_domstol import BASE, SITE, _WEIGHT, _clean, _iso, _listify

#: Where the corpus is mounted in the container (``/corpora``, not
#: ``/data/corpora`` — ``/data`` is the app's own store).
DEFAULT_PATH = "/corpora/se"
#: The rendition to read, best first. ``structured`` carries the section map as well as
#: the text; ``pretrain`` carries the text alone and is the fallback.
_RENDITIONS = ("structured", "pretrain")
#: ``publiceringsform`` values, as against the precedential-weight vocabulary. The
#: snapshot flattens the service's two fields into one ``typ`` column, so each value has
#: to be put back under the field it came from.
_FORMS = frozenset({"REFERAT", "DOM_ELLER_BESLUT", "NOTIS"})

#: ``legal_references[].law`` → SFS, for the four references in five that carry a law's
#: name but no number. Built from the grammar's own tables so the snapshot resolves to the
#: identifiers the rest of the corpus uses — and keyed through the grammar's own folding,
#: because its table is already accent-stripped ("miljobalken") and the snapshot is not.
_BY_NAME = {_fold(name): sfs for name, sfs in ACTS.items()}
_BY_ABBREV = {_fold(abbrev): sfs for abbrev, sfs in ACT_ABBREVS.items()}
_SFS_RE = re.compile(r"^(?P<year>(?:1[6-9]|20)\d{2}):(?P<number>\d{1,5})$")


class SwedishCaseLawBulkAdapter(BaseAdapter):
    source = "se-domstol-bulk"
    min_interval = 0.4
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        path: str | None = None,
        mode: str | None = None,
        ids: str | list[str] | None = None,
        verify_withdrawn: bool | str | None = None,
        start_offset: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.path = Path(path or DEFAULT_PATH)
        self.mode = (mode or "withdrawn").strip().lower()
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = {i for i in (ids or []) if i}
        self.verify_withdrawn = option_flag(verify_withdrawn, True)
        # Handed back by ``jobs`` from an interrupted run's checkpoint — see
        # ``core.adapter.resume_floor``.
        self.start_offset = resume_floor(option_int(start_offset, 0), 100)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        rows = list(self._rows())
        if self.ids:
            rows = [r for r in rows if _clean(r.get("pub_id")) in self.ids]
        elif self.mode != "all":
            rows = [r for r in rows if self._is_withdrawn(_clean(r.get("pub_id")))]
        total = len(rows)
        for offset, row in enumerate(rows):
            if offset < self.start_offset:
                continue
            if max_pages is not None and offset >= max_pages * 100:
                return
            pub_id = _clean(row.get("pub_id"))
            if not pub_id:
                continue
            yield Stub(
                stable_id=f"se/domstol/{pub_id}",
                landing_url=f"{SITE}/avgorande/{pub_id}",
                title=_clean(row.get("malnummer")) or pub_id,
                court=_clean(row.get("domstol")) or None,
                hint_date=_iso(row.get("avgorandedatum") or row.get("datum")),
                hints={"row": row, "resume_offset": offset, "feed_total": total},
            )

    def _rows(self) -> Iterator[dict]:
        """Every distinct publication in the corpus, best rendition first.

        Each rendition is written twice — once flat and once under a split directory —
        so the ``pub_id`` seen set is what keeps the import from doubling.
        """
        import pyarrow.parquet as pq

        seen: set[str] = set()
        for rendition in _RENDITIONS:
            base = self.path / rendition
            if not base.is_dir():
                continue
            shards = sorted(base.glob("*.parquet")) or sorted(base.glob("*/*.parquet"))
            for shard in shards:
                for batch in pq.ParquetFile(shard).iter_batches(batch_size=256):
                    for row in batch.to_pylist():
                        pub_id = _clean(row.get("pub_id"))
                        if pub_id and pub_id not in seen:
                            seen.add(pub_id)
                            yield row
            if seen:
                return

    def _is_withdrawn(self, pub_id: str) -> bool:
        if not pub_id:
            return False
        if pub_id in self._listed():
            return False
        if not self.verify_withdrawn:
            return True
        # The list withholds one member of every publication group, so "not listed" is not
        # yet "withdrawn" — se-domstol reaches those by expanding the group. Only an id
        # the detail route will not serve either is really gone.
        return not self._served(pub_id)

    def _listed(self) -> frozenset[str]:
        """Every id the paged list returns, in 174 requests."""
        if getattr(self, "_listed_cache", None) is None:
            ids: set[str] = set()
            page = 0
            while True:
                rows = _listify(self._get(f"{BASE}/publiceringar",
                                          {"pagesize": 100, "page": page}))
                rows = [r for r in rows if isinstance(r, dict)]
                if not rows:
                    break
                ids.update(_clean(r.get("id")) for r in rows if r.get("id"))
                if len(rows) < 100:
                    break
                page += 1
                if page > 2000:                       # a runaway pager, not a corpus
                    break
            if not ids:
                raise FetchError(
                    "se-domstol-bulk: the live service returned no publications, so "
                    "nothing can be shown to be withdrawn. Re-run when it is reachable, "
                    "or pass mode=all to import the snapshot regardless.")
            self._listed_cache = frozenset(ids)
        return self._listed_cache

    def _served(self, pub_id: str) -> bool:
        """Whether the detail route still answers for this id.

        It answers HTTP 200 with an empty body for a withdrawn publication, which is why
        the status code cannot be the test.
        """
        try:
            resp = self._client.get(f"{BASE}/publiceringar/{pub_id}",
                                    headers={"Accept": "application/json"},
                                    raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return False
        return resp.status_code < 400 and bool((resp.text or "").strip())

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        row = dict(stub.hints.get("row") or {})
        pub_id = _clean(row.get("pub_id"))
        text = (row.get("fulltext") or row.get("text") or "").strip()
        if not text:
            return None
        # The same reader treatment the live adapter gives a judgment. The snapshot's own
        # ``sektioner_json`` is deliberately not used: it is a third party's regex reading
        # of the text, only two thirds of its sections can be located in the text they
        # were taken from, and some that can are wrong — one sampled decision's "titel"
        # is 812 characters beginning mid-sentence. A section map that cannot be anchored
        # exactly would put citations at offsets the words are not at.
        parsed = parse_se_judgment_pdf(text)
        text = parsed.text or text

        court = _clean(row.get("domstol"))
        marks = _malnummer(row)
        kind = _clean(row.get("typ")).upper()
        decided = _iso(row.get("avgorandedatum") or row.get("datum"))

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=_title(court, marks, decided),
            court=court or None,
            decision_date=decided,
            language="sv",
            source_language="sv",
            landing_url=stub.landing_url,
            raw_bytes=text.encode("utf-8"),
            raw_ext="txt",
            text=text,
            segments=list(parsed.segments),
            relations=_reference_relations(row),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in (_WEIGHT.get(kind, ""),
                                    "vägledande" if row.get("ar_vagledande") else "") if t],
            extra={k: v for k, v in {
                "jurisdiction": "se",
                "record_id": pub_id or None,
                "mal_nummer": marks or None,
                "publication_form": kind if kind in _FORMS else None,
                "precedential_weight": kind if kind not in _FORMS else None,
                "is_guiding": bool(row.get("ar_vagledande")),
                "text_source": _clean(row.get("text_kalla")) or None,
                "zones": (parsed.metadata.get("zones") or None),
                # This publication is held on the strength of an archived snapshot, not of
                # the publisher's live service, and a reader is entitled to know that the
                # source of record no longer serves it.
                "provenance": "archived snapshot of rattspraxis.etjanst.domstol.se",
                "upstream_withdrawn": True if self.mode != "all" else None,
                "aliases": _aliases(marks) or None,
            }.items() if v not in (None, "", [], {})},
        )

    # -- http ----------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None):
        try:
            resp = self._client.get(url, params=params or {},
                                    headers={"Accept": "application/json"},
                                    raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return None


def _title(court: str, marks: list[str], decided: date | None) -> str:
    parts = [p for p in (court, marks[0] if marks else None,
                         decided.isoformat() if decided else None) if p]
    return ", ".join(parts) or "Avgörande"


def _malnummer(row: dict) -> list[str]:
    """"Mål nr 6728-25" → ``Ö 4337-25``-style docket strings.

    The snapshot keeps the label the court printed, and the corpus cites the number.
    """
    raw = _clean(row.get("malnummer"))
    if not raw:
        return []
    raw = re.sub(r"^M(?:å|a)l\s*nr\.?\s*", "", raw, flags=re.IGNORECASE).strip()
    return [m for m in (part.strip() for part in re.split(r"[,;]", raw)) if m]


def _aliases(marks: list[str]) -> list[str]:
    return [f"se:mal:{m}" for m in marks]


def _reference_relations(row: dict) -> list[TypedRelation]:
    """``legal_references`` → ``INTERPRETS`` edges on the SFS Work.

    Only one reference in five carries the SFS number; the rest name the law
    ("miljöbalken", "RB", "PBL"), which is exactly what ``citations.swedish`` already
    knows. Resolving through the grammar's own tables keeps the snapshot on the same
    identifiers as the live corpus rather than minting a parallel set.
    """
    try:
        refs = json.loads(row.get("legal_references") or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict):
            continue
        work = _work_id(ref)
        if not work:
            continue
        anchor = _anchor(ref)
        if (work, anchor or "") in seen:
            continue
        seen.add((work, anchor or ""))
        out.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=_clean(ref.get("raw")) or work,
            dst_id=work, dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))
    return out


def _work_id(ref: dict) -> str | None:
    if m := _SFS_RE.match(_clean(ref.get("sfs"))):
        return act_id(m.group("year"), m.group("number"))
    name = _fold(_clean(ref.get("law")))
    if not name:
        return None
    sfs = _BY_NAME.get(name) or _BY_ABBREV.get(name)
    return act_id(*sfs) if sfs else None


def _anchor(ref: dict) -> str | None:
    """``{chapter: "2", paragraphs: "15", symbol: "§"}`` → ``2 kap. 15 §``."""
    paragraphs = _clean(ref.get("paragraphs"))
    if not paragraphs:
        return None
    symbol = _clean(ref.get("symbol")) or "§"
    chapter = _clean(ref.get("chapter"))
    section = f"{paragraphs} {symbol}"
    return f"{chapter} kap. {section}" if chapter else section

