"""Canadian case law — the A2AJ bulk corpus (parquet).

`Access to Algorithmic Justice <https://a2aj.ca>`_ publishes ~223k full-text decisions
from 26 Canadian courts and tribunals as one parquet file per court. It is a *local bulk
import*, not a live source: point ``path`` at the unpacked dataset directory and every
court folder is walked.

Two things make this corpus unusually valuable, and both shape the adapter:

**1. The identifiers line up with what the corpus already cites.** Canadian neutral
citations are bare-year-first (``2011 SCC 10``, no brackets — see [[commonwealth-citations]]),
and the citation extractor already mints ``scc/2011/10`` for them. Minting the *same*
slug as the stable_id means importing this dataset **resolves the pending Canadian
citations already in the corpus** rather than creating a parallel set of nodes. Decisions
with no neutral citation (CITT dockets, older tribunal material) fall back to a surrogate
id derived from the source URL, so they are still held — just not citation-addressable.

**2. It ships its own citation network.** ``cases_cited`` lists the neutral citations
found in each decision's text, already extracted upstream. Those become structured
``cites`` edges at import time, so the graph is populated without waiting for the text
extraction pass — and because both sides key on the same slug, edges between two held
decisions resolve immediately.

**Bilinguality.** Rows carry English and French versions of the same decision. Unlike
Canadian *legislation* (where the two languages are co-equal enactments and both are
stored — see [[ca-hk-nz-legislation]]), a judgment is handed down once and translated, so
this stores **one record per case**, preferring the English text and falling back to
French, with the other language's citation, URL and availability recorded in ``extra``.
That keeps 223k cases at 223k documents instead of doubling them for translations.

**Reporter aliases.** ``citation_*``/``citation2_*`` carry the *law-report* citation
(``[1940] SCR 578``), which is how these cases are cited in practice and which resolves
to no slug of its own. Each is minted as a resolution alias onto the decision's id, in
both the punctuated and unpunctuated spellings, so a pending ``[1999] 2 S.C.R. 817``
lands on the case instead of sitting unresolved forever.

Reading is **streamed**: the courts' parquet files are written as a single row group
(all 10,887 Supreme Court decisions in one), so reading a group to serve one row would
pull the whole file — over a gigabyte for the larger courts — into memory. Instead
``discover`` walks ``iter_batches`` and carries each row on its stub, and the pipeline
consumes stubs one at a time, so memory stays flat regardless of court size.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..citations.reporters import report_citations
from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)

__all__ = ["CanadianCaseLawAdapter", "ca_neutral_slug", "COURT_NAMES"]

# The dataset's court/tribunal codes → their names. Doubles as the allow-list for the
# ``courts`` option and as the display name on the Record.
COURT_NAMES: dict[str, str] = {
    "SCC": "Supreme Court of Canada",
    "FCA": "Federal Court of Appeal",
    "FC": "Federal Court",
    "TCC": "Tax Court of Canada",
    "CMAC": "Court Martial Appeal Court",
    "BCCA": "British Columbia Court of Appeal",
    "BCSC": "Supreme Court of British Columbia",
    "ONCA": "Ontario Court of Appeal",
    "NSCA": "Nova Scotia Court of Appeal",
    "NSSC": "Nova Scotia Supreme Court",
    "NSPC": "Nova Scotia Provincial Court",
    "NSFC": "Nova Scotia Family Court",
    "NSSM": "Nova Scotia Small Claims Court",
    "YKCA": "Yukon Court of Appeal",
    "CHRT": "Canadian Human Rights Tribunal",
    "CIRB": "Canada Industrial Relations Board",
    "CITT": "Canadian International Trade Tribunal",
    "CT": "Competition Tribunal",
    "FPSLREB": "Federal Public Sector Labour Relations and Employment Board",
    "OHSTC": "Occupational Health and Safety Tribunal Canada",
    "OIC": "Information Commissioner of Canada",
    "PSDPT": "Public Service Disclosure Protection Tribunal",
    "RAD": "Refugee Appeal Division (IRB)",
    "RPD": "Refugee Protection Division (IRB)",
    "RLLR": "Refugee Law Lab Reporter",
    "SST": "Social Security Tribunal",
}

# The Canadian neutral citation: bare 4-digit year, court code, running number.
# "R v Smith, 2011 SCC 10" → scc/2011/10. Deliberately anchored on a word boundary and
# a 2–10 char uppercase-ish token so docket strings ("AP-2019-001") can't match.
_CA_NEUTRAL = re.compile(
    r"\b(?P<year>(?:1[89]|20)\d{2})\s+(?P<court>[A-Z][A-Za-z]{1,9})\s+(?P<num>\d{1,6})\b")


def ca_neutral_slug(citation: str | None) -> str | None:
    """``"2011 SCC 10"`` → ``"scc/2011/10"`` — the same slug the citation extractor
    mints for a Canadian neutral citation, so an imported decision lands on the node
    the corpus was already citing. None when the string carries no neutral citation
    (docket-style identifiers, which are not citation-addressable)."""
    m = _CA_NEUTRAL.search(citation or "")
    if not m:
        return None
    return f"{m.group('court').lower()}/{m.group('year')}/{int(m.group('num'))}"


def _surrogate_id(court: str, url: str, citation: str) -> str:
    """A stable id for a decision with no neutral citation. Keyed on the source URL
    (stable upstream) so re-importing the dataset updates rather than duplicates."""
    seed = (url or citation or "").strip().lower()
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return f"ca-case/{court.lower()}/{digest}"


def _as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19].replace("Z", "")).date()
    except ValueError:
        return None


def _clean(value) -> str | None:
    """Empty strings and pandas/pyarrow nulls both mean "absent" in this dataset."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _listy(value) -> list[str]:
    """``cases_cited`` is a list column that is null (not empty) when the decision has
    no text in that language."""
    if value is None:
        return []
    try:
        return [str(v) for v in value if v is not None and str(v).strip()]
    except TypeError:
        return []


# Law-report citations as they are actually written vary in punctuation ("[1999] 2
# S.C.R. 817" vs "[1999] 2 SCR 817") and the report grammar matches both, so the alias
# is minted in both spellings — an alias only fires on an exact folded-string match.
def report_aliases(*citations: str | None) -> list[str]:
    """The alias spellings the law-report citations in these strings should resolve under.

    The alias must be the report citation *as the extractor matches it* — an alias joins
    on the edge's folded raw string, so aliasing a whole style-of-cause would never fire.
    Both punctuated and unpunctuated spellings are minted because the report grammar
    matches "[1999] 2 S.C.R. 817" and "[1999] 2 SCR 817" alike but folds each to itself.
    """
    out: list[str] = []
    for citation in citations:
        for found in report_citations(citation):
            for variant in (found, found.replace(".", "")):
                variant = " ".join(variant.split())
                if variant and variant not in out:
                    out.append(variant)
    return out


class CanadianCaseLawAdapter(BaseAdapter):
    """A2AJ Canadian case law, imported from a local parquet dataset.

    ``path`` is the dataset directory (one folder per court, each holding
    ``train.parquet``). ``courts`` limits which folders are read; ``since`` filters on
    the decision date so a refreshed dataset imports only newer decisions.
    """

    source = "ca-caselaw"
    min_interval = 0.0        # local filesystem
    requires_js = False
    requires_proxy = False

    def __init__(self, *, path: str | Path | None = None,
                 courts: str | tuple[str, ...] | None = None,
                 min_year: int | str | None = None,
                 language: str = "en") -> None:
        self.path = Path(path).expanduser() if path else None
        if isinstance(courts, str):
            courts = tuple(c.strip() for c in courts.split(",") if c.strip())
        self.courts = {c.upper() for c in (courts or ())}
        self.min_year = int(min_year) if str(min_year or "").strip().isdigit() else None
        self.language = (language or "en").lower()

    # -- parquet plumbing ----------------------------------------------------
    @staticmethod
    def _pq():
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise RuntimeError(
                "reading the A2AJ parquet dataset needs pyarrow — install the 'bulk' "
                "extra (pip install '.[bulk]')") from exc
        return pq

    def _files(self) -> list[tuple[str, Path]]:
        if self.path is None or not self.path.is_dir():
            return []
        out: list[tuple[str, Path]] = []
        for folder in sorted(self.path.iterdir()):
            if not folder.is_dir():
                continue
            court = folder.name.upper()
            if self.courts and court not in self.courts:
                continue
            for file in sorted(folder.glob("*.parquet")):
                out.append((court, file))
        return out

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        pq = self._pq()
        cutoff = (since or "")[:10]
        count = 0
        for court, file in self._files():
            handle = pq.ParquetFile(file)
            # Streamed in modest batches rather than by row group: these files hold every
            # decision of a court in ONE group, so a group read is a whole-file read.
            for batch in handle.iter_batches(batch_size=200):
                for row in batch.to_pylist():
                    stub = self._stub(court, row, cutoff)
                    if stub is None:
                        continue
                    yield stub
                    count += 1
                    if max_pages is not None and count >= max_pages * 500:
                        return

    def _stub(self, court: str, row: dict, cutoff: str) -> Stub | None:
        citation = _clean(row.get("citation_en")) or _clean(row.get("citation_fr")) or ""
        url = _clean(row.get("url_en")) or _clean(row.get("url_fr")) or ""
        decided = _as_date(row.get("document_date_en") or row.get("document_date_fr"))
        if self.min_year and decided and decided.year < self.min_year:
            return None
        stamp = decided.isoformat() if decided else None
        if cutoff and stamp and stamp <= cutoff:
            return None
        # No text in either language → nothing to hold.
        if not (_clean(row.get("unofficial_text_en")) or _clean(row.get("unofficial_text_fr"))):
            return None
        stable_id = ca_neutral_slug(citation) or _surrogate_id(court, url, citation)
        title = _clean(row.get("name_en")) or _clean(row.get("name_fr")) or citation
        return Stub(
            stable_id=stable_id, title=title, landing_url=url or None,
            court=court.lower(), hint_date=decided,
            # The row travels ON the stub. The pipeline consumes stubs one at a time from
            # the generator, so exactly one decision is ever in memory — and fetch needs
            # no second read of a file whose row groups are the size of the file.
            hints={"row": row, "court": court, "watermark": stamp},
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        h = stub.hints
        row = h.get("row")
        if not row:
            return None
        court = h["court"]

        text_en = _clean(row.get("unofficial_text_en"))
        text_fr = _clean(row.get("unofficial_text_fr"))
        # A judgment is handed down once and translated, so one record per case: prefer
        # the English text, fall back to French, and record what the other language has.
        if self.language == "fr":
            text, language = (text_fr or text_en), ("fr" if text_fr else "en")
        else:
            text, language = (text_en or text_fr), ("en" if text_en else "fr")
        if not text:
            return None

        citation = _clean(row.get(f"citation_{language}")) or _clean(row.get("citation_en")) \
            or _clean(row.get("citation_fr"))
        decided = _as_date(row.get(f"document_date_{language}")
                           or row.get("document_date_en") or row.get("document_date_fr"))

        # The upstream citation network, already extracted — structured cites edges that
        # resolve immediately between two held decisions because both sides key on the
        # same neutral-citation slug.
        relations: list[TypedRelation] = []
        seen: set[str] = set()
        for cited in _listy(row.get("cases_cited_en")) + _listy(row.get("cases_cited_fr")):
            dst = ca_neutral_slug(cited)
            if not dst or dst == stub.stable_id or dst in seen:
                continue
            seen.add(dst)
            relations.append(TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=cited, dst_id=dst,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

        other = "fr" if language == "en" else "en"
        # Law-report citations → resolution aliases on this decision. These cases are
        # cited in practice by their reporter ("[1999] 2 SCR 817"), which resolves to no
        # slug, so without the alias every such citation stays pending even though the
        # judgment is right here. Both language versions' citations are included: a
        # French judgment cited by its RCS reference should land on the same node.
        aliases = report_aliases(
            _clean(row.get("citation_en")), _clean(row.get("citation_fr")),
            _clean(row.get("citation2_en")), _clean(row.get("citation2_fr")),
        )

        extra = {
            "jurisdiction": "ca",
            "court_code": court,
            "court_name": COURT_NAMES.get(court, court),
            "citation": citation,
            "citation2": _clean(row.get("citation2_en")) or _clean(row.get("citation2_fr")),
            "aliases": aliases or None,
            "dataset": _clean(row.get("dataset")) or court,
            # A2AJ reproduces official decisions but is itself a secondary source —
            # reconcile against the court's own site where fidelity matters.
            "is_authoritative": False,
            "provider": "A2AJ (a2aj.ca)",
            "upstream_license": _clean(row.get("upstream_license")),
            f"url_{other}": _clean(row.get(f"url_{other}")),
            f"citation_{other}": _clean(row.get(f"citation_{other}")),
            # whether the other language version exists upstream (we hold one record)
            f"has_text_{other}": bool(text_fr if other == "fr" else text_en),
            "citing_cases_count": row.get("citing_cases_count"),
            "cases_cited_count": len(seen),
            "scraped_at": (_clean(row.get(f"scraped_timestamp_{language}")) or "")[:10] or None,
            # a decision with no neutral citation isn't citation-addressable: it is held
            # under a URL-derived surrogate, so say so rather than implying otherwise
            "surrogate_id": stub.stable_id.startswith("ca-case/"),
        }

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=stub.title or citation or stub.stable_id,
            court=court.lower(),
            decision_date=decided,
            language=language, source_language=language,
            landing_url=stub.landing_url,
            text=text, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v is not None},
        )
