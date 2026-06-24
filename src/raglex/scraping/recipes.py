"""Built-in scrape recipes (§5a). Illustrative starting points — verify the
selectors against the live DOM before relying on them (sites change; that's what
the recipe abstraction is for). New regulators slot in as more entries here, or as
user-supplied recipes through the API/UI later.
"""

from __future__ import annotations

from ..core.models import DocType
from .recipe import ScrapeRecipe

# UK Information Commissioner — enforcement actions (data protection / FOI).
# First-party, in-scope by construction (§3/§4). Selectors are a best-effort
# template; confirm live before a real backfill.
ICO_ENFORCEMENT = ScrapeRecipe(
    source="uk-ico",
    base_url="https://ico.org.uk",
    listing_url="https://ico.org.uk/action-weve-taken/enforcement/",
    item_link_selector="a.listing__link, .results-listing a",
    title_selector="h1",
    date_selector="time, .article-date",
    body_selector="main, .article-content",
    next_page_selector="a.pagination__next, a[rel='next']",
    doc_type=DocType.DECISION,
    court="ICO",
    requires_js=False,
    requires_proxy=False,
    min_interval=2.0,
)

RECIPES: dict[str, ScrapeRecipe] = {
    "uk-ico": ICO_ENFORCEMENT,
}
