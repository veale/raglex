"""US case law — the CourtListener quarterly bulk exports (local CSV).

The API adapter ([[courtlistener]]) is capped at 125 requests a day on the free tier,
which is fine for resolving a cited case on demand and hopeless for seeding a court.
Walking SCOTUS opinion-by-opinion through the API would take years. The bulk exports
are the same data with **no rate limit at all**, so this is where a corpus actually
comes from; the API is for keeping it current and answering "what is 576 U.S. 644?".

**What the exports are.** PostgreSQL ``COPY TO`` CSV dumps of whole tables, published
to a public S3 bucket (``com-courtlistener-storage``, prefix ``bulk-data/``) on the
last day of March/June/September/December. Each file is a complete snapshot at
generation time — there are no deltas, which is precisely why the API's incremental
poll exists alongside this. Point ``path`` at a directory holding the downloaded
files; names are matched by their distinctive stem, so the timestamped upstream
filenames work as-is.

**Filtering on the way in is the whole trick.** The exports cover every US
jurisdiction CourtListener tracks — 3,000+ courts, tens of millions of rows, and an
opinions file that is enormous because it carries the text. A SCOTUS+circuits seed
wants perhaps 0.5% of that. So ``courts`` is an allowlist applied as the rows stream
past: dockets whose court survives define the cluster set, clusters define the opinion
set, and everything else is discarded before it costs memory or disk. The files are
read with ``csv`` in streaming mode and never loaded whole.

**Identity is shared with the API adapter, deliberately.** Both mint
``us/<reporter>/<vol>/<page>`` via the one ``us_candidate_id`` constructor and both
alias the parallel citations, so a case seeded from bulk and the same case fetched
on demand are the *same node* — re-importing is an upsert, not a duplicate. That is
what lets an operator seed cheaply and still resolve on demand without reconciling
two corpora.

Import order matters and is enforced by ``discover``: courts → dockets → clusters →
opinions → citation map. Each stage only needs the id set the previous one produced.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..citations.us_cases import us_candidate_id
from ..core.adapter import BaseAdapter
from ..core.models import (DocType, ExtractedVia, Record, RelationshipType,
                           ResolutionStatus, Segment, Stub, TypedRelation)
from .courtlistener import (FEDERAL_APPELLATE, _OPINION_TYPE_LABELS, _REPORTER_PRIORITY,
                            _strip_markup)

__all__ = ["CourtListenerBulkAdapter", "BULK_FILES", "BULK_S3_URI"]

BULK_S3_URI = "s3://com-courtlistener-storage/bulk-data/"

# Filename stem → what it holds. The upstream files carry a generation timestamp
# ("2026-03-31-opinions.csv"), so match on the distinctive part rather than the whole
# name; that way a directory of freshly-downloaded exports works untouched.
BULK_FILES = {
    "courts": ("court",),
    "dockets": ("docket",),
    "clusters": ("opinioncluster", "opinion-cluster", "clusters"),
    "opinions": ("opinion",),          # checked last: "opinioncluster" also contains it
    "citations": ("opinionscited", "citation-map", "citations-map"),
}

# The opinions CSV carries whole judgments in a single field; the stdlib default
# (128 KiB) truncates them mid-row and desynchronises the parser for the rest of the
# file. Raise it to the platform maximum once, at import.
_MAX_FIELD = min(sys.maxsize, 2**31 - 1)
while True:
    try:
        csv.field_size_limit(_MAX_FIELD)
        break
    except OverflowError:           # 32-bit C long — halve until it is accepted
        _MAX_FIELD //= 2

# CourtListener's CSVs are exported with ESCAPE '\', not CSV-standard doubling.
_ESCAPE = "\\"


class CourtListenerBulkAdapter(BaseAdapter):
    """CourtListener bulk CSV exports, streamed from a local directory.

    ``path`` is the directory of downloaded exports. ``courts`` is the allowlist
    (defaults to SCOTUS + the federal circuits — the exports are whole-corpus, so
    importing without a filter means every US jurisdiction). ``min_year`` drops older
    decisions; ``citation_map`` (default on) imports the opinion-to-opinion graph.

    No network, no rate limit: ``min_interval`` is 0 and the adapter never makes a
    request. A quarterly refresh is "download the new files, re-run this".
    """

    source = "us-caselaw"       # the SAME source key as the API adapter: one corpus
    min_interval = 0.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, path: str | Path | None = None,
                 courts: str | tuple[str, ...] | None = None,
                 min_year: int | str | None = None,
                 citation_map: bool | str = True,
                 prefer_html: bool = False) -> None:
        self.path = Path(path).expanduser() if path else None
        self.courts = set(_listify(courts) or FEDERAL_APPELLATE)
        self.min_year = int(min_year) if min_year else None
        self.citation_map = _as_bool(citation_map)
        self.prefer_html = prefer_html
        # Filled during discover, in dependency order. These are id *sets*, not row
        # stores: the point of streaming is that only the keys stay resident.
        self._docket_courts: dict[str, str] = {}
        # opinion id → its cluster, so the opinion-to-opinion citation map can be
        # lifted to the cluster level (documents here are clusters, not opinions)
        self._opinion_cluster: dict[str, str] = {}
        # cluster id → its identity slug, so an edge can name the cited case by the
        # same id the cited document is stored under
        self._cluster_slugs: dict[str, str] = {}
        self._edges_cache: dict[str, set[str]] | None = None

    @property
    def configured(self) -> bool:
        return self.path is not None and self.path.exists()

    # -- file discovery ------------------------------------------------------
    def _file(self, kind: str) -> Path | None:
        """The export file for ``kind``, matched by stem.

        "opinions" is resolved last and excludes anything containing "cluster",
        because ``opinionclusters.csv`` also contains the substring "opinion" and
        would otherwise shadow the real opinions file — a silent mis-import where
        every case comes out textless.
        """
        if not self.path:
            return None
        stems = BULK_FILES.get(kind, ())
        for candidate in sorted(self.path.glob("*.csv*")):
            name = candidate.name.lower()
            if kind == "opinions" and "cluster" in name:
                continue
            if any(stem in name for stem in stems):
                return candidate
        return None

    def _rows(self, kind: str) -> Iterator[dict]:
        """Stream one export as dicts. Handles the .csv.bz2/.gz the bucket serves."""
        fp = self._file(kind)
        if fp is None:
            return
        with _open_maybe_compressed(fp) as fh:
            for row in csv.DictReader(fh, escapechar=_ESCAPE):
                yield row

    # -- discover ------------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Walk the exports in dependency order, yielding one stub per opinion cluster.

        ``since`` filters on the cluster's ``date_modified`` so re-pointing at a fresh
        quarterly drop imports what actually changed rather than re-reading a corpus
        that is already held. ``max_pages`` bounds the run (it counts *cases*, not
        pages — there is no pagination here), which is what makes a "try it on 100
        cases first" run possible against a 300 GB export.
        """
        if not self.configured:
            raise FileNotFoundError(
                f"{self.source}: no bulk export directory at {self.path!r}. Download the "
                f"quarterly CSVs from {BULK_S3_URI} (aws s3 cp --no-sign-request) and "
                "point `path` at them.")
        self._scan_dockets()
        yielded = 0
        # cluster id → the cluster row, for the ones that survived the court filter.
        # Only the surviving clusters' metadata is held (an id set plus the fields a
        # Record needs), never the opinion text — that streams past in the next pass.
        clusters = self._scan_clusters(since)
        for cluster_id, cluster in self._attach_opinions(clusters):
            stub = _stub_for_bulk_cluster(cluster_id, cluster)
            if stub is None:
                continue
            yield stub
            yielded += 1
            if max_pages is not None and yielded >= max_pages:
                return

    def _scan_dockets(self) -> None:
        """Court allowlist → the docket ids that survive it.

        Dockets are the only place the court appears, so this pass is what makes every
        later filter cheap: after it, a cluster is in scope iff its docket_id is a key
        here.
        """
        for row in self._rows("dockets"):
            court = (row.get("court_id") or "").strip()
            if court and court in self.courts:
                self._docket_courts[str(row.get("id"))] = court

    def _scan_clusters(self, since: str | None) -> dict[str, dict]:
        keep: dict[str, dict] = {}
        for row in self._rows("clusters"):
            docket_id = str(row.get("docket_id") or "")
            court = self._docket_courts.get(docket_id)
            if court is None:
                continue                # a court we didn't ask for
            filed = _as_date(row.get("date_filed"))
            if self.min_year and filed and filed.year < self.min_year:
                continue
            modified = (row.get("date_modified") or "").strip()
            if since and modified and modified <= since:
                continue
            cluster_id = str(row.get("id"))
            keep[cluster_id] = {**row, "_court": court}
            # Remember the identity slug now, while the row is in hand: the citation
            # map names cases by id, and an edge has to point at the slug the cited
            # document will actually be stored under or it resolves to nothing.
            slugs = _bulk_citation_slugs(keep[cluster_id])
            if slugs:
                self._cluster_slugs[cluster_id] = slugs[0]
        return keep

    def _attach_opinions(self, clusters: dict[str, dict]) -> Iterator[tuple[str, dict]]:
        """Stream the opinions file, gathering each in-scope cluster's opinion text.

        The opinions export is the largest file by far, so it is read exactly once and
        rows for out-of-scope clusters are dropped immediately. Opinions arrive grouped
        by cluster in practice but that is not guaranteed, so text is accumulated per
        cluster and every cluster is emitted only after the file is exhausted.
        """
        pending: dict[str, list[dict]] = defaultdict(list)
        for row in self._rows("opinions"):
            cluster_id = str(row.get("cluster_id") or "")
            if cluster_id not in clusters:
                continue
            opinion_id = str(row.get("id") or "")
            pending[cluster_id].append(row)
            if opinion_id:
                self._opinion_cluster[opinion_id] = cluster_id
        for cluster_id, cluster in clusters.items():
            opinions = pending.get(cluster_id)
            if not opinions:
                continue            # metadata-only cluster: nothing citable to store
            opinions.sort(key=lambda o: (_int_or(_clean_nullable(o.get("ordering_key")), 99),
                                         str(o.get("type") or "")))
            yield cluster_id, {**cluster, "_opinions": opinions}

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        """Build the Record from the row already carried on the stub — no I/O.

        ``discover`` has streamed the data past exactly once; re-reading a
        multi-gigabyte CSV per case would make the import quadratic.
        """
        cluster = stub.hints.get("cluster")
        if not cluster:
            return None
        opinions = cluster.get("_opinions") or []
        text, segments = _assemble_bulk_text(opinions, prefer_html=self.prefer_html)
        if not text.strip():
            return None

        slugs = _bulk_citation_slugs(cluster)
        aliases = [s for s in slugs if s != stub.stable_id]
        relations = self._cited_relations(stub.hints.get("cluster_id"))
        case_name = (_clean(cluster.get("case_name"))
                     or _clean(cluster.get("case_name_full")) or stub.stable_id)

        extra = {
            "jurisdiction": "us",
            "court_code": cluster.get("_court"),
            "cluster_id": stub.hints.get("cluster_id"),
            "citations": [c for c in _bulk_citation_strings(cluster)] or None,
            "aliases": aliases or None,
            "precedential_status": _clean_nullable(cluster.get("precedential_status")),
            "judges": _clean_nullable(cluster.get("judges")),
            "citation_count": _int_or(cluster.get("citation_count"), None),
            "opinion_count": len(opinions) or None,
            "case_name_full": _clean(cluster.get("case_name_full")),
            "scdb_id": _clean_nullable(cluster.get("scdb_id")),
            "date_modified": _clean(cluster.get("date_modified")),
            "is_authoritative": False,
            "provider": "CourtListener bulk export (Free Law Project)",
            "upstream_license": "public domain (CC PDM)",
            "bulk_import": True,
            "surrogate_id": stub.stable_id.startswith("us-case/"),
        }
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=case_name,
            court=cluster.get("_court"),
            decision_date=_as_date(cluster.get("date_filed")),
            language="en", source_language="en",
            landing_url=stub.landing_url,
            text=text,
            segments=segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v not in (None, "", [])},
        )

    def _cited_relations(self, cluster_id: str | None) -> list[TypedRelation]:
        """This cluster's outbound citation edges from the citation-map export.

        The map is opinion-to-opinion (``citing_opinion_id`` → ``cited_opinion_id``)
        while documents here are clusters, so both ends are lifted to their cluster and
        self-edges dropped. Loaded lazily and once — the map is a narrow table, so it
        fits in memory where the opinions file never would.

        These land as PENDING against the cited case's citation slug and resolve the
        moment that case is held, which for a whole-court seed is usually immediately:
        it is the same import.
        """
        if not (self.citation_map and cluster_id):
            return []
        edges = self._citation_edges()
        out: list[TypedRelation] = []
        for dst in edges.get(cluster_id, ()):  # already deduped + slug-keyed
            out.append(TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=dst, dst_id=dst,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))
        return out

    def _citation_edges(self) -> dict[str, set[str]]:
        """citing cluster id → the citation slugs it cites, from the map export."""
        if self._edges_cache is not None:
            return self._edges_cache
        # cluster id → its identity slug, for the in-scope clusters only. An edge to a
        # case outside the allowlist can't be slugged and is dropped: it would resolve
        # to nothing and just inflate the pending queue.
        edges: dict[str, set[str]] = defaultdict(set)
        slugs = self._cluster_slugs
        for row in self._rows("citations"):
            citing = self._opinion_cluster.get(str(row.get("citing_opinion_id") or ""))
            cited = self._opinion_cluster.get(str(row.get("cited_opinion_id") or ""))
            if not citing or not cited or citing == cited:
                continue
            slug = slugs.get(cited)
            if slug:
                edges[citing].add(slug)
        self._edges_cache = edges
        return edges


# -- stub / identity --------------------------------------------------------
def _stub_for_bulk_cluster(cluster_id: str, cluster: dict) -> Stub | None:
    slugs = _bulk_citation_slugs(cluster)
    stable_id = slugs[0] if slugs else f"us-case/cl-{cluster_id}"
    return Stub(
        stable_id=stable_id,
        title=_clean(cluster.get("case_name")) or _clean(cluster.get("case_name_full")),
        court=cluster.get("_court"),
        landing_url=f"https://www.courtlistener.com/opinion/{cluster_id}/",
        hint_date=_as_date(cluster.get("date_filed")),
        hints={"cluster": cluster, "cluster_id": cluster_id,
               "watermark": _clean(cluster.get("date_modified"))},
    )


# The bulk cluster row carries its parallel citations as a repeated set of columns
# rather than a nested list (CSV has no nesting): citation_volume/citation_reporter/…
# and sometimes a flat "citation" string. Read whichever this export vintage uses.
_CITE_COL_RE = re.compile(r"^citation(?P<n>\d*)_(?P<part>volume|reporter|page)$")
# "576 U.S. 644" inside a flat citation column
_FLAT_CITE_RE = re.compile(r"(?P<vol>\d{1,4})\s+(?P<rep>[A-Za-z][A-Za-z0-9.'\s]{0,18}?)\s+(?P<page>\d{1,5})")


def _bulk_citation_slugs(cluster: dict) -> list[str]:
    """The cluster's citations as ``us/<rep>/<vol>/<page>`` slugs, best reporter first.

    Same ordering rule as the API adapter (_REPORTER_PRIORITY): the head becomes the
    document identity and the tail become aliases, so bulk-seeded and API-fetched
    copies of one case agree on which node is canonical.
    """
    seen: dict[str, None] = {}
    for volume, reporter, page in _bulk_citation_parts(cluster):
        seen.setdefault(us_candidate_id(volume, reporter, page))

    def rank(slug: str) -> tuple[int, str]:
        rep = slug.split("/")[1] if "/" in slug else ""
        return (_REPORTER_PRIORITY.index(rep) if rep in _REPORTER_PRIORITY
                else len(_REPORTER_PRIORITY), slug)

    return sorted(seen, key=rank)


def _bulk_citation_strings(cluster: dict) -> list[str]:
    return [f"{v} {r} {p}" for v, r, p in _bulk_citation_parts(cluster)]


def _bulk_citation_parts(cluster: dict) -> list[tuple[str, str, str]]:
    """``(volume, reporter, page)`` triples from whichever citation columns exist."""
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for key, value in cluster.items():
        m = _CITE_COL_RE.match(str(key))
        if m and _clean(value):
            grouped[m.group("n")][m.group("part")] = _clean(value)
    out: list[tuple[str, str, str]] = []
    for parts in grouped.values():
        if parts.get("volume") and parts.get("reporter") and parts.get("page"):
            out.append((parts["volume"], parts["reporter"], parts["page"]))
    if out:
        return out
    # Older/flat exports put the whole citation in one column.
    for key in ("citation", "citations", "federal_cite_one", "state_cite_one"):
        for m in _FLAT_CITE_RE.finditer(_clean(cluster.get(key)) or ""):
            out.append((m.group("vol"), m.group("rep").strip(), m.group("page")))
    return out


# -- text -------------------------------------------------------------------
def _assemble_bulk_text(opinions: list[dict], *, prefer_html: bool) -> tuple[str, list[Segment]]:
    """Concatenate a cluster's opinions into one document, one segment each — the
    bulk-CSV twin of the API adapter's ``_assemble_text``. Same output shape, because
    the two paths must produce interchangeable documents."""
    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for op in opinions:
        body = _bulk_opinion_text(op, prefer_html=prefer_html)
        if not body.strip():
            continue
        label = _OPINION_TYPE_LABELS.get(str(op.get("type") or "").strip(), "Opinion")
        author = _clean_nullable(op.get("author_str"))
        if author:
            label = f"{label} — {author}"
        chunk = f"{label}\n\n{body.strip()}\n\n"
        parts.append(chunk)
        segments.append(Segment(label=label, char_start=cursor,
                                char_end=cursor + len(chunk), kind="zone"))
        cursor += len(chunk)
    return "".join(parts), segments


def _bulk_opinion_text(opinion: dict, *, prefer_html: bool) -> str:
    """One opinion's text from the export's several representations.

    Which column is populated depends on how CourtListener ingested that decision
    (court PDF, Harvard OCR, Columbia, Lawbox…), so there is no single field to read —
    try them in order of how much structure survived.
    """
    order = ["plain_text", "html_with_citations", "html", "html_columbia",
             "html_lawbox", "html_anon_2020", "xml_harvard"]
    if prefer_html:
        order.insert(0, order.pop(order.index("html_with_citations")))
    for key in order:
        value = _clean(opinion.get(key))
        if value:
            return value if key == "plain_text" else _strip_markup(value)
    return ""


# -- small helpers ----------------------------------------------------------
def _open_maybe_compressed(path: Path):
    """Open a CSV that may be bz2/gz-compressed, as the bucket serves them."""
    name = path.name.lower()
    if name.endswith(".bz2"):
        import bz2
        return bz2.open(path, "rt", encoding="utf-8", newline="")
    if name.endswith(".gz"):
        import gzip
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "r", encoding="utf-8", newline="")


def _clean(value) -> str:
    """A CSV field as a stripped string, with PostgreSQL's NULL marker removed."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("\\N", "\\n") else text


def _clean_nullable(value) -> str:
    """As ``_clean``, for columns where a one-character value is never legitimate.

    PostgreSQL COPY writes NULL as ``\\N``, but these exports are generated with
    ``ESCAPE '\\'`` — so by the time the csv reader has consumed the backslash as an
    escape character, a NULL arrives as a bare ``"N"``, indistinguishable from a real
    one-letter value. It cannot be disambiguated after parsing.

    So the two are separated by column instead: ``_clean`` is used everywhere, and this
    stricter version only where a lone "N" cannot be meaningful anyway (an author name,
    an ordering key, a status). Reading a NULL as an author is how every opinion in an
    import ends up attributed to a judge named "N".
    """
    text = _clean(value)
    return "" if text == "N" else text


def _int_or(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_date(value) -> date | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _as_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _listify(value: str | tuple | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]
