"""Construct canonical Legal Information Institute URLs from a neutral-citation slug.

The AustLII-family LIIs (AustLII, NZLII, SAFLII, HKLII, PacLII, BAILII, CyLaw, ULII,
LII of India, and the WorldLII/CommonLII/AsianLII portals) all run AustLII's *Sino*
engine and share one URL grammar::

    https://<host>/<jurisdiction>/cases/<COURT>/<year>/<number>.html

which is exactly the shape of a neutral citation — ``[year] COURT number`` — with a
jurisdiction/court folder prefix in front. CanLII is the odd one out (Lexum, not Sino) and
gets its own builder.

**Why this is derivable here and not in general.** The usual blocker is that a bare
citation doesn't encode the extra path segments some sites insert — BAILII's court
*division* (``EWCA/Civ``, ``EWHC/Admin``) and AustLII's inner jurisdiction
(``au/cases/cth/…``). RAGLex's stable_ids already carry the division (``ewca/civ/2005/324``),
and the inner jurisdiction is recoverable from the court code itself (``NSWSC`` → ``nsw``),
so the whole path can be built locally without touching anyone's resolver.

**What is deliberately *not* attempted.** A link is only emitted when every segment is
derived, and each carries a :attr:`LIILink.certainty`:

* ``derived`` — every segment came from the slug or a lookup table; the URL is as good as
  the site's own naming scheme;
* ``probable`` — the court is recognised but the identifier is LII-assigned rather than
  court-issued (much of PacLII, pre-neutral-citation material), so the file may sit under a
  different number.

Report-series citations (``[2008] 2 NZLR 321``) carry no court/number at all and are never
guessed at — they have no derivable URL and are reported as such, which is the honest
answer rather than a link that 404s.

Everything here is pure string work: no network calls, no resolver hits. That matters —
several of these institutes run on small academic/charity infrastructure, and constructing
canonical URLs locally is precisely what their stable naming schemes are designed for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .courts import KNOWN_COURTS

# ── site registry ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LIISite:
    key: str
    name: str
    host: str


SITES: dict[str, LIISite] = {
    "bailii": LIISite("bailii", "BAILII", "www.bailii.org"),
    "austlii": LIISite("austlii", "AustLII", "www.austlii.edu.au"),
    "nzlii": LIISite("nzlii", "NZLII", "www.nzlii.org"),
    "saflii": LIISite("saflii", "SAFLII", "www.saflii.org"),
    "hklii": LIISite("hklii", "HKLII", "www.hklii.hk"),
    "paclii": LIISite("paclii", "PacLII", "www.paclii.org"),
    "canlii": LIISite("canlii", "CanLII", "www.canlii.org"),
    "commonlii": LIISite("commonlii", "CommonLII", "www.commonlii.org"),
}


@dataclass(frozen=True, slots=True)
class LIILink:
    site: str          # site key, e.g. "bailii"
    site_name: str     # display name, e.g. "BAILII"
    url: str
    certainty: str     # "derived" | "probable"


# ── BAILII: jurisdiction segment per court token ────────────────────────────
# BAILII files by constitutional jurisdiction, not by the court's own code.
_BAILII_JURIS: dict[str, str] = {
    # England & Wales
    **{c: "ew" for c in ("ewca", "ewhc", "ewcop", "ewfc", "ewcc", "ewmc", "ewlands",
                         "ewcst", "ewpcc", "pbra", "misc")},
    # UK-wide courts and tribunals
    **{c: "uk" for c in ("uksc", "ukhl", "ukpc", "ukut", "ukftt", "ukeat", "uket",
                         "ukait", "ukiat", "ukaitur", "ukvat", "ukspc", "ukfsm", "ukit",
                         "siac", "cat", "drs", "ukttl")},
    # Scotland / Northern Ireland / Ireland
    **{c: "scot" for c in ("scotcs", "scothc", "scotsac", "scotsc", "csoh", "csih",
                           "hcjac", "hcj", "sac")},
    **{c: "nie" for c in ("nica", "nihc", "niqb", "nikb", "nich", "nifam", "nimag",
                          "nicc", "niit", "nifet", "nisscsc")},
    **{c: "ie" for c in ("iesc", "iescdet", "ieca", "iehc", "iecca", "iecc", "iedc",
                         "ieic", "iecompa", "iedpc")},
    # Strasbourg / Luxembourg, as BAILII files them
    **{c: "eu" for c in ("echr", "euecj", "euboa")},
    # Crown Dependencies and the offshore courts BAILII republishes
    **{c: "je" for c in ("ur", "jlr", "jrc", "jca")},
    **{c: "gg" for c in ("glr", "grc")},
    "gcci": "ky", "difc": "ae", "adgmcfi": "ae", "adgmca": "ae",
    "qic": "qa", "shsc": "sh", "shca": "sh", "biot": "io", "sicc": "sg",
}

# BAILII uppercases the court folder but keeps a mixed-case form for a few legacy codes.
_BAILII_COURT_CASE: dict[str, str] = {
    "scotcs": "ScotCS", "scothc": "ScotHC", "scotsac": "ScotSAC", "scotsc": "ScotSC",
    "ewlands": "EWLands", "misc": "Misc", "nihc": "NIHC",
}
# Division segments keep BAILII's own capitalisation ("EWCA/Civ", "EWHC/Admin").
_BAILII_DIVISION: dict[str, str] = {
    "civ": "Civ", "crim": "Crim", "admin": "Admin", "ch": "Ch", "qb": "QB", "kb": "KB",
    "fam": "Fam", "comm": "Comm", "tcc": "TCC", "pat": "Patents", "patents": "Patents",
    "ipec": "IPEC", "costs": "Costs", "admlty": "Admlty", "mercantile": "Mercantile",
    "exch": "Exch", "scco": "SCCO", "iac": "IAC", "aac": "AAC", "lc": "LC", "tc": "TC",
    "grc": "GRC", "hesc": "HESC", "pc": "PC", "oj": "OJ", "hcj": "HCJ", "fpc": "FPC",
    "excise": "Excise", "landfill": "Landfill", "fsd": "FSD",
}

# ── Australia: the inner jurisdiction segment AustLII inserts ───────────────
_AU_INNER_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("nsw", "nsw"), ("vic", "vic"), ("qld", "qld"), ("wa", "wa"), ("sa", "sa"),
    ("tas", "tas"), ("act", "act"), ("nt", "nt"),
)
# Court codes whose state isn't a plain prefix of the code.
_AU_INNER_BY_COURT: dict[str, str] = {
    "hca": "cth", "fca": "cth", "fcafc": "cth", "famca": "cth", "famcafc": "cth",
    "fcca": "cth", "fmca": "cth", "fmcafam": "cth", "aata": "cth", "arta": "cth",
    "fedcfamc1a": "cth", "fedcfamc2f": "cth",
    "nswca": "nsw", "nswcca": "nsw", "nswsc": "nsw", "nswdc": "nsw", "nswlec": "nsw",
    "nswirconm": "nsw", "nswcatad": "nsw",
    "vsca": "vic", "vsc": "vic", "vcc": "vic", "vcat": "vic",
    "qca": "qld", "qsc": "qld", "qdc": "qld", "qcat": "qld",
    "wasca": "wa", "wasc": "wa", "wadc": "wa",
    "sasca": "sa", "sascfc": "sa", "sasc": "sa",
    "tascca": "tas", "tasfc": "tas", "tassc": "tas",
    "actca": "act", "actsc": "act", "actmc": "act",
    "ntca": "nt", "ntcca": "nt", "ntsc": "nt",
}

# ── Canada: CanLII's province segment ───────────────────────────────────────
_CA_PROVINCE_BY_COURT: dict[str, str] = {
    "scc": "ca", "fca": "ca", "fc": "ca", "tcc": "ca", "cmac": "ca", "rad": "ca",
    "onca": "on", "onsc": "on", "onscdc": "on", "oncj": "on",
    "bcca": "bc", "bcsc": "bc", "bcpc": "bc",
    "abca": "ab", "abqb": "ab", "abkb": "ab", "abpc": "ab",
    "qcca": "qc", "qccs": "qc", "qccq": "qc",
    "mbca": "mb", "mbqb": "mb", "mbkb": "mb",
    "skca": "sk", "skqb": "sk", "skkb": "sk",
    "nsca": "ns", "nssc": "ns", "nspc": "ns",
    "nbca": "nb", "nbqb": "nb", "nbkb": "nb",
    "nlca": "nl", "nlsc": "nl",
    "peca": "pe", "pesc": "pe",
    "ykca": "yk", "yksc": "yk",
    "nwtca": "nt", "nwtsc": "nt",
    "nuca": "nu", "nucj": "nu",
}

# ── PacLII: country segment from the court-code prefix ──────────────────────
_PACLII_CC: dict[str, str] = {
    "FJ": "fj", "PG": "pg", "SB": "sb", "VU": "vu", "WS": "ws", "TO": "to",
    "NR": "nr", "CK": "ck", "KI": "ki", "TV": "tv",
}

# jurisdiction (from the court registry) → the site that hosts it
_JURIS_SITE: dict[str, str] = {
    "GB": "bailii", "IE": "bailii", "JE": "bailii", "GG": "bailii", "IM": "bailii",
    "KY": "bailii", "AE": "bailii", "QA": "bailii", "SH": "bailii", "IO": "bailii",
    "AU": "austlii", "NZ": "nzlii", "ZA": "saflii", "HK": "hklii", "CA": "canlii",
    "FJ": "paclii", "PG": "paclii", "SB": "paclii", "VU": "paclii", "WS": "paclii",
    "TO": "paclii", "NR": "paclii", "CK": "paclii", "KI": "paclii", "TV": "paclii",
    # Singapore's SICC is republished on BAILII; the rest sits on CommonLII.
    "SG": "commonlii", "MY": "commonlii", "IN": "commonlii",
}

_SLUG_RE = re.compile(r"^([a-z0-9()]+)((?:/[a-z0-9()_-]+)*)/(\d{4})/([0-9a-z_.-]+)$", re.I)


def _split_slug(slug: str) -> tuple[str, list[str], str, str] | None:
    """``ewhc/admin/2025/1466`` → ``("ewhc", ["admin"], "2025", "1466")``."""
    m = _SLUG_RE.match((slug or "").strip().lower())
    if not m:
        return None
    court, divs, year, num = m.group(1), m.group(2), m.group(3), m.group(4)
    divisions = [d for d in divs.split("/") if d]
    return court, divisions, year, num


def _bailii_url(court: str, divisions: list[str], year: str, num: str) -> LIILink | None:
    juris = _BAILII_JURIS.get(court)
    if not juris:
        return None
    folder = _BAILII_COURT_CASE.get(court, court.upper())
    parts = [folder] + [_BAILII_DIVISION.get(d, d.upper()) for d in divisions]
    url = f"https://{SITES['bailii'].host}/{juris}/cases/{'/'.join(parts)}/{year}/{num}.html"
    return LIILink("bailii", "BAILII", url, "derived")


def _austlii_url(court: str, year: str, num: str) -> LIILink | None:
    inner = _AU_INNER_BY_COURT.get(court)
    if inner is None:
        for prefix, seg in _AU_INNER_BY_PREFIX:
            if court.startswith(prefix):
                inner = seg
                break
    if inner is None:
        return None
    url = (f"https://{SITES['austlii'].host}/cgi-bin/viewdoc/au/cases/"
           f"{inner}/{court.upper()}/{year}/{num}.html")
    return LIILink("austlii", "AustLII", url, "derived")


def _canlii_url(court: str, year: str, num: str) -> LIILink | None:
    """CanLII (Lexum, not Sino) keys a decision by its neutral citation squashed into one
    token: ``2011 SCC 10`` → ``/en/ca/scc/doc/2011/2011scc10/2011scc10.html``."""
    prov = _CA_PROVINCE_BY_COURT.get(court)
    if not prov or not num.isdigit():
        return None
    token = f"{year}{court}{num}"
    url = (f"https://{SITES['canlii'].host}/en/{prov}/{court}/doc/"
           f"{year}/{token}/{token}.html")
    return LIILink("canlii", "CanLII", url, "derived")


def _sino_url(site_key: str, juris: str, court: str, year: str, num: str,
              *, certainty: str = "derived") -> LIILink:
    site = SITES[site_key]
    url = f"https://{site.host}/{juris}/cases/{court.upper()}/{year}/{num}.html"
    return LIILink(site_key, site.name, url, certainty)


def lii_links(slug: str | None, *, court: str | None = None) -> list[LIILink]:
    """Every canonical LII URL derivable for a neutral-citation slug.

    >>> [l.url for l in lii_links("ewca/civ/2005/324")]
    ['https://www.bailii.org/ew/cases/EWCA/Civ/2005/324.html']
    >>> [l.url for l in lii_links("nzhc/2012/2551")]
    ['https://www.nzlii.org/nz/cases/NZHC/2012/2551.html']
    >>> [l.url for l in lii_links("zasca/2011/73")]
    ['https://www.saflii.org/za/cases/ZASCA/2011/73.html']
    >>> [l.url for l in lii_links("scc/2011/10")]
    ['https://www.canlii.org/en/ca/scc/doc/2011/2011scc10/2011scc10.html']

    Returns ``[]`` when nothing is derivable — an unrecognised court, or an identifier
    that isn't a neutral citation at all (a report series, an opaque database id)."""
    parts = _split_slug(slug or "")
    if parts is None:
        return None if False else []
    code, divisions, year, num = parts
    court_code = (court or code).lower()

    # BAILII first: it is the only site whose paths carry the division our slug records.
    links: list[LIILink] = []
    if court_code in _BAILII_JURIS:
        link = _bailii_url(court_code, divisions, year, num)
        if link:
            links.append(link)
        return links

    known = KNOWN_COURTS.get(court_code.upper())
    juris = known.jurisdiction if known else None
    site_key = _JURIS_SITE.get(juris or "")
    if site_key is None:
        return links

    if site_key == "austlii":
        link = _austlii_url(court_code, year, num)
        return [link] if link else []
    if site_key == "canlii":
        link = _canlii_url(court_code, year, num)
        return [link] if link else []
    if site_key == "nzlii":
        return [_sino_url("nzlii", "nz", court_code, year, num)]
    if site_key == "saflii":
        return [_sino_url("saflii", "za", court_code, year, num)]
    if site_key == "hklii":
        return [_sino_url("hklii", "hk", court_code, year, num)]
    if site_key == "paclii":
        cc = _PACLII_CC.get((juris or "").upper())
        if not cc:
            return []
        # PacLII assigns many of its own database numbers, so a constructed path is a
        # good guess rather than a guarantee.
        return [_sino_url("paclii", cc, court_code, year, num, certainty="probable")]
    if site_key == "commonlii":
        cc = (juris or "").lower()
        return [_sino_url("commonlii", cc, court_code, year, num, certainty="probable")]
    return links
