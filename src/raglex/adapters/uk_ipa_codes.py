"""UK Investigatory Powers Act 2016 codes of practice (Home Office) — a fixed,
one-time import of the statutory codes published on gov.uk (§1.9/§4a).

The nine codes (interception, equipment interference, communications data, bulk
acquisition, bulk personal datasets, notices, …) are Home-Office guidance issued
under the Investigatory Powers Act 2016. gov.uk serves an *accessible* HTML version
of each — clean article markup — which is fetched through the stealth tier and stored
as ``guidance`` under ``court = Home Office``.

**Provision edges.** These codes cite the Act constantly, almost always *bare* —
"section 87", "Schedule 7", "section 61(7)(b)", "sections 138 to 140" — because "the
Act" is the IPA throughout. So every section/schedule reference that is **not** tied
to a *different* named statute is linked to the Investigatory Powers Act 2016
(``ukpga/2016/25``) with the provision as the pinpoint. A reference explicitly
qualified by another Act ("section 6 of the Human Rights Act 1998") is left for the
normal citation resolver and is not attributed to the IPA.

Fixed set, no feed: a re-run re-fetches (picking up a gov.uk revision via the content
hash) but discovers nothing new — it is a maintenance import, run on demand or on a
schedule, not an incremental crawl.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)

# The Investigatory Powers Act 2016 (c. 25) — the resolution target every bare
# provision reference in these codes points at.
IPA_2016 = "ukpga/2016/25"

# The nine codes of practice (title, gov.uk accessible-HTML URL). A fixed corpus.
CODES: tuple[tuple[str, str], ...] = (
    ("Bulk acquisition of communications data code of practice",
     "https://www.gov.uk/government/publications/bulk-acquisition-of-communications-data-code-of-practice/bulk-acquisition-of-communications-data-code-of-practice-accessible--2"),
    ("Bulk personal datasets: low or no reasonable expectation of privacy code of practice",
     "https://www.gov.uk/government/publications/bulk-personal-datasets-low-or-no-reasonable-expectation-of-privacy-code-of-practice/bulk-personal-datasets-low-or-no-reasonable-expectation-of-privacy-code-of-practice-accessible"),
    ("Communications data code of practice",
     "https://www.gov.uk/government/publications/communications-data-code-of-practice/communications-data-code-of-practice-accessible--2"),
    ("Annex B: communications data code of practice",
     "https://www.gov.uk/government/publications/communications-data-code-of-practice/annex-b-communications-data-code-of-practice-accessible"),
    ("Equipment interference code of practice",
     "https://www.gov.uk/government/publications/equipment-interference-code-of-practice--2/equipment-interference-code-of-practice-accessible--2"),
    ("Interception of communications code of practice",
     "https://www.gov.uk/government/publications/interception-of-communications-code-of-practice-2022/interception-of-communications-code-of-practice-accessible"),
    ("Notices regime code of practice",
     "https://www.gov.uk/government/publications/notices-regime-code-of-practice/notices-regime-code-of-practice-accessible"),
    ("Intelligence services' retention and use of bulk personal datasets code of practice",
     "https://www.gov.uk/government/publications/intelligence-services-retention-and-use-of-bulk-personal-datasets/intelligence-services-retention-and-use-of-bulk-personal-datasets-code-of-practice-accessible"),
    ("Intelligence services' use of third party bulk personal datasets: code of practice",
     "https://www.gov.uk/government/publications/third-party-bulk-personal-datasets-code-of-practice/intelligence-services-use-of-third-party-bulk-personal-datasets-code-of-practice-accessible"),
)


# ── provision extraction ─────────────────────────────────────────────────────
# "section 87", "s. 61(7)(b)", "Schedule 7", "Sch. 3", "sections 138 to 140",
# "sections 61 and 62" — the leading word fixes section vs schedule; the tail is a
# number list with optional pinpoints.
_PROVISION = re.compile(
    r"\b(?P<kind>[Ss]ections?|[Ss]chedules?|[Ss]ch\.?|[Ss]s?\.)\s+"
    r"(?P<list>\d+[A-Z]?(?:\([^)]{1,8}\))*"
    r"(?:\s*(?:,|and|to|&|-|–)\s*\d+[A-Z]?(?:\([^)]{1,8}\))*)*)",
)
# a section/schedule tied to a DIFFERENT named statute → not the IPA; leave it to the
# resolver. "of the Act" / "of the Investigatory Powers Act 2016" stay with the IPA.
_OF_NAMED_ACT = re.compile(
    r"^\s+of\s+the\s+(?P<name>[A-Z][\w’'.-]+(?:\s+(?:of\s+|and\s+|the\s+)?[A-Z][\w’'.-]+){0,6}?"
    r"\s+(?:Act|Regulations|Order|Rules|Measure|Convention))\b",
)
_ONE = re.compile(r"\d+[A-Z]?(?:\([^)]{1,8}\))*")
_SEP = re.compile(r"\s*(,|and|&|to|through|–|—|-)\s*")
_RANGE_OPS = {"to", "through", "–", "—", "-"}


def _expand_list(list_str: str) -> list[str]:
    """A provision list ("138 to 140", "61 and 62", "5, 7 to 9") → its individual
    provision tokens, expanding small integer ranges (≤50) inclusively."""
    out: list[str] = []
    prev_int: str | None = None
    is_range = False
    for atom in _SEP.split(list_str):
        if not atom:
            continue
        if atom in _RANGE_OPS:
            is_range = True
            continue
        if atom in {",", "and", "&"}:
            is_range = False
            continue
        if is_range and prev_int is not None and atom.isdigit():
            lo, hi = int(prev_int), int(atom)
            if 0 < hi - lo <= 50:
                out.extend(str(n) for n in range(lo + 1, hi + 1))
                prev_int, is_range = atom, False
                continue
        out.append(atom)
        prev_int = atom if atom.isdigit() else None
        is_range = False
    return out


def ipa_provision_relations(text: str) -> list[TypedRelation]:
    """Every bare section/schedule reference in the code → an ``interprets`` edge to the
    IPA 2016, pinpointed. References qualified by another named Act are skipped."""
    rels: list[TypedRelation] = []
    seen: set[str] = set()
    for m in _PROVISION.finditer(text):
        tail = text[m.end(): m.end() + 80]
        named = _OF_NAMED_ACT.match(tail)
        if named and "investigatory powers" not in named.group("name").lower():
            continue  # belongs to a different statute
        is_sched = m.group("kind").lower().startswith(("schedule", "sch"))
        label = "Schedule" if is_sched else "section"
        for one in _expand_list(m.group("list")):
            anchor = f"{label} {one}"
            if anchor.lower() in seen:
                continue
            seen.add(anchor.lower())
            rels.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=f"{anchor} of the Investigatory Powers Act 2016",
                dst_id=IPA_2016, dst_anchor=anchor,
                extracted_via=ExtractedVia.REGEX,
                resolution_status=ResolutionStatus.PENDING,
            ))
    return rels


def _slug(url: str) -> str:
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = re.sub(r"-accessible(?:--\d+)?$", "", last)
    return re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-")


class UKIPACodesAdapter(BaseAdapter):
    """One-time import of the nine IPA 2016 codes of practice (Home Office guidance)."""

    source = "uk-ipa-codes"
    min_interval = 2.0
    requires_js = True    # fetched through the stealth tier, as the operator requested
    requires_proxy = False

    def __init__(self, *, fetcher=None) -> None:
        self._fetcher = fetcher

    def _get(self):
        if self._fetcher is None:
            from ..scraping.fetcher import get_fetcher
            self._fetcher = get_fetcher("stealth", source=self.source,
                                        min_interval=self.min_interval, requires_js=True)
        return self._fetcher

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # A fixed corpus: always offer every code; the pipeline dedups unchanged pages
        # and re-ingests a revised one (content-hash), so this is safe to re-run/schedule.
        for title, url in CODES:
            yield Stub(stable_id=f"uk-ipa-code/{_slug(url)}", landing_url=url,
                       raw_url=url, title=title, hints={"title": title})

    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        page = self._get().fetch(stub.landing_url)
        html = page.html or ""
        text = extract_bytes(html.encode("utf-8"), ext="html").text or ""
        relations = ipa_provision_relations(text)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=stub.hints.get("title") or stub.title or stub.stable_id,
            court="Home Office",
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=html.encode("utf-8"),
            raw_ext="html",
            text=text or None,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ipa-code", "home-office", "investigatory-powers-act-2016"],
            extra={
                "issuer": "Home Office",
                "statutory_basis": "Investigatory Powers Act 2016",
                "url": stub.landing_url,
                "provision_refs": len(relations),
            },
        )
