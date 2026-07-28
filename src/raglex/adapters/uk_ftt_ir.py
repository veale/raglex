"""First-tier Tribunal (Information Rights) — the Tribunal's own decisions database.

``informationrights.decisions.tribunals.gov.uk`` is the register of every information-
rights appeal decided by the Information Tribunal and, after 2010, the First-tier
Tribunal's General Regulatory Chamber: ~3,200 decisions under FOIA, the EIR, the DPA,
PECR and the national-security rules, each published as a PDF with its appeal number
("EA/2022/0273"), parties, date and outcome.

**Why a separate adapter when ``uk-grc`` already pulls the GRC from Find Case Law.** FCL
only carries the chamber from 2023 — the fifteen years before that exist *only* here, and
they are the ones the corpus keeps citing by appeal number. The two overlap for recent
decisions, which is what the identity ladder is for:

  1. the **neutral citation printed in the PDF** ("[2023] UKFTT 123 (GRC)") → the Find
     Case Law slug ``ukftt/grc/2023/123``, so a decision already held from FCL is the
     SAME node — the pipeline dedups it instead of storing a second copy;
  2. else the **appeal number** → ``uk/ftt-ir/ea/2022/0273``, the form the corpus cites
     these by (and minted as an alias in its written variants either way);
  3. else the register's internal file id → ``uk/ftt-ir/i3253``, so a decision with no
     usable reference still lands under a stable key rather than a content hash.

Discovery walks the register's own paging (``search.aspx?Page=N``, ten rows a page,
newest first — plain GET, no ASP.NET postback needed).

**A closed archive, so a one-off backfill rather than a watch.** The register took its
last decision in August 2023, when the chamber moved to Find Case Law; ``uk-grc`` covers
everything since. The watermark logic below is kept anyway — it costs nothing, and it is
what makes a re-run cheap if the register ever does publish again: because the order is
by decision date descending, an incremental pass stops at the first row at or before the
cursor, while a backfill (``since=None``) walks to the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import synthesise_numbered_segments
from ..extraction.extractors import PdfExtractor

BASE = "https://informationrights.decisions.tribunals.gov.uk"
SEARCH_URL = f"{BASE}/Public/search.aspx"

# An appeal number as the register writes it: EA/2022/0273, with an optional trailing
# jurisdiction tag (EA/2023/0042/GDPR) and, on older rows, a spelling with dots or
# hyphens instead of slashes (the PDFs are inconsistent; the register mostly isn't).
_APPEAL_RE = re.compile(
    r"\b(?P<series>[A-Z]{2,4})[/.\-](?P<year>(?:19|20)\d{2})[/.\-](?P<num>\d{1,5})"
    r"(?:[/.\-](?P<tag>[A-Z]{2,8}))?\b")
# the register's own file id, from the PDF path (/DBFiles/Decision/i3253/…)
_FILE_ID_RE = re.compile(r"/DBFiles/\w+/(i\d+)/", re.IGNORECASE)
# "[2023] UKFTT 123 (GRC)" printed in the decision itself
_NEUTRAL_RE = re.compile(r"\[(?:19|20)\d{2}\]\s+UKFTT\s+\d+\s*\((?:GRC|IR)\)", re.IGNORECASE)
# the ICO decision notice the appeal is against ("IC-130630-R7Y9") — the join between a
# tribunal decision and the Commissioner's notice under appeal
_DN_RE = re.compile(r"\bIC\s*-\s*\d{5,7}\s*-\s*[A-Z0-9]{4}\b", re.IGNORECASE)
_CASE_REF_RE = re.compile(r"(?im)^\s*Case\s+(?:Reference|Number|No\.?)\s*:?\s*(.+)$")
_HEARD_RE = re.compile(r"(?im)^\s*(?:Heard\s+on|Date\s+of\s+hearing)\s*:?\s*(.+?)\s*$")
_DECIDED_RE = re.compile(
    r"(?im)^\s*(?:Decision\s+given\s+on|Date\s+of\s+decision|Promulgated)\s*:?\s*(.+?)\s*$")
_JUDGE_RE = re.compile(
    r"(?im)^\s*(?:Tribunal\s+)?(?:Judge|Member)s?\s*:?\s*(.+?)\s*$")


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def appeal_number(raw: str | None) -> str | None:
    """The canonical ``EA/2022/0273`` form of an appeal number written any way."""
    m = _APPEAL_RE.search((raw or "").upper())
    if not m:
        return None
    parts = [m.group("series"), m.group("year"), m.group("num")]
    if m.group("tag"):
        parts.append(m.group("tag"))
    return "/".join(parts)


def appeal_id(number: str | None) -> str | None:
    """``EA/2022/0273`` → ``uk/ftt-ir/ea/2022/0273`` — the stable_id for a decision with
    no neutral citation (the great majority: the chamber only got one from 2023)."""
    canon = appeal_number(number)
    return "uk/ftt-ir/" + canon.lower() if canon else None


def appeal_aliases(number: str | None) -> list[str]:
    """Every written form of an appeal number the corpus might cite, so a reference to
    "EA/2022/0273" — or "EA.2022.0273", as the PDFs' own filenames spell it — resolves."""
    canon = appeal_number(number)
    if not canon:
        return []
    return [canon, canon.replace("/", "."), canon.replace("/", "-")]


def _parse_uk_date(value: str | None) -> date | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text.rstrip("."), fmt).date()
        except ValueError:
            continue
    # "Heard on: 2 August 2023." style lines carry trailing prose — take the date part
    m = re.search(r"(\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d %B %Y").date()
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class Decision:
    """One row of the register's results table."""

    appeal_number: str | None
    title: str
    additional_party: str | None
    decision_url: str
    summary_url: str | None
    appeal_url: str | None
    decided: date | None
    area: str | None
    outcome: str | None
    file_id: str | None

    @property
    def stable_id(self) -> str:
        """The provisional id discovery can mint. ``fetch`` may replace it with the FCL
        slug once the PDF reveals a neutral citation — the pipeline's provisional-id
        dedup then folds it into the copy already held from Find Case Law."""
        return appeal_id(self.appeal_number) or f"uk/ftt-ir/{self.file_id or 'unknown'}"


def _area(cell) -> str | None:
    """The jurisdictional-area cell. It prints the area and its sub-area on two lines,
    and for most rows the two are the same string — so keep the distinct lines only."""
    lines = [_clean(ln) for ln in cell.get_text("\n").split("\n")]
    seen = list(dict.fromkeys(ln for ln in lines if ln))
    return " · ".join(seen) or None


def parse_results(html: bytes | str) -> list[Decision]:
    """Every decision row on a results page. Rows without a decision PDF are skipped:
    the register lists a handful of entries whose document was never published, and a
    row with no document is nothing to harvest."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.search-results-table")
    if table is None:
        return []
    out: list[Decision] = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        link = cells[1].select_one('a[href*="/Decision/"]')
        if link is None or not str(link.get("href") or "").strip():
            continue
        decision_url = urljoin(SEARCH_URL, str(link.get("href")))
        if not urlsplit(decision_url).path.lower().endswith(".pdf"):
            continue
        # the appeal number is the purple span; the additional party follows its label
        number = _clean(getattr(cells[1].select_one("span.purple"), "text", None))
        extra_party = None
        label = cells[1].find("span", class_="bold")
        if label is not None:
            tail = "".join(
                str(s) for s in label.next_siblings
                if getattr(s, "name", None) is None)
            extra_party = _clean(tail) or None
        summary = cells[3].select_one("a[href]") if len(cells) > 3 else None
        appeal = cells[4].select_one('a[href*="/Appeal/"]') if len(cells) > 4 else None
        outcome = None
        if len(cells) > 4:
            # the outcome is the cell's own text, before the two confirm-link anchors
            outcome = _clean("".join(
                str(s) for s in cells[4].children if getattr(s, "name", None) is None))
        file_id = None
        fm = _FILE_ID_RE.search(decision_url)
        if fm:
            file_id = fm.group(1).lower()
        out.append(Decision(
            appeal_number=appeal_number(number),
            title=_clean(link.get_text(" ", strip=True)),
            additional_party=extra_party,
            decision_url=decision_url,
            summary_url=urljoin(SEARCH_URL, str(summary.get("href"))) if summary else None,
            appeal_url=urljoin(SEARCH_URL, str(appeal.get("href"))) if appeal else None,
            decided=_parse_uk_date(cells[2].get_text(" ", strip=True)),
            area=_area(cells[0]),
            outcome=outcome or None,
            file_id=file_id,
        ))
    return out


def total_results(html: bytes | str) -> int | None:
    """The register's own "Displaying results 1 to 10 (of 3167)" count — used only to
    bound a backfill's page walk (the last page is otherwise indistinguishable from a
    transient empty response)."""
    m = re.search(r"of\s+([\d,]+)\s*\)", str(html))
    return int(m.group(1).replace(",", "")) if m else None


def parse_decision_pdf(text: str) -> dict:
    """The metadata a decision prints on its face: the case reference, the neutral
    citation (2023+), the hearing/decision dates, the panel, and the Commissioner's
    decision-notice reference the appeal is against."""
    head = text[:4000]
    judges: list[str] = []
    for m in _JUDGE_RE.finditer(head):
        for name in re.split(r"\s+and\s+|,", m.group(1)):
            # a panel line runs on to the next member ("Paul Taylor and" / "Dave Sivers")
            name = re.sub(r"(?i)\s+and\s*$", "", _clean(name)).rstrip(".")
            # the label line sometimes runs on into the next field
            if name and len(name) < 60 and not name.lower().startswith(("between", "on the")):
                judges.append(name)
    neutral = _NEUTRAL_RE.search(text)
    dn = _DN_RE.search(text)
    ref = _CASE_REF_RE.search(head)
    return {
        "neutral_citation": _clean(neutral.group(0)) if neutral else None,
        "appeal_number": appeal_number(ref.group(1) if ref else None) or appeal_number(head),
        "heard_on": _parse_uk_date(_HEARD_RE.search(head).group(1)) if _HEARD_RE.search(head) else None,
        "decided_on": _parse_uk_date(_DECIDED_RE.search(head).group(1)) if _DECIDED_RE.search(head) else None,
        "panel": list(dict.fromkeys(judges)) or None,
        "decision_notice": _clean(dn.group(0)).replace(" ", "") if dn else None,
    }


class InformationRightsAdapter(BaseAdapter):
    source = "uk-ftt-ir"
    court = "ukftt/grc"
    min_interval = 2.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        seen: set[str] = set()
        page = 1
        total = None
        while True:
            if max_pages is not None and page > max_pages:
                return
            url = SEARCH_URL if page == 1 else f"{SEARCH_URL}?Page={page}"
            html = self._client.get(url).content
            rows = parse_results(html)
            if not rows:
                return                       # walked past the last page
            if total is None:
                total = total_results(html)
            for row in rows:
                if row.decision_url in seen:
                    continue
                seen.add(row.decision_url)
                wm = row.decided.isoformat() if row.decided else None
                # Newest first: the first row at or before the cursor means everything
                # after it is older still, so an incremental run is done here.
                if since and wm and wm <= since:
                    return
                yield Stub(
                    stable_id=row.stable_id,
                    landing_url=row.decision_url,
                    raw_url=row.decision_url,
                    title=row.title or None,
                    court=self.court,
                    hint_date=row.decided,
                    hints={
                        "watermark": wm,
                        "appeal_number": row.appeal_number,
                        "additional_party": row.additional_party,
                        "area": row.area,
                        "outcome": row.outcome,
                        "summary_url": row.summary_url,
                        "appeal_url": row.appeal_url,
                        "file_id": row.file_id,
                    },
                )
            if total is not None and page * len(rows) >= total:
                return
            page += 1

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        try:
            raw = self._client.get(stub.raw_url or stub.landing_url).content
        except FetchError as exc:
            if exc.transient:
                raise            # not an absence — let the runner retry it
            return None
        extracted = PdfExtractor().extract(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        needs_ocr = extracted.needs_ocr
        if len(text) < 200 and needs_ocr:
            # the pre-2005 decisions are scans of typed originals
            try:
                from .edpb import ocr_pdf
                text = (ocr_pdf(raw) or "").strip()
                needs_ocr = not bool(text)
            except Exception:  # noqa: BLE001 — keep the row, flagged as needing OCR
                pass
        if len(text) < 200:
            return None
        meta = parse_decision_pdf(text)
        # The register's own column wins over the number printed in the document: it is
        # structured data, where the PDF's is a regex over prose that may have come
        # through OCR. A disagreement is kept rather than silently resolved.
        number = stub.hints.get("appeal_number") or meta["appeal_number"]
        in_doc = meta["appeal_number"] if meta["appeal_number"] != number else None

        # Identity ladder (see the module docstring): FCL slug → appeal number → file id.
        stable_id = stub.stable_id
        neutral = meta["neutral_citation"]
        if neutral:
            from ..resolve.matchers import first_candidate
            cand = first_candidate(neutral)
            if cand:
                stable_id = cand.value
        elif appeal_id(number):
            stable_id = appeal_id(number)

        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=stub.title or number or stable_id,
            court=self.court,
            decision_date=meta["decided_on"] or stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            segments=synthesise_numbered_segments(text),
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["uk", "information-rights", "tribunal"],
            extra={k: v for k, v in {
                "jurisdiction": "uk",
                "tribunal": "First-tier Tribunal (General Regulatory Chamber) — Information Rights",
                "appeal_number": number,
                "appeal_number_in_document": in_doc,
                "neutral_citation": neutral,
                "jurisdictional_area": stub.hints.get("area"),
                "outcome": stub.hints.get("outcome"),
                "additional_party": stub.hints.get("additional_party"),
                # the Commissioner's decision notice under appeal — the join to the ICO side
                "ico_decision_notice": meta["decision_notice"],
                "panel": meta["panel"],
                "heard_on": meta["heard_on"].isoformat() if meta["heard_on"] else None,
                "summary_url": stub.hints.get("summary_url"),
                "appeal_url": stub.hints.get("appeal_url"),
                "register_file_id": stub.hints.get("file_id"),
                "download_url": stub.raw_url,
                "needs_ocr": needs_ocr or None,
                # every written form of the appeal number → this document (§5b)
                "aliases": appeal_aliases(number) or None,
            }.items() if v is not None},
        )
