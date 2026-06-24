"""Neutral-citation court registry — the substrate for the snowball (§5, §5a).

A neutral citation has a recognisable *shape* (``[YEAR] COURT NUMBER`` in most
common-law systems, ``YEAR COURT NUMBER`` in Canada/India) but the COURT token is
an open set. We detect the shape generically, then look the token up here:

- a **known** court tells us the jurisdiction (and, eventually, which adapter can
  fetch it) — the citation resolves or queues against the right source;
- an **unknown** court is exactly the snowball signal: the corpus is citing a body
  we don't harvest yet. Those surface in ``snowball`` ranked by frequency, so a
  human (or an agent) sees "47 pending citations to 'EWHC' — no adapter" and knows
  what to build next.

Growing coverage is a data edit here, not a code change. Each entry says the
jurisdiction and whether an adapter exists today (``adapter``), so the worklist
can separate "harvestable now" from "needs a new adapter".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Court:
    code: str
    name: str
    jurisdiction: str  # ISO-ish: GB, IE, CA, AU, NZ, IN, EU …
    adapter: str | None = None  # the source adapter that can fetch it, if any
    bracketed: bool = True  # [2024] CODE n  vs  2024 CODE n (CA/IN)


# Seed set — common-law neutral-citation courts. Extend freely; the detector
# already finds *unknown* codes, this just classifies the ones we recognise.
KNOWN_COURTS: dict[str, Court] = {
    c.code: c
    for c in (
        # United Kingdom (Find Case Law covers these)
        Court("UKSC", "UK Supreme Court", "GB", adapter="uk-caselaw"),
        Court("UKPC", "Judicial Committee of the Privy Council", "GB", adapter="uk-caselaw"),
        Court("UKHL", "House of Lords", "GB", adapter="uk-caselaw"),
        Court("EWCA", "Court of Appeal (England & Wales)", "GB", adapter="uk-caselaw"),
        Court("EWHC", "High Court (England & Wales)", "GB", adapter="uk-caselaw"),
        Court("EWCOP", "Court of Protection", "GB", adapter="uk-caselaw"),
        Court("EWFC", "Family Court", "GB", adapter="uk-caselaw"),
        Court("UKUT", "Upper Tribunal", "GB", adapter="uk-caselaw"),
        Court("UKFTT", "First-tier Tribunal", "GB", adapter="uk-caselaw"),
        Court("UKAITUR", "Immigration & Asylum Tribunal", "GB", adapter="uk-caselaw"),
        # Ireland
        Court("IESC", "Supreme Court of Ireland", "IE"),
        Court("IECA", "Court of Appeal of Ireland", "IE"),
        Court("IEHC", "High Court of Ireland", "IE"),
        # Canada (bracketless: 2024 SCC 1)
        Court("SCC", "Supreme Court of Canada", "CA", bracketed=False),
        Court("FCA", "Federal Court of Appeal (Canada)", "CA", bracketed=False),
        Court("FC", "Federal Court (Canada)", "CA", bracketed=False),
        Court("ONCA", "Court of Appeal for Ontario", "CA", bracketed=False),
        # Australia
        Court("HCA", "High Court of Australia", "AU"),
        Court("FCA", "Federal Court of Australia", "AU"),  # note: AU FCA (collides w/ CA — jurisdiction by context)
        Court("FCAFC", "Full Federal Court of Australia", "AU"),
        # New Zealand
        Court("NZSC", "Supreme Court of New Zealand", "NZ"),
        Court("NZCA", "Court of Appeal of New Zealand", "NZ"),
        Court("NZHC", "High Court of New Zealand", "NZ"),
        # India (bracketless: 2024 INSC 1)
        Court("INSC", "Supreme Court of India", "IN", bracketed=False),
    )
}

# Court tokens that are really *divisions*, valid only after a parent court code
# (so "[2024] EWCA Civ 1" is one citation, not court "Civ").
DIVISIONS = {
    "Civ", "Crim", "Admin", "Fam", "Ch", "QB", "KB", "Pat", "TCC", "Comm",
    "Admlty", "Mercantile", "IPEC", "SCCO", "Costs",
}


def lookup(code: str) -> Court | None:
    return KNOWN_COURTS.get(code.upper())
