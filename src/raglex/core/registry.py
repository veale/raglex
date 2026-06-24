"""The pluggable-registry primitive — RagLex's core extensibility contract.

Five subsystems already share one shape: **behaviour as registered data**, so
coverage grows by *registering* rather than rewriting —

  - format parsers (`formats/`)        — markup family → text + segments
  - embedding providers (`embeddings/`) — (provider, model) → vectors
  - tag predicates (`tagging/`)         — predicate type → boolean test
  - scrape recipes (`scraping/`)        — source → selectors
  - citation grammars (`citations/`)    — citation form → candidate + pinpoint

This makes that contract explicit and reusable instead of five parallel
conventions: a ``Registry`` is a named map you ``register`` into and ``get`` from,
with ``names``/``available`` for discovery (what the UI/MCP enumerate). New
backends are a ``register(...)`` call at import time; nothing else changes.
"""

from __future__ import annotations

from typing import Generic, Iterator, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind  # e.g. "format", "embedding provider", "citation grammar"
        self._items: dict[str, T] = {}

    def register(self, name: str, item: T, *, replace: bool = False) -> T:
        if name in self._items and not replace:
            raise ValueError(f"{self.kind} {name!r} already registered")
        self._items[name] = item
        return item

    def get(self, name: str) -> T:
        try:
            return self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "(none)"
            raise KeyError(f"unknown {self.kind} {name!r}; known: {known}") from None

    def names(self) -> list[str]:
        return sorted(self._items)

    available = names  # alias used across the codebase

    def values(self) -> list[T]:
        return list(self._items.values())

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)
