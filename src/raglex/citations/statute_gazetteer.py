"""Statute-title → URI gazetteer (vendored from the legislation.gov.uk eMarkup pipeline).

The official Table-of-Effects pipeline resolves statute *names* in text to URIs with a
gazetteer — lists of every Act's short title mapped to its legislation.gov.uk id. We
reuse those lists (citations/data/statutes/*.lst) to do the same thing offline: turn
"the Data Protection Act 2018" into ``ukpga/2018/12`` (and abbreviations like "ICTA" into
``ukpga/1988/1``) with no network round-trip — which lets the §5 grammars recognise and
resolve the *thousands* of statutes a corpus cites, not just a hand-maintained handful.

Precision comes from confirmation, not the regex: the grammar matches the loose shape
"<Title> Act <year>", and we only mint a candidate when the gazetteer actually has it.

Lists are loaded lazily and cached (≈8.5k primary-legislation entries).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).with_name("data") / "statutes"
# the full-title lists (Title;year=YYYY;context=URI) and the short-form list
# (ABBR;context=URI). All map to a legislation.gov.uk /id/ URI.
_FULL_LISTS = ("ukpga", "asp", "nia", "anaw", "ukla", "ukcm", "apni", "mwa", "mnia")
_SHORT_LISTS = ("ukpga_short",)

_URI_RE = re.compile(r"legislation\.gov\.uk/(?:id/)?(?P<path>[a-z]{2,6}/[A-Za-z0-9]+/[A-Za-z0-9/-]+)", re.I)


def _stable_id(uri: str) -> str | None:
    """A gazetteer ``context`` URI → the stable_id (type/year/number or regnal form)."""
    m = _URI_RE.search(uri or "")
    if not m:
        return None
    parts = m.group("path").split("/")
    n = 4 if (len(parts) >= 4 and not parts[1].isdigit()) else 3
    keep = parts[:n]
    return "/".join(keep) if all(keep) else None


def normalise_title(text: str) -> str:
    """Normalise a short title for matching: lower-case, ``&``→``and``, drop a leading
    "the", strip punctuation/brackets, collapse whitespace. Mirrors the pipeline's
    title-normalisation so a cited "AIDS (Control) Act" matches the listed form."""
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    if t.startswith("the "):
        t = t[4:]
    return re.sub(r"\s+", " ", t)


@lru_cache(maxsize=1)
def _index() -> dict[tuple[str, str | None], str]:
    """Build ``{(normalised_title, year|None): stable_id}``.

    Full titles are keyed by ``(title, year)`` — exact, because the *same* short title is
    reused across years ("Data Protection Act" → 1984 / 1998), so a year-less guess would
    be wrong. A year-less ``(title, None)`` key is added ONLY when the title is unambiguous
    (one act ever bore it). Short-form abbreviations are year-less (the year is baked in)."""
    titles: dict[str, dict[str, str]] = {}  # title → {year: stable_id}
    for name in _FULL_LISTS:
        _load_full(_DATA / f"{name}.lst", titles)
    idx: dict[tuple[str, str | None], str] = {}
    for title, years in titles.items():
        for year, sid in years.items():
            idx[(title, year)] = sid
        if len(set(years.values())) == 1:  # only one act ever had this title → safe
            idx[(title, None)] = next(iter(years.values()))
    for name in _SHORT_LISTS:
        _load_short(_DATA / f"{name}.lst", idx)
    return idx


def _load_full(path: Path, titles: dict) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        parts = line.split(";")
        title = normalise_title(parts[0])
        meta = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        sid = _stable_id(meta.get("context", ""))
        year = meta.get("year")
        if title and sid and year:
            titles.setdefault(title, {}).setdefault(year, sid)


def _load_short(path: Path, idx: dict) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        parts = line.split(";")
        meta = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        sid = _stable_id(meta.get("context", ""))
        abbr = normalise_title(parts[0])
        if abbr and sid:
            idx.setdefault((abbr, None), sid)


# A leading provision reference ("section 78 of the …", "Part II of …", "Schedule B1 to
# …") sits in front of the Act title in a cited string; strip it so the title matches.
_PROVISION_PREFIX = re.compile(
    r"^(?:section|sections|s|ss|subsection|sub section|part|schedule|sch|paragraph|"
    r"paragraphs|para|paras|article|articles|art|arts)\s+[0-9a-z()]+\s+(?:of|to)\s+(?:the\s+)?",
)


def reference_key(raw: str) -> str:
    """Normalise a *cited* statute reference to the key its title is indexed by — dropping
    any leading provision phrase ("section 78 of the Police and Criminal Evidence Act 1984"
    → ``police and criminal evidence act 1984``). Used to match a name-only citation
    against the titles of legislation the corpus already holds (which never goes stale, and
    unlike the offline gazetteer covers every Act that's been harvested)."""
    return _PROVISION_PREFIX.sub("", normalise_title(raw)).strip()


def resolve(title: str, year: str | None = None) -> str | None:
    """Resolve a statute short title to its stable_id, or None. With a year, matches
    **exactly** (no wrong-year guess); without one, only resolves an unambiguous title.
    Incomplete by design — a miss just means "not in the offline gazetteer", not "no such
    act" (recent acts especially may be absent; fall back to a live title lookup)."""
    idx = _index()
    norm = normalise_title(title)
    if not norm:
        return None
    if year:
        return idx.get((norm, str(year)))  # exact only — never a different year
    return idx.get((norm, None))
