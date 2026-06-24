"""Recipe scrape adapter (§5a) — quarantines all scraping fragility inside one
adapter that returns the standard ``Record``. The pipeline sees no difference
between this and a pristine REST source.
"""

from __future__ import annotations

from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.models import ExtractedVia, Record, Stub, sha256_bytes
from .fetcher import Fetcher, get_fetcher
from .recipe import ScrapeRecipe, parse_detail, parse_listing


class RecipeScrapeAdapter(BaseAdapter):
    def __init__(self, recipe: ScrapeRecipe, *, fetcher: Fetcher | None = None) -> None:
        self.recipe = recipe
        self.source = recipe.source
        self.min_interval = recipe.min_interval
        self.requires_js = recipe.requires_js
        self.requires_proxy = recipe.requires_proxy
        self._fetcher = fetcher or get_fetcher(
            source=recipe.source, min_interval=recipe.min_interval,
            requires_js=recipe.requires_js,
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        url = self.recipe.listing_url
        pages = 0
        while url:
            page = self._fetcher.fetch(url)
            items, next_url = parse_listing(page.html, self.recipe)
            for item in items:
                yield Stub(
                    stable_id=item.url, landing_url=item.url, raw_url=item.url, title=item.title,
                    court=self.recipe.court,
                )
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            url = next_url

    def fetch(self, stub: Stub) -> Record | None:
        page = self._fetcher.fetch(stub.raw_url)
        detail = parse_detail(page.html, self.recipe)
        raw = page.html.encode("utf-8")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=self.recipe.doc_type,
            title=detail.title or stub.title,
            court=self.recipe.court,
            decision_date=detail.decision_date,
            language=self.recipe.language,
            source_language=self.recipe.language,
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="html",
            payload_hash=sha256_bytes(raw),
            text=detail.text,
            extracted_via=ExtractedVia.SCRAPE,
            extra={"fetch_engine": page.engine},
        )
