"""Reading the drafting hierarchy out of a run of statutory text.

A UK section arrives as ONE segment whose body is newline-separated provisions::

    (1) In sections 20 to 23 and this section, unless the context otherwise requires—
    (2) Nothing in sections 20 to 23 shall—
    (a) be construed as limiting the jurisdiction of the High Court…
    (b) affect the provisions of section 226…
    (3) In this section— …

Rendered flat, every provision sits at the same margin and the structure is lost:
the reader cannot see that (a) and (b) hang off (2) rather than standing beside it.
This module recovers the nesting from the enumerators alone.

The hard part is that there is NO fixed hierarchy to assume. Drafters use
``(1)/(a)/(i)``, but also ``(a)/(1)``, roman before alpha, upper-case tiers, and
inserted provisions like ``(4A)`` or ``(2ZA)``. So depth is assigned by ORDER OF
FIRST APPEARANCE — a marker that continues a sequence already in progress returns
to that level; one that starts a new sequence opens a deeper one. That adapts to
whatever order a particular Act happens to use, instead of imposing one.

The other hard part is that ``(i)`` is both "roman one" and "the letter i". It is
resolved by context: if an alpha sequence is sitting at ``(h)``, then ``(i)`` is
the letter that follows it; if no run in progress expects it, it opens a new roman
tier. Nothing else disambiguates these — the tokens are genuinely identical.
"""

from __future__ import annotations

import re

# A provision marker at the head of a line: "(1)", "(4A)", "(a)", "(ii)", "(A)",
# or the trailing-dot forms "1." / "a.". Deliberately narrow — a line opening with
# "(see section 5)" or "(b)(i) applies" prose must not read as an enumerator, so
# the token has to be short and followed by whitespace.
_MARKER_RE = re.compile(r"^[ \t]*(?:\((?P<paren>[0-9A-Za-z]{1,6})\)|(?P<dot>[0-9]{1,3}|[A-Za-z])\.)\s")

_ROMAN_RE = re.compile(r"^(?:x{0,3})(?:ix|iv|v?i{0,3})$", re.IGNORECASE)
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}

# how deep one tier is drawn; the caller multiplies by the returned depth
MAX_DEPTH = 6


def _roman_value(tok: str) -> int:
    """Value of a small lower-case roman numeral (i…xxxix), else 0."""
    tok = tok.lower()
    if not tok or not _ROMAN_RE.fullmatch(tok):
        return 0
    total = prev = 0
    for ch in reversed(tok):
        v = _ROMAN_VALUES.get(ch, 0)
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def _alpha_value(tok: str) -> int:
    """Spreadsheet-style letter position: a=1 … z=26, aa=27. Case-insensitive."""
    n = 0
    for ch in tok.lower():
        if not ("a" <= ch <= "z"):
            return 0
        n = n * 26 + (ord(ch) - 96)
    return n


def _candidates(tok: str) -> list[tuple[str, int, str]]:
    """The (kind, value, suffix) readings a marker token could carry, best first.

    A token is usually unambiguous; the exception that matters is a roman-valid
    letter ("i", "v", "x"), which is returned as BOTH an alpha and a roman reading
    so the caller can pick whichever continues a sequence already running.
    """
    out: list[tuple[str, int, str]] = []
    m = re.fullmatch(r"(\d{1,3})([A-Za-z]{0,3})", tok)
    if m:                                    # "12", and inserted "4A" / "2ZA"
        return [("num", int(m.group(1)), m.group(2).upper())]

    upper = tok.isupper()
    roman = _roman_value(tok)
    alpha = _alpha_value(tok) if tok.isalpha() else 0
    # Multi-letter tokens that parse as roman are roman ("ii", "iv"); a single
    # letter is ambiguous and gets both readings, alpha first because a-b-c runs
    # are far commoner than a roman tier that happens to start mid-sequence.
    if alpha and (len(tok) == 1 or not roman):
        out.append(("ALPHA" if upper else "alpha", alpha, ""))
    if roman:
        out.append(("ROMAN" if upper else "roman", roman, ""))
    return out


def _continues(level: dict, kind: str, value: int, suffix: str) -> bool:
    """Does this reading carry on the sequence recorded at ``level``?"""
    if level["kind"] != kind:
        return False
    last, last_suffix = level["value"], level["suffix"]
    if value == last + 1:
        return True
    # an inserted provision: (4) → (4A) → (4B), all one tier
    if value == last and suffix and suffix != last_suffix:
        return True
    return False


def line_depths(text: str) -> list[tuple[int, int, int]]:
    """Split ``text`` into lines and give each its nesting depth.

    Returns ``(start, end, depth)`` per line, as offsets INTO ``text``, with
    ``depth`` counted from 0. A line with no enumerator inherits the depth of the
    line above it, so a wrapped or continuation line stays with its provision
    rather than snapping back to the margin.
    """
    out: list[tuple[int, int, int]] = []
    levels: list[dict] = []
    depth = 0
    pos = 0
    for raw in text.split("\n"):
        start, end = pos, pos + len(raw)
        pos = end + 1                        # step over the newline
        if not raw.strip():
            out.append((start, end, depth))
            continue
        m = _MARKER_RE.match(raw)
        if not m:
            out.append((start, end, depth))  # continuation: keep the current tier
            continue
        tok = m.group("paren") or m.group("dot")
        cands = _candidates(tok)
        if not cands:
            out.append((start, end, depth))
            continue

        # 1) does this continue a sequence already running? deepest tier first, so
        #    "(h) → (i)" carries on the alpha run rather than opening a roman one
        hit = -1
        chosen = cands[0]
        for i in range(len(levels) - 1, -1, -1):
            for kind, value, suffix in cands:
                if _continues(levels[i], kind, value, suffix):
                    hit, chosen = i, (kind, value, suffix)
                    break
            if hit >= 0:
                break

        if hit >= 0:
            del levels[hit + 1:]             # closing a sub-run pops back out
            levels[hit].update(kind=chosen[0], value=chosen[1], suffix=chosen[2])
            depth = hit
        else:
            # 2) a new sequence: prefer a reading whose tier isn't already open,
            #    so an alpha run nested under an alpha run reads as roman
            open_kinds = {lv["kind"] for lv in levels}
            fresh = [c for c in cands if c[0] not in open_kinds]
            chosen = fresh[0] if fresh else cands[0]
            if len(levels) >= MAX_DEPTH:
                del levels[MAX_DEPTH - 1:]
            levels.append({"kind": chosen[0], "value": chosen[1], "suffix": chosen[2]})
            depth = len(levels) - 1
        out.append((start, end, depth))
    return out
