"""Generate the forms a case might be *cited by*, from its BAILII index title.

A judgment imported as "Baxter, R (on the application of) v Lincolnshire County Council"
gets cited a dozen ways: "R (Baxter) v Lincolnshire CC", "R (on the application of Baxter)
v Lincolnshire County Council", "R v Lincolnshire County Council", just "Baxter". To make
those citations resolve to the one imported document we mint each distinctive form as a
``citation_aliases`` alias, and — for the reporter matcher — we normalise the standard
law-report abbreviations (A-G ⇄ Attorney General, Ltd ⇄ Limited, CC ⇄ County Council…) so
a name and a title that differ only in abbreviation share their distinctive tokens.

Two consumers, one abbreviation table:
  * :func:`name_variants` → readable ``(variant, kind)`` pairs the importer folds into
    aliases (skipping the too-ambiguous ``single-party`` kind);
  * :func:`normalise_abbrev` → collapses every long/short form to one canonical token,
    used by ``report_match.surnames`` so both sides of a comparison tokenise the same.
"""

from __future__ import annotations

import re

# (long form, short form). Order is longest-long-form first so "United States of America"
# is replaced before "United States". Canonical token for each pair is derived from the
# long form (lower-cased, non-alphanumerics dropped) — see ``_CANON``.
ABBREV: tuple[tuple[str, str], ...] = (
    ("Attorney General", "A-G"),
    ("Area Health Authority", "AHA"),
    ("British Broadcasting Corporation", "BBC"),
    ("Crown Prosecution Service", "CPS"),
    ("Director of Public Prosecutions", "DPP"),
    ("Her Majesty's Revenue and Customs", "HMRC"),
    ("His Majesty's Revenue and Customs", "HMRC"),
    ("Her Majesty's Revenue Commissioners", "HMRC"),
    ("London Borough Council", "LBC"),
    ("Rural District Council", "RDC"),
    ("Urban District Council", "UDC"),
    ("United States of America", "USA"),
    ("United States", "US"),
    ("United Kingdom", "UK"),
    ("Great Britain", "GB"),
    ("New Zealand", "NZ"),
    ("South Africa", "SA"),
    ("European Communities", "EC"),
    ("Health Authority", "HA"),
    ("Borough Council", "BC"),
    ("County Council", "CC"),
    ("District Council", "DC"),
    ("Vice-Chancellor", "V-C"),
    ("public limited company", "plc"),
    ("Co-operative", "Co-op"),
    ("Corporation", "Corp"),
    ("Commissioners", "Comrs"),
    ("Commissioner", "Comr"),
    ("Incorporated", "Inc"),
    ("Proprietary", "Pty"),
    ("Executrix", "Exrx"),
    ("Executor", "Exor"),
    ("deceased", "decd"),
    ("Department", "Dept"),
    ("liquidation", "liq"),
    ("Brothers", "Bros"),
    ("Anonymous", "Anon"),
    ("Railway", "Rly"),
    ("Company", "Co"),
    ("Limited", "Ltd"),
    ("another", "Anor"),
    ("others", "Ors"),
)


def _canon(long: str) -> str:
    return re.sub(r"[^a-z0-9]", "", long.lower())


# form (long or short) → canonical token, longest-first so multi-word forms win.
_FORMS: list[tuple[str, str]] = sorted(
    ((form, _canon(long)) for long, short in ABBREV for form in (long, short)),
    key=lambda t: len(t[0]), reverse=True,
)
_NORM_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f, _ in _FORMS) + r")\b",
    re.IGNORECASE,
)
_CANON_OF = {f.lower(): c for f, c in _FORMS}


def normalise_abbrev(text: str) -> str:
    """Replace every abbreviation form — long or short — with its single canonical token,
    so "Attorney General" and "A-G" both become "attorneygeneral". Used before tokenising
    a case name/title for comparison (the two forms then share the token)."""
    return _NORM_RE.sub(lambda m: _CANON_OF[m.group(1).lower()], text or "")


# ── readable variants (aliases) ──────────────────────────────────────────────

_ROAO = re.compile(r"^(?P<who>.+?),\s*R\s*\(on the application of\)\s*(?P<rest>v\b.*)$", re.I)
_RE_SUFFIX = re.compile(r"^(?P<who>.+?),\s*Re$", re.I)
_NUMBERED = re.compile(r"\(\d+\)\s*")
_TAIL = re.compile(r"\s*(?:&|and)\s+(?:Ors?|Anor|another|others)\.?\s*$", re.I)
_ROLE_WORDS = frozenset({
    "v", "and", "another", "others", "or", "the", "of", "in", "re", "ex", "parte",
    "appellant", "appellants", "respondent", "respondents", "fc", "no", "ors", "anor",
})


def _expand(text: str) -> str:
    """All short forms → long forms."""
    out = text
    for long, short in ABBREV:
        out = re.sub(rf"\b{re.escape(short)}\b", long, out)
    return out


def _contract(text: str) -> str:
    """All long forms → short forms (longest-first, already the ABBREV order)."""
    out = text
    for long, short in ABBREV:
        out = re.sub(rf"\b{re.escape(long)}\b", short, out, flags=re.IGNORECASE)
    return out


def _clean(s: str) -> str:
    return re.sub(r"\s{2,}", " ", s).strip(" ,;.-")


def name_variants(title: str) -> list[tuple[str, str]]:
    """Distinct ``(variant, kind)`` forms a case titled ``title`` might be cited by.

    Kinds: ``exact`` (normalised original), ``abbrev`` (expanded / contracted),
    ``role-form`` (R (on the application of …) reorderings, ``Re`` forms, numbered-party
    stripping), ``drop-tail`` (without "& Ors"/"& Anor"), and ``single-party`` (one party
    alone — distinctive but ambiguous, so the importer does *not* alias these blindly).
    """
    title = _clean(title or "")
    if not title:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(v: str, kind: str) -> None:
        v = _clean(v)
        key = v.lower()
        if v and len(v) > 3 and key not in seen:
            seen.add(key)
            out.append((v, kind))

    add(title, "exact")

    # -- R (on the application of) reorderings --
    base_forms = [title]
    m = _ROAO.match(title)
    if m:
        who, rest = m.group("who").strip(), m.group("rest").strip()
        long_form = _clean(f"R (on the application of {who}) {rest}")
        short_form = _clean(f"R ({who}) {rest}")
        add(long_form, "role-form")
        add(short_form, "role-form")
        # NB: deliberately *not* the bare "R v <defendant>" — dropping the claimant makes it
        # collide across every R (oao X) case against the same public body.
        base_forms += [long_form, short_form]
    rm = _RE_SUFFIX.match(title)
    if rm:
        who = rm.group("who").strip()
        add(f"Re {who}", "role-form")
        add(f"In re {who}", "role-form")
    if _NUMBERED.search(title):
        add(_NUMBERED.sub("", title), "role-form")

    # -- abbreviation expand / contract (over the role-normalised base forms) --
    for base in base_forms:
        exp, con = _expand(base), _contract(base)
        if exp != base:
            add(exp, "abbrev")
        if con != base:
            add(con, "abbrev")

    # -- drop "& Ors" / "& Anor" tails --
    for base in list(base_forms):
        if _TAIL.search(base):
            add(_TAIL.sub("", base), "drop-tail")

    # -- single distinctive parties (held back from blind aliasing) --
    for side in re.split(r"\bv\.?\b", title, maxsplit=1):
        party = _clean(_NUMBERED.sub("", side))
        toks = [t for t in party.split() if t.lower() not in _ROLE_WORDS]
        if 0 < len(toks) <= 5 and party.lower() != title.lower():
            add(party, "single-party")

    return out
