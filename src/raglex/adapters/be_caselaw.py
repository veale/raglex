"""Belgian Council of State and Constitutional Court case law.

The Council of State has two complementary official interfaces.  Its archive is only
addressable by sequential decision number (``arr.php?nr=...``), while its rolling
"decisions recentes" pages are the reliable keep-current feed.  A missing archive
number returns a tiny HTML page with HTTP 200, so PDF signature validation is part of
the source protocol rather than merely defensive parsing.

The Constitutional Court's yearly judgments page is an authoritative manifest with
decision date, procedure, docket, subject matter and the exact French PDF URL.  Walking
that manifest is both faster and complete compared with guessing the last judgment of
each year from consecutive 404s.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction.ocr import text_or_ocr

RVS_BASE = "https://www.raadvst-consetat.be/"
RVS_PDF = f"{RVS_BASE}arr.php"
RVS_RECENT = f"{RVS_BASE}?page=lastmonth&lang=fr"
CONST_BASE = "https://fr.const-court.be"
CONST_INDEX = f"{CONST_BASE}/judgments"

_ECLI_RE = re.compile(r"\bECLI\s*:\s*BE\s*:\s*(?:RVSCE|GHCC)\s*:\s*\d{4}\s*:\s*ARR(?:\s*[.:]\s*\d+)+", re.I)
_RVS_NUMBER_RE = re.compile(
    r"\b(?:n[°o]|nr\.?)\s*([\d.]+)\s+(?:du|van)\s+"
    r"\d{1,2}\s+[A-Za-zÀ-ž]+\s+(?:19|20)\d{2}", re.I)
_CONST_NUMBER_RE = re.compile(r"\b(?:Arr[êe]t|Arrest)\s+(?:n[°o]|nr\.?)\s*(\d+)\s*/\s*(\d{4})", re.I)

_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "decembre": 12,
}
_NL_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5,
    "juni": 6, "juli": 7, "augustus": 8, "september": 9, "oktober": 10,
    "november": 11, "december": 12,
}
_LONG_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+([A-Za-zÀ-ž]+)\s+((?:19|20)\d{2})\b", re.I)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _option_bool(value: bool | str | None) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalise_ecli(value: str | None) -> str | None:
    match = _ECLI_RE.search(value or "")
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(0)).upper().replace("ARR:", "ARR.")


def _long_date(value: str | None) -> date | None:
    match = _LONG_DATE_RE.search(value or "")
    if not match:
        return None
    month = {**_FR_MONTHS, **_NL_MONTHS}.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _numeric_date(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _language(text: str) -> str:
    head = text[:12000].casefold()
    french = sum(head.count(x) for x in ("conseil d’état", "conseil d'etat", "arrêt", "considérant", "la cour"))
    dutch = sum(head.count(x) for x in ("raad van state", "arrest", "overwegende", "het hof"))
    return "fr" if french > dutch else "nl"


def _rvs_display_number(number: int) -> str:
    """The court prints thousands with a dot (262672 -> 262.672)."""
    return f"{number:,}".replace(",", ".")


def _pdf_text(blob: bytes) -> tuple[str, bool, list, str]:
    text, needs_ocr, spans, engine = text_or_ocr(blob, max_pages=500)
    text = (text or "").strip()
    if len(text) < 120:
        raise FetchError("Belgian judgment PDF yielded no usable text", transient=True)
    return text, needs_ocr, spans, engine


def rvs_recent_pages(html: bytes | str) -> list[str]:
    """Return the rolling month pages in the site's displayed order."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for link in soup.select('a[href*="page=lastmonth_"]'):
        url = urljoin(RVS_BASE, str(link.get("href") or ""))
        if url not in out:
            out.append(url)
    return out


def rvs_recent_stubs(html: bytes | str) -> list[Stub]:
    """Parse and deduplicate decisions repeated under several subject headings."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    seen: set[int] = set()
    for link in soup.select('a[href*="arr.php?"]'):
        url = urljoin(RVS_BASE, str(link.get("href") or ""))
        query = parse_qs(urlsplit(url).query)
        try:
            number = int(query.get("nr", [""])[0])
        except ValueError:
            continue
        if number in seen:
            continue
        seen.add(number)
        label = _clean(link.get_text(" ", strip=True))
        added_match = re.search(r"Ajouté le\s+(\d{2}/\d{2}/\d{4})", label, re.I)
        subject_match = re.search(r"^\d+\s*\((.*?)\)\s*\[", label)
        canonical = f"{RVS_PDF}?nr={number}"
        out.append(Stub(
            stable_id=f"be/rvsce/{number}", landing_url=canonical, raw_url=canonical,
            title=f"Conseil d'État / Raad van State — arrêt {number}",
            court="be-rvsce",
            hints={
                "decision_number": number,
                "added_date": added_match.group(1) if added_match else None,
                "subject": _clean(subject_match.group(1)) if subject_match else None,
                "advertised": True,
            },
        ))
    return out


class BelgianCouncilOfStateAdapter(BaseAdapter):
    source = "be-rvsce"
    min_interval = 0.35
    PAGE_SIZE = 100

    def __init__(
        self, *, client: RateLimitedClient | None = None,
        first_number: int | str | None = 10000,
        end_number: int | str | None = None,
        start_offset: int | str | None = None,
        watch_mode: bool | str | None = False,
    ) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)
        self.first_number = max(1, option_int(first_number, 10000))
        self.end_number = option_int(end_number, 0) or None
        self.watch_mode = _option_bool(watch_mode)
        cursor = option_int(start_offset, 0)
        self.resume_number = max(
            self.first_number,
            resume_floor(cursor, self.PAGE_SIZE) if cursor else self.first_number,
        )

    def _recent(self) -> list[Stub]:
        index = self._client.get(RVS_RECENT).content
        pages = rvs_recent_pages(index)
        if not pages:
            raise FetchError(f"{self.source}: recent-decisions index contained no month pages")
        seen: set[str] = set()
        out: list[Stub] = []
        # The rolling year is intentionally walked in full. Decisions with old numbers
        # are added to a current month, and rows repeat under multiple subject headings.
        for page in pages:
            for stub in rvs_recent_stubs(self._client.get(page).content):
                if stub.stable_id not in seen:
                    seen.add(stub.stable_id)
                    out.append(stub)
        return out

    def _archive_end(self) -> int:
        rows = self._recent()
        numbers = [option_int(row.hints.get("decision_number"), 0) for row in rows]
        if not numbers:
            raise FetchError(f"{self.source}: recent register contained no decisions")
        return max(numbers)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.watch_mode or since:
            yield from self._recent()
            return

        end = self.end_number or self._archive_end()
        if end < self.resume_number:
            return
        if max_pages is not None:
            end = min(end, self.resume_number + max(0, max_pages) * self.PAGE_SIZE - 1)
        for number in range(self.resume_number, end + 1):
            url = f"{RVS_PDF}?nr={number}"
            yield Stub(
                stable_id=f"be/rvsce/{number}", landing_url=url, raw_url=url,
                title=f"Conseil d'État / Raad van State — arrêt {number}",
                court="be-rvsce",
                hints={"decision_number": number, "resume_offset": number,
                       "advertised": False},
            )

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url or stub.landing_url)
        blob = response.content
        if not blob.startswith(b"%PDF"):
            # The sequential archive represents holes as HTTP-200 HTML. An advertised
            # monthly decision returning the same body is a broken source, not a hole.
            if stub.hints.get("advertised"):
                raise FetchError(f"{self.source}: advertised decision was not a PDF")
            return None
        text, needs_ocr, spans, engine = _pdf_text(blob)
        ecli = _normalise_ecli(text[:15000])
        language = _language(text)
        # Match the number/date unit rather than the word ARRET: PDFs often space the
        # heading as "A R R Ê T", and a later cited judgment can otherwise become this
        # judgment's date (262.672 cites an older arrêt in its opening facts).
        date_match = re.search(
            r"\b(?:n[°o]|nr\.?)\s*[\d.]+\s+(?:du|van)\s+"
            r"(\d{1,2}\s+[A-Za-zÀ-ž]+\s+(?:19|20)\d{2})", text[:5000], re.I)
        decided = _long_date(date_match.group(1) if date_match else text[:5000])
        number = option_int(stub.hints.get("decision_number"), 0)
        number_match = _RVS_NUMBER_RE.search(text[:10000])
        printed_number = re.sub(r"\D", "", number_match.group(1)) if number_match else str(number)
        fallback_id = f"be/rvsce/{number}"
        return Record(
            source=self.source, stable_id=ecli or fallback_id, doc_type=DocType.JUDGMENT,
            title=("Conseil d'État / Raad van State — arrêt "
                   f"{_rvs_display_number(int(printed_number or number))}"),
            court="be-rvsce", decision_date=decided, language=language,
            source_language=language, ecli=ecli, landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext="pdf", text=text, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["belgium", "administrative-law", f"lang-{language}"],
            extra={
                "jurisdiction": "be", "decision_number": number,
                "citation_number": _rvs_display_number(number),
                "subject": stub.hints.get("subject"),
                "register_added_date": stub.hints.get("added_date"),
                "aliases": [fallback_id, str(number), f"arrêt n° {number}", f"arrest nr. {number}"],
                "needs_ocr": needs_ocr, "page_spans": spans,
                "extraction_engine": engine,
                "citation_languages": ["fr", "nl"],
            },
        )


def constitutional_stubs(html: bytes | str, year: int) -> list[Stub]:
    """Parse the yearly authoritative manifest, one row per judgment PDF."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    for card in soup.select('[data-testid="judgment-card"]'):
        link = card.select_one(f'a[href$="/{year}.pdf"]')
        if not link:
            continue
        url = urljoin(CONST_BASE, str(link.get("href") or ""))
        path = urlsplit(url).path.strip("/").split("/")
        if len(path) != 2 or not path[0].isdigit():
            continue
        number = int(path[0])
        header = card.select_one(".w-100.mb-1")
        header_parts = [_clean(x.get_text(" ", strip=True)) for x in header.find_all("span")] if header else []
        decided = _numeric_date(header_parts[0] if header_parts else None)
        procedure = header_parts[-1] if len(header_parts) > 1 else None
        subject_node = card.select_one("div.mt-2")
        subject = _clean(subject_node.get_text(" ", strip=True)) if subject_node else None
        role_match = re.search(r"Numéro de rôle\s*:\s*([^\n]+?)(?=\s{2,}|$)", card.get_text("\n", strip=True), re.I)
        fallback_id = f"be/const-court/{year}/{number}"
        out.append(Stub(
            stable_id=fallback_id, landing_url=f"{CONST_BASE}/a/{number}/{year}",
            raw_url=url, hint_date=decided,
            title=f"Cour constitutionnelle — arrêt n° {number}/{year}" + (f" — {subject}" if subject else ""),
            court="be-const-court",
            hints={
                "year": year, "number": number, "procedure": procedure,
                "subject": subject, "role_number": _clean(role_match.group(1)) if role_match else None,
                "watermark": decided.isoformat() if decided else f"{year}-01-01",
            },
        ))
    return sorted(out, key=lambda stub: option_int(stub.hints.get("number"), 0))


class BelgianConstitutionalCourtAdapter(BaseAdapter):
    source = "be-const-court"
    min_interval = 0.8
    PAGE_SIZE = 25

    def __init__(
        self, *, client: RateLimitedClient | None = None,
        start_year: int | str | None = 2000,
        end_year: int | str | None = None,
        start_offset: int | str | None = None,
        watch_mode: bool | str | None = False,
    ) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)
        self.start_year = max(2000, option_int(start_year, 2000))
        self.end_year = max(self.start_year, option_int(end_year, date.today().year))
        self.start_offset = resume_floor(option_int(start_offset, 0), self.PAGE_SIZE)
        self.watch_mode = _option_bool(watch_mode)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        first_year = self.start_year
        end_year = self.end_year
        if self.watch_mode:
            # Revisit last year too: a judgment dated in December can be posted after
            # the January watch has rolled into a new calendar year.
            first_year = max(first_year, date.today().year - 1)
            end_year = min(end_year, date.today().year)
        elif since:
            match = re.match(r"((?:19|20)\d{2})", since)
            if match:
                first_year = max(first_year, int(match.group(1)))
        years = range(first_year, end_year + 1)
        if max_pages is not None:
            years = range(first_year, min(end_year + 1, first_year + max(0, max_pages)))
        offset = 0
        for year in years:
            response = self._client.get(CONST_INDEX, params={"year": year})
            rows = constitutional_stubs(response.content, year)
            if not rows:
                raise FetchError(f"{self.source}: yearly manifest for {year} contained no judgments")
            for stub in rows:
                current = offset
                offset += 1
                if current < self.start_offset:
                    continue
                stub.hints["resume_offset"] = current
                yield stub

    def fetch(self, stub: Stub) -> Record | None:
        blob = self._client.get(stub.raw_url or stub.landing_url).content
        if not blob.startswith(b"%PDF"):
            raise FetchError(f"{self.source}: manifest PDF link did not return a PDF")
        text, needs_ocr, spans, engine = _pdf_text(blob)
        ecli = _normalise_ecli(text[:15000])
        language = _language(text)
        number = option_int(stub.hints.get("number"), 0)
        year = option_int(stub.hints.get("year"), 0)
        printed = _CONST_NUMBER_RE.search(text[:10000])
        if printed:
            number, year = int(printed.group(1)), int(printed.group(2))
        fallback_id = f"be/const-court/{year}/{number}"
        decided = stub.hint_date
        if not decided:
            date_match = re.search(r"\bdu\s+(\d{1,2}\s+[A-Za-zÀ-ž]+\s+(?:19|20)\d{2})", text[:8000], re.I)
            decided = _long_date(date_match.group(1) if date_match else text[:5000])
        return Record(
            source=self.source, stable_id=ecli or fallback_id, doc_type=DocType.JUDGMENT,
            title=stub.title or f"Cour constitutionnelle — arrêt n° {number}/{year}",
            court="be-const-court", decision_date=decided, language=language,
            source_language=language, ecli=ecli, landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext="pdf", text=text, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["belgium", "constitutional-law", f"lang-{language}"],
            extra={
                "jurisdiction": "be", "judgment_number": f"{number}/{year}",
                "citation_number": f"{number}/{year}",
                "procedure": stub.hints.get("procedure"), "subject": stub.hints.get("subject"),
                "role_number": stub.hints.get("role_number"),
                "aliases": [fallback_id, f"{number}/{year}", f"Arrêt n° {number}/{year}"],
                "needs_ocr": needs_ocr, "page_spans": spans,
                "extraction_engine": engine,
                "citation_languages": ["fr", "nl"],
            },
        )
