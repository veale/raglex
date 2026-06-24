"""Scraping tier (§5a) — pluggable anti-bot fetchers + recipe-driven adapters,
quarantined behind the adapter boundary."""

from .fetcher import (
    FetchedPage,
    Fetcher,
    HttpxFetcher,
    PlaywrightFetcher,
    StealthyFetcher,
    get_fetcher,
)
from .recipe import ScrapeRecipe, parse_detail, parse_listing
from .recipes import RECIPES
from .scrape_adapter import RecipeScrapeAdapter

__all__ = [
    "FetchedPage",
    "Fetcher",
    "HttpxFetcher",
    "PlaywrightFetcher",
    "StealthyFetcher",
    "get_fetcher",
    "ScrapeRecipe",
    "parse_detail",
    "parse_listing",
    "RECIPES",
    "RecipeScrapeAdapter",
]
