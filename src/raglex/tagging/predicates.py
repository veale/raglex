"""Rule predicates (§4a) — the leaves of a rule's boolean condition tree.

A rule's condition is a small boolean expression tree; leaves are predicates over
a document's features. This module implements the rungs that need only the
catalogue + extracted text (no embeddings/graph), per build step 4:

  - ``literal``   — case/accent-folded substring match in text or a named field
  - ``regex``     — regex over text or a field (validated + input-capped, see safety)
  - ``grep_like`` — whole-word / proximity ("X within N words of Y")
  - ``field``     — structured-column predicate (court=, date>=, in [...])

``citation``/``graph`` (needs §5b edges), ``semantic`` (needs §6 embeddings),
``tag`` (compose other tags), and ``script`` (sandboxed escape hatch) are later
build steps; they slot in as new entries in ``PREDICATES`` without touching the
tree evaluator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..core.text import fold

# Safety rail (§4a): cap the text a single regex/literal scans so a pathological
# pattern can't run unbounded. A true catastrophic-backtracking guard (timeouts)
# lands with the `script`/regex hardening step; this is the first-line bound.
_MAX_SCAN_CHARS = 5_000_000


@dataclass(slots=True)
class DocView:
    """The evaluable view of one document: its catalogue row plus lazily-loaded
    extracted text (so a field-only rule never reads the text file)."""

    row: Mapping[str, Any]
    _load_text: Callable[[], str | None]
    _text: str | None = None
    _text_loaded: bool = False

    @property
    def text(self) -> str:
        if not self._text_loaded:
            self._text = self._load_text()
            self._text_loaded = True
        return self._text or ""

    def field(self, name: str) -> Any:
        try:
            return self.row[name]
        except (KeyError, IndexError):
            return None


def _haystack(doc: DocView, field: str | None) -> str:
    if field and field != "text":
        value = doc.field(field)
        return str(value) if value is not None else ""
    return doc.text


def pred_literal(doc: DocView, args: Mapping[str, Any]) -> bool:
    """Folded substring match — '2016/679' matches 'Regulation 2016/679'."""
    needle = fold(str(args["value"]))
    hay = fold(_haystack(doc, args.get("field"))[:_MAX_SCAN_CHARS])
    return needle in hay


def pred_regex(doc: DocView, args: Mapping[str, Any]) -> bool:
    pattern = _compile(args["pattern"], args.get("flags"))
    hay = _haystack(doc, args.get("field"))[:_MAX_SCAN_CHARS]
    try:
        return pattern.search(hay) is not None
    except re.error:
        return False


def pred_grep_like(doc: DocView, args: Mapping[str, Any]) -> bool:
    """Word-boundary / proximity match — looser than regex, tighter than substring."""
    hay = fold(_haystack(doc, args.get("field"))[:_MAX_SCAN_CHARS])
    near = args.get("near")
    if near:
        a, b = fold(str(near[0])), fold(str(near[1]))
        within = int(args.get("within", 10))
        words = re.findall(r"\w+", hay)
        a_pos = [i for i, w in enumerate(words) if w == a]
        b_pos = [i for i, w in enumerate(words) if w == b]
        return any(abs(i - j) <= within for i in a_pos for j in b_pos)
    term = fold(str(args["value"]))
    return re.search(rf"\b{re.escape(term)}\b", hay) is not None


_FIELD_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "like": lambda a, b: a is not None and fold(str(b)) in fold(str(a)),
}


def pred_field(doc: DocView, args: Mapping[str, Any]) -> bool:
    """Structured-column predicate: court=, decision_date>=, jurisdiction IN [...].
    ISO date strings compare lexicographically, which is also chronological."""
    value = doc.field(args["field"])
    op = _FIELD_OPS[args.get("op", "eq")]
    return op(value, args["value"])


PREDICATES: dict[str, Callable[[DocView, Mapping[str, Any]], bool]] = {
    "literal": pred_literal,
    "regex": pred_regex,
    "grep_like": pred_grep_like,
    "field": pred_field,
}


def _compile(pattern: str, flags: str | None) -> re.Pattern[str]:
    f = re.IGNORECASE if (flags and "i" in flags) else 0
    return re.compile(pattern, f)


def validate_pattern(pattern: str, flags: str | None = None) -> None:
    """Raise re.error if a regex rule's pattern won't compile — caught at rule-add
    time (§4a safety rail) so a broken rule never reaches ingest."""
    _compile(pattern, flags)
