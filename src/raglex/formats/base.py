"""Pluggable document-format parsers (the user's "format adapters").

Fetching and *parsing* are different concerns: an adapter knows how to reach a
source's bytes; a **format parser** knows how to turn one markup family into text +
structural segments + citations. Splitting them means (a) one Akoma Ntoso parser
serves UK legislation *and* UK judgments, (b) a source that changes format (or
serves several) just selects a different parser, and (c) adding a format is a drop-
in — the same plug-in discipline as extraction (§5c), embedding providers (§6d),
and tag rules (§4a).

A parser takes raw bytes and returns a ``ParsedDoc``: flat text (for FTS/display),
``Segment``s on the document's own structural units (§6b), citation relations, and
any metadata it can read (title, date). Adapters map that onto a ``Record``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from ..core.models import Segment, TypedRelation
from ..core.registry import Registry


@dataclass(slots=True)
class ParsedDoc:
    text: str | None = None
    segments: list[Segment] = field(default_factory=list)
    relations: list[TypedRelation] = field(default_factory=list)
    title: str | None = None
    decision_date: date | None = None
    metadata: dict = field(default_factory=dict)


# format name -> parser(bytes) -> ParsedDoc (the format-parser registry, §core.registry)
_PARSERS: Registry[Callable[[bytes], ParsedDoc]] = Registry("format")


def register(name: str, parser: Callable[[bytes], ParsedDoc]) -> None:
    _PARSERS.register(name, parser, replace=True)


def parse(format_name: str, data: bytes) -> ParsedDoc:
    return _PARSERS.get(format_name)(data)


def available() -> list[str]:
    return _PARSERS.names()
