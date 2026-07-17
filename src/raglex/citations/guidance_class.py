"""Guidance classification (§1.9/§4a) — sort regulatory guidance into its buckets
deterministically, and SHOW YOUR WORKING.

A guidance document is filed when five fields are set: issuer (EDPB / A29WP /
Ofcom / ICO…), identity (the citable series number — "Guidelines 05/2020",
"WP248 rev.01" — plus the alias forms citers use), version, status (draft-for-
consultation vs adopted), and regime (what it is guidance *under* — resolved
separately from the document's own dominant legislation citation, which needs the
catalogue). Every field this module sets carries provenance: WHICH rule fired and
WHAT text it matched — so the classification is inspectable, contestable, and a
user can see exactly why a rule mis-fired before editing it.

Rules are data, not code: the built-in defaults below are merged with the
user-edited overlay (``data_dir/guidance_rules.json``, managed via the facade),
so growing coverage — a new DPA, a new numbering style — is a rules edit in the
UI, then a re-classify pass. No LLMs anywhere; every decision is a regex or a
table lookup a human can read.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# ── issuer rules (data — the defaults the user overlay extends/overrides) ────
# Two independent signals per issuer: the source URL's domain and first-page
# boilerplate. Agreement ⇒ near-certainty; either alone still classifies but the
# evidence says which; disagreement is surfaced, never silently resolved.
DEFAULT_ISSUERS: list[dict] = [
    {"code": "edpb", "label": "European Data Protection Board",
     "domains": ["edpb.europa.eu"],
     "boilerplate": ["european data protection board"],
     "default_regime": "32016R0679"},
    {"code": "a29wp", "label": "Article 29 Working Party",
     "domains": ["ec.europa.eu/newsroom/article29", "ec.europa.eu/justice/article-29"],
     "boilerplate": ["article 29 data protection working party"],
     "default_regime": "31995L0046"},
    {"code": "ofcom", "label": "Ofcom",
     "domains": ["ofcom.org.uk"],
     "boilerplate": ["ofcom"],
     "default_regime": "ukpga/2023/50"},
    {"code": "ico", "label": "Information Commissioner's Office",
     "domains": ["ico.org.uk"],
     "boilerplate": ["information commissioner's office"],
     "default_regime": None},
    {"code": "edps", "label": "European Data Protection Supervisor",
     "domains": ["edps.europa.eu"],
     "boilerplate": ["european data protection supervisor"],
     "default_regime": None},
]

# ── identity grammars (code — the numbering styles are strictly regular) ─────
# EDPB instruments: "Guidelines 05/2020", "Recommendations 01/2020", "Opinion
# 5/2019", "Statement 04/2021", "Binding Decision 01/2021" — kind + number/year,
# number conventionally zero-padded to two digits. Bare "Decision N/YYYY" is NOT
# matched (that shape belongs to EU decisions), only the Board's "Binding Decision".
_EDPB_SERIES = re.compile(
    r"\b(?P<kind>Guidelines|Recommendations?|Opinion|Statement|Binding\s+Decision)\s+"
    r"(?P<num>\d{1,3})/(?P<year>(?:19|20)\d{2})",
    re.IGNORECASE)
# A29WP working papers: "WP248", "WP 248 rev.01", "WP29" is the BODY not a paper —
# require ≥ 2 digits or a rev suffix so the body's own name never matches.
_WP_SERIES = re.compile(
    r"\bWP\s?(?P<num>\d{2,3})(?:\s?rev\.?\s?0?(?P<rev>\d+))?\b", re.IGNORECASE)
_VERSION = re.compile(r"\bVersion\s+(?P<v>\d+(?:\.\d+)?)\b", re.IGNORECASE)
_ADOPTED = re.compile(
    r"\bAdopted(?:\s+on)?\s+(?P<d>\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2})?", re.IGNORECASE)
_CONSULTATION = re.compile(r"\b(?:version\s+)?for\s+public\s+consultation\b", re.IGNORECASE)

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _parse_long_date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s.strip())
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def _field(value, rule: str, evidence: str, method: str = "rule") -> dict:
    """One classified field WITH its working: the rule that fired and the text it
    matched. This provenance is what the rules UI renders, and what protects a
    manual correction (method='manual') from being overwritten by a re-classify."""
    return {"value": value, "method": method, "rule": rule, "evidence": evidence[:200]}


def merge_rules(user: dict | None) -> dict:
    """Built-in defaults ⊕ the user overlay. Issuers merge by ``code`` (a user row
    replaces the default with the same code, extra rows append); ``collections``
    (Zotero collection key → intake mapping) come from the overlay alone."""
    issuers = {i["code"]: dict(i) for i in DEFAULT_ISSUERS}
    for row in (user or {}).get("issuers", []):
        if row.get("code"):
            issuers[row["code"]] = {**issuers.get(row["code"], {}), **row}
    return {"issuers": list(issuers.values()),
            "collections": dict((user or {}).get("collections", {}))}


def classify_guidance(*, title: str | None = None, text: str | None = None,
                      url: str | None = None, rules: dict | None = None) -> dict:
    """Classify one guidance document from its title, first-page text and source
    URL. Returns ``{field: {value, method, rule, evidence}}`` plus ``aliases``
    (the citation forms to mint) — pure and catalogue-free; the regime's
    dominant-citation signal is added by the facade, which can count citations."""
    rules = rules or merge_rules(None)
    head = f"{title or ''}\n{(text or '')[:3000]}"
    out: dict[str, Any] = {}

    # -- issuer: domain and boilerplate are independent witnesses ------------
    dom_hit = bp_hit = None
    for issuer in rules["issuers"]:
        for d in issuer.get("domains") or []:
            if d and d.lower() in (url or "").lower():
                dom_hit = (issuer, d)
                break
        for b in issuer.get("boilerplate") or []:
            if b and b.lower() in head.lower():
                bp_hit = (issuer, b)
                break
    if dom_hit and bp_hit and dom_hit[0]["code"] != bp_hit[0]["code"]:
        # the two signals disagree — classify by the domain but SAY SO
        out["issuer"] = _field(dom_hit[0]["code"], f"domain:{dom_hit[1]}",
                               f"url={url} BUT boilerplate says {bp_hit[0]['code']} "
                               f"({bp_hit[1]!r}) — check me")
    elif dom_hit or bp_hit:
        issuer, matched = dom_hit or bp_hit
        rule = f"domain:{matched}" if dom_hit else f"boilerplate:{matched}"
        both = " (+boilerplate agrees)" if dom_hit and bp_hit else ""
        out["issuer"] = _field(issuer["code"], rule + both,
                               (url if dom_hit else matched) or "")
        if issuer.get("default_regime"):
            out["regime_default"] = _field(issuer["default_regime"],
                                           f"issuer-default:{issuer['code']}",
                                           "the issuer's usual instrument; the document's "
                                           "own dominant citation overrides this")

    # -- identity: the citable series number + the alias forms citers use ----
    aliases: list[str] = []
    m = _EDPB_SERIES.search(head)
    if m:
        kind = m.group("kind").capitalize()
        if kind.lower().startswith("recommendation"):
            kind = "Recommendations"
        elif kind.lower().startswith("binding"):
            kind = "Binding Decision"
        num, year = m.group("num"), m.group("year")
        canonical = f"{kind} {int(num):02d}/{year}"
        out["number"] = _field(canonical, "series:edpb", m.group(0))
        aliases += [f"{kind} {int(num):02d}/{year}", f"{kind} {int(num)}/{year}"]
    wp = _WP_SERIES.search(head)
    # "WP29" (no rev) is the BODY — the Article 29 Working Party's own shorthand,
    # 29 being the article number — not working paper №29. A real paper reference
    # carries a rev suffix or a number that isn't the body's. Skip the bare body name.
    if wp and not (wp.group("num") == "29" and not wp.group("rev")):
        num, rev = wp.group("num"), wp.group("rev")
        canonical = f"WP{num}" + (f" rev.{int(rev):02d}" if rev else "")
        # the WP number is the identity for A29WP papers; secondary otherwise
        key = "number" if "number" not in out else "wp_number"
        out[key] = _field(canonical, "series:a29wp", wp.group(0))
        aliases += [f"wp{num}", f"wp {num}"] + ([f"wp{num} rev.{int(rev):02d}"] if rev else [])

    # -- version + status -----------------------------------------------------
    v = _VERSION.search(head)
    if v:
        out["version"] = _field(v.group("v"), "version-phrase", v.group(0))
    if _CONSULTATION.search(head):
        out["status"] = _field("consultation", "consultation-phrase",
                               _CONSULTATION.search(head).group(0))
    else:
        a = _ADOPTED.search(head)
        if a:
            out["status"] = _field("adopted", "adopted-phrase", a.group(0))
            iso = _parse_long_date(a.group("d"))
            if iso:
                out["adopted_date"] = _field(iso, "adopted-phrase", a.group(0))

    out["aliases"] = sorted({a.lower() for a in aliases})
    return out


# legislation-shaped citation kinds that can be a guidance document's regime
REGIME_KINDS = frozenset({"act", "regulation", "directive", "decision", "treaty",
                          "eu_instrument", "named"})


def dominant_regime(citation_rows) -> dict | None:
    """The instrument a guidance document is guidance UNDER, from its own
    citations: the most-cited legislation candidate, accepted only when it
    dominates (≥3× the runner-up, or unrivalled) — ambiguity returns None and
    the issuer default / a human decides."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for c in citation_rows:
        if c["candidate_id"] and c["entity_kind"] in REGIME_KINDS:
            counts[c["candidate_id"]] += 1
    if not counts:
        return None
    top = counts.most_common(2)
    (winner, n) = top[0]
    runner = top[1][1] if len(top) > 1 else 0
    if runner and n < 3 * runner:
        return None  # contested — don't guess
    return _field(winner, "dominant-citation",
                  f"cited {n}× (runner-up {runner}×) in the document body")
