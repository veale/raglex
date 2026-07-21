"""Corpus taxonomy — map any held document OR any pending citation to a single
``(category, sub-type)`` pair, so the dashboard's Corpus Map can tally held-vs-pending
the same way for both. One source of truth for "what kind of legal thing is this".

The category key is the *adapter* that fetches the thing (``uk-legislation``, ``eu-cellar``,
…), so it lines up with the harvest endpoints (``harvest_all_references(adapter=…)``). Sub-type
splits each category the way a lawyer would: SIs vs Acts vs assimilated (and by UK nation),
CJEU vs General Court vs AG opinions, by UK court/tribunal, ECHR cases vs the Convention.

Reuses the slug/CELEX classification already in :mod:`.snowball` and the court registry in
:mod:`.courts`; adds only the sub-type split on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .courts import KNOWN_COURTS
from .snowball import (
    ASSIMILATED_LEG_TYPES,
    PRIMARY_LEG_TYPES,
    SECONDARY_LEG_TYPES,
    _classify,
    _CELEX_RE,
)

# Category keys == the fetching adapter, so a row's "Harvest" action maps straight to
# harvest_all_references(adapter=…). "other" collects anything unrouted.
CATEGORY_LABELS: dict[str, str] = {
    "uk-caselaw": "UK case-law",
    "uk-legislation": "UK legislation",
    "ie-caselaw": "Irish case-law",
    "ie-legislation": "Irish legislation",
    "eu-cellar": "EU case-law",
    "eu-legislation": "EU legislation",
    "echr": "ECHR",
    "guidance": "Regulatory guidance",
    "ca-caselaw": "Canadian case-law",
    "au-caselaw": "Australian case-law",
    "nz-caselaw": "New Zealand case-law",
    "in-caselaw": "Indian case-law",
    "us-caselaw": "US case-law",
    "fr-caselaw": "French case-law",
    "fr-legislation": "French legislation",
    "de-caselaw": "German case-law",
    "de-legislation": "German legislation",
    "sg-caselaw": "Singapore case-law",
    "hk-caselaw": "Hong Kong case-law",
    "za-caselaw": "South African case-law",
    "my-caselaw": "Malaysian case-law",
    "africa-caselaw": "African case-law (other)",
    "caribbean-caselaw": "Caribbean case-law",
    "pacific-caselaw": "Pacific case-law",
    "ci-caselaw": "Channel Islands case-law",
    "offshore-caselaw": "Offshore & int'l commercial courts",
    "ca-legislation": "Canadian legislation (federal)",
    "au-legislation": "Australian legislation",
    "nz-legislation": "New Zealand legislation",
    "hk-legislation": "Hong Kong legislation",
    "sg-legislation": "Singapore legislation",
    "other": "Other / unrouted",
}
CATEGORY_ORDER = ["uk-caselaw", "uk-legislation", "ie-caselaw", "ie-legislation",
                  "eu-cellar", "eu-legislation", "echr", "fr-caselaw", "fr-legislation",
                  "de-caselaw", "de-legislation", "guidance",
                  "ca-caselaw", "au-caselaw", "nz-caselaw", "in-caselaw", "us-caselaw",
                  "sg-caselaw", "hk-caselaw", "za-caselaw", "my-caselaw",
                  "africa-caselaw", "caribbean-caselaw", "pacific-caselaw",
                  "ci-caselaw", "offshore-caselaw",
                  "ca-legislation", "au-legislation", "nz-legislation", "hk-legislation",
                  "sg-legislation",
                  "other"]

# Neutral-citation jurisdictions with no adapter (cases arrive by upload, if at all):
# the KNOWN_COURTS jurisdiction → the Corpus Map bucket. A "[2020] NZSC 12" pending
# citation should read as New Zealand case-law, not "Other / unrouted" — the corpus
# understanding these places precedes any import route existing.
JURISDICTION_CATEGORY: dict[str, str] = {
    "IE": "ie-caselaw", "CA": "ca-caselaw", "AU": "au-caselaw",
    "NZ": "nz-caselaw", "IN": "in-caselaw", "US": "us-caselaw",
    "FR": "fr-caselaw",
    "DE": "de-caselaw",
    # The wider Commonwealth. The big single jurisdictions get their own row; the long
    # tail is grouped by region, because ~30 one-case rows would bury the map while
    # "African case-law: 214 pending" is a signal worth acting on.
    "SG": "sg-caselaw", "HK": "hk-caselaw", "ZA": "za-caselaw", "MY": "my-caselaw",
    # Crown Dependencies and the offshore/international commercial courts BAILII carries.
    **{j: "ci-caselaw" for j in ("JE", "GG", "IM")},
    **{j: "offshore-caselaw" for j in ("KY", "AE", "QA", "SH", "IO", "BM", "GI")},
    **{j: "africa-caselaw" for j in
       ("KE", "GH", "TZ", "UG", "NG", "ZM", "MW", "ZW", "NA", "SZ", "BW", "MU", "SC",
        "AFRICA", "EAC")},
    **{j: "caribbean-caselaw" for j in ("TT", "JM", "BB", "BS", "GY", "BZ", "CARICOM")},
    **{j: "pacific-caselaw" for j in
       ("FJ", "PG", "SB", "VU", "WS", "TO", "NR", "CK", "KI", "TV")},
}

# The case-law categories that aren't UK — all classified by court token alone.
NON_UK_CASELAW_CATEGORIES: frozenset[str] = frozenset(JURISDICTION_CATEGORY.values())

# Commonwealth legislation sources → their Corpus Map category. Without these the
# registers land in "Other / unrouted", which hides several thousand held documents.
COMMONWEALTH_LEG_CATEGORY: dict[str, str] = {
    "ca-federal": "ca-legislation",
    "hk-legislation": "hk-legislation",
    "nz-legislation": "nz-legislation",
    "au-cth": "au-legislation", "au-qld": "au-legislation",
    "au-nsw": "au-legislation", "au-tas": "au-legislation",
    "sg-legislation": "sg-legislation",
}

# The register-native document types, as they appear in a stable_id's second segment.
COMMONWEALTH_LEG_TYPES: dict[str, str] = {
    "act": "Acts", "regulation": "Regulations",          # Canada
    "cap": "Ordinances & subsidiary legislation",         # Hong Kong (chapter-numbered)
    "instrument": "Constitutional instruments",           # HK Basic Law and companions
    "sl": "Subordinate legislation", "sr": "Statutory rules",  # Australia (also SG SL)
    "si": "Statutory instruments", "ni": "Notifiable instruments",
    "public": "Public Acts", "secondary-legislation": "Secondary legislation",  # NZ
    "bill": "Bills",
}

# Australia's nine registers, keyed by the jurisdiction segment of an au/… stable_id.
AU_JURISDICTIONS: dict[str, str] = {
    "cth": "Commonwealth", "nsw": "New South Wales", "qld": "Queensland",
    "vic": "Victoria", "wa": "Western Australia", "sa": "South Australia",
    "tas": "Tasmania", "act": "ACT", "nt": "Northern Territory",
}

# Which UK nation a legislation type-code belongs to (for the SI/Act-by-country split).
UK_LEG_COUNTRY: dict[str, str] = {
    # UK-wide
    "ukpga": "UK-wide", "uksi": "UK-wide", "ukla": "UK-wide", "ukcm": "UK-wide",
    "ukmo": "UK-wide", "uksro": "UK-wide", "gbla": "UK-wide", "gbppp": "UK-wide",
    "apgb": "UK-wide", "aep": "UK-wide", "aip": "UK-wide",
    # Scotland
    "asp": "Scotland", "ssi": "Scotland", "aosp": "Scotland",
    # Wales
    "anaw": "Wales", "asc": "Wales", "mwa": "Wales", "wsi": "Wales",
    # Northern Ireland
    "nia": "N. Ireland", "apni": "N. Ireland", "nisi": "N. Ireland", "nisr": "N. Ireland",
    "nisro": "N. Ireland", "mnia": "N. Ireland",
}
_LEG_KIND_LABEL = {"primary": "Primary", "secondary": "Secondary", "assimilated": "Assimilated"}
_CELEX_LEG_DESC = {"R": ("reg", "Regulation"), "L": ("dir", "Directive"),
                   "D": ("dec", "Decision")}

# Irish legislation sub-types (irishstatutebook.ie eli shapes: eli/YYYY/act/N,
# eli/YYYY/si/N, plus the Constitution). The category exists ahead of any harvest
# adapter — nothing populates it yet; it's the bucket Irish acts will land in.
IE_LEG_TYPES: dict[str, tuple[str, str]] = {
    "act": ("act", "Act of the Oireachtas"),
    "si": ("si", "Statutory Instrument (IE)"),
    "const": ("const", "Constitution of Ireland"),
}


@dataclass(frozen=True, slots=True)
class Tax:
    """A document/candidate's place in the corpus. ``filter`` is the query that deep-links
    the Corpus browser to exactly this set of *held* items (best-effort per category)."""

    category: str          # adapter key, e.g. "uk-legislation"
    category_label: str
    subtype: str           # stable sub-type key, e.g. "secondary:UK-wide"
    subtype_label: str     # human label, e.g. "Secondary · UK-wide"
    filter: dict           # {source, doc_type?, court?, id_prefix?} for list_documents


def _leg_subtype(slug_prefix: str) -> tuple[str, str]:
    """A UK legislation type-code → (sub_key, sub_label) combining kind + nation."""
    head = slug_prefix.lower()
    if head in PRIMARY_LEG_TYPES:
        kind = "primary"
    elif head in SECONDARY_LEG_TYPES:
        kind = "secondary"
    elif head in ASSIMILATED_LEG_TYPES or head == "european":
        return "assimilated", "Assimilated EU law"
    else:
        return "other", "Other"
    country = UK_LEG_COUNTRY.get(head, "UK-wide")
    return f"{kind}:{country}", f"{_LEG_KIND_LABEL[kind]} · {country}"


def _eu_case_subtype(doc_type: str | None, court: str | None, celex: str | None) -> tuple[str, str]:
    """EU case-law sub-type from the stored court/doc_type, falling back to the CELEX
    descriptor (…CJ…/…TJ…/…CC…/…CO…) when court isn't stored (pending candidates)."""
    c = (court or "").lower()
    if doc_type == "opinion" or "advocate" in c:
        return "ag", "AG Opinion"
    if "general court" in c:
        return "gc", "General Court"
    if "court of justice" in c:
        return "cj", "CJEU judgment"
    # pending candidate: read the CELEX descriptor (6 2018 CJ 0311)
    m = re.match(r"^6\d{4}([A-Z]{2})", (celex or "").upper())
    if m:
        return {"CJ": ("cj", "CJEU judgment"), "TJ": ("gc", "General Court"),
                "CC": ("ag", "AG Opinion"), "CO": ("order", "Order"),
                "CO2": ("order", "Order")}.get(m.group(1), ("cj", "CJEU judgment"))
    return "cj", "CJEU judgment"


def _eu_leg_subtype(celex: str) -> tuple[str, str]:
    m = _CELEX_RE.match(celex or "")
    if m:
        if m.group("sector") == "1":
            return "treaty", "Treaty / primary law"
        key, label = _CELEX_LEG_DESC.get(m.group("desc").upper()[0], ("other", "Other instrument"))
        return key, label
    return "other", "Other instrument"


# HUDOC formation (doctypebranch) → (sub_key, sub_label) for the ECHR held split.
_ECHR_FORMATION = {
    "GRANDCHAMBER": ("gc", "Grand Chamber"),
    "CHAMBER": ("chamber", "Chamber"),
    "COMMITTEE": ("committee", "Committee"),
    "ADMISSIBILITY": ("decision", "Admissibility decision"),
    "DECCOMMISSION": ("commission", "Commission decision"),
    "REPORTS": ("report", "Commission report"),
}


def echr_formation(branch: str | None) -> tuple[str, str]:
    """A HUDOC ``doctypebranch`` value → (sub_key, sub_label). Unknown/blank → catch-all."""
    return _ECHR_FORMATION.get((branch or "").strip().upper(), ("other", "Other / unspecified"))


def classify_document(*, source: str, doc_type: str | None = None, court: str | None = None,
                      stable_id: str = "") -> Tax:
    """Place a HELD document (from its stored columns)."""
    # the held path passes the bare slug-head ("uksi"); the full-id path ("uksi/2016/413")
    # collapses to the same — either way the part before the first '/'.
    prefix = stable_id.split("/", 1)[0].lower()
    if source.startswith("fr-"):
        legislation = doc_type == "legislation"
        category = "fr-legislation" if legislation else "fr-caselaw"
        subtype = "codes" if legislation else (court or source).casefold()
        return Tax(category, CATEGORY_LABELS[category], subtype,
                   "Codes and legislation" if legislation else (court or source),
                   {"source": source, **({"doc_type": doc_type} if doc_type else {})})
    if source.startswith("de-"):
        legislation = doc_type == "legislation"
        category = "de-legislation" if legislation else "de-caselaw"
        subtype = "federal" if legislation else (court or source).casefold()
        return Tax(category, CATEGORY_LABELS[category], subtype,
                   "Federal legislation" if legislation else (court or source),
                   {"source": source, **({"doc_type": doc_type} if doc_type else {})})
    if source == "uk-legislation":
        sub, label = _leg_subtype(prefix)
        return Tax("uk-legislation", CATEGORY_LABELS["uk-legislation"], sub, label,
                   {"source": "uk-legislation", "id_prefix": prefix})
    # The House of Lords scraper (uk-hol) shares the UK case-law taxonomy: its ukhl/YYYY/N
    # (and pre-2001 hol/ surrogate) cases belong under the same "House of Lords" sub-type as
    # the pending "[YYYY] UKHL N" citations, so held + pending line up in one row.
    if source in ("uk-caselaw", "uk-hol"):
        tok = (court or prefix or "").upper()
        known = KNOWN_COURTS.get(tok)
        # opaque Find Case Law identifiers (tna.5mz…, d-uuid) aren't a court token — they'd
        # otherwise each show as their own junk sub-type row; bucket them as uncategorised.
        if not known and (tok.startswith(("TNA.", "D-")) or not tok.replace("/", "").isalpha()
                          or len(tok) > 8):
            return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], "other",
                       "Other / uncategorised", {"source": source})
        # a court sub-type filters by court token (not source), so the list shows the court's
        # cases whether they came from Find Case Law or the House of Lords scrape.
        return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], tok.lower() or "other",
                   known.name if known else (tok or "Other court"),
                   {"court": (court or prefix or "")})
    # Every non-UK case-law category routes the same way: the court token is the
    # sub-type. Derived from CATEGORY_LABELS so registering a new jurisdiction bucket
    # is a one-line data edit there, not a second edit here.
    if source in NON_UK_CASELAW_CATEGORIES:
        tok = (court or prefix or "").upper()
        known = KNOWN_COURTS.get(tok)
        return Tax(source, CATEGORY_LABELS[source], tok.lower() or "other",
                   known.name if known else (tok or "Other court"),
                   {"court": (court or prefix or "")})
    if source == "ie-legislation":
        sub, label = IE_LEG_TYPES.get(prefix, ("other", "Other"))
        return Tax("ie-legislation", CATEGORY_LABELS["ie-legislation"], sub, label,
                   {"source": "ie-legislation", "id_prefix": prefix})
    # Commonwealth legislation registers. Each splits into the register's own document
    # types, taken from the stable_id's second segment (ca/act/… vs ca/regulation/…,
    # hk/cap/… vs hk/instrument/…), so Acts and secondary legislation are separate rows
    # rather than one undifferentiated pile.
    if source in COMMONWEALTH_LEG_CATEGORY:
        category = COMMONWEALTH_LEG_CATEGORY[source]
        parts = stable_id.split("/")
        # Australia is nine registers under one banner (au/{juris}/{type}/…), so the
        # useful split is by jurisdiction — Commonwealth vs Queensland vs NSW — not by
        # instrument type. Everywhere else the second segment IS the type.
        # The Corpus Map's held path supplies only the leading segments of the id
        # ("ca/act"), so the type is parts[1] whenever a second segment exists at all.
        sub = parts[1].lower() if len(parts) > 1 and parts[1] else "other"
        label = (AU_JURISDICTIONS.get(sub, sub.upper()) if category == "au-legislation"
                 else COMMONWEALTH_LEG_TYPES.get(sub, sub.title() or "Other"))
        return Tax(category, CATEGORY_LABELS[category], sub, label,
                   {"source": source, "id_prefix": f"{prefix}/{sub}"})
    if source == "eu-cellar":
        sub, label = _eu_case_subtype(doc_type, court, stable_id)
        filt = {"source": "eu-cellar"}
        if sub == "ag":
            filt["doc_type"] = "opinion"
        elif court:
            filt["court"] = court
        return Tax("eu-cellar", CATEGORY_LABELS["eu-cellar"], sub, label, filt)
    if source == "eu-legislation":
        sub, label = _eu_leg_subtype(stable_id)
        return Tax("eu-legislation", CATEGORY_LABELS["eu-legislation"], sub, label,
                   {"source": "eu-legislation"})
    # Regulatory guidance (§1.9/§4a): the EDPB corpus splits by what the document IS
    # (doc_type), the OSS register by lead DPA (court = dpa-xx — the per-authority
    # split), A29WP by papers vs press/plenary context. Zotero-imported guidance
    # (any other source, doc_type=guidance) joins the same category.
    if source == "edpb":
        sub, label = {
            "guidance": ("edpb-guidance", "EDPB guidelines & recommendations"),
            "opinion": ("edpb-opinion", "EDPB opinions"),
            "decision": ("edpb-decision", "EDPB binding decisions"),
            "commentary": ("edpb-study", "EDPB commissioned studies"),
        }.get(doc_type or "", ("edpb-other", "EDPB other documents"))
        return Tax("guidance", CATEGORY_LABELS["guidance"], sub, label,
                   {"source": "edpb", **({"doc_type": doc_type} if doc_type else {})})
    if source == "edpb-oss":
        cc = (court or "").removeprefix("dpa-").upper() or "??"
        return Tax("guidance", CATEGORY_LABELS["guidance"], f"oss:{cc.lower()}",
                   f"OSS decisions · {cc}",
                   {"source": "edpb-oss", **({"court": court} if court else {})})
    if source == "a29wp":
        if doc_type == "note":
            return Tax("guidance", CATEGORY_LABELS["guidance"], "a29wp-context",
                       "A29WP press & plenary", {"source": "a29wp", "doc_type": "note"})
        return Tax("guidance", CATEGORY_LABELS["guidance"], "a29wp",
                   "A29WP opinions & papers", {"source": "a29wp"})
    if source == "dma-cases":
        return Tax("guidance", CATEGORY_LABELS["guidance"], "dma",
                   "DMA enforcement cases", {"source": "dma-cases"})
    if source == "ofcom-osa":
        return Tax("guidance", CATEGORY_LABELS["guidance"], "ofcom-osa",
                   "Ofcom online-safety documents", {"source": "ofcom-osa"})
    if source == "ofcom-enforcement":
        return Tax("guidance", CATEGORY_LABELS["guidance"], "ofcom-enforcement",
                   "Ofcom enforcement actions", {"source": "ofcom-enforcement"})
    if doc_type == "guidance":
        return Tax("guidance", CATEGORY_LABELS["guidance"], source or "other",
                   f"Guidance ({source})" if source else "Guidance",
                   {"doc_type": "guidance", **({"source": source} if source else {})})
    if source == "echr":
        # "echr" arrives when grouped by slug-prefix (echr/convention → "echr"); a held
        # ECtHR case is an ECLI:CE:… id with no slash, so this only catches the Convention.
        if stable_id in ("echr/convention", "echr"):
            return Tax("echr", CATEGORY_LABELS["echr"], "convention", "Convention (treaty)",
                       {"source": "echr", "id_prefix": "echr"})
        return Tax("echr", CATEGORY_LABELS["echr"], "case", "ECHR case",
                   {"source": "echr"})
    return Tax("other", CATEGORY_LABELS["other"], source or "other", source or "Other",
               {"source": source} if source else {})


def classify_candidate(candidate: str, kind: str = "") -> Tax:
    """Place a PENDING citation (a not-yet-held reference) by its shape alone, so pending
    tallies line up with held tallies in the same category/sub-type buckets."""
    _form, _juris, adapter = _classify(candidate, kind)
    cand = candidate or ""
    if cand.lower().startswith("fr:code:") or cand.lower().startswith("fr:text:") or cand.upper().startswith(("LEGIARTI", "LEGITEXT", "JORFARTI", "JORFTEXT")):
        return Tax("fr-legislation", CATEGORY_LABELS["fr-legislation"], "legislation",
                   "Codes and legislation", {"source": "fr-legislation"})
    if cand.lower().startswith(("fr:pourvoi:", "fr:decision:")) or cand.upper().startswith(("JURITEXT", "CETATEXT", "CONSTEXT", "CNILTEXT")):
        return Tax("fr-caselaw", CATEGORY_LABELS["fr-caselaw"], "case", "French decision",
                   {"source": "fr-dila"})
    if cand.lower().startswith("de/gesetz/") or cand.lower().startswith("eli/bund/"):
        return Tax("de-legislation", CATEGORY_LABELS["de-legislation"], "federal",
                   "Federal legislation", {"source": "de-gii"})
    if cand.lower().startswith("de:case:"):
        return Tax("de-caselaw", CATEGORY_LABELS["de-caselaw"], "federal",
                   "Federal courts", {"source": "de-rii"})
    if adapter == "uk-legislation":
        prefix = cand.split("/", 1)[0].lower() if "/" in cand else cand.lower()
        sub, label = _leg_subtype(prefix)
        return Tax("uk-legislation", CATEGORY_LABELS["uk-legislation"], sub, label,
                   {"source": "uk-legislation", "id_prefix": prefix})
    if adapter == "uk-caselaw":
        tok = cand.split("/", 1)[0].upper() if "/" in cand else ""
        known = KNOWN_COURTS.get(tok)
        return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], tok.lower() or "other",
                   known.name if known else (tok or "Other court"), {"source": "uk-caselaw"})
    if adapter == "eu-cellar":
        sub, label = _eu_case_subtype(None, None, cand)
        return Tax("eu-cellar", CATEGORY_LABELS["eu-cellar"], sub, label, {"source": "eu-cellar"})
    if adapter == "eu-legislation":
        sub, label = _eu_leg_subtype(cand)
        return Tax("eu-legislation", CATEGORY_LABELS["eu-legislation"], sub, label,
                   {"source": "eu-legislation"})
    if adapter == "echr":
        if cand.lower() == "echr/convention":
            return Tax("echr", CATEGORY_LABELS["echr"], "convention", "Convention (treaty)", {})
        return Tax("echr", CATEGORY_LABELS["echr"], "case", "ECHR case", {"source": "echr"})
    if adapter == "us-caselaw":
        # Sub-typed by reporter ("U.S.", "F.3d"), which is how US case law is actually
        # organised — the reporter series carries the court and the precedential level,
        # where a US slug has no court token to bucket on.
        from .us_cases import reporter_name

        rep = cand.split("/")[1].lower() if cand.lower().startswith("us/") else "other"
        return Tax("us-caselaw", CATEGORY_LABELS["us-caselaw"], rep,
                   reporter_name(rep) if rep != "other" else "Other reporter",
                   {"source": "us-caselaw"})
    # Adapter-less neutral-citation courts: a recognised court token still buckets the
    # candidate by its jurisdiction — Irish/Commonwealth citations are first-class
    # pending case-law (upload/link resolves them), and NI/Scottish courts belong
    # under UK case-law with their court sub-type, never "other".
    head = cand.split("/", 1)[0].lower() if "/" in cand else ""
    known = KNOWN_COURTS.get(head.upper()) if head else None
    if known:
        catkey = JURISDICTION_CATEGORY.get(known.jurisdiction)
        if catkey:
            return Tax(catkey, CATEGORY_LABELS[catkey], head, known.name,
                       {"source": catkey})
        if known.jurisdiction == "GB":
            return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], head, known.name,
                       {"court": head})
    return Tax("other", CATEGORY_LABELS["other"], "other", _form or "Other", {})
