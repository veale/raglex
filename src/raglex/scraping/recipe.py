"""Recipe-driven scraping (§5a) — a new scrape source is *data*, not code.

A ``ScrapeRecipe`` is a set of CSS selectors describing a listing page (where to
find item links) and a detail page (title / date / body). The parse functions are
pure (BeautifulSoup over a string), so a source is testable against fixture HTML
with no network, and adding a regulator portal is one stored recipe — the same
"editable data, not deployments" discipline as the tag rules (§4a) and providers
(§6d). When a site changes its DOM, you edit the recipe, not Python.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urljoin

from ..core.models import DocType


@dataclass(slots=True)
class ScrapeRecipe:
    source: str
    base_url: str
    listing_url: str
    item_link_selector: str  # CSS selector for <a> links to detail pages
    title_selector: str
    body_selector: str
    date_selector: str | None = None
    date_formats: tuple[str, ...] = ("%d %B %Y", "%Y-%m-%d", "%d/%m/%Y")
    next_page_selector: str | None = None  # CSS for the "next page" link
    doc_type: DocType = DocType.DECISION
    court: str | None = None
    language: str = "en"
    requires_js: bool = False
    requires_proxy: bool = False
    min_interval: float = 2.0


@dataclass(slots=True)
class ListingItem:
    url: str
    title: str | None = None


@dataclass(slots=True)
class DetailDoc:
    title: str | None
    decision_date: date | None
    text: str | None


def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def parse_listing(html: str, recipe: ScrapeRecipe) -> tuple[list[ListingItem], str | None]:
    """Extract item links (+ the next-page URL) from a listing page (pure)."""
    soup = _soup(html)
    items: list[ListingItem] = []
    seen: set[str] = set()
    for a in soup.select(recipe.item_link_selector):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(recipe.base_url, href)
        if url in seen:
            continue
        seen.add(url)
        items.append(ListingItem(url=url, title=a.get_text(strip=True) or None))
    next_url = None
    if recipe.next_page_selector:
        nxt = soup.select_one(recipe.next_page_selector)
        if nxt and nxt.get("href"):
            next_url = urljoin(recipe.base_url, nxt["href"])
    return items, next_url


def parse_detail(html: str, recipe: ScrapeRecipe) -> DetailDoc:
    """Extract title / date / body text from a detail page (pure)."""
    soup = _soup(html)
    title_el = soup.select_one(recipe.title_selector)
    title = title_el.get_text(strip=True) if title_el else None

    decision_date = None
    if recipe.date_selector:
        date_el = soup.select_one(recipe.date_selector)
        if date_el:
            decision_date = _parse_date(date_el.get_text(strip=True), recipe.date_formats)

    body_el = soup.select_one(recipe.body_selector)
    text = _clean(body_el.get_text("\n", strip=True)) if body_el else None
    return DetailDoc(title=title, decision_date=decision_date, text=text)


def _parse_date(raw: str, formats: tuple[str, ...]) -> date | None:
    raw = raw.strip()
    m = re.search(r"\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}", raw)
    candidate = m.group(0) if m else raw
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


_WS = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    return _WS.sub("\n\n", text).strip()
