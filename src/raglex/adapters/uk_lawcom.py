"""Law Commission completed projects and their nested document PDFs."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..citations.extractor import extract_citations
from ..core.adapter import BaseAdapter
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
from ..extraction.extractors import PdfExtractor

BASE = "https://lawcom.gov.uk"
COMPLETED_URL = BASE + "/completed-projects-table/"
FOIA_2000 = "ukpga/2000/36"

_YEAR_ACT = re.compile(
    r"(?:(?:section|s\.)\s*(?P<section>\d+[A-Za-z]?"
    r"(?:\(\d+[A-Za-z]?\))*)\s+of\s+)?"
    r"(?P<label>the\s+(?P<year>(?:18|19|20)\d{2})\s+Act)\b",
    re.I,
)
_FULL_ACT_YEAR = re.compile(r"\b((?:18|19|20)\d{2})\b")
_DEFINED_YEAR_ACT = re.compile(r"^the\s+((?:18|19|20)\d{2})\s+Act$", re.I)


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def parse_completed_projects(raw: bytes | str) -> list[dict]:
    """Parse every project row, retaining report number/status/related measures."""
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.select_one("main")
    if main is None:
        return []
    out: list[dict] = []
    year: int | None = None
    for node in main.find_all(["h2", "table"]):
        if node.name == "h2":
            label = _clean(node.get_text(" ", strip=True))
            year = int(label) if re.fullmatch(r"20\d{2}|19\d{2}", label) else year
            continue
        for row in node.select("tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            link = cells[1].select_one("a[href]")
            if link is None:
                continue
            number = _clean(cells[0].get_text(" ", strip=True))
            title = _clean(link.get_text(" ", strip=True))
            url = urljoin(COMPLETED_URL, str(link.get("href") or ""))
            if not title or not url:
                continue
            out.append({
                "number": number if number and number.casefold() not in {"n/a", "n.a"} else None,
                "title": title,
                "url": url,
                "year": year,
                "status": _clean(cells[2].get_text(" ", strip=True)) if len(cells) > 2 else "",
                "measures": _clean(cells[3].get_text(" ", strip=True)) if len(cells) > 3 else "",
                "archived": "webarchive.nationalarchives.gov.uk" in urlsplit(url).netloc,
            })
    return out


def _original_archive_target(url: str) -> str:
    match = re.search(r"/https?://", url)
    return url[match.start() + 1:] if match else url


def _document_row(
    *, project: dict, project_title: str, label: str, category: str,
    url: str, landing_url: str | None = None,
) -> dict:
    identity_url = _original_archive_target(url).split("?", 1)[0]
    digest = hashlib.sha1(identity_url.encode("utf-8")).hexdigest()[:14]
    project_path = urlsplit(_original_archive_target(project["url"])).path
    project_slug = _slug(project_path.rstrip("/").rsplit("/", 1)[-1])
    title = label if project_title.casefold() in label.casefold() else (
        f"{project_title} — {label}"
    )
    return {
        "stable_id": f"uk/lawcom/{project_slug}/{digest}",
        "title": title,
        "url": url,
        "landing_url": landing_url or project["url"],
        "category": category,
        "project_title": project_title,
    }


def parse_project_documents(raw: bytes | str, project: dict) -> list[dict]:
    """Return direct PDFs and new-style nested publication links under Documents."""
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.select_one("main article") or soup.select_one("main")
    if main is None:
        return []
    heading = main.select_one("h1")
    project_title = _clean(heading.get_text(" ", strip=True)) if heading else project["title"]
    in_documents = False
    category = ""
    out: list[dict] = []
    seen: set[str] = set()
    for node in main.find_all(["h2", "h3", "h4", "a"]):
        if node.name == "h2":
            label = _clean(node.get_text(" ", strip=True))
            in_documents = label.casefold().startswith("documents")
            category = ""
            continue
        if node.name in {"h3", "h4"}:
            if in_documents:
                category = _clean(node.get_text(" ", strip=True))
            continue
        if not in_documents:
            continue
        href = _clean(str(node.get("href") or ""))
        url = urljoin(project["url"], href)
        is_pdf = ".pdf" in href.casefold()
        is_publication = (
            urlsplit(url).netloc.casefold() == "lawcom.gov.uk"
            and urlsplit(url).path.startswith("/publication/")
        )
        if not (is_pdf or is_publication):
            continue
        if url in seen:
            continue
        seen.add(url)
        label = _clean(node.get_text(" ", strip=True))
        if not label:
            label = urlsplit(_original_archive_target(url)).path.rsplit("/", 1)[-1]
        if is_pdf:
            out.append(_document_row(
                project=project, project_title=project_title, label=label,
                category=category, url=url,
            ))
        else:
            out.append({
                "title": label,
                "url": url,
                "category": category,
                "project_title": project_title,
                "publication": True,
            })
    return out


def parse_publication_documents(
    raw: bytes | str, project: dict, publication: dict
) -> list[dict]:
    """Resolve every PDF attached to a new-style Law Commission publication page."""
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.select_one("main article") or soup.select_one("main")
    if main is None:
        return []
    heading = main.select_one("h1")
    publication_title = (
        _clean(heading.get_text(" ", strip=True))
        if heading else publication["title"]
    )
    out: list[dict] = []
    seen: set[str] = set()
    for link in main.select("a[href]"):
        href = _clean(str(link.get("href") or ""))
        if ".pdf" not in href.casefold():
            continue
        url = urljoin(publication["url"], href)
        if url in seen:
            continue
        seen.add(url)
        label = _clean(link.get_text(" ", strip=True))
        # The current template renders the same PDF twice: once as an unlabelled
        # thumbnail and once as a labelled download. Prefer its publication title
        # over a filename when the first link is the only one present.
        if not label:
            label = publication_title
        label = re.sub(r"\s*\(PDF,\s*[^)]*\)\s*$", "", label, flags=re.I)
        out.append(_document_row(
            project=project,
            project_title=publication["project_title"],
            label=label,
            category=publication["category"],
            url=url,
            landing_url=publication["url"],
        ))
    return out


def lawcom_year_act_relations(
    text: str, *, context: str = "", minimum_uses: int = 3
) -> list[TypedRelation]:
    """Cautious fallback for repeated, undefined ``the YYYY Act`` references.

    A colon definition is handled by the ordinary shorthand engine and wins. Without
    one, a year is inferred only where this report names exactly one resolvable Act of
    that year (including its completed-project table context) and uses the bare label
    at least ``minimum_uses`` times. FOIA is never a 2000 candidate because Law
    Commission PDFs routinely mention it in publication boilerplate.
    """
    definitions: list[dict] = []
    citations = extract_citations(text, defs_out=definitions)
    context_citations = extract_citations(context) if context else []
    defined_years = {
        match.group(1)
        for row in definitions
        if (match := _DEFINED_YEAR_ACT.fullmatch(row.get("shorthand") or ""))
    }
    candidates: dict[str, set[str]] = defaultdict(set)
    for citation in [*citations, *context_citations]:
        if citation.entity_kind != "act" or not citation.candidate_id:
            continue
        match = _FULL_ACT_YEAR.search(citation.raw)
        if not match:
            continue
        year = match.group(1)
        if year == "2000" and citation.candidate_id == FOIA_2000:
            continue
        # Only a full named Act establishes the year candidate; shorthand uses
        # themselves must not bootstrap the fallback.
        if " act " not in f" {citation.raw.casefold()} ":
            continue
        candidates[year].add(citation.candidate_id)

    matches = list(_YEAR_ACT.finditer(text))
    counts = Counter(match.group("year") for match in matches)
    occupied = [(c.char_start, c.char_end) for c in citations]
    relations: list[TypedRelation] = []
    for match in matches:
        year = match.group("year")
        if year in defined_years or counts[year] < minimum_uses:
            continue
        targets = candidates.get(year, set())
        if len(targets) != 1:
            continue
        if any(start < match.end() and match.start() < end for start, end in occupied):
            continue
        section = match.group("section")
        relations.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=match.group(0),
            dst_id=next(iter(targets)),
            dst_anchor=f"s. {section}" if section else None,
            extracted_via=ExtractedVia.INFERRED,
            resolution_status=ResolutionStatus.PENDING,
            context_start=match.start(),
            context_end=match.end(),
        ))
    return relations


class LawCommissionReportsAdapter(BaseAdapter):
    source = "uk-lawcom-reports"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=240
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        projects = parse_completed_projects(self._client.get(COMPLETED_URL).content)
        visited: set[str] = set()
        pages = 0
        for project in projects:
            # Archived projects are immutable: include them in a backfill, but do not
            # burn ~200 Wayback requests on every keep-current run.
            if since and project["archived"]:
                continue
            if project["url"] in visited:
                continue
            if max_pages is not None and pages >= max_pages:
                return
            visited.add(project["url"])
            pages += 1
            response = self._client.get(project["url"])
            final_url = str(getattr(response, "url", project["url"]))
            project = {**project, "url": final_url}
            for row in parse_project_documents(response.content, project):
                if row.get("publication"):
                    publication_response = self._client.get(row["url"])
                    publication_url = str(getattr(
                        publication_response, "url", row["url"]
                    ))
                    publication = {**row, "url": publication_url}
                    rows = parse_publication_documents(
                        publication_response.content, project, publication
                    )
                else:
                    rows = [row]
                for document in rows:
                    yield Stub(
                        stable_id=document["stable_id"],
                        landing_url=document["landing_url"],
                        raw_url=document["url"],
                        title=document["title"],
                        court="Law Commission",
                        hints={
                            **project,
                            **document,
                            "watermark": (
                                str(project["year"])
                                if project.get("year") else None
                            ),
                        },
                    )

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        # Preserve PDF line boundaries: the report glossaries use a paragraph-initial
        # ``short label: Full Act`` layout, and the generic layout extractor deliberately
        # flattens hard line breaks within a text block.
        extracted = PdfExtractor().extract(
            raw, ext="pdf", mime="application/pdf"
        )
        text = (extracted.text or "").strip()
        needs_ocr = extracted.needs_ocr
        if len(text) < 100 and needs_ocr:
            try:
                from .edpb import ocr_pdf
                text = (ocr_pdf(raw) or "").strip()
                needs_ocr = not bool(text)
            except Exception:  # noqa: BLE001 — retain the processed OCR-needed row
                pass
        if len(text) < 100:
            return None
        segments = [
            Segment(label=f"p. {page}", char_start=start, char_end=end, kind="page")
            for page, start, end in (extracted.page_spans or [])
        ]
        context = " ".join(filter(None, [
            stub.hints.get("project_title"),
            stub.hints.get("measures"),
        ]))
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.PREPARATORY,
            title=stub.title,
            court="Law Commission",
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            segments=segments,
            relations=lawcom_year_act_relations(text, context=context),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk", "law-reform", "law-commission", _slug(
                stub.hints.get("category") or "project-document"
            )],
            extra={
                "jurisdiction": "uk",
                "issuer": "Law Commission",
                "law_commission_number": stub.hints.get("number"),
                "project": stub.hints.get("project_title"),
                "project_year": stub.hints.get("year"),
                "project_status": stub.hints.get("status"),
                "related_measures": stub.hints.get("measures"),
                "document_category": stub.hints.get("category"),
                "archived_project": bool(stub.hints.get("archived")),
                "download_url": stub.raw_url,
                "needs_ocr": needs_ocr,
                "require_recognized_legal_citation": True,
            },
        )
