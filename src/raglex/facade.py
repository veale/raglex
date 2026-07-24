"""Service facade — one place that does everything, used by BOTH the web API and
the MCP server so they never drift (the user's requirement: "an MCP endpoint
which can do all the things the API can do").

Every method opens the catalogue + stores, does the work, returns plain JSON-able
dicts, and closes. That keeps it safe to call from FastAPI's thread pool and from
the MCP server alike. The agent workflow the design imagines — "augment each
section of a law with secondary material found via other tools" — is exactly:
``list_documents`` to iterate sections, then ``import_url`` / ``import_bytes`` /
``add_note`` + ``link`` to attach what you find, in several posting modes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterator


log = logging.getLogger("raglex.facade")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watch_phase_seconds(watch_id: int, cadence_minutes: int) -> int:
    """A deterministic phase offset (seconds, in ``[0, cadence)``) unique-ish per watch.
    Knuth's multiplicative hash spreads consecutive watch_ids across the whole window, so
    watches created together and sharing a cadence land in different slots."""
    cadence_s = max(1, cadence_minutes) * 60
    return int((watch_id * 2654435761) % cadence_s)


def watch_is_due(watch_id: int, cadence_minutes: int, last_run_at, now) -> bool:
    """Whether a watch should run now, with per-watch **staggering** so equal-cadence
    watches don't all fire in the same tick.

    A never-run watch is due immediately (first harvest shouldn't wait). Otherwise the
    timeline is cut into ``cadence``-long slots anchored to the epoch and shifted by the
    watch's own phase (:func:`_watch_phase_seconds`); the watch is due once its slot index
    has advanced past the slot of its last run. Two weekly watches with different phases
    therefore come due on different ticks and stay offset every week, instead of
    re-synchronising to a shared last-run time and stampeding together.
    """
    import datetime as _dt

    if not last_run_at:
        return True
    try:
        prev = _dt.datetime.fromisoformat(last_run_at)
    except (ValueError, TypeError):
        return True
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=_dt.timezone.utc)
    cadence_s = max(1, cadence_minutes) * 60
    phase = _watch_phase_seconds(watch_id, cadence_minutes)
    slot_now = int((now.timestamp() - phase) // cadence_s)
    slot_prev = int((prev.timestamp() - phase) // cadence_s)
    return slot_now > slot_prev


def _progress(cb, **fields) -> None:
    """Report coarse progress to an optional callback (used by the background-job
    runner so the UI can poll "fetching 5/30"). Never lets a callback error break
    the operation."""
    if cb is None:
        return
    try:
        cb(**fields)
    except Exception:  # noqa: BLE001
        pass

from .citations.oscola import cite as _oscola_cite
from .config import Config
from .core.models import DocType, RelationshipType
from .embeddings import EmbedStage
from .imports import (
    add_note,
    attach_asset,
    import_file,
    import_url,
    link_documents,
    tag_document,
)
from .imports.zotero import ZoteroImporter
from .ops import check_alerts, corpus_stats, pipeline_queues, resolution_worklist, source_dashboard
from .resolve import Resolver
from .retrieval import SearchEngine, expand
from .settings import SettingsStore
from .storage import Catalogue, RawStore, TextStore


def _row_meta(row) -> dict:
    """Decode a document row's ``meta_json`` into a dict without an extra query — the row
    (from ``get_document``) already carries the column."""
    if row is None:
        return {}
    try:
        raw = row["meta_json"]
    except (KeyError, IndexError, TypeError):
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _match_segment(segs, anchor: str) -> int:
    """Index of the segment a citable label names — the server-side twin of the
    reader's ``matchSegIndex``: paragraph pinpoints ("para 80", "[80]") match by
    number; legislation pinpoints ("Article 17", "s. 45") by normalised label,
    exact before substring (so "Article 4" prefers "Article 4" over "Article 40")."""
    import re as _re

    if not anchor or not segs:
        return -1
    para = _re.search(r"para\.?\s*(\d+)|^\[?(\d+)\]?$", anchor.strip(), _re.IGNORECASE)
    num = para and (para.group(1) or para.group(2))
    if num:
        pat = _re.compile(rf"^\[?{num}[.\]]?\b")
        for i, s in enumerate(segs):
            if pat.match((s.label or "").strip()):
                return i

    def norm(x: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", (x or "").lower())

    a = norm(anchor)
    if not a:
        return -1
    for i, s in enumerate(segs):
        if norm(s.label) == a:
            return i
    if len(a) > 2:
        for i, s in enumerate(segs):
            if a in norm(s.label):
                return i
    return -1


def _doc_type(value: str | None, default: DocType) -> DocType:
    if not value:
        return default
    try:
        return DocType(value)
    except ValueError:
        return default


def _sniff_format(raw: bytes) -> str | None:
    """Infer the structural format of stored raw bytes (for re-parsing) — a zip or
    Formex ``<ACT>`` → Formex; Akoma Ntoso; a BWB ``<toestand>`` → BWB."""
    head = raw[:4096]
    if raw[:2] == b"PK":
        return "formex-legislation"  # CELLAR Formex zip
    low = head.lower()
    if b"akomantoso" in low:
        return "akoma-ntoso"
    if b"<act" in low or b"formex" in low or b"enacting.terms" in low:
        return "formex-legislation"
    if b"toestand" in low or b"<wetgeving" in low:
        return "bwb"
    # juris rii case-law XML (de-rii): a <dokument> with the court field <gertyp>.
    # Distinguishes it from de-gii legislation XML, which has no gertyp.
    if b"<dokument" in low and (b"gertyp" in low or b"<doknr" in low):
        return "rii-xml"
    # DILA JADE/LEGI XML (fr-dila): both the case-law <TEXTE_JURI_ADMIN> and the
    # legislation <ARTICLE> carry a <META><META_COMMUN> block near the top.
    if b"<meta_commun" in low or b"texte_juri_admin" in low or b"<meta_article" in low:
        return "dila-xml"
    if b'id="fragview"' in low or b"topheadingparagraph" in low or b"headingparagraph" in low:
        return "lawmaker-html"
    return None


def _act_level(candidate: str | None) -> str | None:
    from .resolve.matchers import act_level

    return act_level(candidate)


# European Court Reports series → the CJEU court its ECLI must name:
#   "ECR I-…"  → Court of Justice     (ECLI:EU:C:)
#   "ECR II-…" → General Court / CFI  (ECLI:EU:T:), incl. the Civil Service Tribunal (EU:F:)
#   no series letter (pre-1989 "[1974] ECR 837") → Court of Justice (EU:C:)
# so an ECR string can never legitimately resolve to a decision from the wrong court.
def _ecr_series_ok(ecr_alias: str, target: str) -> bool:
    """True if ``target`` (an ECLI or a raw id) is court-consistent with the ECR series in
    ``ecr_alias``. Non-ECLI / court-less targets pass (nothing to contradict)."""
    m = re.search(r"ECLI:EU:([CTF]):", target or "", re.IGNORECASE)
    if not m:
        return True
    court = m.group(1).upper()
    low = ecr_alias.lower()
    if re.search(r"\bii-", low):
        return court in ("T", "F")
    if re.search(r"\bi-", low):
        return court == "C"
    return court == "C"  # no series letter → Court of Justice


def _neutral_citation_from_slug(stable_id: str) -> str | None:
    """A UK Find Case Law slug → its neutral citation, for searching out citing cases.
    ``uksc/2021/12`` → ``[2021] UKSC 12``; ``ewca/civ/2015/454`` → ``[2015] EWCA Civ 454``;
    ``ukut/aac/2012/440`` → ``[2012] UKUT 440 (AAC)``. None for non-case slugs (legislation)."""
    from .citations.snowball import UK_LEG_TYPES

    parts = stable_id.split("/")
    if not parts or parts[0].lower() in UK_LEG_TYPES or not parts[0].isalpha():
        return None  # legislation or opaque id — not a neutral-citation case
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        court, year, num = parts
        return f"[{year}] {court.upper()} {num}"
    if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        court, div, year, num = parts
        cu = court.upper()
        # the division is written inline for EWCA ("EWCA Civ 1") but parenthetically for
        # tribunals and the High Court ("UKUT 440 (AAC)", "EWHC 22 (Admin)").
        if cu == "EWCA":
            return f"[{year}] {cu} {div.title()} {num}"
        # High Court divisions are title-case (Admin, Comm); tribunal chambers are
        # upper-case initialisms (AAC, GRC, IAC).
        divtxt = div.title() if cu == "EWHC" else div.upper()
        return f"[{year}] {cu} {num} ({divtxt})"
    return None


def _case_title_from(text: str) -> str | None:
    """A case name from the top of a judgment — the first non-empty header line that looks
    like a party-v-party title ("Killock v ICO"), so an imported case gets a real title
    instead of the filename."""
    for line in (text or "")[:600].splitlines():
        line = line.strip()
        if len(line) > 8 and re.search(r"\bv\.?\b", line) and not line.lower().startswith(("in the", "before")):
            return line[:200]
    return None


def _is_junk_ref(ref: str) -> bool:
    """A reference string with no citation value (stray ``#`` anchors, js/mailto
    links) — kept out of the manual-resolution worklist."""
    if not ref or len(ref) < 3:
        return True
    low = ref.lower()
    if ref.startswith("#") or low.startswith(("javascript:", "mailto:", "tel:")):
        return True
    # A candidate-less bare URL as the group key means no candidate could be derived
    # from it (a derivable URL's group key is its candidate). Nothing a human can do
    # with it either — legacy eu-exit webarchive footnote links alone were ~10k rows.
    return low.startswith(("http://", "https://"))


# Corpus-Map category → retrieval jurisdiction bucket, for the Westlaw/Lexis export filter.
# (Report series map via reporters.series_jurisdiction; neutral citations & bare names map
# here, off the candidate's court token.) The big single jurisdictions get their own bucket;
# the long tail is grouped by region the same way the Corpus Map's taxonomy does, so the
# picker stays short without collapsing Canada/Australia/NZ/etc. into one "Commonwealth" row.
_CATEGORY_JURISDICTION: dict[str, str] = {
    "uk-caselaw": "uk", "uk-legislation": "uk",
    "ie-caselaw": "ie", "ie-legislation": "ie",
    "eu-cellar": "eu", "eu-legislation": "eu", "eu-preparatory": "eu", "echr": "eu",
    "us-caselaw": "us",
    "fr-caselaw": "fr", "fr-legislation": "fr",
    "de-caselaw": "de", "de-legislation": "de",
    "ca-caselaw": "ca", "ca-legislation": "ca",
    "au-caselaw": "au", "au-legislation": "au",
    "nz-caselaw": "nz", "nz-legislation": "nz",
    "in-caselaw": "in",
    "sg-caselaw": "sg", "sg-legislation": "sg",
    "hk-caselaw": "hk", "hk-legislation": "hk",
    "za-caselaw": "za", "my-caselaw": "my",
    "africa-caselaw": "africa", "caribbean-caselaw": "caribbean",
    "pacific-caselaw": "pacific", "ci-caselaw": "ci", "offshore-caselaw": "offshore",
}

# The canonical retrieval-jurisdiction buckets, in the order a UK-subscription user reads
# them (their own first). Both the report-series and candidate-court lookups resolve into
# exactly these keys (via _retrieval_bucket), so the Westlaw/Lexis filter can only ever
# offer these. Served to the UI rather than duplicated there, so a new bucket appears in the
# picker automatically.
RETRIEVAL_JURISDICTIONS: tuple[tuple[str, str], ...] = (
    ("uk", "United Kingdom"),
    ("ie", "Ireland"),
    ("eu", "EU (CMLR, ECR…)"),
    ("fr", "France"),
    ("de", "Germany"),
    ("us", "United States"),
    ("ca", "Canada"),
    ("au", "Australia"),
    ("nz", "New Zealand"),
    ("in", "India"),
    ("sg", "Singapore"),
    ("hk", "Hong Kong"),
    ("za", "South Africa"),
    ("my", "Malaysia"),
    ("africa", "Africa (other)"),
    ("caribbean", "Caribbean"),
    ("pacific", "Pacific"),
    ("ci", "Channel Islands"),
    ("offshore", "Offshore & int'l commercial"),
)

# Fine country code (from a report series or a candidate court token) → retrieval picker
# bucket. The majors pass through unchanged; the long tail of individual African / Pacific /
# Caribbean / offshore jurisdictions folds into a regional bucket so the picker stays short.
_RETRIEVAL_BUCKET: dict[str, str] = {
    "gb": "uk", "uk": "uk", "ie": "ie", "eu": "eu", "fr": "fr", "de": "de", "us": "us",
    "ca": "ca", "au": "au", "nz": "nz", "in": "in", "sg": "sg", "hk": "hk",
    "za": "za", "my": "my",
    **{c: "africa" for c in ("ke", "gh", "ng", "zw", "zm", "na", "ug", "tz", "mw",
                             "sz", "bw", "mu", "sc")},
    **{c: "pacific" for c in ("fj", "pg", "sb", "vu", "ws", "to", "nr", "ck", "ki", "tv")},
    **{c: "caribbean" for c in ("tt", "jm", "bb", "bs", "gy", "bz")},
    **{c: "ci" for c in ("je", "gg", "im")},
    **{c: "offshore" for c in ("ky", "ae", "qa", "sh", "io", "bm", "gi")},
}


def _retrieval_bucket(code: str | None) -> str:
    """Collapse a fine jurisdiction code into one of the RETRIEVAL_JURISDICTIONS picker
    buckets (majors pass through; the African/Pacific/Caribbean/offshore long tail folds to
    its region), so the export filter and the picker always speak the same vocabulary."""
    c = (code or "").lower()
    return _RETRIEVAL_BUCKET.get(c, c or "uk")


def _candidate_jurisdiction(candidate: str | None) -> str:
    """The retrieval bucket of a non-report reference, from its candidate's court token — so
    an Irish neutral citation ("[2019] IESC 4" → ``iesc/2019/4``) reads as Irish and an
    Australian one ("[2003] HKCFA 46" → hk) reads as Hong Kong, not the "uk" default. Bare
    names → "uk"."""
    if not candidate:
        return "uk"
    from .citations.taxonomy import classify_candidate

    return _CATEGORY_JURISDICTION.get(classify_candidate(candidate).category, "uk")


class _SingleStubAdapter:
    """Wrap a real adapter to fetch exactly one known item: ``discover`` yields a
    single constructed stub, ``fetch`` delegates to the base adapter. Used for
    targeted resolution of a hanging reference whose adapter discovers by crawling
    (e.g. uk-caselaw) rather than by id."""

    def __init__(self, base, stub) -> None:
        self._base = base
        self._stub = stub
        self.source = base.source
        self.min_interval = getattr(base, "min_interval", 0.0)

    def discover(self, since, *, max_pages=None):
        yield self._stub

    def fetch(self, stub):
        return self._base.fetch(stub)


def _targeted_uk_legislation(candidate: str, patient: bool = False):
    from .adapters.registry import get_adapter

    return get_adapter("uk-legislation", ids=candidate, patient=patient)


def _targeted_eu_legislation(candidate: str):
    from .adapters.registry import get_adapter

    return get_adapter("eu-legislation", celex=candidate)


def _targeted_eu_preparatory(candidate: str):
    from .adapters.registry import get_adapter

    return get_adapter("eu-preparatory", celex=candidate)


def _targeted_uk_caselaw(candidate: str):
    from .adapters.registry import get_adapter
    from .core.models import Stub

    base = get_adapter("uk-caselaw")
    base_url = "https://caselaw.nationalarchives.gov.uk"
    stub = Stub(stable_id=candidate, landing_url=f"{base_url}/{candidate}",
                raw_url=f"{base_url}/{candidate}/data.xml")
    return _SingleStubAdapter(base, stub)


def _targeted_eu_cellar(candidate: str):
    """A CJEU case by CELEX (``62018CJ0511`` from "C-511/18") or by **ECLI**
    (``ECLI:EU:C:2020:791``) — the ECLI is mapped to its CELEX via one SPARQL hop,
    so EU case citations resolve whichever form they take.

    A case-number citation carries no signal about whether the case ended in a judgment
    or an order, so the grammar's CELEX is a guess. Confirm it against CELLAR (probing
    the order/judgment variants) before fetching, and carry the guessed form through as
    an alias so the citing edges resolve to whatever the case really is."""
    from .adapters.eu_cellar import CJEUCaseAdapter, EUCellarAdapter, resolve_case_celex

    cu = candidate.upper()
    if re.fullmatch(r"\d{5}[A-Z]{1,2}\d{4}", cu):
        real = resolve_case_celex(cu)
        if real is None:
            return None  # absent from CELLAR under any descriptor
        return CJEUCaseAdapter(real, celex_aliases=(cu,))
    if cu.startswith("ECLI:EU:"):
        meta = EUCellarAdapter().case_metadata(ecli=candidate)
        if meta.get("celex"):
            return CJEUCaseAdapter(meta["celex"])
    return None


def _targeted_echr(candidate: str):
    """An ECtHR case by ECLI (``ECLI:CE:ECHR:…``) or application number (``58170/13``) —
    the HUDOC adapter resolves either via the same app-number lookup."""
    from .adapters.registry import get_adapter

    return get_adapter("echr", ids=candidate)


def _targeted_uk_hol(candidate: str):
    """A House of Lords case by ``ukhl/YYYY/N`` — scraped from publications.parliament.uk
    when Find Case Law doesn't hold it (older HoL judgments live there, not on TNA)."""
    from .adapters.registry import get_adapter

    return get_adapter("uk-hol", ids=candidate)


def _targeted_us_caselaw(candidate: str):
    """A US case by its reporter citation (``us/us/576/644``) — resolved through
    CourtListener's citation-lookup endpoint.

    Returns None when there is no API token, so the reference is reported as an
    absence for this run rather than raising: the citation is perfectly good, we just
    can't reach the source. The free-tier quota is enforced inside the adapter (a
    persisted rolling-window ledger); when it is spent the fetch surfaces as
    rate-limiting, which stops the drain's batch and leaves the rest of the queue
    intact for the next tick.
    """
    from .adapters.registry import get_adapter

    adapter = get_adapter("us-caselaw", ids=candidate)
    return adapter if getattr(adapter, "configured", False) else None


def _targeted_ca_canlii(candidate: str):
    """A Canadian case by neutral citation (``scc/2011/10``) — resolved through the
    CanLII API into a METADATA STUB (CanLII's API never returns judgment text): title,
    date, parallel-citation aliases, citator edges and a verified canlii.ca permalink,
    held under the same slug the extractor mints so the citing edges resolve.

    Raises when no API key is configured — the caller records that as *transient*
    (short retry), never as a 90-day absence: the citation is perfectly good, we just
    can't reach the source without a key."""
    from .adapters.registry import get_adapter

    adapter = get_adapter("ca-canlii", ids=candidate)
    if not getattr(adapter, "configured", False):
        raise RuntimeError("ca-canlii: no API key — set RAGLEX_CANLII_API_KEY "
                           "(granted via canlii.org/en/feedback/feedback.html)")
    return adapter


def _targeted_nl_rechtspraak(candidate: str):
    """A Dutch judgment by ECLI — Rechtspraak fetches the content directly by ECLI."""
    if not candidate.upper().startswith("ECLI:NL:"):
        return None
    from .adapters.nl_rechtspraak import CONTENT_URL
    from .adapters.registry import get_adapter
    from .core.models import Stub

    base = get_adapter("nl-rechtspraak")
    stub = Stub(stable_id=candidate, raw_url=f"{CONTENT_URL}?id={candidate}",
                landing_url=f"https://uitspraken.rechtspraak.nl/details?id={candidate}")
    return _SingleStubAdapter(base, stub)


def _targeted_nl_legislation(candidate: str):
    """A BWB work or an exact dated copy (``BWBR…@YYYY-MM-DD``)."""
    import re
    m = re.fullmatch(r"(?i)(BWBR\d{7})(?:@(\d{4}-\d{2}-\d{2}))?", candidate.strip())
    if not m:
        return None
    from .adapters.registry import get_adapter
    return get_adapter("nl-legislation", ids=m.group(1).upper(),
                       version_date=m.group(2), use_sru=False)


# adapter key (from the snowball classifier) → a builder that returns a one-item
# adapter run for a given candidate id. Extend as adapters gain id-fetch support.
_TARGETED_HARVEST = {
    "uk-legislation": _targeted_uk_legislation,
    "eu-legislation": _targeted_eu_legislation,
    "eu-preparatory": _targeted_eu_preparatory,
    "uk-caselaw": _targeted_uk_caselaw,
    "uk-hol": _targeted_uk_hol,
    "eu-cellar": _targeted_eu_cellar,
    "echr": _targeted_echr,
    "nl-rechtspraak": _targeted_nl_rechtspraak,
    "nl-legislation": _targeted_nl_legislation,
    "us-caselaw": _targeted_us_caselaw,
    "ca-canlii": _targeted_ca_canlii,
}


# Canonical anchor key — the server-side mirror of the reader's anchorKey() (views.tsx):
# "Article 17 Right to erasure (right to be forgotten)" → "art:17", "Recital 47" →
# "rec:47", "[80]" → "80". Unit type + number alone, so a segment label that carries the
# provision's TITLE still meets the bare "Article 17" the citation edges pin to.
_ANCHOR_TYPES = {
    "article": "art", "art": "art", "recital": "rec", "rec": "rec",
    "section": "s", "sec": "s", "s": "s", "schedule": "sch", "sch": "sch",
    "paragraph": "para", "para": "para", "regulation": "reg", "reg": "reg",
    "rule": "rule", "point": "pt", "pt": "pt", "annex": "annex",
}


def _anchor_key(text: str | None) -> str | None:
    t = (text or "").strip().lower().lstrip("[(")
    m = re.match(r"^([a-z]+)?\.?\s*(\d+[a-z]?)", t)
    if not m or not m.group(2):
        return None
    typ = _ANCHOR_TYPES.get(m.group(1) or "", "")
    return f"{typ}:{m.group(2)}" if typ else m.group(2)


def _today_iso() -> str:
    from datetime import date as _date

    return _date.today().isoformat()


def _rel_type(value: str | None, default: RelationshipType | None = None) -> RelationshipType | None:
    if not value:
        return default
    try:
        return RelationshipType(value)
    except ValueError:
        return default


class Facade:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()
        self.settings = SettingsStore(self.config.settings_path)
        # runtime statute-gazetteer top-up (acts newer than the vendored lists) lives in
        # the data dir; register it so extraction confirms recent acts by name
        from .citations.statute_gazetteer import register_extra_list
        register_extra_list(self.config.data_dir / "statutes_extra.lst")
        # short-TTL cache for the expensive dashboard aggregates (full scans over the
        # ~1.5M-row relations table). Stale-while-revalidate: once warm, every request is
        # instant — a stale entry is served immediately and refreshed in the background, so
        # no user request ever blocks on the scan (only the very first, cold call does).
        self._cache: dict[str, tuple[float, dict]] = {}
        self._refreshing: set[str] = set()

    def _cached(self, key: str, ttl: float, fn, *, placeholder: dict | None = None,
                sync_wait: float = 0.0):
        """Stale-while-revalidate cache. With a ``placeholder``, a request NEVER blocks
        beyond ``sync_wait``: the first cold call kicks off a background compute and —
        after giving it ``sync_wait`` seconds to finish (so cheap slices still answer
        in one round trip) — returns ``{_warming}`` for the UI to poll; a stale entry
        is served instantly and refreshed behind the scenes. Without a placeholder the
        first call computes synchronously."""
        import threading
        import time as _t

        def _compute_async():
            self._refreshing.add(key)

            def _run():
                try:
                    self._cache[key] = (_t.time(), fn())
                except Exception as exc:  # noqa: BLE001 — keep serving stale / retry next time
                    # NEVER silently: a warm that fails every retry means the UI shows
                    # an empty placeholder forever with no trace anywhere (a KeyError
                    # blanked the Explore homepage for days). Stale/placeholder is
                    # still served; the log is how the failure becomes diagnosable.
                    log.warning("cache warm %r failed: %s: %s",
                                key, type(exc).__name__, exc)
                finally:
                    self._refreshing.discard(key)
            threading.Thread(target=_run, daemon=True).start()

        hit = self._cache.get(key)
        if hit is not None:
            age = _t.time() - hit[0]
            if age >= ttl and key not in self._refreshing:
                _compute_async()
            return {**hit[1], "_cached": True, "_stale": age >= ttl}
        # cold: nothing cached yet
        if placeholder is not None:
            if key not in self._refreshing:
                _compute_async()
            deadline = _t.time() + sync_wait
            while _t.time() < deadline:
                done = self._cache.get(key)
                if done is not None:
                    return {**done[1], "_cached": True, "_stale": False}
                _t.sleep(0.02)
            return {**placeholder, "_warming": True}
        val = fn()  # synchronous (used for the cheap aggregates)
        self._cache[key] = (_t.time(), val)
        return val

    _VOLATILE_CACHE_PREFIXES = ("coverage", "stats", "corpus_map", "queues", "worklist",
                                "snowball", "unfetchable", "drill")

    def _invalidate_caches(self) -> None:
        """Drop the cached dashboard aggregates after an op that changes the citation
        graph (harvest/resolve), so the worklist's per-source "remaining" counts and
        coverage refresh instead of serving the pre-harvest snapshot."""
        for key in [k for k in self._cache
                    if k.startswith(self._VOLATILE_CACHE_PREFIXES)]:
            self._cache.pop(key, None)
            self._refreshing.discard(key)

    def warm_caches(self) -> None:
        """Pre-compute the heavy dashboard aggregates in the background (called on app
        startup) so the first page load after a restart is instant, not a cold scan."""
        import threading
        import time as _t

        def _warm():
            for fn in (self.coverage, self.stats, self.corpus_map):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
            # Explore: the shape table, then every jurisdiction's default drill
            # slices (all-time, authority sort, each kind toggle) — sequential, so
            # a restart doesn't stampede the pool; each also warms PG's buffers,
            # which is most of a cold drill's cost (16s cold vs 0.3s warm).
            try:
                self._cache["corpus-shape"] = (_t.time(), self._corpus_shape_uncached())
                for row in self._cache["corpus-shape"][1].get("jurisdictions", []):
                    for kind in (None, "cases", "legislation", "guidance", "administrative"):
                        key = self._drill_key(row["jurisdiction"], None, kind, None,
                                              "authority", 25)
                        if key not in self._cache:
                            self._cache[key] = (_t.time(), self._drill_uncached(
                                row["jurisdiction"], kind=kind))
            except Exception:  # noqa: BLE001 — warming is best-effort
                pass
        threading.Thread(target=_warm, daemon=True).start()

    @contextmanager
    def _open(self) -> Iterator[tuple[Catalogue, RawStore, TextStore]]:
        cat = Catalogue(self.config.catalogue_path)
        try:
            yield cat, RawStore(self.config.raw_dir), TextStore(self.config.text_dir)
        finally:
            cat.close()

    def _provider(self):
        """Build the embedding provider from live settings (env > file), so the UI
        can switch provider/model without a restart."""
        from .embeddings import get_provider

        name = self.settings.resolve("RAGLEX_EMBED_PROVIDER") or self.config.embed_provider
        model = self.settings.resolve("RAGLEX_EMBED_MODEL") or self.config.embed_model
        return get_provider(name, **({"model": model} if model else {}))

    def _reranker(self):
        """The §6c precision stage — the ML sidecar's cross-encoder when configured,
        otherwise the identity (fused RRF order)."""
        from .embeddings import get_reranker

        return get_reranker(self.settings.resolve("RAGLEX_RERANKER"))

    # -- settings (UI-editable secrets, §ops) ------------------------------
    def get_settings(self) -> dict:
        return self.settings.masked()

    def update_settings(self, patch: dict) -> dict:
        masked = self.settings.update(patch)
        self.settings.apply_to_env()  # pick up new file values this process (env still wins)
        return masked

    # -- read / research ---------------------------------------------------
    def search(self, query: str, *, k: int = 5, filters: dict | None = None) -> list[dict]:
        # RAGLEX_SEARCH_SEMANTIC: "auto" (default) gates the vector half on an ANN index
        # existing; "0"/"off" forces lexical-only (e.g. while embeddings are incomplete);
        # "1"/"on" forces it on.
        import os
        _sem = (os.environ.get("RAGLEX_SEARCH_SEMANTIC") or "auto").strip().lower()
        semantic = None if _sem in ("auto", "") else _sem in ("1", "on", "true", "yes")
        with self._open() as (cat, _rs, _ts):
            engine = SearchEngine(cat, self._provider(), reranker=self._reranker())
            hits = engine.search(query, k=k, filters=filters or None, semantic=semantic)
            out = []
            for h in hits:
                doc = cat.get_document(h.doc_id)
                out.append({
                    "doc_id": h.doc_id, "ecli": h.ecli, "title": h.title, "court": h.court,
                    "source": h.source, "doc_type": h.doc_type, "decision_date": h.decision_date,
                    "score": h.score, "structural_unit": h.structural_unit,
                    "char_start": h.char_start, "char_end": h.char_end, "chunk_text": h.chunk_text,
                    "oscola": _oscola_cite(doc, _row_meta(doc)) if doc else None,
                    "signals": h.signals,
                    "neighbours": [
                        {"id": n.dst_id, "relationship_type": n.relationship_type,
                         "direction": n.direction, "title": n.title, "authority": n.authority}
                        for n in (h.neighbours.neighbours if h.neighbours else [])
                    ],
                })
            return out

    def get_document(self, stable_id: str) -> dict:
        with self._open() as (cat, _rs, _ts):
            doc = cat.get_document(stable_id)
            if doc is None:
                return {"error": "not found", "stable_id": stable_id}
            rels = [dict(r) for r in cat.relations_for(stable_id)]
            suppressed = [r for r in rels if r["relationship_type"] == "suppressed"]
            # "Cited by" (JADE's reverse-citation gloss) — one row per citing document
            # (a doc may cite this many times), enriched with the citing doc's name +
            # HOW it cites this one (treatment), which JADE doesn't surface. The true
            # distinct count is reported; only the first N are title-enriched (avoid an
            # N+1 over a heavily-cited authority).
            # Incoming edges via ONE bounded, PageRank-ordered indexed query — the
            # old unbounded scan materialised a mega-authority's 100k citers in
            # Python and pinned a pool connection for seconds per page view
            # (a prime suspect in the pool-exhaustion freezes). `inferred` edges
            # (heuristic carry-forwards) are excluded there and counted apart.
            ids_self = [stable_id] + ([doc["ecli"]] if doc["ecli"] else [])
            incoming = self._assemble_cited_by(
                cat, cat.top_citing_edges(ids_self, limit=600), cap=200)
            cited_by_total = cat.cited_by_stats(ids_self)["documents"]
            inferred_total = cat.inferred_citer_count(ids_self)
            preparatory_count = cat.citer_count_by_doc_type(ids_self, "preparatory")
            meta = cat.document_meta(stable_id)  # adapter extras (celex, origin_country, …)
            # Summary line: distinct authorities this document cites, split into cases vs
            # statutory material by the citation's entity_kind (OSCOLA's two source families).
            _STATUTE = {"act", "regulation", "directive", "treaty", "eu_instrument"}
            cases_cited: set = set()
            statute_cited: set = set()
            for c in cat.citations_for(stable_id):
                ek = (c["entity_kind"] or "").lower()
                key = c["candidate_id"] or c["raw"]
                if ek in _STATUTE:
                    statute_cited.add(key)
                elif ek:
                    cases_cited.add(key)
            # "Also cited as" — the report citations / application numbers aliased to this
            # document (parallel mining, report matching, user confirmations). Human-citable
            # forms only: a bracketed-year report or an ECHR appno; machine ids stay hidden.
            import re as _recite
            also_cited: list[str] = []
            own = {stable_id.casefold(), (doc["ecli"] or "").casefold()}
            for a in cat.aliases_to([stable_id, doc["ecli"]]):
                al = a["alias"]
                if al.casefold() in own or not _recite.search(
                        r"[\[(](?:1[6-9]|20)\d{2}[\])]|^\d{1,5}/\d{2}$", al):
                    continue
                # aliases are stored folded — restore conventional capitalisation
                # for display ("[2003] 1 all e.r. (comm) 140" → "… All ER (Comm) …")
                from .citations.reporters import display_citation
                if _recite.fullmatch(r"\d{1,5}/\d{2}", al):
                    disp = f"app no {al}"
                else:
                    disp = display_citation(al)
                if disp not in also_cited:
                    also_cited.append(disp)
            return {
                "document": dict(doc),
                "oscola": _oscola_cite(doc, meta),  # this document's own OSCOLA citation
                # the reader shows names, never slugs: "Court of Appeal (Civil
                # Division)" + "England & Wales", not "ewca"
                "court_label": self.court_label(doc["court"], doc["source"]) if doc["court"] else None,
                "jurisdiction": self._doc_bucket(doc["source"], doc["court"]),
                "source_label": self.source_label(doc["source"]),
                "link_label": self.link_label(doc["landing_url"], doc["source"]),
                "also_cited_as": also_cited[:10],
                "meta": meta,
                "cases_cited_count": len(cases_cited),
                "statute_cited_count": len(statute_cited),
                "tags": [dict(t) for t in cat.tags_for(stable_id)],
                "relations": [r for r in rels if r["relationship_type"] != "suppressed"],
                "suppressed_count": len(suppressed),
                "incoming": incoming,
                "cited_by_count": cited_by_total,
                "preparatory_documents": {
                    "available": bool(preparatory_count),
                    "count": preparatory_count,
                    "message": (f"Preparatory documents exist for this item — "
                                f"{preparatory_count} available."
                                if preparatory_count else None),
                    "retrieve_with": "document_mentions",
                },
                "inferred_by_count": max(0, inferred_total),
                "assets": [dict(a) for a in cat.assets_for(stable_id)],
                "versions": [dict(v) for v in cat.list_versions(stable_id)],
            }

    _TREATMENT_RANK = {"overrules": 0, "distinguishes": 1, "applies": 2, "follows": 3,
                       "considers": 4, "mentions": 5}

    def _assemble_cited_by(self, cat, edge_rows, *, cap: int = 200) -> list[dict]:
        """Fold raw citing edges into the panel's one-row-per-citing-document shape.

        A document may cite this authority in several passages: the row shown is the
        strongest treatment among them, but the OTHER passages are kept as anchors
        (not discarded) so the reader can open each place it was engaged with —
        "and 3 other places" is a signal about depth of engagement that a single
        collapsed row throws away. Shared by get_document's global top slice and
        cited_by_slice's per-jurisdiction fetch."""
        best: dict[str, dict] = {}
        others: dict[str, list[dict]] = {}
        for r in edge_rows:
            sid = r["src_id"]
            cur = best.get(sid)
            if cur is None:
                best[sid] = dict(r)
                others.setdefault(sid, [])
                continue
            if (self._TREATMENT_RANK.get(r["relationship_type"], 9)
                    < self._TREATMENT_RANK.get(cur["relationship_type"], 9)):
                best[sid] = dict(r)
                demoted = cur
            else:
                demoted = dict(r)
            others.setdefault(sid, []).append(
                {"dst_anchor": demoted.get("dst_anchor"),
                 "relationship_type": demoted.get("relationship_type")})
        incoming: list[dict] = []
        page_ids = list(best.items())[:cap]
        # one grouped aggregate for the whole page, not one query per row
        citer_counts = cat.cited_by_counts([sid for sid, _ in page_ids])
        for sid, r in page_ids:
            src = cat.get_document(sid)
            # OSCOLA citation for the citing document, so "cited by / mentioned by"
            # reads in proper form. meta_json is on the row → no extra query.
            src_oscola = _oscola_cite(src, _row_meta(src)) if src else None
            incoming.append({**r, "src_title": src["title"] if src else None,
                             "src_court": src["court"] if src else None,
                             "src_date": src["decision_date"] if src else None,
                             "src_authority": r.get("src_pagerank") or 0.0,
                             # jurisdiction × kind, so the cited-by list can be
                             # sliced the way a lawyer actually reads it
                             # ("UK cases", "EU legislation")
                             "src_jurisdiction": self._doc_bucket(
                                 src["source"], src["court"]) if src else None,
                             "src_kind": self._doc_kind(
                                 src["source"], src["doc_type"], src["court"]) if src else None,
                             # how heavily THIS citer is itself cited — a subtle
                             # authority cue next to each name
                             "src_cited_by": citer_counts.get(sid),
                             "src_oscola": src_oscola,
                             # the other passages in this document that cite it
                             "other_passages": others.get(sid) or []})
        return incoming

    def cited_by_breakdown(self, stable_id: str) -> dict:
        """HONEST facet counts for the cited-by panel: distinct citing documents per
        jurisdiction × kind over the WHOLE resolved incoming set, not the loaded page.

        The panel's rows are the bounded top slice by PageRank (a pool-health
        necessity on mega-authorities), but computing the facet chips from that slice
        silently erased whole jurisdictions: 2,484 French decisions citing the GDPR
        rendered as "no French case law", because the top-600-edge window filled with
        UK/EU legislation and EDPB material first. One indexed aggregate.

        Cached stale-while-revalidate per document: the aggregate is ~1s warm on a
        26k-edge authority but the monsters (echr/convention: 358k edges) would pin
        a pool connection for many seconds — so a cold call computes in the
        background and returns a warming placeholder, which the panel treats as
        "fall back to the loaded-rows facets for now"."""
        def _compute() -> dict:
            with self._open() as (cat, _rs, _ts):
                doc = cat.get_document(stable_id)
                if doc is None:
                    return {"error": "not found", "stable_id": stable_id}
                ids_self = [stable_id] + ([doc["ecli"]] if doc["ecli"] else [])
                buckets: dict[tuple[str, str], int] = {}
                for r in cat.citing_breakdown(ids_self):
                    key = (self._doc_bucket(r["source"], r["court"]),
                           self._doc_kind(r["source"], r["doc_type"], r["court"]))
                    buckets[key] = buckets.get(key, 0) + r["docs"]
                out = [{"jurisdiction": j, "kind": k, "documents": n}
                       for (j, k), n in sorted(buckets.items(), key=lambda kv: -kv[1])]
                return {"stable_id": stable_id, "buckets": out,
                        "total": sum(b["documents"] for b in out)}
        return self._cached(
            f"cited-by-breakdown:{stable_id}", 21600, _compute,
            placeholder={"stable_id": stable_id, "buckets": [], "total": None},
            sync_wait=2.5)

    def cited_by_slice(self, stable_id: str, *, jurisdiction: str,
                       kind: str | None = None, limit: int = 60) -> dict:
        """The cited-by panel's per-facet fetch: the top citers of this document FROM
        ONE jurisdiction (× kind), PageRank-ordered — reachable even when the global
        top slice never gets there. The SQL filter is by adapter source (what the
        index can use); the exact jurisdiction × kind bucket is confirmed on the
        assembled rows, since a few sources fan out per court (dpa-* splits)."""
        with self._open() as (cat, _rs, _ts):
            doc = cat.get_document(stable_id)
            if doc is None:
                return {"error": "not found", "stable_id": stable_id}
            ids_self = [stable_id] + ([doc["ecli"]] if doc["ecli"] else [])
            sources = sorted({
                r["source"] for r in cat.citing_breakdown(ids_self)
                if self._doc_bucket(r["source"], r["court"]) == jurisdiction
                and (not kind or self._doc_kind(r["source"], r["doc_type"],
                                                r["court"]) == kind)})
            if not sources:
                return {"stable_id": stable_id, "jurisdiction": jurisdiction,
                        "kind": kind, "incoming": []}
            edges = cat.top_citing_edges(ids_self, limit=max(600, limit * 6),
                                         sources=sources)
            rows = self._assemble_cited_by(cat, edges, cap=limit * 3)
            rows = [r for r in rows
                    if r["src_jurisdiction"] == jurisdiction
                    and (not kind or r["src_kind"] == kind)][:limit]
            return {"stable_id": stable_id, "jurisdiction": jurisdiction,
                    "kind": kind, "incoming": rows}

    def _resolved_target(self, cat, cand: str | None, raw: str | None) -> str | None:
        """The held document a citation points to — by its candidate id, else by the alias
        its folded raw string maps to. The alias rung is where the report/parallel/
        legislation/EHRR matches live, so without it the reader shows every alias-resolved
        citation (a WLR linked to its neutral cite, a statute name → the Act) as unlinked."""
        if cand:
            hit = cat.find_document_id(cand)
            if hit:
                return hit
        if raw:
            from .core.text import fold

            dst = cat.get_alias(fold(raw))
            if dst:
                return cat.find_document_id(dst)
        return None

    def document_raw(self, stable_id: str) -> dict | None:
        """Path + extension of the stored ORIGINAL file (the raw bytes the document
        was ingested from — a guidance PDF, a styled BAILII page, Formex XML), for
        the reader's original-document pane. None when nothing is stored."""
        with self._open() as (cat, _rs, _ts):
            real = cat.find_document_id(stable_id) or stable_id
            doc = cat.get_document(real)
            if doc is None or not doc["raw_path"]:
                return None
            path = doc["raw_path"]
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else "bin"
            return {"path": path, "ext": ext, "title": doc["title"], "stable_id": real}

    def scan_citations(self, *, text: str, limit: int = 400) -> list[dict]:
        """Grammar-recognise citations in ARBITRARY text and resolve each against the
        corpus — the backend of the PDF viewer's text-layer linkification (the viewer
        sends each rendered page's text; matched spans become live links, exactly like
        the extracted-text reader), and a handy grammar testbed. Read-only."""
        from .citations import extract_citations

        out: list[dict] = []
        with self._open() as (cat, _rs, _ts):
            for c in extract_citations(text or "")[:limit]:
                resolved = self._resolved_target(cat, c.candidate_id, c.raw)
                out.append({
                    "char_start": c.char_start, "char_end": c.char_end, "raw": c.raw,
                    "candidate_id": c.candidate_id, "pinpoint": c.pinpoint,
                    "entity_kind": c.entity_kind, "resolved_id": resolved,
                    "state": "resolved" if resolved else ("pending" if c.candidate_id else "maybe"),
                })
        return out

    def document_body(self, stable_id: str) -> dict:
        """The document's extracted text + structural segments (§6b) for the reader.
        Segments carry kind/level so legislation renders as a hierarchy."""
        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(stable_id)
            if doc is None or not doc["payload_hash"]:
                return {"text": None, "segments": [], "doc_type": doc["doc_type"] if doc else None}
            ph = doc["payload_hash"]
            try:
                text = ts.get(ph)
            except OSError:
                text = None
            # Inline citations (JADE-style): each recognised reference with its exact
            # char span, resolved to its target document where we hold it, plus its
            # pinpoint — so the reader can wrap the matched text in a live link to the
            # cited authority (and deep-link to the pinpointed section).
            citations = []
            for c in cat.citations_for(stable_id):
                cand = c["candidate_id"]
                resolved = self._resolved_target(cat, cand, c["raw"])
                citations.append({
                    "char_start": c["char_start"], "char_end": c["char_end"],
                    "raw": c["raw"], "candidate_id": cand, "pinpoint": c["pinpoint"],
                    "entity_kind": c["entity_kind"], "resolved_id": resolved,
                    "method": c["method"],
                    # resolved | pending (have an id, not harvested) | maybe (a case
                    # reference with no resolvable id, e.g. a law-report citation)
                    "state": "resolved" if resolved else ("pending" if cand else "maybe"),
                })
            raw_path = doc["raw_path"]
            meta = _row_meta(doc)
            segments = ts.get_segments(ph)
            if not segments and text:
                # flat-text imports (Canadian A2AJ, BAILII long tail) carry their
                # paragraph numbers in the prose — synthesise segments so "[15]"
                # pinpoints land and peeks can scroll (quote-guarded)
                from .core.segmentation import synthesise_numbered_segments
                segments = synthesise_numbered_segments(text)
            segs = [asdict(s) for s in segments]
            # Legislation only: a section arrives as ONE segment whose body is
            # newline-separated provisions ("(1)…\n(2)…\n(a)…"). Recover the
            # drafting hierarchy so the reader can indent (a) under (2) instead of
            # ranging everything flush left. Judgments are flat numbered
            # paragraphs with no such nesting, so they're left alone.
            #
            # Computed PER SEGMENT, never across the whole document: each section
            # restarts its own numbering, so a stack carried across a section
            # boundary would read s.2(1) as a continuation of s.1's subsections.
            flat_lines = None
            if doc["doc_type"] == "legislation" and text:
                from .core.structure import line_depths

                def _spans(body: str, base: int) -> list[dict]:
                    return [{"start": base + a, "end": base + b, "depth": d}
                            for a, b, d in line_depths(body)]

                for s in segs:
                    body = text[s["char_start"]:s["char_end"]]
                    if "\n" in body:
                        s["lines"] = _spans(body, s["char_start"])
                # unsegmented legislation (flat-text imports) renders as one block,
                # which still wants indenting
                if not segs and "\n" in text:
                    flat_lines = _spans(text, 0)
            return {
                "text": text,
                "segments": segs,
                "lines": flat_lines,
                "citations": citations,
                "doc_type": doc["doc_type"],
                "title": doc["title"],
                "oscola": _oscola_cite(doc, meta),
                # the reader offers an "original" pane when the ingested file is stored
                "raw_ext": (raw_path.rsplit(".", 1)[-1].lower()
                            if raw_path and "." in raw_path else None),
                # a BAILII PDF-only stub: no transcript here, but a link to the original
                # PDF on bailii.org the reader can offer (source_url is the landing page)
                "external_pdf": meta.get("bailii_pdf_url"),
                "source_url": doc["landing_url"] or meta.get("bailii_url"),
            }

    # How the "See all mentions" tray orders citing documents. PageRank is the
    # default because raw citation counts flatter the merely-popular: a much-cited
    # first-instance decision outranks the Supreme Court judgment that settled the
    # point. The rest are there because the right order depends on the question —
    # "what's the leading authority" wants pagerank, "is this still live" wants
    # newest, "who engages with it most" wants passages.
    MENTION_SORTS = {
        "pagerank": "most authoritative",
        "cited": "most cited",
        "newest": "newest first",
        "oldest": "oldest first",
        "passages": "most passages",
    }

    def document_mentions(self, stable_id: str, *, anchor: str | None = None,
                          exact: bool = False, offset: int = 0, limit: int = 40,
                          snippet_docs: int = 40, max_groups: int = 120,
                          sort: str = "pagerank") -> dict:
        """Who mentions this document (and, optionally, one paragraph of it), grouped by the
        citing document and ranked by ``sort`` (default: the citer's own PageRank).

        Powers the reader's per-paragraph "Mentioned by …" line (``by_anchor``) and the
        "See all mentions" tray (``groups`` — each citing document with the passages, drawn
        from the citation's context span, where it cites this one, and its OSCOLA citation).
        Heuristic carry-forward (inferred) edges are excluded — they aren't citations.
        """
        with self._open() as (cat, _rs, ts):
            rels = [r for r in cat.relations_to(stable_id) if r["extracted_via"] != "inferred"]
            if anchor and exact:
                # A specific SUB-provision: the sub-paragraph mention badges want only the
                # documents pinned to exactly this pinpoint (Article 47(1)), not the whole
                # Article 47 family. Match on a whitespace/case-normalised anchor so
                # "Article 47(1)" and "article 47 (1)" coincide.
                def _norm(a: str | None) -> str:
                    return re.sub(r"\s+", "", (a or "")).lower()
                want = _norm(anchor)
                rels = [r for r in rels if _norm(r["dst_anchor"]) == want]
            elif anchor:
                # A provision heading represents its whole family. "Mentions of
                # Article 22" includes citations pinned to Article 22(1), 22(2), …;
                # exact string equality made the UI inherit whichever subparagraph
                # happened to appear first and hid the rest.
                parent = re.sub(r"(?:\([^()]+\))+\s*$", "", anchor).strip()
                family = re.compile(rf"^{re.escape(parent)}(?:\([^()]+\))*$", re.IGNORECASE)
                matched = [r for r in rels if family.match((r["dst_anchor"] or "").strip())]
                if not matched:
                    # The reader's "See all mentions" sends the whole SEGMENT LABEL
                    # ("Article 17 Right to erasure (right to be forgotten)") while
                    # edges pin to the bare unit ("Article 17", "Article 17(2)") —
                    # the title text made the exact family match find nothing, so the
                    # tray claimed nothing mentions a heavily-cited provision. Fall
                    # back to the canonical anchor key (the server-side mirror of the
                    # reader's own anchorKey()): unit type + number alone, which
                    # still keeps Article 17 distinct from Article 170 and from
                    # Recital 17.
                    key = _anchor_key(anchor)
                    if key:
                        matched = [r for r in rels
                                   if _anchor_key(r["dst_anchor"]) == key]
                rels = matched
            by_src: dict[str, list] = {}
            for r in rels:
                by_src.setdefault(r["src_id"], []).append(r)
            srcs = {sid: cat.get_document(sid) for sid in by_src}
            # rank citers by their own authority (occurrences in the citation-count roll-up)
            auth_ids: list[str] = []
            for sid, sdoc in srcs.items():
                auth_ids.append(sid)
                if sdoc and sdoc["ecli"]:
                    auth_ids.append(sdoc["ecli"])
            auth = cat.authority_counts(auth_ids)
            # PageRank for the same set, so the tray can rank by standing in the
            # citation network rather than by raw popularity
            pr_rows = cat.authority_for(auth_ids)

            def _authority(sid: str, sdoc) -> int:
                return max(auth.get(sid, 0), auth.get((sdoc["ecli"] or "") if sdoc else "", 0))

            def _pagerank(sid: str, sdoc) -> float:
                ecli = (sdoc["ecli"] or "") if sdoc else ""
                return max(float((pr_rows.get(sid) or {}).get("pagerank", 0.0) or 0.0),
                           float((pr_rows.get(ecli) or {}).get("pagerank", 0.0) or 0.0))

            groups = []
            for sid, rs in by_src.items():
                sdoc = srcs[sid]
                if not sdoc:
                    continue
                anchors = sorted({r["dst_anchor"] for r in rs if r["dst_anchor"]})
                groups.append({
                    "src_id": sid,
                    "src_oscola": _oscola_cite(sdoc, _row_meta(sdoc)),
                    "src_court": sdoc["court"], "src_date": sdoc["decision_date"],
                    # name the citing court and its jurisdiction, as the explorer does
                    "src_court_label": self.court_label(sdoc["court"], sdoc["source"]) if sdoc["court"] else None,
                    "src_jurisdiction": self._doc_bucket(sdoc["source"], sdoc["court"]),
                    "src_kind": self._doc_kind(sdoc["source"], sdoc["doc_type"], sdoc["court"]),
                    "authority": _authority(sid, sdoc), "count": len(rs),
                    "pagerank": _pagerank(sid, sdoc),
                    "anchors": anchors, "_rels": rs,
                })

            # ties always fall back to authority then count, so a sort key that is
            # absent for most rows (an undated document under "newest") degrades to
            # the default order rather than to arbitrary id order
            sort = sort if sort in self.MENTION_SORTS else "pagerank"

            def _year(g) -> int:
                d = str(g["src_date"] or "")[:4]
                return int(d) if d.isdigit() else 0

            _tie = lambda g: (-g["authority"], -g["count"], g["src_id"])  # noqa: E731
            keys = {
                "pagerank": lambda g: (-g["pagerank"], *_tie(g)),
                "cited": lambda g: _tie(g),
                "newest": lambda g: (-_year(g), *_tie(g)),
                "oldest": lambda g: (_year(g) or 9999, *_tie(g)),
                "passages": lambda g: (-g["count"], *_tie(g)),
            }
            groups.sort(key=keys[sort])
            # Legislative history is useful but qualitatively different from case-law
            # treatment. Keep it in a conditional, separately named section at the foot
            # of the mentions tray (and expose it to MCP clients), rather than intermixing
            # impact assessments and explanatory material with judgments.
            preparatory_groups = [g for g in groups if g["src_kind"] == "preparatory"]
            groups = [g for g in groups if g["src_kind"] != "preparatory"]

            # snippets (the passages where the top citers cite this) — from the citation's
            # stored context span, so we read each citer's text at most once. Computed for
            # the requested PAGE (offset:offset+limit) so the reader's "all mentions" tray
            # can lazy-load previews for every citer as it scrolls, not just the first page
            # (a heavily-cited authority used to show snippets for the first 40 and then a
            # long tail of preview-less rows). preparatory snippets ride the first page.
            total_groups = len(groups)
            page = groups[offset: offset + limit] if limit else groups[offset:]
            snippet_groups = [*page, *(preparatory_groups[:snippet_docs] if offset == 0 else [])]
            for g in snippet_groups:
                sdoc = srcs[g["src_id"]]
                text = None
                if sdoc and sdoc["payload_hash"]:
                    try:
                        text = ts.get(sdoc["payload_hash"])
                    except OSError:
                        text = None
                snippets = []
                if text:
                    for r in g["_rels"]:
                        cs, ce = r["context_start"], r["context_end"]
                        if cs is None:
                            continue
                        a = max(0, cs - 90)
                        b = min(len(text), (ce or cs) + 200)
                        # offsets of the citation itself within the snippet, so the
                        # tray can mark the words that actually made the connection
                        # ("Arbitration Act s 7"). context_start/end is the matched
                        # citation's own span, not a wider window.
                        window = text[a:b]
                        lead = len(window) - len(window.lstrip())
                        body = window.strip()
                        ms = min(max(0, cs - a - lead), len(body))
                        me = min(max(ms, (ce or cs) - a - lead), len(body))
                        # the anchor labels WHERE IN THE CITING DOCUMENT the passage
                        # sits, so the reader can place the quote. Never fall back to
                        # dst_anchor: that is the paragraph of the *cited* document the
                        # user just clicked, so every snippet would be labelled with
                        # the thing they already know.
                        snippets.append({"anchor": r["src_anchor"], "text": body,
                                         "start": cs,
                                         "mark": [ms, me] if me > ms else None,
                                         "raw": r["raw_citation_string"]})
                g["snippets"] = snippets[:8]
            for g in [*groups, *preparatory_groups]:
                g.pop("_rels", None)
                g.setdefault("snippets", [])

            # per-paragraph roll-up for the reader's inline "Mentioned by …" line
            by_anchor: dict[str, list] = {}
            for r in rels:
                lab = r["dst_anchor"]
                if not lab:
                    continue
                seen = by_anchor.setdefault(lab, {})
                if r["src_id"] not in seen:
                    sdoc = srcs.get(r["src_id"])
                    seen[r["src_id"]] = {
                        "src_id": r["src_id"],
                        "src_oscola": _oscola_cite(sdoc, _row_meta(sdoc)) if sdoc else None,
                        "authority": _authority(r["src_id"], sdoc),
                    }
            by_anchor = {lab: sorted(v.values(), key=lambda x: -x["authority"])
                         for lab, v in by_anchor.items()}
            end = (offset + limit) if limit else total_groups
            return {"target": stable_id, "anchor": anchor,
                    "total": total_groups, "groups": page,
                    "offset": offset, "limit": limit,
                    "has_more": end < total_groups,
                    # preparatory + the per-anchor rollup are whole-set summaries, so they
                    # ride the first page only (subsequent lazy-load pages stay light)
                    "preparatory_count": len(preparatory_groups),
                    "preparatory_groups": preparatory_groups[:max_groups] if offset == 0 else [],
                    "preparatory_note": (f"Preparatory documents exist for this item — "
                                         f"{len(preparatory_groups)} available."
                                         if preparatory_groups and offset == 0 else None),
                    "sort": sort, "sorts": dict(self.MENTION_SORTS),
                    "by_anchor": by_anchor if offset == 0 else {}}

    _STATUTE_KINDS = {"act", "regulation", "directive", "treaty", "eu_instrument"}

    def document_citations_out(self, stable_id: str, *, family: str = "cases") -> dict:
        """The distinct authorities this document cites, one OSCOLA-formatted row each, split
        into the ``cases`` and ``statute`` families (for the summary-line trays). Each row
        collapses that authority's pinpoints (paragraphs, articles, sections) into one list,
        and links to the held document where we hold it."""
        want_statute = family == "statute"
        with self._open() as (cat, _rs, _ts):
            seen: dict[str, dict] = {}
            for c in cat.citations_for(stable_id):
                ek = (c["entity_kind"] or "").lower()
                if not ek:
                    continue
                if (ek in self._STATUTE_KINDS) != want_statute:
                    continue
                cand = c["candidate_id"]
                key = cand or c["raw"]
                entry = seen.get(key)
                if entry is None:
                    resolved = self._resolved_target(cat, cand, c["raw"])
                    rdoc = cat.get_document(resolved) if resolved else None
                    entry = seen[key] = {
                        "candidate": cand, "raw": c["raw"], "resolved_id": resolved,
                        "oscola": _oscola_cite(rdoc, _row_meta(rdoc)) if rdoc else None,
                        "entity_kind": ek, "occurrences": 0, "_pins": set(),
                    }
                entry["occurrences"] += 1
                if c["pinpoint"]:
                    entry["_pins"].add(c["pinpoint"])
            items = []
            for e in seen.values():
                e["pinpoints"] = sorted(e.pop("_pins"))
                items.append(e)
            # held authorities first, then by how often this document cites them
            items.sort(key=lambda e: (e["resolved_id"] is None, -e["occurrences"]))
            return {"family": family, "total": len(items), "items": items}

    def list_documents(self, **filters) -> list[dict]:
        with self._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.list_documents(**filters)]
        # Enrich with the jurisdiction bucket + natural-language court name, so the
        # manual-match autocomplete can show a jurisdiction token per option (a UK case
        # citing an Irish Act needs the "Ireland" tag to be pickable with confidence).
        for r in rows:
            r["jurisdiction"] = self._doc_bucket(r.get("source", ""), r.get("court"))
            if r.get("court"):
                r["court_label"] = self.court_label(r["court"], r.get("source"))
        return rows

    # metadata filters the search accepts (everything else — sort/limit/offset/facets — is
    # handled separately, so an unknown key can't leak into the SQL builder)
    _SEARCH_FILTERS = ("source", "doc_type", "tag", "query", "court", "id_prefix",
                       "year_from", "year_to", "cites", "cited_by", "cites_pinpoint")

    def _citation_query_ids(self, cat, query: str) -> list[str]:
        """If the search text is itself a citation ("[2011] IESC 26", an ECLI, a report
        citation), the document id(s) it resolves to — via the citation grammar's candidate
        and the folded alias table — so search can match the exact document by id. Empty for
        an ordinary keyword query, which then falls through to the substring search."""
        q = (query or "").strip()
        if not q:
            return []
        from .core.text import fold
        ids: list[str] = []
        dst = cat.get_alias(fold(q))            # a report/neutral form stored as an alias
        if dst:
            ids.append(dst)
        try:
            from .citations import extract_citations
            for c in extract_citations(q):
                if c.candidate_id:
                    ids.append(c.candidate_id)  # the slug may itself be the stable_id
                    hit = cat.find_document_id(c.candidate_id)
                    if hit:
                        ids.append(hit)
        except Exception:  # noqa: BLE001 — never let citation parsing break search
            pass
        return list(dict.fromkeys(i for i in ids if i))

    def search_corpus(self, *, sort: str | None = None, limit: int = 50, offset: int = 0,
                      facets: bool = True, **filters) -> dict:
        """Unified metadata search: filtered, sortable results plus the facet distribution of
        the whole match set (counts per source / doc_type / court and a year histogram) so the
        sidebar can offer refine tick-boxes with live counts. Each result carries its OSCOLA
        citation and a cited-by count for display and 'most-cited' ranking."""
        f = {k: v for k, v in filters.items() if k in self._SEARCH_FILTERS and v not in (None, "")}
        with self._open() as (cat, _rs, _ts):
            # Citation-format query ("[2011] IESC 26", an ECLI, a report cite) → resolve to
            # the exact document id(s) and match by PK, instead of substring-scanning (the
            # id slug omits the brackets, so the trigram OR would miss it). Ordinary keyword
            # queries resolve to nothing and fall through to the fast title/id/ECLI search.
            if f.get("query"):
                ids = self._citation_query_ids(cat, f["query"])
                if ids:
                    f = {k: v for k, v in f.items() if k != "query"}
                    f["id_in"] = ids
            rows = cat.search_documents(sort=sort, limit=limit, offset=offset, **f)
            items = []
            for r in rows:
                d = dict(r)
                d["oscola"] = _oscola_cite(r, _row_meta(r))
                items.append(d)
            out = {"items": items, "total": cat.count_documents(**f),
                   "limit": limit, "offset": offset, "sort": sort or "date"}
            if facets:
                out["facets"] = cat.document_facets(**f)
            return out

    def corpus_facet_values(self) -> dict:
        """The available values for each advanced-search facet (sources, doc types, courts,
        tags) with counts — populates the field dropdowns / autocomplete."""
        with self._open() as (cat, _rs, _ts):
            return {
                "sources": [{"key": k, "n": v} for k, v in cat._count_by("source").items()],
                "doc_types": [{"key": k, "n": v} for k, v in cat._count_by("doc_type").items()],
                "courts": [{"key": r["k"], "n": r["n"]} for r in cat.distinct_courts()],
                "tags": [{"key": k, "n": v} for k, v in cat.tag_counts().items()],
                # the Westlaw/Lexis retrieval filter's bucket vocabulary
                "retrieval_jurisdictions": [{"key": k, "label": lb}
                                            for k, lb in RETRIEVAL_JURISDICTIONS],
            }

    def count_documents(self, **filters) -> dict:
        """Total documents matching the filters (for the Corpus page count/paging)."""
        filters.pop("limit", None)
        filters.pop("offset", None)
        with self._open() as (cat, _rs, _ts):
            return {"total": cat.count_documents(**filters)}

    def graph(self, stable_id: str, *, rel: list[str] | None = None) -> dict:
        with self._open() as (cat, _rs, _ts):
            exp = expand(cat, stable_id, relationship_types=rel, limit=25)
            return {
                "focus": stable_id,
                "neighbours": [
                    {"id": n.dst_id, "relationship_type": n.relationship_type,
                     "direction": n.direction, "title": n.title, "court": n.court,
                     "src_anchor": n.src_anchor, "dst_anchor": n.dst_anchor,
                     "extracted_via": n.extracted_via, "authority": n.authority}
                    for n in exp.neighbours
                ],
            }

    # -- citation-network statistics (design §3: the mentions-only graph) ----
    def rebuild_authority(self, *, on_progress=None, cancel_check=None) -> dict:
        """Recompute the PageRank authority roll-up (raw + age-decayed + percentile)
        over the resolved, non-inferred citation graph. Treatment types are NOT
        weighted — they aren't reliable yet. A batch job, like the citation-count
        rebuild; search fusion, ranked neighbours, the citator and 'sort by
        authority' all read the resulting ``doc_authority`` table."""
        with self._open() as (cat, _rs, _ts):
            n = cat.rebuild_authority(on_progress=on_progress)
        self._invalidate_caches()
        return {"documents": n}

    def related_documents(self, stable_id: str, *, limit: int = 12) -> dict:
        """"Related" via the citation network, not vectors (design §3b): documents
        most often cited *together with* this one (co-citation), and documents that
        rely on the same authorities (bibliographic coupling). Both are honest,
        cheap graph statistics; each row is labelled with why it's related."""
        def _compute():
            with self._open() as (cat, _rs, _ts):
                doc = cat.get_document(stable_id)
                ids = [stable_id] + ([doc["ecli"]] if doc and doc["ecli"] else [])
                out = {"co_cited": cat.co_cited_with(ids, limit=limit),
                       "coupled": cat.coupled_with(stable_id, limit=limit)}
                # enrich with titles/OSCOLA for display (bounded: 2×limit lookups)
                for rows in out.values():
                    for r in rows:
                        d = cat.get_document(r["id"]) or (
                            cat.get_document(cat.find_document_id(r["id"]) or "") if r["id"] else None)
                        r["title"] = d["title"] if d else None
                        r["court"] = d["court"] if d else None
                        r["date"] = str(d["decision_date"])[:10] if d and d["decision_date"] else None
                        r["oscola"] = _oscola_cite(d, _row_meta(d)) if d else None
                return out
        return self._cached(f"related:{stable_id}:{limit}", 300, _compute)

    def citator(self, stable_id: str) -> dict:
        """The "how does this authority stand" report an agent or the UI asks for
        first: citation volume + recency, network-authority percentile, the most
        significant citing documents, and (for legislation) version/effects state.
        Treatment counts are deliberately ABSENT — the classifier isn't reliable
        enough to present Shepard's-style signals yet (design §6c caveat)."""
        with self._open() as (cat, _rs, _ts):
            doc = cat.get_document(stable_id)
            if doc is None:
                return {"error": "not found", "stable_id": stable_id}
            ids = [stable_id] + ([doc["ecli"]] if doc["ecli"] else [])
            stats = cat.cited_by_stats(ids)
            auth = cat.authority_for([stable_id]).get(stable_id)
            citors = cat.top_citors(ids, limit=8)
            for c in citors:
                d = cat.get_document(c["id"])
                c["title"] = d["title"] if d else None
                c["oscola"] = _oscola_cite(d, _row_meta(d)) if d else None
                c["date"] = str(d["decision_date"])[:10] if d and d["decision_date"] else None
            out = {
                "stable_id": stable_id,
                "cited_by": stats,
                "cited_by_types": cat.cited_by_types(ids),
                "authority": {
                    "pagerank": auth["pagerank"] if auth else 0.0,
                    "pagerank_decayed": auth["pagerank_decayed"] if auth else 0.0,
                    "percentile": auth["percentile"] if auth else None,
                    "in_degree": auth["in_degree"] if auth else 0,
                } if auth else None,
                "most_significant_citors": citors,
                "treatments": None,  # joins when the treatment classifier is trustworthy
            }
            if doc["doc_type"] == "legislation":
                out["versions"] = [
                    {"version": v["version"], "archived_at": v["archived_at"]}
                    for v in cat.list_versions(stable_id)]
            return out

    # -- the agent's front door: resolve a citation, fetch it if we can, return it -----
    def lookup(self, *, citation: str, pincite: str | None = None, context: int = 1,
               cited_by: bool = True, similar: bool = True, autofetch: bool = True,
               full: bool = False) -> dict:
        """Resolve a citation (or a stable_id) and return one self-contained answer.

        This is the retrieval front door — it folds fetching in as a silent fallback rather
        than making the agent orchestrate resolve/harvest itself:

        * held already → the document's metadata + a short text PREVIEW and its structural
          outline (token-cheap by default); with ``pincite`` just that passage plus
          ``context`` neighbouring segments (0 = the pinpoint alone / 1 = some / 2 = lots),
          or with ``full`` the whole text (capped, use a pincite for anything targeted);
        * routable but not held, and ``autofetch`` → fetched SILENTLY from its source
          (CourtListener, Find Case Law, legislation.gov.uk, CELLAR, HUDOC…) then returned,
          so a case that is merely new to the corpus still comes back with its text;
        * not fetchable at all → the external LII / BAILII URL(s), so the agent can read or
          scrape it itself.

        Alongside the text it returns the ways this authority is cited (parallel citations
        and shorthands), who cites it (``cited_by``), and cocitation neighbours
        (``similar`` — "cases like this"), each of which the agent can then query in depth."""
        from .citations import extract_citations
        from .citations.snowball import _classify
        from .resolve.matchers import first_candidate

        raw = (citation or "").strip()
        if not raw:
            return {"error": "empty citation"}
        # 1. resolve to a candidate id — the citation as written, an ECLI/CELEX, or a slug
        cand: str | None = None
        hits = extract_citations(raw)
        if hits and hits[0].candidate_id:
            cand = hits[0].candidate_id
        if not cand:
            fc = first_candidate(raw)
            cand = fc.value if fc else None
        with self._open() as (cat, _rs, _ts):
            held_id = cat.find_document_id(cand) if cand else None
            if held_id is None and cand is None and ("/" in raw or ":" in raw):
                # maybe the agent passed a stable_id straight through
                if cat.get_document(raw) is not None:
                    held_id, cand = raw, raw
        form = adapter = None
        if cand:
            form, _juris, adapter = _classify(cand, "case")
        # 2. silent autofetch when routable but not held
        fetched = False
        if held_id is None and autofetch and cand and adapter is not None:
            try:
                hr = self.harvest_reference(ref=raw, candidate=cand)
            except Exception:  # noqa: BLE001 — a fetch failure just falls through to the URL
                hr = {}
            if hr.get("resolved") and hr.get("document"):
                held_id, fetched = hr["document"], True
        # 3a. held → the rich answer
        if held_id:
            return self._lookup_held(held_id, raw=raw, pincite=pincite, context=context,
                                     cited_by=cited_by, similar=similar, fetched=fetched,
                                     full=full)
        # 3b. not held → external links (the agent reads / scrapes it itself)
        links = self.reference_links(ref=cand or raw, raw=raw)
        bucket = _candidate_jurisdiction(cand) if cand else None
        return {
            "citation": raw, "candidate": cand, "held": False,
            "form": form, "routable": adapter is not None,
            "jurisdiction": dict(RETRIEVAL_JURISDICTIONS).get(bucket, bucket) if bucket else None,
            "autofetch_attempted": bool(autofetch and cand and adapter is not None),
            "external_links": links["links"],
            "note": ("Not held, and could not be fetched automatically — read it at one of "
                     "the external links (a free legal-information institute) and, if useful, "
                     "add it with the maintenance import tools."
                     if links["links"] else
                     "Not recognised as a routable citation — try search() by party name."),
        }

    # Token discipline (MCP best practice): never dump a whole judgment into context by
    # default. A preview orients the agent; a pincite quotes exactly; ``full`` is the
    # explicit, still-capped escape hatch. ~2.5k chars ≈ 600 tokens preview; ~48k ≈ 12k
    # tokens for a capped full read (well under the 25k-token tool-response ceiling).
    _LOOKUP_PREVIEW_CHARS = 2500
    _LOOKUP_FULL_CHARS = 48_000

    def _lookup_held(self, held_id: str, *, raw: str, pincite: str | None, context: int,
                     cited_by: bool, similar: bool, fetched: bool, full: bool = False) -> dict:
        """Assemble the held-document answer for :meth:`lookup`."""
        doc = self.get_document(held_id)
        d = doc.get("document", {}) or {}
        out: dict = {
            "held": True, "fetched_now": fetched, "stable_id": held_id,
            "queried_as": raw,
            "title": d.get("title"), "oscola": doc.get("oscola"),
            "jurisdiction": doc.get("jurisdiction"), "court": doc.get("court_label"),
            "date": str(d.get("decision_date"))[:10] if d.get("decision_date") else None,
            "doc_type": d.get("doc_type"), "source": doc.get("source_label"),
            # every way this authority is cited — parallel citations & shorthands
            "also_cited_as": doc.get("also_cited_as"),
            "cited_by_count": doc.get("cited_by_count"),
        }
        # text: the pincited passage (+ context scale), a capped full read, or — by
        # default — a short preview plus the structural outline, so the agent decides what
        # to pull rather than paying for the whole document up front.
        if pincite:
            out["pincite"] = pincite
            out["passage"] = self.get_provision(held_id, label=pincite, context=context)
        else:
            body = self.document_body(held_id)
            text = body.get("text") or ""
            segs = body.get("segments") or []
            out["segment_count"] = len(segs)
            if full:
                out["text"] = text[:self._LOOKUP_FULL_CHARS]
                if len(text) > self._LOOKUP_FULL_CHARS:
                    out["text_truncated"] = True
                    out["text_note"] = ("truncated — pincite a provision/paragraph for an "
                                        "exact, complete quote")
            else:
                out["text_preview"] = text[:self._LOOKUP_PREVIEW_CHARS]
                out["preview_truncated"] = len(text) > self._LOOKUP_PREVIEW_CHARS
                # the structural spine (headings / section & article labels), so the agent
                # can pincite the right provision without reading the body
                out["outline"] = [s.get("label") for s in segs
                                  if s.get("label") and s.get("kind") not in ("paragraph",)][:60]
                out["how_to_read"] = ("preview only — pass pincite='<label>' for one "
                                      "provision (with context 0/1/2), or full=true for the "
                                      "whole text")
        if cited_by:
            cit = self.citator(held_id)
            out["cited_by"] = {"stats": cit.get("cited_by"),
                               "significant": cit.get("most_significant_citors", [])[:8]}
        if similar:
            out["similar"] = self.related_documents(held_id, limit=8).get("co_cited", [])
        return out

    def holdings_overview(self) -> dict:
        """A dense, parsimonious snapshot of the corpus for an agent to orient itself in
        ONE call: per meaningfully-populated jurisdiction, how much case-law / legislation
        / guidance is HELD, and whether more can be FETCHED on demand (a live adapter). The
        balance of holdings to read before deciding what the corpus can be relied on for."""
        from .adapters.registry import SOURCE_INFO

        _REG_NAME = {"GB": "United Kingdom", "EU": "European Union", "US": "United States",
                     "IE": "Ireland", "AU": "Australia", "CA": "Canada", "NZ": "New Zealand",
                     "SG": "Singapore", "HK": "Hong Kong", "NL": "Netherlands",
                     "CoE": "Council of Europe"}
        fetch: dict[str, list[str]] = {}
        for si in SOURCE_INFO.values():
            fetch.setdefault(_REG_NAME.get(si.jurisdiction, si.jurisdiction), []).append(si.key)
        shape = self._shape_ready()
        rows = []
        for j in shape.get("jurisdictions", []):
            total = j.get("total", 0)
            if total < 1:
                continue
            rows.append({
                "jurisdiction": j["jurisdiction"],
                "held": {"cases": j.get("cases", 0), "legislation": j.get("legislation", 0),
                         "guidance": (j.get("guidance", 0) or 0) + (j.get("administrative", 0) or 0)},
                "total": total,
                "fetch_on_demand": sorted(fetch.get(j["jurisdiction"], [])),
            })
        rows.sort(key=lambda r: -r["total"])
        return {"jurisdictions": rows, "total_documents": shape.get("total", 0),
                "warming": bool(shape.get("_warming")),
                "note": "fetch_on_demand lists adapters that can pull MORE for that "
                        "jurisdiction on demand; an empty list means upload-only. Give a "
                        "citation to lookup() and it will fetch silently where it can."}

    def _shape_ready(self) -> dict:
        """The corpus shape, computed synchronously if the warmed cache is still cold — so
        the (infrequent) overview/jurisdictions tools never hand an agent an empty
        placeholder just because the background warm hasn't finished."""
        shape = self.corpus_shape()
        if not shape.get("jurisdictions") and shape.get("_warming"):
            shape = self._corpus_shape_uncached()
        return shape

    def jurisdictions(self) -> list[dict]:
        """The selectable jurisdictions for search/retrieval, each with its held-document
        count — the vocabulary the ``jurisdiction`` search filter accepts."""
        shape = self._shape_ready()
        return [{"jurisdiction": j["jurisdiction"], "documents": j.get("total", 0)}
                for j in shape.get("jurisdictions", []) if j.get("total", 0) > 0]

    def sources_for_jurisdiction(self, name: str) -> list[str]:
        """The corpus sources belonging to a jurisdiction bucket (its natural-language name
        as returned by :meth:`jurisdictions`), so a search can be scoped by jurisdiction."""
        want = (name or "").strip().lower()
        return [s for s in self._all_sources() if self._jurisdiction_of(s).lower() == want]

    def get_provision(self, stable_id: str, *, label: str | None = None,
                      char_start: int | None = None, char_end: int | None = None,
                      context: int = 1) -> dict:
        """ONE provision/paragraph of a document by its citable label ("Article 17",
        "s. 45", "[42]") or by a char span (a search hit), with ``context``
        neighbouring segments either side and the structural ancestor path
        (heading breadcrumb). The agent's most common need — quote one provision
        exactly — without shipping the whole document body; also the backend of
        the search UI's show-context expander."""
        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(stable_id)
            if doc is None or not doc["payload_hash"]:
                return {"error": "not found or no text", "stable_id": stable_id}
            try:
                text = ts.get(doc["payload_hash"])
            except OSError:
                return {"error": "text unavailable", "stable_id": stable_id}
            segs = ts.get_segments(doc["payload_hash"])
            if not segs:
                from .core.segmentation import synthesise_numbered_segments
                segs = synthesise_numbered_segments(text)
            idx = -1
            if label:
                idx = _match_segment(segs, label)
            elif char_start is not None:
                for i, s in enumerate(segs):
                    if s.char_start <= char_start < s.char_end:
                        idx = i
                        break
                else:
                    # offset in a gap between segments → the last segment starting before it
                    for i in range(len(segs) - 1, -1, -1):
                        if segs[i].char_start <= char_start:
                            idx = i
                            break
            if idx < 0 and segs:
                return {"error": "no matching segment", "stable_id": stable_id,
                        "labels_sample": [s.label for s in segs[:40] if s.label]}
            if not segs:
                lo = max(0, (char_start or 0) - 400)
                hi = min(len(text), (char_end or len(text)) + 400)
                return {"stable_id": stable_id, "title": doc["title"], "segments": [
                    {"label": None, "kind": "block", "level": 0, "focus": True,
                     "char_start": lo, "char_end": hi, "text": text[lo:hi]}], "path": []}
            lo, hi = max(0, idx - context), min(len(segs), idx + context + 1)
            out_segs = []
            for i in range(lo, hi):
                s = segs[i]
                out_segs.append({
                    "label": s.label, "kind": s.kind, "level": s.level,
                    "char_start": s.char_start, "char_end": s.char_end,
                    "focus": i == idx, "text": text[s.char_start:s.char_end].strip(),
                })
            # ancestor path: nearest preceding segments of strictly shallower level
            path: list[str] = []
            level = segs[idx].level
            for i in range(idx - 1, -1, -1):
                if segs[i].level < level and segs[i].label:
                    path.append(segs[i].label)
                    level = segs[i].level
                if level == 0:
                    break
            return {"stable_id": stable_id, "title": doc["title"],
                    "oscola": _oscola_cite(doc, _row_meta(doc)),
                    "segments": out_segs, "path": list(reversed(path))}

    def decide_suggestions(self, *, items: list[dict]) -> dict:
        """Bulk tick/cross over near-miss suggestions — each item
        ``{ref, suggested_id, accept}``. Decides every row with the resolver pass
        deferred, then resolves ONCE at the end (the whole point of batching)."""
        decided = 0
        accepted = 0
        errors: list[dict] = []
        for it in items:
            try:
                r = self.decide_suggestion(ref=it["ref"], suggested_id=it["suggested_id"],
                                           accept=bool(it.get("accept", True)), resolve=False)
                decided += r.get("updated", 0)
                if it.get("accept", True):
                    accepted += 1
            except Exception as exc:  # noqa: BLE001 — one bad row mustn't kill the batch
                errors.append({"ref": it.get("ref"), "error": str(exc)})
        out: dict = {"decided": decided, "accepted": accepted, "errors": errors}
        if accepted:
            out["resolved_edges"] = self.resolve().get("resolved")
        self._invalidate_caches()
        return out

    # source-key prefix → jurisdiction bucket for the Explore shape view. Order
    # matters (first match wins); anything unmatched lands in "Other".
    _JURISDICTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
        (("uk-", "bailii", "westlaw", "ofcom", "ico", "hol"), "United Kingdom"),
        (("eu-", "edpb", "a29wp", "dma", "cellar", "eur-lex"), "European Union"),
        (("echr",), "Council of Europe"),
        (("fr-",), "France"),
        (("de-",), "Germany"),
        (("nl-",), "Netherlands"),
        (("ie-", "eisb"), "Ireland"),
        (("au-",), "Australia"),
        (("ca-",), "Canada"),
        (("nz-",), "New Zealand"),
        (("sg-",), "Singapore"),
        (("hk-",), "Hong Kong"),
        (("in-",), "India"),
        (("us-",), "United States"),
    )

    # source key → the natural-language name a person recognises (and, where the
    # source has a public face, the label used for external links). Fallback:
    # prettified key.
    _SOURCE_LABELS = {
        "uk-caselaw": "Find Case Law", "uk-legislation": "legislation.gov.uk",
        "uk-hol": "House of Lords archive", "hol": "House of Lords archive",
        "bailii": "BAILII", "bailii-corpus": "BAILII", "bailii-html": "BAILII",
        "bailii-parquet": "BAILII", "westlaw": "Westlaw import",
        "westlaw-rtf": "Westlaw import", "ofcom": "Ofcom", "ofcom-osa": "Ofcom (OSA)",
        "ofcom-enforcement": "Ofcom enforcement", "ico": "ICO",
        "eu-cellar": "EUR-Lex (CJEU)", "eu-legislation": "EUR-Lex",
        "eu-preparatory": "EUR-Lex (EU preparatory & Commission policy documents)",
        "edpb": "EDPB", "edpb-oss": "EDPB one-stop-shop", "a29wp": "Article 29 WP",
        "dma-cases": "DMA case register", "echr": "HUDOC (ECtHR)",
        "nl-rechtspraak": "Rechtspraak.nl", "nl-legislation": "wetten.overheid.nl",
        "ie-legislation": "eISB (Ireland)", "ie-caselaw": "Irish courts",
        "au-caselaw": "Open Australian Legal Corpus", "au-legislation": "Federal Register (AU)",
        # A2AJ publish their own bulk corpus; it is not a CanLII scrape, so naming
        # CanLII here credited the wrong service. (CanLII *links* are unaffected —
        # those come from _HOST_LABELS, keyed on where a URL points.)
        "ca-caselaw": "A2AJ", "ca-legislation": "Justice Laws (Canada)",
        "nz-caselaw": "NZ courts", "nz-legislation": "NZ Legislation",
        "us-caselaw": "CourtListener",
        "sg-legislation": "Singapore Statutes Online", "hk-legislation": "HK e-Legislation",
        "in-caselaw": "Indian Kanoon", "user-import": "Manual imports",
        "fr-dila": "DILA open data", "fr-judilibre": "Judilibre",
        "fr-legislation": "Légifrance", "fr-conseil-etat": "Conseil d'État",
        "fr-cnil": "CNIL", "fr-constit": "Conseil constitutionnel",
        "de-gii": "Gesetze im Internet", "de-rii": "Rechtsprechung im Internet",
        "de-neuris": "NeuRIS", "de-neuris-legislation": "NeuRIS",
        "ci-caselaw": "Channel Islands", "offshore-caselaw": "Offshore courts",
        "uk-grc": "FTT (General Regulatory Chamber)",
    }
    # short tokens in a prettified slug are almost always initialisms — "uk-grc"
    # must read "UK GRC", never "Uk Grc"
    _ACRONYM_TOKENS = {"uk", "eu", "us", "hk", "nz", "sg", "nl", "ie", "ca", "au", "in",
                       "grc", "echr", "hol", "oss", "osa", "dma", "dsa", "ico", "rtf",
                       "html", "xml", "api", "sso", "frl", "a29wp", "fcl"}

    def source_label(self, source: str) -> str:
        if source in self._SOURCE_LABELS:
            return self._SOURCE_LABELS[source]
        words = (source or "").replace("_", "-").split("-")
        # `capitalize()` LOWERCASES everything after the first letter, so a value that
        # is already a proper name comes back mangled — "Court of Justice" (which is
        # what the corpus actually stores for CJEU judgments) rendered "Court of
        # justice". Only case a token that carries no capitals of its own.
        return " ".join(w.upper() if w.lower() in self._ACRONYM_TOKENS or len(w) <= 2
                        else w if any(c.isupper() for c in w)
                        else w.capitalize() for w in words if w)

    # An external link is labelled by WHERE IT POINTS, never by the source that
    # ingested the document. The two diverge constantly: 272k judgments carry
    # source "uk-caselaw" (the Find Case Law adapter) but a landing_url on
    # bailii.org, because FCL holds no copy and the adapter fell back to BAILII.
    # Labelling those "Find Case Law" sends the reader to the wrong service, and
    # in particular claims a National Archives provenance the text doesn't have.
    # Host wins; source is only the fallback when there is no URL to read.
    _HOST_LABELS = {
        # the LIIs — labelled as the LII, not as whatever adapter reached them
        "bailii.org": "BAILII", "austlii.edu.au": "AustLII",
        "canlii.org": "CanLII", "nzlii.org": "NZLII",
        "worldlii.org": "WorldLII", "commonlii.org": "CommonLII",
        "paclii.org": "PacLII", "saflii.org": "SAFLII", "asianlii.org": "AsianLII",
        # TNA: only ever a genuine Find Case Law scrape reaches this host
        "caselaw.nationalarchives.gov.uk": "National Archives",
        "legislation.gov.uk": "legislation.gov.uk",
        "publications.parliament.uk": "UK Parliament",
        "ofcom.org.uk": "Ofcom",
        "eur-lex.europa.eu": "EUR-Lex",
        "digital-markets-act-cases.ec.europa.eu": "DMA case register",
        "edpb.europa.eu": "EDPB", "ec.europa.eu": "European Commission",
        "hudoc.echr.coe.int": "HUDOC", "echr.coe.int": "HUDOC",
        "uitspraken.rechtspraak.nl": "Rechtspraak.nl",
        # Australia
        "caselaw.nsw.gov.au": "NSW Caselaw",
        "judgments.fedcourt.gov.au": "Federal Court of Australia",
        "eresources.hcourt.gov.au": "High Court of Australia",
        "legislation.gov.au": "Federal Register of Legislation",
        "legislation.tas.gov.au": "Tasmanian Legislation",
        "legislation.qld.gov.au": "Queensland Legislation",
        # Canada
        "bccourts.ca": "BC Courts", "courts.gov.bc.ca": "BC Courts",
        "decisions.scc-csc.ca": "Supreme Court of Canada",
        "decisions.fct-cf.gc.ca": "Federal Court of Canada",
        "decisions.fca-caf.gc.ca": "Federal Court of Appeal (Canada)",
        "coadecisions.ontariocourts.ca": "Ontario Court of Appeal",
        "decision.tcc-cci.gc.ca": "Tax Court of Canada",
        "decisions.sst-tss.gc.ca": "Social Security Tribunal (Canada)",
        "decisions.citt-tcce.gc.ca": "Trade Tribunal (Canada)",
        "decisions.fpslreb-crtespf.gc.ca": "Labour Board (Canada)",
        "decisions.chrt-tcdp.gc.ca": "Human Rights Tribunal (Canada)",
        "decisions.ct-tc.gc.ca": "Competition Tribunal (Canada)",
        "decisions.cmac-cacm.ca": "Court Martial Appeal Court (Canada)",
        "decisions.psdpt-tpfd.gc.ca": "Disclosure Protection Tribunal (Canada)",
        "oic-ci.gc.ca": "Information Commissioner (Canada)",
        "laws-lois.justice.gc.ca": "Justice Laws (Canada)",
        "decisia.lexum.com": "Lexum", "norma.lexum.com": "Lexum",
        "refugeelab.ca": "Refugee Law Lab",
        # rest of world
        "courtsofnz.govt.nz": "Courts of New Zealand",
        "elegislation.gov.hk": "HK e-Legislation",
        "sso.agc.gov.sg": "Singapore Statutes Online",
        "indian-supreme-court-judgments.s3.amazonaws.com": "Supreme Court of India",
        "legifrance.gouv.fr": "Légifrance", "courdecassation.fr": "Cour de cassation",
        "conseil-etat.fr": "Conseil d'État",
        "gesetze-im-internet.de": "Gesetze im Internet",
        "rechtsprechung-im-internet.de": "Rechtsprechung im Internet",
        "rechtsinformationen.bund.de": "NeuRIS",
    }

    def link_label(self, url: str | None, source: str | None = None) -> str | None:
        """Label for an outbound link, resolved from the URL's host so the reader
        is told which service they are actually being sent to. Falls back to the
        ingest source's label only when there is no URL host to read."""
        import re as _re

        m = _re.match(r"https?://([^/:]+)", (url or "").strip(), _re.I)
        if not m:
            return self.source_label(source) if source else None
        host = m.group(1).lower().removeprefix("www.")
        if host in self._HOST_LABELS:
            return self._HOST_LABELS[host]
        # match a registered parent domain ("bailii.org" covers any subdomain)
        for known, label in self._HOST_LABELS.items():
            if host.endswith("." + known):
                return label
        return host

    # registry annotations that are for citation-matching, not for humans
    _COURT_NOTE_RE = None

    def court_label(self, code: str, source: str | None = None) -> str:
        """Natural-language name for a court/body slug ('ukaitur' → 'Immigration
        & Asylum Tribunal'), from the citations court registry. CONVENTION: every
        court code a new adapter introduces must have a name in
        citations/courts.py — the UI renders these labels, never raw slugs, so an
        unnamed code shows up prettified-but-wrong until it's registered.

        ``source`` disambiguates the cross-jurisdiction code collisions the registry
        resolves by citation STYLE: "FCA" is the Federal Court of Australia when
        bracketed ([2020] FCA 1) and the Federal Court of Appeal of Canada when not
        (2020 FCA 1). A stored document has no brackets to read, but its source says
        which country it came from — so Canadian documents stop being labelled with
        Australian courts."""
        import re as _re

        from .citations.courts import classify, lookup

        if Facade._COURT_NOTE_RE is None:
            Facade._COURT_NOTE_RE = _re.compile(
                r"\s*\((?:BAILII legacy code|pre-\d{4}|unidentified)\)\s*$")
        low = (code or "").lower()
        if low == "euecj":
            return "Court of Justice (BAILII archive)"
        if low.startswith("dpa-"):
            cc = low[4:]
            if cc in self._DPA_PROPER_NAME:
                return self._DPA_PROPER_NAME[cc]
            country = self._DPA_COUNTRY.get(cc)
            # The country is deliberately part of the label: in a courts rail, thirty
            # rows all reading "Data Protection Authority" would be unusable. Surfaces
            # that print the jurisdiction alongside drop the duplicate themselves.
            return f"Data Protection Authority · {country}" if country \
                else "Data Protection Authority"
        # US CourtListener court-id slugs (scotus, ca9, cand…) aren't neutral-citation
        # court codes, so resolve them from the US map before the citation registry —
        # otherwise "scotus" prettifies to "Scotus".
        if (source or "").lower().startswith("us-"):
            from .citations.us_cases import us_court_name

            name = us_court_name(low)
            if name:
                return name
        # bracketless-citation jurisdictions (Canada, US) vs bracketed (AU, NZ, UK)
        src = (source or "").lower()
        hint = False if src.startswith(("ca-", "ca/")) else True if src.startswith(
            ("au-", "nz-", "uk-")) else None
        up = (code or "").upper()
        c = (lookup(up, bracketed=hint) if hint is not None else None) \
            or lookup(up) or classify(up)
        if c and c.name:
            return Facade._COURT_NOTE_RE.sub("", c.name)
        return self.source_label(code)

    def _jurisdiction_of(self, source: str) -> str:
        s = (source or "").lower()
        for prefixes, label in self._JURISDICTIONS:
            if any(s.startswith(p) or s == p.rstrip("-") for p in prefixes):
                return label
        return "Other"

    # National regulators' decisions (EDPB one-stop-shop, court = dpa-xx) belong to
    # their own COUNTRY, not to "European Union" where the register happens to live.
    _DPA_COUNTRY = {
        "ie": "Ireland", "se": "Sweden", "fr": "France", "lu": "Luxembourg",
        "at": "Austria", "de": "Germany", "es": "Spain", "it": "Italy",
        "nl": "Netherlands", "be": "Belgium", "pl": "Poland", "pt": "Portugal",
        "dk": "Denmark", "fi": "Finland", "no": "Norway", "gr": "Greece",
        "el": "Greece", "cz": "Czechia", "hu": "Hungary", "ro": "Romania",
        "bg": "Bulgaria", "hr": "Croatia", "sk": "Slovakia", "si": "Slovenia",
        "lt": "Lithuania", "lv": "Latvia", "ee": "Estonia", "cy": "Cyprus",
        "mt": "Malta", "is": "Iceland", "li": "Liechtenstein",
    }
    # Regulators whose output is ADMINISTRATIVE DECISIONS — a kind of its own, not
    # case law and not guidance. Extend as bodies join (Scottish Information
    # Commissioner, Irish DPC's pre-GDPR decisions, state privacy commissioners…).
    _ADMIN_SOURCES = {"edpb-oss", "ofcom-enforcement", "ico"}

    # A DPA the corpus knows by its proper name — shown instead of the generic
    # "Data protection authority · <country>". The `iedpc` court code (BAILII's
    # Irish DPC case studies) is canonicalised to `dpa-ie` at write time
    # (Catalogue._COURT_CANON), so this one label covers both intake paths.
    _DPA_PROPER_NAME = {"ie": "Data Protection Commission (Ireland)"}

    def _doc_bucket(self, source: str, court: str | None) -> str:
        c = (court or "").lower()
        if c.startswith("dpa-"):
            return self._DPA_COUNTRY.get(c[4:], "European Union")
        return self._jurisdiction_of(source)

    def _doc_kind(self, source: str, doc_type: str, court: str | None) -> str:
        # GUIDANCE wins first: a regulator's guidance is guidance, not an
        # "administrative decision", even though it comes from an admin source (ICO,
        # EDPB) — otherwise guidance never appears as its own filter category.
        if doc_type == "guidance":
            return "guidance"
        if doc_type == "preparatory":
            return "preparatory"
        # then an administrative body's DECISIONS (a DPA decision, an enforcement
        # notice) — before the case-type check, since those carry doc_type "decision"
        if (court or "").lower().startswith("dpa-") or source in self._ADMIN_SOURCES:
            return "administrative"
        if doc_type in self._CASE_TYPES:
            return "cases"
        if doc_type == "legislation":
            return "legislation"
        return "other"

    def corpus_shape(self) -> dict:
        """The Explore homepage's data: the whole corpus's shape in one payload —
        per JURISDICTION (bucketed from sources): document counts split by kind,
        the year distribution (a sparkline per row), text/embedding coverage,
        citation density, top courts, and the most authoritative documents
        (PageRank). Drill-down targets are ids, not prefilled searches — the UI
        expands in place. Heavy aggregates → stale-while-revalidate cached."""
        return self._cached("corpus-shape", 600, self._corpus_shape_uncached,
                            placeholder={"jurisdictions": [], "total": 0})

    _CASE_TYPES = ("judgment", "decision", "opinion")

    def _corpus_shape_uncached(self) -> dict:
        with self._open() as (cat, _rs, _ts):
            # EVERYTHING scan-shaped on this page reads an hourly roll-up. The live
            # versions — two full documents scans (46s + 32s cold at 4.9M docs) plus
            # a relations×documents GROUP BY (minutes) plus a per-document taxonomy
            # pass (~6 min) — ran inside every cache warm and kept the Explore
            # homepage on its empty placeholder. A DB whose roll-ups have never been
            # built (fresh install, tests) seeds them live once.
            rows = cat.corpus_shape_stats()
            if not rows:
                cat.refresh_corpus_shape_stats()
                rows = cat.corpus_shape_stats()
            dens = cat.source_stats()
            if not dens:
                cat.refresh_source_stats()
                dens = cat.source_stats()
            # the courts facet is a projection of the same roll-up rows
            court_agg: dict[tuple, int] = {}
            for r in rows:
                if r["court"]:
                    k = (r["source"], r["court"], r["doc_type"])
                    court_agg[k] = court_agg.get(k, 0) + r["n"]
            courts = [{"source": s, "court": c, "doc_type": dt, "n": n}
                      for (s, c, dt), n in court_agg.items()]

            juris: dict[str, dict] = {}

            _KINDS = ("cases", "legislation", "guidance", "administrative", "preparatory")

            def _blank_slice() -> dict:
                return {"years": {}, "courts": {}, "sources": {}}

            def _bucket_named(j: str) -> dict:
                return juris.setdefault(j, {
                    "jurisdiction": j, "total": 0, "cases": 0, "legislation": 0,
                    "guidance": 0, "administrative": 0, "preparatory": 0, "other": 0,
                    "with_text": 0, "embedded": 0,
                    "years": {}, "sources": {}, "citations": 0, "courts": {},
                    # per-kind rail data: selecting a kind in the drill re-scopes
                    # the timeline, courts/bodies and sources too
                    "kinds": {k: _blank_slice() for k in _KINDS}})

            def _bucket(source: str, court: str | None = None) -> dict:
                return _bucket_named(self._doc_bucket(source, court))

            for r in rows:
                b = _bucket(r["source"], r["court"])
                n = r["n"]
                b["total"] += n
                b["with_text"] += r["with_text"] or 0
                b["embedded"] += r["embedded"] or 0
                b["sources"][r["source"]] = b["sources"].get(r["source"], 0) + n
                kind = self._doc_kind(r["source"], r["doc_type"], r["court"])
                # .get() so a kind _doc_kind learns before this dict does can NEVER
                # crash the whole homepage again — "preparatory" did exactly that:
                # one unknown kind → KeyError inside the silent cache warm → Explore
                # served its empty placeholder for days.
                b[kind] = b.get(kind, 0) + n
                ks = b["kinds"].get(kind)
                if ks is not None:
                    ks["sources"][r["source"]] = ks["sources"].get(r["source"], 0) + n
                yr = r["yr"]
                if yr and yr.isdigit() and 1200 <= int(yr) <= 2100:
                    b["years"][yr] = b["years"].get(yr, 0) + n
                    if ks is not None:
                        ks["years"][yr] = ks["years"].get(yr, 0) + n
            for src, n in dens.items():
                _bucket(src)["citations"] += n
            for r in courts:
                b = _bucket(r["source"], r["court"])
                b["courts"][r["court"]] = b["courts"].get(r["court"], 0) + r["n"]
                ks = b["kinds"].get(self._doc_kind(r["source"], r["doc_type"], r["court"]))
                if ks is not None:
                    ks["courts"][r["court"]] = ks["courts"].get(r["court"], 0) + r["n"]

            # top authority per jurisdiction: one indexed pass over the roll-up
            top_auth = cat.conn.execute(
                "SELECT d.*, a.pagerank, a.percentile "
                "FROM doc_authority a JOIN documents d ON d.stable_id = a.doc_id "
                "ORDER BY a.pagerank DESC LIMIT 400").fetchall()
            for r in top_auth:
                b = _bucket(r["source"], r["court"])
                lst = b.setdefault("top_authority", [])
                if len(lst) < 5:
                    lst.append({
                        "id": r["stable_id"], "title": r["title"], "doc_type": r["doc_type"],
                        "date": str(r["decision_date"])[:10] if r["decision_date"] else None,
                        "percentile": r["percentile"],
                        "oscola": _oscola_cite(r, _row_meta(r)),
                    })

            # report series (WLR, AC, …) are neither courts nor bodies — keep them
            # out of the facet even if an import wrote one into the court column
            from .citations.reporters import REPORT_SERIES
            _SERIES = {s.upper() for s in REPORT_SERIES}

            # Legislation TYPES per jurisdiction — the same taxonomy the Unresolved
            # page uses. Read from the leg_type_stats roll-up: the classification is
            # a per-document Python pass, and running it inline grew from seconds at
            # 122k legislation rows to ~6 MINUTES at 1.9M (French LEGI) — inside
            # every homepage cache warm. The roll-up is rebuilt hourly with
            # citation_counts; a small/fresh corpus (tests, dev) seeds it live.
            leg_rows = cat.leg_type_stats()
            if not leg_rows and cat.legislation_count() <= 200_000:
                self._refresh_leg_type_stats(cat)
                leg_rows = cat.leg_type_stats()
            for r in leg_rows:
                b = _bucket(r["source"])
                ks = b["kinds"]["legislation"]
                t = ks.setdefault("types", {}).setdefault(
                    r["label"], {"n": 0, "years": {}, "filters": []})
                t["n"] += r["n"]
                for yr, n in json.loads(r["years_json"] or "{}").items():
                    t["years"][yr] = t["years"].get(yr, 0) + n
                for filt in json.loads(r["filters_json"] or "[]"):
                    if filt not in t["filters"] and len(t["filters"]) < 16:
                        t["filters"].append(filt)

            def _finish(slice_: dict) -> None:
                # the slice's dominant source disambiguates the cross-jurisdiction
                # court-code collisions (a "FCA" inside a Canadian slice is the
                # Federal Court of Appeal, not the Federal Court of Australia)
                srcs = slice_.get("sources") or {}
                hint = max(srcs, key=srcs.get) if isinstance(srcs, dict) and srcs else None
                slice_["courts"] = sorted(
                    ({"court": c, "label": self.court_label(c, hint), "n": n}
                     for c, n in slice_["courts"].items()
                     if c.upper() not in _SERIES),
                    key=lambda x: -x["n"])[:12]
                if "types" in slice_:  # legislation taxonomy rail
                    slice_["types"] = sorted(
                        ({"label": lbl, **t} for lbl, t in slice_["types"].items()),
                        key=lambda x: -x["n"])[:14]
                slice_["sources"] = sorted(
                    ({"source": s, "label": self.source_label(s), "n": n}
                     for s, n in slice_["sources"].items()),
                    key=lambda x: -x["n"])

            out = []
            for b in sorted(juris.values(), key=lambda x: -x["total"]):
                b["density"] = round(b["citations"] / b["total"], 1) if b["total"] else 0
                _finish(b)
                for ks in b["kinds"].values():
                    _finish(ks)
                b.setdefault("top_authority", [])
                b.pop("citations", None)
                out.append(b)
            return {"jurisdictions": out, "total": sum(b["total"] for b in out)}

    _DRILL_SORTS = {
        "authority": "pagerank DESC, cited_by DESC, d.decision_date DESC",
        "cited": "cited_by DESC, pagerank DESC, d.decision_date DESC",
        "newest": "d.decision_date DESC, pagerank DESC",
        "oldest": "d.decision_date ASC, pagerank DESC",
    }

    # administrative decisions = regulator output: OSS register rows (court dpa-xx)
    # or a registered admin source. Must be excluded from "cases" so DPA decisions
    # never masquerade as case law.
    _ADMIN_CLAUSE = "(d.court LIKE 'dpa-%' OR d.source IN ('edpb-oss', 'ofcom-enforcement', 'ico'))"

    @staticmethod
    def _drill_key(jurisdiction: str, court: str | None, kind: str | None,
                   leg: str | None, sort: str, limit: int) -> str:
        return f"drill:{jurisdiction}|{court or ''}|{kind or ''}|{leg or ''}|{sort}|{limit}"

    def jurisdiction_drill(self, jurisdiction: str, *, court: str | None = None,
                           kind: str | None = None, year_from: str | None = None,
                           year_to: str | None = None, cites: str | None = None,
                           leg: str | None = None,
                           sort: str = "authority", limit: int = 25) -> dict:
        """One drill-down step inside Explore: the top documents of a slice
        (jurisdiction × optional court × kind × year range), ranked by the chosen
        sort (network authority / most cited / newest / oldest) — plus, for
        legislation, what hangs off each instrument. ``cites`` flips the panel to
        the documents CITING that target (the clickable cited-by drill), same
        facets and sorts. Each item carries availability (text/pdf) and its
        source's public link + label for the external-link affordance.

        All-time slices (no year brush, no cited-by target) are what every Explore
        click lands on first, and their answer only changes when the corpus does —
        so they are served stale-while-revalidate (the UI polls ``_warming`` on a
        cold key) and pre-warmed at startup. A year-brushed or cited-by drill
        stays a live query: the year filter narrows the scan, and the key space
        (any doc × any range) is far too big to cache usefully."""
        if not cites and not year_from and not year_to:
            key = self._drill_key(jurisdiction, court, kind, leg, sort, limit)
            return self._cached(
                key, 3600,
                lambda: self._drill_uncached(jurisdiction, court=court, kind=kind,
                                             leg=leg, sort=sort, limit=limit),
                placeholder={"jurisdiction": jurisdiction, "court": court,
                             "kind": kind, "sort": sort, "items": []},
                sync_wait=2.0)
        return self._drill_uncached(jurisdiction, court=court, kind=kind,
                                    year_from=year_from, year_to=year_to,
                                    cites=cites, leg=leg, sort=sort, limit=limit)

    def _drill_uncached(self, jurisdiction: str, *, court: str | None = None,
                        kind: str | None = None, year_from: str | None = None,
                        year_to: str | None = None, cites: str | None = None,
                        leg: str | None = None,
                        sort: str = "authority", limit: int = 25) -> dict:
        sources = [s for s in self._all_sources() if self._jurisdiction_of(s) == jurisdiction] \
            if jurisdiction else []
        # a DPA-country bucket (Sweden, France…) has no sources of its own: its
        # documents live in the OSS register under court dpa-xx
        dpa_codes = [c for c, name in self._DPA_COUNTRY.items() if name == jurisdiction]
        order = self._DRILL_SORTS.get(sort, self._DRILL_SORTS["authority"])
        with self._open() as (cat, _rs, _ts):
            clauses: list[str] = []
            params: list = []
            if sources and dpa_codes:
                qs = ",".join("?" * len(sources))
                ds = ",".join("?" * len(dpa_codes))
                clauses.append(f"(d.source IN ({qs}) OR d.court IN ({ds}))")
                params.extend(sources)
                params.extend(f"dpa-{c}" for c in dpa_codes)
            elif sources:
                clauses.append("d.source IN (%s)" % ",".join("?" * len(sources)))
                params.extend(sources)
            elif dpa_codes:
                clauses.append("d.court IN (%s)" % ",".join("?" * len(dpa_codes)))
                params.extend(f"dpa-{c}" for c in dpa_codes)
            # legislation-type filter: the taxonomy's own filter dicts (whitelisted
            # keys only), OR-ed — "Secondary · UK-wide" = uksi OR uksro OR …
            if leg:
                import json as _json
                ors: list[str] = []
                try:
                    filts = _json.loads(leg)
                except ValueError:
                    filts = []
                for filt in filts[:20]:
                    ands: list[str] = []
                    if filt.get("source"):
                        ands.append("d.source = ?")
                        params_add = [filt["source"]]
                    else:
                        params_add = []
                    if filt.get("id_prefix"):
                        ands.append("d.stable_id LIKE ?")
                        params_add.append(filt["id_prefix"].replace("%", "") + "/%")
                    if filt.get("doc_type"):
                        ands.append("d.doc_type = ?")
                        params_add.append(filt["doc_type"])
                    if filt.get("court"):
                        ands.append("d.court = ?")
                        params_add.append(filt["court"])
                    if filt.get("celex_kind") in ("R", "L", "D"):
                        ands.append("substr(d.stable_id, 6, 1) = ?")
                        params_add.append(filt["celex_kind"])
                    if ands:
                        ors.append("(" + " AND ".join(ands) + ")")
                        params.extend(params_add)
                if ors:
                    clauses.append("(" + " OR ".join(ors) + ")")
            if cites:
                tdoc = cat.get_document(cites)
                tids = [cites] + ([tdoc["ecli"]] if tdoc and tdoc["ecli"] else [])
                clauses.append(
                    "EXISTS (SELECT 1 FROM relations r WHERE r.src_id = d.stable_id "
                    f"AND r.dst_id IN ({','.join('?' * len(tids))}) "
                    "AND r.resolution_status = 'resolved' AND r.extracted_via <> 'inferred' "
                    "AND r.src_id <> r.dst_id)")
                params.extend(tids)
            if court:
                clauses.append("d.court = ?")
                params.append(court)
            if kind == "cases":
                clauses.append("d.doc_type IN ('judgment', 'decision', 'opinion')")
                clauses.append(f"NOT {self._ADMIN_CLAUSE}")
            elif kind == "administrative":
                clauses.append(self._ADMIN_CLAUSE)
            elif kind:
                clauses.append("d.doc_type = ?")
                params.append(kind)
            if year_from:
                clauses.append("d.decision_date >= ?")
                params.append(f"{year_from}-01-01")
            if year_to:
                clauses.append("d.decision_date <= ?")
                params.append(f"{year_to}-12-31")
            if not clauses:
                return {"items": []}
            rows = cat.conn.execute(
                f"""
                SELECT d.*, COALESCE(a.pagerank, 0) AS pagerank, a.percentile,
                       -- cited_by = DISTINCT citing documents on the resolved graph
                       -- (alias-aware: report citations funnel in), falling back to
                       -- the string roll-up for docs outside the authority table.
                       -- The roll-up alone showed ICS [1997] UKHL 28 as "cited by 30"
                       -- when 558 documents cite it via its WLR/AC report forms.
                       COALESCE(a.in_degree,
                                (SELECT MAX(cc.occurrences) FROM citation_counts cc
                                 WHERE cc.candidate_id IN (d.stable_id, d.ecli)), 0) AS cited_by
                FROM documents d LEFT JOIN doc_authority a ON a.doc_id = d.stable_id
                WHERE {' AND '.join(clauses)}
                ORDER BY {order}
                LIMIT ?
                """, (*params, limit)).fetchall()
            # one batched aggregate for every legislation row's "what hangs off it"
            hanging = cat.cited_by_types_by_id(
                [r["stable_id"] for r in rows if r["doc_type"] == "legislation"])
            items = []
            for r in rows:
                raw_path = r["raw_path"] or ""
                item = {
                    "id": r["stable_id"], "title": r["title"], "doc_type": r["doc_type"],
                    "court": r["court"],
                    "court_label": self.court_label(r["court"], r["source"]) if r["court"] else None,
                    "date": str(r["decision_date"])[:10] if r["decision_date"] else None,
                    "percentile": r["percentile"], "cited_by": r["cited_by"],
                    "oscola": _oscola_cite(r, _row_meta(r)),
                    # availability: full text / original pdf only / metadata only
                    "has_text": bool(r["has_text"]),
                    "pdf": raw_path.rsplit(".", 1)[-1].lower() == "pdf" if "." in raw_path else False,
                    "url": r["landing_url"],
                    "source_label": self.link_label(r["landing_url"], r["source"]),
                }
                if r["doc_type"] == "legislation":
                    item["hanging"] = hanging.get(r["stable_id"], {})
                items.append(item)
            out: dict = {"jurisdiction": jurisdiction, "court": court, "kind": kind,
                         "sort": sort, "items": items}
            if cites:
                tdoc = cat.get_document(cites)
                out["cites"] = {"id": cites,
                                "oscola": _oscola_cite(tdoc, _row_meta(tdoc)) if tdoc else None,
                                "title": tdoc["title"] if tdoc else cites}
            return out

    def _all_sources(self) -> list[str]:
        def _compute():
            with self._open() as (cat, _rs, _ts):
                return {"sources": [r["k"] for r in cat.conn.execute(
                    "SELECT DISTINCT source AS k FROM documents").fetchall()]}
        return self._cached("all-sources", 600, _compute)["sources"]

    # "1999/468/EC: Council Decision of 28 June 1999 laying down the procedures…"
    # — the title line old EUR-Lex HTML pages carry near the top. Matched against
    # the first ~3k chars of the text projection.
    _EU_TITLE_RE = None  # compiled lazily

    def backfill_eu_stubs(self, *, limit: int = 500, on_progress=None,
                          cancel_check=None) -> dict:
        """Re-fetch EU instruments held only as metadata stubs, so heavily-cited
        acts stop being dead ends.

        An instrument becomes a stub when NEITHER Formex nor the EUR-Lex HTML came
        back at harvest time — but that includes every transient failure, and
        nothing ever retried them: ~7,400 eu-legislation records sit at
        ``metadata_only``, some (31987D0373, cited 45 times) with a perfectly good
        HTML rendition upstream the whole time. Re-running the adapter's fetch
        upgrades the ones that now parse and leaves the genuinely-absent alone.

        Non-destructive and re-runnable: a stub that still yields nothing is left
        exactly as it was.
        """
        from .adapters.eu_legislation import CELEX_BASE, EULegislationAdapter
        from .core.models import Stub
        from .pipeline import Pipeline
        from .pipeline.runner import RunStats

        checked = upgraded = 0
        with self._open() as (cat, rs, ts):
            # Select on has_text = 0, NOT on the meta_json marker: an older
            # generation of stubs (bare CELEX title, meta_json NULL — 31970L0156
            # among them) carried no marker at all, so the marker-LIKE selection
            # could never see the very rows most in need of repair. Textless IS the
            # condition being repaired; most-cited first so the pass spends itself
            # on the instruments the corpus actually leans on.
            rows = cat.conn.execute(
                "SELECT d.stable_id, d.landing_url FROM documents d "
                "LEFT JOIN citation_counts cc ON cc.candidate_id = d.stable_id "
                "WHERE d.source = 'eu-legislation' AND d.has_text = 0 "
                "ORDER BY COALESCE(cc.occurrences, 0) DESC, d.stable_id LIMIT ?",
                (limit,)).fetchall()
            if not rows:
                return {"checked": 0, "upgraded": 0}
            adapter = EULegislationAdapter()
            pipe = Pipeline(cat, rs, textstore=ts)
            for r in rows:
                if cancel_check and cancel_check():
                    break
                checked += 1
                if on_progress and checked % 25 == 0:
                    on_progress(stage="eu stubs", done=checked, total=len(rows))
                celex = r["stable_id"]
                stub = Stub(
                    stable_id=celex,
                    landing_url=r["landing_url"]
                    or f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                    raw_url=f"{CELEX_BASE}/{celex}",
                )
                try:
                    rec = adapter.fetch(stub)
                except Exception:  # noqa: BLE001 — one bad instrument must not stop the pass
                    continue
                # still a stub upstream: leave the existing record untouched
                if rec is None or not rec.text or (rec.extra or {}).get("metadata_only"):
                    continue
                if pipe._ingest(rec, RunStats(source=adapter.source)):
                    upgraded += 1
        self._invalidate_caches()
        return {"checked": checked, "upgraded": upgraded}

    def backfill_eu_titles(self, *, limit: int = 2000, on_progress=None) -> dict:
        """Construct titles for EU instruments that have none (or a bare CELEX
        echo) from their own scraped text — the '31999D0468 has no title but the
        HTML plainly states it' fix. Non-destructive: only fills empty/echo
        titles, recorded as a system backfill, re-runnable."""
        import re as _re
        from pathlib import Path

        if Facade._EU_TITLE_RE is None:
            Facade._EU_TITLE_RE = _re.compile(
                r"^\s*(\d{4}/\d{1,4}/(?:EC|EEC|EU|JHA|CFSP|Euratom)\s*:\s*[^\n]{15,400})$"
                r"|^\s*((?:Council |Commission )?(?:Regulation|Directive|Decision)\s*"
                r"\((?:EC|EEC|EU|Euratom)\)\s*No\s*\d+/\d+[^\n]{15,400})$",
                _re.MULTILINE)
        done = fixed = 0
        # a "title" that merely echoes the instrument number ("Decision 468/1999")
        # is as good as none — the scraped page states the real one
        echo = _re.compile(r"^(?:Regulation|Directive|Decision)\s*(?:\((?:EC|EEC|EU)\)\s*)?"
                           r"(?:No\.?\s*)?\d+/\d+$", _re.IGNORECASE)
        with self._open() as (cat, _rs, ts):
            rows = [r for r in cat.conn.execute(
                "SELECT stable_id, title, payload_hash FROM documents "
                "WHERE source IN ('eu-legislation', 'eu-cellar') AND has_text = 1 "
                "AND doc_type IN ('legislation', 'decision') "
                "AND (title IS NULL OR title = '' OR title = stable_id "
                "     OR LENGTH(title) < 40) LIMIT ?",
                (limit,)).fetchall()
                if not r["title"] or r["title"] == r["stable_id"] or echo.match(r["title"])]
            tag = _re.compile(r"<[^>]+>")
            for r in rows:
                done += 1
                if on_progress and done % 100 == 0:
                    on_progress(stage="eu titles", done=done, total=len(rows))
                m = None
                try:
                    m = Facade._EU_TITLE_RE.search(ts.get(r["payload_hash"])[:3000])
                except OSError:
                    pass
                if not m:
                    # the text projection often strips the page header — the title
                    # line then lives only in the RAW HTML (the 31999D0468 case)
                    doc = cat.get_document(r["stable_id"])
                    raw_path = doc["raw_path"] if doc else None
                    if raw_path:
                        try:
                            raw_head = Path(raw_path).read_bytes()[:12000].decode("utf-8", "ignore")
                            m = Facade._EU_TITLE_RE.search(tag.sub("\n", raw_head))
                        except OSError:
                            m = None
                if not m:
                    continue
                title = " ".join((m.group(1) or m.group(2)).split())
                cat.update_document_fields(r["stable_id"], {"title": title}, curate=False)
                fixed += 1
        self._invalidate_caches()
        return {"scanned": done, "titled": fixed}

    def repair_led_context(self, *, on_progress=None) -> dict:
        """Re-apply the LED acronym guard to STORED citations: a bare 'LED' match
        without a preceding "the/of" is prose ("EVIDENCE LED AT TRIAL"), not
        Directive 2016/680. The anachronism repair caught the pre-2016 slice;
        this catches the post-2016 false matches by re-reading each span's
        context. When a document loses its last real LED citation, its dependent
        2016/680 edges and carry-forward children go too. One-off, re-runnable."""
        import re as _re

        guard = _re.compile(r"(?i)\b(?:the|of)\s+$")
        with self._open() as (cat, _rs, ts):
            rows = cat.conn.execute(
                "SELECT citation_id, src_id, char_start FROM citations "
                "WHERE raw = 'LED' AND method = 'eu_named'").fetchall()
            by_doc: dict[str, list] = {}
            for r in rows:
                by_doc.setdefault(r["src_id"], []).append(r)
            deleted = kept = 0
            cleared_docs: list[str] = []
            for i, (sid, items) in enumerate(by_doc.items()):
                if on_progress and i % 200 == 0:
                    on_progress(stage="LED context", done=i, total=len(by_doc))
                doc = cat.get_document(sid)
                try:
                    text = ts.get(doc["payload_hash"]) if doc and doc["payload_hash"] else None
                except OSError:
                    text = None
                bad_ids = []
                doc_kept = 0
                for r in items:
                    s = r["char_start"]
                    ok = bool(text) and s is not None and guard.search(text[max(0, s - 12):s])
                    if ok:
                        doc_kept += 1
                    else:
                        bad_ids.append(r["citation_id"])
                if bad_ids:
                    qs = ",".join("?" * len(bad_ids))
                    cat.conn.execute(f"DELETE FROM citations WHERE citation_id IN ({qs})", bad_ids)
                    deleted += len(bad_ids)
                kept += doc_kept
                if doc_kept == 0:
                    # nothing real remains: drop the carry-forward children + edges
                    cat.conn.execute(
                        "DELETE FROM citations WHERE src_id = ? AND candidate_id = '32016L0680'",
                        (sid,))
                    cat.conn.execute(
                        "DELETE FROM relations WHERE src_id = ? AND extracted_via IN ('regex', 'inferred') "
                        "AND (dst_id = '32016L0680' OR candidate_id = '32016L0680')", (sid,))
                    cleared_docs.append(sid)
            cat.conn.commit()
        self._invalidate_caches()
        return {"docs_checked": len(by_doc), "false_led_deleted": deleted,
                "kept": kept, "docs_fully_cleared": len(cleared_docs)}

    def run_probes(self, *, only: list[str] | None = None) -> list[dict]:
        """Corpus-integrity probes (§8): invariant checks over the citation
        network — mis-carried pinpoints, self-edges, kind mismatches, broken
        resolution invariants — each with a count + violating samples."""
        from .ops.probes import run_probes

        with self._open() as (cat, _rs, _ts):
            return [p.to_dict() for p in run_probes(cat, only=only)]

    def repair_probe(self, name: str) -> dict:
        """Run the targeted repair matched to a repairable probe. Read the
        probe's samples first — repairs delete rows (bounded to the probe's own
        matching set) and are re-runnable."""
        from .ops.probes import run_repair

        with self._open() as (cat, _rs, _ts):
            out = run_repair(cat, name)
        self._invalidate_caches()
        return out

    def stats(self) -> dict:
        def _compute():
            with self._open() as (cat, _rs, _ts):
                return corpus_stats(cat).to_dict()
        return self._cached("stats", 30, _compute)

    def sources(self) -> list[dict]:
        with self._open() as (cat, _rs, _ts):
            return [s.to_dict() for s in source_dashboard(cat)]

    def queues(self) -> dict:
        # Counting across relations/documents is a second of scanning; the dashboard polls
        # it. Serve it stale-while-revalidate like the other aggregates.
        def _compute():
            with self._open() as (cat, _rs, _ts):
                return pipeline_queues(cat)
        return self._cached("queues", 30, _compute)

    def us_caselaw_budget(self) -> dict:
        """CourtListener's remaining free-tier quota, plus what is queued against it.

        US case law is the one source with a hard *daily* ceiling (125 requests on the
        free tier), so "how much is left today" is operational information rather than
        trivia: it is the difference between a queue that is stalled and one that is
        merely waiting. ``pending_us_references`` is the backlog the drip is working
        through — with the day's allowance beside it, an operator can see that the
        queue has, say, 900 cases and four days of quota ahead of it.
        """
        from .adapters.courtlistener import queue_reserve
        from .adapters.registry import get_adapter

        status = get_adapter("us-caselaw").budget_status()
        pending = sum(1 for r in self.unresolved_references(limit=None)
                      if r["suggested_adapter"] == "us-caselaw")
        allowance = status["queue_allowance"]
        day_limit = (status["windows"].get("day") or {}).get("limit")
        # A case costs one request per opinion in its cluster; 2 is a fair average
        # across SCOTUS + circuits (a lead opinion, sometimes a separate one).
        per_case = 2
        # Both projections are None when there is no daily cap: without one the queue
        # is paced by the minute/hour windows instead, and "days to clear" would be a
        # confident number derived from a limit that doesn't exist.
        daily_cases = None if allowance is None else allowance // per_case
        days_to_clear = None
        if pending and day_limit:
            cases_per_day = max(1, (day_limit * queue_reserve()) / per_case)
            days_to_clear = round(pending / cases_per_day, 1)
        return {
            **status,
            "queue_reserve": queue_reserve(),
            "pending_us_references": pending,
            "estimated_cases_today": daily_cases,
            "estimated_days_to_clear": days_to_clear,
        }

    def canlii_budget(self) -> dict:
        """The CanLII key's remaining quota, plus what is queued against it.

        Two queues spend this budget: the routable worklist's pending Canadian
        citations (each a targeted stub fetch, ~4 requests with the citator) and the
        held-document enrichment backlog (``canlii_enrich``, ~3-4 requests per case).
        Both are reported so the operator can see how many days of quota the work
        ahead represents."""
        from .adapters.registry import get_adapter

        status = get_adapter("ca-canlii").budget_status()
        pending = sum(1 for r in self.unresolved_references(limit=None)
                      if r["suggested_adapter"] == "ca-canlii")
        with self._open() as (cat, _rs, _ts):
            # the queue is "how many are left", so probe one page above the UI's
            # display need rather than counting the whole corpus every poll
            unenriched = len(cat.canadian_unenriched_documents(limit=100_000))
        day_limit = (status["windows"].get("day") or {}).get("limit")
        per_case = 4        # metadata + citedCases + citedLegislations + citingCases
        days_to_clear = None
        total = pending + unenriched
        if total and day_limit:
            days_to_clear = round(total / max(1, day_limit / per_case), 1)
        return {
            **status,
            "pending_ca_references": pending,
            "unenriched_documents": unenriched,
            "estimated_days_to_clear": days_to_clear,
        }

    def canlii_enrich(self, *, limit: int = 200, include_citing: bool = True,
                      on_progress=None, cancel_check=None) -> dict:
        """Decorate held Canadian decisions with what the CanLII API knows (§1.9, §5b).

        For each un-checked Canadian judgment (most-cited first): the canlii.ca
        permalink + verified long URL, docket number, subject keywords/topics, the
        citator counts, parallel-citation aliases (so "[2008] 1 SCR 190" resolves
        here), the CanLII-number alias (so ``canlii/1980/21`` citations land on the
        held full-text node), and the citator's edges — cited cases and legislation as
        ``mentions``, citing cases as deferred ``cited_by`` (capped: see the adapter).

        Every case is stamped ``canlii_checked_at`` whether the lookup hit or missed,
        so re-runs walk forward through the backlog instead of re-asking. Stops
        cleanly when the budget ledger says stop — the rest of the queue is simply
        next run's work."""
        from .adapters.registry import get_adapter
        from .adapters.canlii import parse_ca_ref, ca_slug
        from .core.errors import RateLimitException

        adapter = get_adapter("ca-canlii")
        if not adapter.configured:
            return {"error": "ca-canlii: no API key — set RAGLEX_CANLII_API_KEY",
                    "enriched": 0}
        enriched, missing, edges_added, aliases_added = [], 0, 0, 0
        rate_limited = False
        with self._open() as (cat, _rs, _ts):
            rows = cat.canadian_unenriched_documents(limit=limit)
            for i, row in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                sid = row["stable_id"]
                _progress(on_progress, stage="CanLII enrich", done=i, total=len(rows),
                          item=sid)
                if parse_ca_ref(sid) is None or sid.startswith("canlii/"):
                    # surrogate / bare-CanLII ids can't be looked up (no database);
                    # stamp them so they leave the queue rather than clogging its head
                    meta = cat.document_meta(sid)
                    meta["canlii_checked_at"] = _today_iso()
                    meta["canlii_missing"] = True
                    cat.set_document_meta(sid, meta, commit=False)
                    missing += 1
                    continue
                try:
                    found = adapter.case_metadata(sid)
                    rels, counts = ([], {})
                    if found:
                        rels, counts = adapter.citator_relations(
                            found["_database"], str(found.get("caseId")),
                            exclude=sid, include_citing=include_citing)
                except RateLimitException:
                    # budget spent — stop the batch, leave the queue for the next run
                    rate_limited = True
                    _progress(on_progress, stage="rate limited — pausing", done=i,
                              total=len(rows), msg="CanLII budget spent; resuming next run")
                    break
                meta = cat.document_meta(sid)
                meta["canlii_checked_at"] = _today_iso()
                if not found:
                    meta["canlii_missing"] = True
                    cat.set_document_meta(sid, meta, commit=False)
                    missing += 1
                    continue
                meta.pop("canlii_missing", None)
                meta.update({k: v for k, v in {
                    "canlii_url": found.get("url"),
                    "canlii_long_url": found.get("longUrl"),
                    "canlii_database": found.get("_database"),
                    "canlii_case_id": found.get("caseId"),
                    "docket_number": found.get("docketNumber"),
                    "keywords": meta.get("keywords") or found.get("keywords"),
                    "topics": meta.get("topics") or found.get("topics"),
                    **counts,
                }.items() if v not in (None, "")})
                cat.set_document_meta(sid, meta, commit=False,
                                      title_if_empty=found.get("title"))
                # parallel report citations + the CanLII number both resolve here
                from .adapters.ca_caselaw import report_aliases
                for alias in report_aliases(found.get("citation")):
                    if cat.get_alias(alias.casefold()) is None:
                        cat.put_alias(alias.casefold(), sid, source="canlii", commit=False)
                        aliases_added += 1
                parsed = parse_ca_ref(str(found.get("caseId") or ""))
                if parsed and parsed[0] == "canlii":
                    canlii_id = ca_slug(*parsed)
                    if canlii_id != sid and cat.get_alias(canlii_id) is None:
                        cat.put_alias(canlii_id, sid, source="canlii", commit=False)
                        aliases_added += 1
                # citator edges, deduped against what this doc already carries (the
                # A2AJ import ships its own cases_cited edges; never double-mint)
                if rels:
                    existing = set()
                    for r in cat.relations_for(sid):
                        key = (r["candidate_id"] or r["raw_fold"] or "")
                        existing.add((r["relationship_type"], key.casefold()))
                    fresh = []
                    for rel in rels:
                        cand, raw_fold = cat._edge_keys(rel)
                        key = (str(rel.relationship_type), (cand or raw_fold or "").casefold())
                        if key[1] and key not in existing:
                            existing.add(key)
                            fresh.append(rel)
                    if fresh:
                        cat.add_relations(sid, fresh)
                        edges_added += len(fresh)
                enriched.append(sid)
                if i % 25 == 0:
                    cat.commit()
            cat.commit()
            if enriched:
                _progress(on_progress, stage="resolving citations", done=0,
                          total=len(enriched))
                resolved = Resolver(cat).run_for_documents(enriched,
                                                           cancel_check=cancel_check)
                resolved_edges = resolved.resolved
            else:
                resolved_edges = 0
        self._invalidate_caches()
        return {"checked": len(enriched) + missing, "enriched": len(enriched),
                "not_on_canlii": missing, "edges_added": edges_added,
                "aliases_added": aliases_added, "resolved_edges": resolved_edges,
                "rate_limited": rate_limited,
                "remaining": max(0, len(rows) - len(enriched) - missing)}

    def alerts(self) -> list[dict]:
        with self._open() as (cat, _rs, _ts):
            return [a.to_dict() for a in check_alerts(cat)]

    def push_alerts(self, *, seen: set | None = None) -> list[dict]:
        """Compute alerts and push the NEW ones to the configured notifier (webhook, else
        the log). ``seen`` carries the (code, subject) pairs already notified, so a
        standing condition — "this source has been stale for 40 days" — is announced once
        rather than every scheduler tick. Returns only what was pushed."""
        from .ops.alerts import default_notifier

        notifier = default_notifier()
        pushed = []
        with self._open() as (cat, _rs, _ts):
            alerts = check_alerts(cat)
        live = {(a.code, a.subject) for a in alerts}
        for alert in alerts:
            key = (alert.code, alert.subject)
            if seen is not None and key in seen:
                continue
            notifier.notify(alert)
            if seen is not None:
                seen.add(key)
            pushed.append(alert.to_dict())
        if seen is not None:  # a condition that cleared may be announced again if it returns
            seen.intersection_update(live)
        return pushed

    def worklist(self, *, limit: int = 50) -> list[dict]:
        def _compute():
            with self._open() as (cat, _rs, _ts):
                return {"rows": resolution_worklist(cat, limit=limit)}
        return self._cached(f"worklist:{limit}", 60, _compute)["rows"]

    def snowball(self, *, limit: int = 50, only_unharvestable: bool = False) -> list[dict]:
        """The citation frontier (§5a): forms the corpus cites but doesn't yet
        hold, grouped by (form, jurisdiction, adapter) and ranked by frequency.
        ``only_unharvestable=True`` narrows to forms with no adapter — the
        build-an-adapter list."""
        from .citations import snowball

        def _compute():
            with self._open() as (cat, _rs, _ts):
                return {"rows": snowball(cat, limit=limit, only_unharvestable=only_unharvestable)}
        # The frontier is a corpus-wide roll-up; it doesn't move between page loads.
        return self._cached(f"snowball:{limit}:{only_unharvestable}", 300, _compute)["rows"]

    def refresh_statute_gazetteer(self) -> dict:
        """Top up the statute gazetteer from the legislation.gov.uk feeds (current +
        previous year) into the data-dir extra list — so newly passed acts confirm by
        name without a package release. Run weekly by the scheduler; cheap and no-op
        when nothing new has been enacted."""
        from .citations.statute_gazetteer import refresh_from_feeds

        n = refresh_from_feeds(self.config.data_dir / "statutes_extra.lst")
        return {"added": n}

    def rebuild_citation_counts(self) -> dict:
        """Refresh the snowball's frequency roll-up (scheduler; ~13s over 10M citations)."""
        with self._open() as (cat, _rs, _ts):
            n = cat.rebuild_citation_counts()
            # same cadence, same shape of work: every roll-up the Explore homepage
            # reads instead of live full-table scans and aggregates
            srcs = cat.refresh_source_stats()
            shape = cat.refresh_corpus_shape_stats()
            leg = self._refresh_leg_type_stats(cat)
        self._invalidate_caches()
        return {"candidates": n, "sources": srcs, "shape_rows": shape, "leg_types": leg}

    def _refresh_leg_type_stats(self, cat) -> int:
        """Rebuild the legislation-type rail roll-up (the Explore drill's
        Primary/Secondary/Assimilated/… split with year histograms + drill filters).

        This is the classification pass that used to run inline in every homepage
        cache warm — ~6 minutes at 1.9M legislation rows. Here it streams the rows
        once on the hourly counts cadence, memoising the (pure) classification on a
        16-char id prefix per (source, court): ids sharing that prefix classify
        identically under every current grammar (slug heads and the CELEX descriptor
        letter all fall inside it), which collapses 1.9M classify calls to a few
        thousand."""
        from .citations.taxonomy import classify_document

        _REGULARISE = {
            ("ca-legislation", "act"): "Primary · Federal",
            ("ca-legislation", "regulation"): "Secondary · Federal",
            ("nz-legislation", "public"): "Primary",
            ("nz-legislation", "secondary-legislation"): "Secondary",
            ("hk-legislation", "cap"): "Ordinances",
            ("hk-legislation", "instrument"): "Constitutional instruments",
            ("sg-legislation", "act"): "Acts",
            ("sg-legislation", "sl"): "Subsidiary legislation",
        }
        _CELEX_LETTER = {"reg": "R", "dir": "L", "dec": "D"}
        memo: dict[tuple, tuple] = {}
        agg: dict[tuple, dict] = {}
        for r in cat.conn.execute(
                "SELECT stable_id, source, court, substr(decision_date, 1, 4) AS yr "
                "FROM documents WHERE doc_type = 'legislation'"):
            key = (r["source"], r["court"], r["stable_id"][:16])
            hit = memo.get(key)
            if hit is None:
                tax = classify_document(source=r["source"], doc_type="legislation",
                                        court=r["court"], stable_id=r["stable_id"])
                label = _REGULARISE.get((tax.category, tax.subtype), tax.subtype_label)
                filt = dict(tax.filter)
                if tax.category == "eu-legislation" and tax.subtype in _CELEX_LETTER:
                    filt["celex_kind"] = _CELEX_LETTER[tax.subtype]
                hit = memo[key] = (label, filt)
            label, filt = hit
            t = agg.setdefault((r["source"], label),
                               {"n": 0, "years": {}, "filters": []})
            t["n"] += 1
            yr = r["yr"]
            if yr and yr.isdigit() and 1200 <= int(yr) <= 2100:
                t["years"][yr] = t["years"].get(yr, 0) + 1
            if filt not in t["filters"] and len(t["filters"]) < 16:
                t["filters"].append(filt)
        rows = [(source, label, t["n"], json.dumps(t["years"]),
                 json.dumps(t["filters"]))
                for (source, label), t in agg.items()]
        return cat.replace_leg_type_stats(rows)

    def system_storage(self) -> dict:
        """Database disk footprint for the Maintain page (catalog lookups, instant)."""
        with self._open() as (cat, _rs, _ts):
            return cat.storage_size()

    def backfill_edge_keys(self, *, on_progress=None, cancel_check=None) -> dict:
        """One-off: populate candidate_id/raw_fold on edges written before those columns
        existed, so the set-based resolver and the SQL worklist see the whole graph."""
        with self._open() as (cat, _rs, _ts):
            done = cat.backfill_edge_keys(on_progress=on_progress)
        self._invalidate_caches()
        return {"strings_backfilled": done}

    def retry_failed_references(self) -> dict:
        """Clear the harvest cool-down lists so the next drain re-attempts everything.
        The escape hatch for a poisoned skip-list — a bad afternoon at a source used to
        write thousands of live documents off for three months."""
        with self._open() as (cat, _rs, _ts):
            cat.clear_enrichment_misses("harvest-miss")
            cat.clear_enrichment_misses("harvest-retry")
        self._invalidate_caches()
        return {"cleared": True}

    def coverage(self) -> dict:
        """A completeness/uncertainty dashboard for the corpus (§8): per-source
        counts + date spans + text coverage, the citation-resolution rate, how many
        references are still hanging (what we *know* we're missing), and the top
        frontiers the corpus keeps citing but doesn't hold (the snowball). The data
        an academic needs to judge "is my dataset complete for this area, and what's
        the uncertainty about what exists?"."""
        # never block: serve a "warming" placeholder on the first cold call (scanning
        # >1M pending edges takes seconds) while it computes in the background.
        return self._cached("coverage", 90, self._coverage_uncached, placeholder={
            "stats": None, "sources": [], "hanging_references": None,
            "routable_references": None, "frontier": [], "hanging_sample": []})

    def _coverage_uncached(self) -> dict:
        with self._open() as (cat, _rs, _ts):
            base = corpus_stats(cat).to_dict()
            sources = [dict(r) for r in cat.source_date_ranges()]
        # snowball + unresolved open their own connections (separate methods)
        frontier = self.snowball(limit=10)
        # uncapped: count EVERY distinct hanging reference (the grouping is built in full
        # regardless of limit, so this is no extra cost) — the headline number must not
        # plateau at an arbitrary cap.
        hanging = self.unresolved_references(limit=None)
        low_conf = [h for h in hanging if h["confidence"] == "low"]
        # The TRUE count of one-click-harvestable references (distinct docs we could
        # fetch), as opposed to the frontier's *occurrence* counts (one instrument can
        # be cited hundreds of times) — so the "Harvest all routable (N)" button can
        # show the real total instead of only what a page happens to have loaded.
        routable = [h for h in hanging if h["suggested_adapter"]
                    and h["confidence"] != "low" and not h["needs_identifier"]]
        # How many routable references a drain would actually attempt right now. The rest
        # are cooling off after an earlier failure — the difference between these two
        # numbers is the whole explanation for a "Harvest all" that appears to do nothing.
        import os as _os
        miss_ttl = float(_os.environ.get("RAGLEX_MISS_TTL_DAYS") or 90)
        retry_ttl_days = float(_os.environ.get("RAGLEX_RETRY_TTL_HOURS") or 6) / 24.0
        with self._open() as (cat, _rs, _ts):
            absent_keys = cat.enrichment_misses("harvest-miss", max_age_days=miss_ttl)
            retry_keys = cat.enrichment_misses("harvest-retry", max_age_days=retry_ttl_days)
        cooled = absent_keys | retry_keys
        ready = [h for h in routable if h["candidate"] not in cooled]
        # routable counts broken down by source, and UK legislation by primary/secondary/
        # assimilated — so the worklist can show "Harvest all (N)" per category. Counted
        # over the READY set so the per-category buttons promise only what they can do.
        from collections import Counter
        by_cat: Counter = Counter()
        for h in ready:
            by_cat[h["suggested_adapter"]] += 1
            if h["suggested_adapter"] == "uk-legislation" and h.get("leg_kind"):
                by_cat[f"uk-legislation:{h['leg_kind']}"] += 1
        return {
            "stats": base,
            "sources": sources,
            "hanging_references": len(hanging),
            "low_confidence_references": len(low_conf),
            "needs_identifier": sum(1 for h in hanging if h["needs_identifier"]),
            "routable_references": len(routable),
            "ready_references": len(ready),
            "cooling_off": len(routable) - len(ready),
            "cooling_off_absent": sum(1 for h in routable if h["candidate"] in absent_keys),
            "cooling_off_retry": sum(1 for h in routable if h["candidate"] in retry_keys),
            "routable_by_category": dict(by_cat),
            "frontier": frontier,
            "hanging_sample": hanging[:10],
        }

    def unresolved_references(self, *, limit: int | None = 100,
                             with_citing: bool = False) -> list[dict]:
        """The hanging references the corpus can't satisfy — one row per distinct
        reference, ranked by how often it's cited. Each says what it *looks like*
        (form/jurisdiction/suggested adapter), how confidently it was recognised,
        whether it still needs an identifier (recognised by name only, no candidate),
        and which documents cite it — the data a human or agent needs to resolve it
        by upload / scrape / link / supplying the missing citation (§5b, §5a).

        The grouping is one SQL GROUP BY over the persisted ``candidate_id``; the
        per-reference citing-document list costs a query each, so it's only filled for
        the rows a human will actually look at (``with_citing``)."""
        from .citations.snowball import ECHR_APPNO_RE, _classify, uk_leg_category as _uk_leg_category
        from .citations.taxonomy import classify_candidate
        from .adapters.bailii import bailii_url as _bailii_url

        with self._open() as (cat, _rs, _ts):
            import os as _os
            miss_ttl = float(_os.environ.get("RAGLEX_MISS_TTL_DAYS") or 90)
            retry_ttl = float(_os.environ.get("RAGLEX_RETRY_TTL_HOURS") or 6) / 24.0
            absent = cat.enrichment_misses("harvest-miss", max_age_days=miss_ttl)
            retry = cat.enrichment_misses("harvest-retry", max_age_days=retry_ttl)
            rows = []
            for g in cat.pending_reference_groups():
                ref = g["ref"]
                if not ref or _is_junk_ref(ref):
                    continue
                cand = g["candidate"]
                methods = sorted((g["methods"] or "").split(",")) if g["methods"] else []
                if cand:
                    form, juris, adapter = _classify(cand, "case")
                    needs_identifier = False
                else:
                    form, juris, adapter = "unidentified (name only)", None, None
                    needs_identifier = True
                # A bare "115/92" is an ECtHR application number in a Strasbourg judgment
                # and an old CJEU case number everywhere else — the shape alone cannot tell
                # them apart. Route it to HUDOC only if something Strasbourg-shaped cites
                # it; otherwise it is a guess, and guesses must not drive auto-harvest.
                misrouted_appno = (
                    adapter == "echr" and cand and ECHR_APPNO_RE.match(cand)
                    and not g["echr_citing"]
                )
                # low confidence: no candidate, an LLM-surfaced reference, a form we can't
                # route to an adapter, OR a fuzzy name-based ECHR match (keep these out of
                # auto-harvest — a HUDOC docname guess wants a human's eye).
                low = (needs_identifier or "llm" in methods or adapter is None
                       or misrouted_appno
                       or (cand or "").lower().startswith("echr:"))
                cooling_reason = ("source reported absent" if cand in absent else
                                  "temporary retrieval failure" if cand in retry else None)
                tax = classify_candidate(cand or "", "" if cand else "case")
                rows.append({
                    "ref": ref, "candidate": cand, "raw": g["raw"],
                    "pinpoint": g["anchor"], "form": form, "jurisdiction": juris,
                    "suggested_adapter": adapter, "needs_identifier": needs_identifier,
                    "category": tax.category,
                    "cooling": cooling_reason is not None,
                    "cooling_reason": cooling_reason,
                    # UK legislation sub-category, so the worklist can filter/harvest
                    # primary vs secondary vs assimilated separately
                    "leg_kind": _uk_leg_category(cand) if adapter == "uk-legislation" else None,
                    "confidence": "low" if low else "ok",
                    "methods": methods,
                    "citing_count": g["citing_count"],
                    "citing_documents": [],
                    # BAILII link: for UK case-law that 404s on TNA, provide a direct
                    # download link so the user can grab the RTF and drop it in manually.
                    "bailii_url": _bailii_url(cand) if adapter == "uk-caselaw" and cand else None,
                })
            rows.sort(key=lambda r: (r["citing_count"], r["confidence"] == "low"), reverse=True)
            out = rows if limit is None else rows[:limit]
            sugg = cat.suggestions_for([r["ref"] for r in out])
            for r in out:
                r["suggestions"] = sugg.get(r["ref"], [])
            if with_citing:
                citing = cat.citing_documents_for([r["ref"] for r in out])
                for r in out:
                    r["citing_documents"] = citing.get(r["ref"], [])
            return out

    # A "most-cited" panel can never surface a reference cited once, and 70% of the
    # ~517k hanging references are — so they are filtered in SQL rather than regex-
    # classified in Python and then thrown away. The export path overrides this
    # (it legitimately wants the long tail), which is why it's a parameter.
    _UNFETCHABLE_MIN_CITING = 2

    def unfetchable_references(self, *, limit: int = 200,
                               min_citing: int | None = None) -> dict:
        """The **most-cited references the system cannot fetch** — the pre-neutral-citation
        frontier (§5). Distinct from the routable worklist: these have no adapter route at
        all — a classic law report ("[1982] AC 1"), a case cited only by name, or a court
        with no adapter. Each is ranked by how often the corpus reaches for it and carries
        a BAILII link (a direct RTF where a neutral citation exists, else a citation
        search) plus whether an upload can resolve it in place.

        This is the answer to "what heavily-cited authority am I missing that I'll have to
        source by hand?" — the thing a completeness-minded corpus most needs to surface."""
        floor = self._UNFETCHABLE_MIN_CITING if min_citing is None else max(1, int(min_citing))
        return self._cached(f"unfetchable:{limit}:{floor}", 300,
                            lambda: self._unfetchable_uncached(limit, min_citing=floor),
                            placeholder={"total": None, "references": []})

    def _unfetchable_uncached(self, limit: int, *, min_citing: int | None = None,
                              scan_limit: int | None = None) -> dict:
        from .citations.frontier import classify as _frontier_classify
        from .citations.snowball import _classify
        from .adapters.bailii import external_link
        from .citations.reporters import report_series, series_jurisdiction

        floor = self._UNFETCHABLE_MIN_CITING if min_citing is None else min_citing
        rows = []
        with self._open() as (cat, _rs, _ts):
            # echr_citing is only consulted by the routable worklist; skipping it here
            # drops a nested-loop join over 1.8M rows.
            for g in cat.pending_reference_groups(min_citing=floor, limit=scan_limit,
                                                  need_echr=False):
                ref, raw, cand = g["ref"], g["raw"], g["candidate"]
                if not ref or _is_junk_ref(ref):
                    continue
                # 1. specific classification from the raw string — report / statute by
                #    name / EU instrument by name (or None → junk URL, dropped).
                fc = _frontier_classify(raw, cand)
                if fc is not None:
                    # a statute name that resolves in the offline gazetteer IS routable —
                    # skip it here so it appears in the harvest worklist, not the dead list.
                    if fc.get("gazetteer_id"):
                        continue
                    form, link, is_report = fc["form"], fc["link"], fc["is_report"]
                elif cand:
                    _form, _juris, adapter = _classify(cand, "case")
                    if adapter is not None:
                        continue  # routable — belongs in the harvest worklist, not here
                    form, link, is_report = _form, external_link(cand, raw), False
                else:
                    # a raw we can't specifically classify AND with no candidate: could be a
                    # junk URL that slipped through, or a genuine case-by-name.
                    if raw and raw.startswith("http"):
                        continue
                    form, link, is_report = "case (by name)", external_link(cand, raw), False
                # Where this authority BELONGS, as far as it can be told from the
                # citation itself: a recognised report series names its jurisdiction
                # outright ("[1982] AC 1" → uk), otherwise the candidate's court token
                # does ("[2019] IESC 4" → ie). Neither fires for a bare case name — those
                # fall back to where the reference is CITED FROM, below.
                series = report_series((raw or ref or "").strip())
                # A recognised report series names its jurisdiction (bracket style
                # disambiguates the ambiguous ones — English vs Australian FCR — hence raw);
                # otherwise the candidate's court token does. Both resolve into the picker's
                # bucket vocabulary via _retrieval_bucket.
                jur = (_retrieval_bucket(series_jurisdiction(series, raw or ref))
                       if series else _candidate_jurisdiction(cand))
                rows.append({
                    "ref": ref, "raw": raw, "candidate": cand, "form": form,
                    "is_report": is_report, "citing_count": g["citing_count"], "link": link,
                    "series": series, "jurisdiction": jur,
                })
            rows.sort(key=lambda r: r["citing_count"], reverse=True)
            out = rows[:limit]
            refs = [r["ref"] for r in out]
            citing = cat.citing_documents_for(refs) if refs else {}
            sugg = cat.suggestions_for(refs) if refs else {}
            # Where the reference is CITED FROM. For a bare case name ("Cooper v Hobart")
            # nothing in the citation itself gives a jurisdiction, but the documents
            # reaching for it usually do — a name cited only by Canadian judgments is
            # almost certainly Canadian. Shown as evidence, never as a hard claim.
            all_citers = {sid for ids in citing.values() for sid in ids}
            src_court = cat.source_court_for(sorted(all_citers)) if all_citers else {}
        for r in out:
            r["citing_documents"] = citing.get(r["ref"], [])
            r["suggestions"] = sugg.get(r["ref"], [])
            buckets: dict[str, int] = {}
            for sid in r["citing_documents"]:
                source, court = src_court.get(sid, ("", ""))
                b = self._doc_bucket(source, court)
                if b:
                    buckets[b] = buckets.get(b, 0) + 1
            r["cited_from"] = [b for b, _ in sorted(buckets.items(), key=lambda kv: -kv[1])]
        return {"total": len(rows), "references": out,
                "min_citing": floor}

    # -- export the unfetchable frontier for Westlaw / Lexis batch retrieval ----
    def export_retrieval_citations(self, *, min_citing: int = 2, batch_size: int = 100,
                                   scan_limit: int = 20000, include_names: bool = False,
                                   separator: str = "newline",
                                   include_series: tuple[str, ...] | None = None,
                                   jurisdictions: tuple[str, ...] | None = None) -> dict:
        """Mention-ranked citation batches to paste into Westlaw UK **Find & Print** or
        Lexis+ UK **Get & Print** — the pre-neutral / report-only authorities BAILII and
        Find Case Law don't hold, which those subscription databases usually do.

        Only *pasteable* references are exported: a report citation ("[1987] AC 460") or a
        neutral citation the corpus can't route — never a bare case name (Find & Print
        needs a citation) unless ``include_names``. ECR / EHRR are dropped (their sources —
        CELLAR / HUDOC — are already wired). Each batch holds at most ``batch_size``
        citations (both tools cap a run at 100); ``separator`` is ``newline`` or
        ``semicolon`` (both platforms accept either). ``include_series`` restricts to
        named report series (e.g. only WLR + Cr App R that Westlaw actually holds).
        ``jurisdictions`` restricts by the reference's jurisdiction bucket (``uk`` / ``ie``
        / ``eu`` / ``us`` / a specific Commonwealth country like ``ca`` / ``au`` / ``nz`` /
        ``sg`` / ``hk`` — see :data:`RETRIEVAL_JURISDICTIONS`) — a UK subscription can't
        retrieve a foreign report, so those citations just burn slots in a 100-cap batch."""
        import re as _re

        from .citations.reporters import is_report_citation, report_series, series_jurisdiction

        sep = ";\n" if separator == "semicolon" else "\n"
        want_series = {s.upper() for s in include_series} if include_series else None
        want_jur = {j.strip().lower() for j in jurisdictions if j.strip()} if jurisdictions else None
        # a bracketed/parenthesised year is the pasteable signal (report or neutral cite)
        cite_shape = _re.compile(r"[\[(](?:1[6-9]|20)\d{2}[\])]")
        seen: set[str] = set()
        items: list[dict] = []
        # reuse the frontier computation (uncapped), then filter to pasteable cites
        # min_citing is applied per-item below, so push the caller's own floor into SQL
        # rather than the panel's default — the export legitimately wants the long tail.
        frontier = self._unfetchable_uncached(scan_limit, min_citing=max(1, min_citing))
        for r in frontier["references"]:
            if r["citing_count"] < min_citing:
                continue
            # Collapse internal whitespace: a citation extracted across a PDF line break
            # ("[1991] ATPR\n   41") is stored with the newline, and pasted verbatim it
            # spans two lines and won't retrieve. One space between tokens is the paste form.
            raw = " ".join((r["raw"] or r["ref"] or "").split())
            series = r.get("series")          # computed once, on the frontier row
            if series and series.upper() in ("ECR", "EHRR"):
                continue  # own sources (CELLAR / HUDOC), not a Westlaw/Lexis target
            is_cite = bool(r["is_report"]) or is_report_citation(raw) or bool(cite_shape.search(raw))
            if not is_cite:
                if not (include_names and r["form"] == "case (by name)"):
                    continue
            if want_series and (not series or series.upper() not in want_series):
                continue
            # Jurisdiction: a recognised report series maps directly; otherwise the
            # reference is a neutral citation ("[2019] IESC 4") or bare name, whose
            # jurisdiction is read from the candidate's court token — so Irish (IESC/
            # IECA/IEHC) and Commonwealth neutral citations don't default to "uk" and
            # leak into a UK-only Westlaw batch.
            jur = r.get("jurisdiction")
            if want_jur and jur not in want_jur:
                continue
            key = _re.sub(r"[\s.'’\[\]()]+", "", raw).upper()  # fold for dedup
            if not key or key in seen:
                continue
            seen.add(key)
            items.append({"citation": raw, "citing_count": r["citing_count"],
                          "series": series, "jurisdiction": jur, "form": r["form"]})

        items.sort(key=lambda x: x["citing_count"], reverse=True)
        batches = []
        for i in range(0, len(items), batch_size):
            chunk = items[i: i + batch_size]
            batches.append({
                "index": i // batch_size + 1, "count": len(chunk),
                "mentions": sum(c["citing_count"] for c in chunk),
                "text": sep.join(c["citation"] for c in chunk),
                "items": chunk,
            })
        # one combined text for a single download, batches delimited by a header comment
        combined = "\n\n".join(
            f"### Batch {b['index']} — {b['count']} citations, {b['mentions']} mentions "
            f"(paste into one Find & Print / Get & Print run)\n{b['text']}"
            for b in batches)
        return {"total_citations": len(items),
                "total_mentions": sum(c["citing_count"] for c in items),
                "batch_size": batch_size, "batch_count": len(batches),
                "separator": separator, "batches": batches, "combined_text": combined}

    # -- Corpus Map: held-vs-pending by category & sub-type (§8) ------------
    def corpus_map(self) -> dict:
        """The dashboard's coverage table: every legal category and sub-type with how much we
        HOLD vs how much is PENDING (cited-but-not-held, routable) vs NAME-ONLY (recognised but
        not routable). Cached + warmed → loads instantly; the heavy per-category "cites"
        breakdown is computed separately and lazily by :meth:`corpus_map_cites`."""
        return self._cached("corpus_map", 90, self._corpus_map_uncached,
                            placeholder={"categories": [], "totals": {}})

    def refresh_corpus_map(self) -> dict:
        """Force a background recompute of the corpus map — the "↻ refresh table" action.
        Drops the cached snapshot (and the lazy per-category cites) and kicks a fresh
        compute, returning the warming placeholder for the UI to poll."""
        for key in [k for k in self._cache
                    if k == "corpus_map" or k.startswith("corpus_cites:")]:
            self._cache.pop(key, None)
            self._refreshing.discard(key)
        return self.corpus_map()

    def _corpus_map_uncached(self) -> dict:
        from .citations.taxonomy import (CATEGORY_LABELS, CATEGORY_ORDER,
                                         classify_candidate, classify_document)
        cats: dict[str, dict] = {}

        def _cat(key: str) -> dict:
            c = cats.get(key)
            if c is None:
                c = cats[key] = {"key": key, "label": CATEGORY_LABELS.get(key, key),
                                 "held": 0, "pending": 0, "cooling": 0, "name_only": 0,
                                 "subtypes": {}}
            return c

        def _sub(c: dict, tax) -> dict:
            s = c["subtypes"].get(tax.subtype)
            if s is None:
                s = c["subtypes"][tax.subtype] = {"key": tax.subtype, "label": tax.subtype_label,
                                                  "held": 0, "pending": 0, "cooling": 0,
                                                  "name_only": 0, "filter": tax.filter}
            return s

        # held — one GROUP BY query, classified in Python; plus the harvest cool-down set,
        # so a pending reference the drain recently tried and parked reads as "cooling"
        # (tried, waiting out its retry/miss TTL) rather than "untried, one click away".
        import os as _os
        miss_ttl = float(_os.environ.get("RAGLEX_MISS_TTL_DAYS") or 90)
        retry_ttl_days = float(_os.environ.get("RAGLEX_RETRY_TTL_HOURS") or 6) / 24.0
        with self._open() as (cat, _rs, _ts):
            held_rows = cat.document_subtype_counts()
            cooled = cat.enrichment_misses("harvest-miss", max_age_days=miss_ttl)
            cooled |= cat.enrichment_misses("harvest-retry", max_age_days=retry_ttl_days)
        for r in held_rows:
            tax = classify_document(source=r["source"], doc_type=r["doc_type"],
                                    court=r["court"], stable_id=r["prefix"] or "")
            c = _cat(tax.category); s = _sub(c, tax)
            c["held"] += r["n"]; s["held"] += r["n"]

        # pending — reuse the (uncapped) hanging-reference grouping
        for h in self.unresolved_references(limit=None):
            tax = classify_candidate(h["candidate"] or "", "" if h["candidate"] else "case")
            c = _cat(tax.category); s = _sub(c, tax)
            if h["needs_identifier"] or h["confidence"] == "low" or not h["suggested_adapter"]:
                c["name_only"] += 1; s["name_only"] += 1
            elif h["candidate"] in cooled:
                c["cooling"] += 1; s["cooling"] += 1
            else:
                c["pending"] += 1; s["pending"] += 1

        # ECHR: re-split the held cases by HUDOC formation (Grand Chamber / Chamber / …) — the
        # one sub-division CELLAR/HUDOC actually stores. Pending cases have no formation, so they
        # stay on a generic "ECHR case" row; the Convention row is preserved.
        if "echr" in cats:
            from .citations.taxonomy import echr_formation
            c = cats["echr"]
            old = c["subtypes"]
            new_subs: dict[str, dict] = {}
            with self._open() as (cat, _rs, _ts):
                for r in cat.echr_formation_counts():
                    key, label = echr_formation(r["branch"])
                    s = new_subs.get(key)
                    if s is None:
                        s = new_subs[key] = {"key": key, "label": label, "held": 0,
                                             "pending": 0, "cooling": 0, "name_only": 0,
                                             "filter": {"source": "echr"}}
                    s["held"] += r["n"]
            if "convention" in old:
                new_subs["convention"] = old["convention"]
            case = old.get("case")
            if case and (case["pending"] or case["cooling"] or case["name_only"]):  # no formation
                new_subs["case"] = {**case, "held": 0, "label": "ECHR case (pending / by name)"}
            c["subtypes"] = new_subs

        order = {k: i for i, k in enumerate(CATEGORY_ORDER)}
        out = sorted(cats.values(), key=lambda c: order.get(c["key"], 99))
        for c in out:
            c["subtypes"] = sorted(c["subtypes"].values(),
                                   key=lambda s: (-s["held"], -s["pending"], s["label"]))
        totals = {k: sum(c[k] for c in out) for k in ("held", "pending", "cooling", "name_only")}
        return {"categories": out, "totals": totals}

    def corpus_map_cites(self, *, category: str) -> dict:
        """LAZY: what the held documents of ``category`` cite, broken down by target category —
        ``unique`` distinct targets (a doc citing the same case 3× counts once) and ``total``
        occurrences. Scans one source's edges; cached 5 min per category."""
        return self._cached(f"corpus_cites:{category}", 300,
                            lambda: self._corpus_map_cites_uncached(category))

    def _corpus_map_cites_uncached(self, category: str) -> dict:
        from .citations.taxonomy import (CATEGORY_LABELS, classify_candidate,
                                         classify_document)
        from .resolve.matchers import first_candidate
        buckets: dict[str, dict] = {}
        with self._open() as (cat, _rs, _ts):
            # Category keys are presentation taxonomy, not necessarily source names:
            # fr-caselaw is stored as fr-dila, de-caselaw as de-rii, and nl-caselaw as
            # nl-rechtspraak. Derive the mapping from the same classifier that builds
            # the Held column so the two halves of the map cannot drift apart.
            pairs: set[tuple[str, str | None]] = set()
            for d in cat.document_subtype_counts():
                tax = classify_document(source=d["source"], doc_type=d["doc_type"],
                                        court=d["court"], stable_id=d["prefix"] or "")
                if tax.category == category:
                    pairs.add((d["source"], d["doc_type"]))
            rows = cat.outgoing_citation_targets_for(sorted(pairs))
        for r in rows:
            dst = r["dst_id"]
            if (not dst or dst.startswith("http")):
                fc = first_candidate(dst or r["raw"] or "")
                dst = fc.value if fc else dst
            if not dst:
                continue
            tax = classify_candidate(dst, "")
            b = buckets.get(tax.category)
            if b is None:
                b = buckets[tax.category] = {"category": tax.category,
                    "label": CATEGORY_LABELS.get(tax.category, tax.category),
                    "_uniq": set(), "total": 0}
            b["_uniq"].add(dst); b["total"] += int(r["n"] if "n" in r.keys() else 1)
        targets = [{"category": b["category"], "label": b["label"],
                    "unique": len(b["_uniq"]), "total": b["total"]} for b in buckets.values()]
        targets.sort(key=lambda t: t["total"], reverse=True)
        return {"category": category, "targets": targets}

    def refresh_category(self, *, category: str, on_progress=None, cancel_check=None) -> dict:
        """"Total refresh" for one category: harvest its pending routable references, then —
        for EU case-law — pull the cases that cite our held EU cases. (A global citation
        re-scan stays a separate action; it isn't category-scoped.)"""
        out: dict = {"category": category}
        _progress(on_progress, stage=f"harvesting pending — {category}", done=0, total=0)
        out["harvest"] = self.harvest_all_references(
            adapter=category, limit=1000000, on_progress=on_progress, cancel_check=cancel_check)
        if category == "eu-cellar" and not (cancel_check and cancel_check()):
            _progress(on_progress, stage="finding citing EU cases", done=0, total=0)
            out["expand"] = self.expand_citing_cases(
                source="eu-cellar", on_progress=on_progress, cancel_check=cancel_check)
        self._invalidate_caches()
        return out

    def pull_ag_opinions(self, *, limit: int = 100000, on_progress=None, cancel_check=None) -> dict:
        """Pull the Advocate General's Opinion for every held CJEU judgment that lacks one.
        A CJEU judgment CELEX ``6yyyyCJnnnn`` has its AG opinion at ``6yyyyCCnnnn`` — so this
        derives the opinion CELEX and harvests it via CELLAR. Court-of-Justice cases only (the
        General Court has no Advocate General). Skips opinions already held; idempotent."""
        import re as _re
        with self._open() as (cat, _rs, _ts):
            rows = cat.list_documents(source="eu-cellar", doc_type="judgment", limit=200000)
            wanted: list[str] = []
            for r in rows:
                if (r["court"] or "").lower() != "court of justice":
                    continue
                celex = cat.document_meta(r["stable_id"]).get("celex") or r["stable_id"]
                m = _re.match(r"^(6\d{4})CJ(\d.*)$", (celex or "").upper())
                if m:
                    wanted.append(f"{m.group(1)}CC{m.group(2)}")
        wanted = sorted(set(wanted))[:limit]
        pulled, held, failed = [], 0, 0
        for i, op in enumerate(wanted, 1):
            if cancel_check and cancel_check():
                break
            with self._open() as (cat, _rs, _ts):
                if cat.find_document_id(op) is not None:
                    held += 1
                    _progress(on_progress, stage="pulling AG opinions", done=i, total=len(wanted),
                              item=op, ok=True, msg="already held")
                    continue
            _progress(on_progress, stage="pulling AG opinions", done=i, total=len(wanted), item=op)
            try:
                res = self.harvest_reference(ref=op, candidate=op)
                if res.get("stored") or res.get("resolved") or res.get("ok"):
                    pulled.append(op)
                else:
                    failed += 1
            except Exception:  # noqa: BLE001 — one missing opinion mustn't stop the run
                failed += 1
        self._invalidate_caches()
        return {"cjeu_judgments": len(wanted), "opinions_pulled": len(pulled),
                "already_held": held, "no_opinion_or_failed": failed, "new_ids": pulled[:200]}

    def resolve_reference(
        self, *, ref: str, identifier: str | None = None, jurisdiction: str | None = None,
        existing_id: str | None = None, url: str | None = None,
        content_base64: str | None = None, filename: str | None = None,
        title: str | None = None, doc_type: str = "commentary",
    ) -> dict:
        """Manually satisfy a hanging reference (§5b). Four interchangeable, combinable
        modes — supply whichever the situation allows:

        - ``identifier`` (+ optional ``jurisdiction``): the missing citation for a
          reference recognised by *name only* — e.g. a neutral citation, ECLI, or
          CELEX. It's parsed by the same grammars into a canonical candidate id, so
          the reference resolves now (if that target is already in the corpus) or
          the moment it's harvested, and the snowball can route it.
        - ``existing_id``: point the reference at a document already in the corpus.
        - ``url``: fetch the source (via the configured scraping engine) as a new
          document and resolve to it.
        - ``content_base64`` (+ ``filename``): upload the source file and resolve to it.

        Returns what it did, including how many edges became live."""
        from .citations import extract_citations

        # 1. Parse a user-supplied identifier into a canonical candidate id.
        canonical: str | None = None
        if identifier:
            for c in extract_citations(identifier):
                if c.candidate_id:
                    canonical = c.candidate_id
                    break
            canonical = canonical or identifier.strip()

        # 2. If the user is providing the source material, import it → a target doc.
        target: str | None = existing_id
        imported: dict | None = None
        if url:
            imported = self.import_url(url=url, doc_type=doc_type, title=title or identifier or ref)
            target = imported.get("stable_id")
        elif content_base64:
            # A Westlaw legislation export satisfies a hanging *statute* reference — and it
            # must land as the Act itself (ukpga/1889/63, section-segmented) rather than as
            # an opaque commentary blob, or the pinpoint edges ("s. 38 of …") still can't
            # resolve. Try that first; anything else falls through to the generic import.
            import base64 as _b64

            raw = _b64.b64decode(content_base64)
            leg = self.import_westlaw_legislation(data=raw, filename=filename)
            if not leg.get("error"):
                imported, target = leg, leg["stable_id"]
            else:
                imported = self.import_base64(content_base64=content_base64,
                                              filename=filename or "reference.pdf",
                                              doc_type=doc_type, title=title or identifier or ref)
                target = imported.get("stable_id")

        with self._open() as (cat, _rs, _ts):
            if existing_id and cat.get_document(existing_id) is None:
                return {"error": f"no document {existing_id!r} in corpus", "ref": ref}

            # 3. Re-key the hanging edges and/or register the alias so resolution links.
            new_candidate = canonical or target
            rekeyed = 0
            if new_candidate and new_candidate != ref:
                rekeyed = cat.set_pending_candidate(ref, new_candidate)
            if canonical and target:
                # canonical id (e.g. an ECLI) is what the edges now carry; alias it
                # to the concrete document so find_document_id() lands on it.
                cat.put_alias(canonical.casefold(), target, source="manual-resolve")
            elif jurisdiction and canonical:
                cat.put_alias(canonical.casefold(), canonical, source=f"manual:{jurisdiction}")

            # 4. Resolve — turns every now-satisfiable hanging edge live.
            resolved = Resolver(cat).run()
            still = cat.find_document_id(new_candidate) if new_candidate else None
            self._invalidate_caches()
            return {
                "ref": ref, "canonical": canonical, "target": target,
                "imported": imported, "edges_rekeyed": rekeyed,
                "resolved_edges": resolved.resolved,
                "resolved": still is not None,
            }

    def import_legislation_akn(self, *, data: bytes, stable_id: str | None = None,
                               filename: str | None = None) -> dict:
        """Import a hand-supplied Akoma Ntoso file as a full legislation document.

        legislation.gov.uk occasionally won't serve an instrument's AKN (or an old
        harvest missed it), so ukpga/2006/46 and the like end up absent even though
        the XML exists. Given the file, this keys it under the proper legislation
        URI (derived from the AKN's own FRBR, or supplied) and runs the SAME
        structural parse as a live harvest — schedules, unapplied-effects edges,
        pinpoints and all — then resolves its citations. Supersedes any existing
        copy of that id (raw is canonical, §1.2)."""
        from .adapters.uk_legislation import UKLegislationAdapter
        from .formats.akoma_ntoso import _frbr_work_id

        sid = (stable_id or "").strip() or _frbr_work_id(data)
        if not sid:
            return {"error": "no stable_id given and none derivable from the AKN "
                             "FRBRWork — pass one explicitly, e.g. ukpga/2006/46"}
        # a full URL or /id/ form → the bare path
        import re as _re
        m = _re.search(r"legislation\.gov\.uk/(?:id/)?([a-z]{2,6}/[^\s?#]+)", sid, _re.I)
        if m:
            sid = m.group(1)
        sid = sid.strip("/")

        try:
            record = UKLegislationAdapter().record_from_akn(sid, data)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"AKN parse failed: {exc}"}
        if not record.text:
            return {"error": "AKN parsed but produced no text — is this an Akoma "
                             "Ntoso legislation file?"}
        record.ensure_payload_hash()
        with self._open() as (cat, rs, ts):
            raw_path = str(rs.path_for(rs.put(data, ext="xml"), "xml"))
            text_path = str(ts.put(record.payload_hash, record.text))
            ts.put_segments(record.payload_hash, record.segments)
            cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
            resolved = Resolver(cat).run()
        self._invalidate_caches()
        return {"stable_id": sid, "title": record.title,
                "chars": len(record.text or ""), "segments": len(record.segments),
                "resolved_edges": resolved.resolved}

    def reparse_document(self, *, stable_id: str) -> dict:
        """Re-derive a document's text + structural segments from its **immutable raw**
        using the current format parser — the projection-refresh path when a parser
        improves (e.g. better legislation formatting / recitals), without re-fetching
        (§1.2: raw is canonical, everything else is re-derivable). No-op for docs with
        no structural format."""
        from .formats import parse as parse_format
        from pathlib import Path

        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(stable_id)
            if doc is None or not doc["raw_path"] or not doc["payload_hash"]:
                return {"stable_id": stable_id, "reparsed": False, "reason": "no raw"}
            try:
                raw = Path(doc["raw_path"]).read_bytes()
            except OSError:
                return {"stable_id": stable_id, "reparsed": False, "reason": "raw missing"}
            # CJEU judgments use the bespoke Formex judgment parser (NP.ECR/GR.SEQ grounds
            # + ruling), NOT the legislation Formex parser the format registry would pick.
            if doc["source"] == "eu-cellar":
                from .adapters.eu_cellar import extract_formex
                text, segments = extract_formex(raw)
                fmt = "formex-judgment"
            else:
                # Older harvests can pre-date (or omit) a byte signature that the
                # current sniffer knows about.  The importer records the parser format
                # in document metadata, so prefer that durable projection hint and use
                # byte sniffing only as a fallback.  This is especially important for
                # LawMaker pages whose surrounding site template changes over time.
                meta = cat.document_meta(stable_id)
                hinted = str(meta.get("format") or "").strip().lower()
                fmt = hinted if hinted in {
                    "akn", "bwb", "formex-legislation", "lawmaker-html",
                } else _sniff_format(raw)
                if fmt is None:
                    return {"stable_id": stable_id, "reparsed": False, "reason": "no structural format"}
                if fmt == "lawmaker-html":
                    from .formats.lawmaker_html import parse_lawmaker_html
                    parts = stable_id.split("/")
                    pd = parse_lawmaker_html(raw, jurisdiction=parts[1] if len(parts) > 1 else "")
                else:
                    pd = parse_format(fmt, raw)
                text, segments = pd.text, pd.segments
            if not text:
                return {"stable_id": stable_id, "reparsed": False, "reason": "parser produced no text"}
            ts.put(doc["payload_hash"], text)            # overwrite (same hash → same path)
            ts.put_segments(doc["payload_hash"], segments)
            return {"stable_id": stable_id, "reparsed": True, "format": fmt,
                    "segments": len(segments)}

    def backfill_document_metadata(self, *, on_progress=None) -> dict:
        """Repair already-stored docs from their immutable raw (no re-fetch): derive the
        UK court from the FCL slug where the column is blank; **re-parse CJEU judgments**
        (fixing any that came out ruling-only) and re-extract their citations from the now
        full text; and derive a case-name title from the Formex where CELLAR gave none."""
        from pathlib import Path

        from .adapters.eu_cellar import extract_formex, formex_case_title
        from .adapters.uk_caselaw import court_from_slug
        from .citations import extract_document

        fixed = {"uk_court": 0, "eu_reparsed": 0, "eu_titled": 0, "eu_recovered": 0}
        with self._open() as (cat, _rs, ts):
            # 1) UK court from the slug
            for src in ("uk-caselaw", "uk-grc"):
                for r in cat.list_documents(source=src, limit=100000):
                    if not r["court"]:
                        c = court_from_slug(r["stable_id"])
                        if c:
                            cat.update_document_fields(r["stable_id"], {"court": c}, curate=False)
                            fixed["uk_court"] += 1
            # 2) re-parse CJEU judgments + titles
            eu = cat.list_documents(source="eu-cellar", limit=100000)
            for i, r in enumerate(eu, 1):
                _progress(on_progress, stage="reparsing CJEU", done=i, total=len(eu), item=r["stable_id"])
                doc = cat.get_document(r["stable_id"])
                if not doc or not doc["raw_path"] or not doc["payload_hash"]:
                    continue
                try:
                    raw = Path(doc["raw_path"]).read_bytes()
                except OSError:
                    continue
                text, segments = extract_formex(raw)
                if text:
                    before = (ts.get(doc["payload_hash"]) if doc["has_text"] else "") or ""
                    ts.put(doc["payload_hash"], text)
                    ts.put_segments(doc["payload_hash"], segments)
                    fixed["eu_reparsed"] += 1
                    if len(text.split()) > len(before.split()) + 200:  # recovered real body
                        fixed["eu_recovered"] += 1
                        extract_document(cat, ts, r["stable_id"])  # re-mine the full text
                # (re)title when missing OR when the stored title is a raw parties dump
                # (very long / full of "represented by …" boilerplate)
                title = doc["title"] or ""
                if not title or len(title) > 160 or "represented" in title.lower():
                    t = formex_case_title(raw)
                    if t and t != title and len(t) < len(title or "x" * 999):
                        cat.update_document_fields(r["stable_id"], {"title": t}, curate=False)
                        fixed["eu_titled"] += 1
            _progress(on_progress, stage="resolving citations", done=0, total=0)
            Resolver(cat).run()
        return fixed

    def reparse_all(self, *, doc_type: str | None = "legislation") -> dict:
        """Re-derive text+segments for every structural document (default: legislation)
        — run after a parser upgrade so already-harvested docs pick up the new
        formatting/recitals."""
        with self._open() as (cat, _rs, _ts):
            # ALL matching docs, not a 100k slice — the corpus holds ~145k pieces of
            # legislation, so the old cap silently skipped ~45k of them, meaning a
            # parser upgrade (new schedule/indent handling) never reached the tail.
            # text_document_ids is unbounded and already scopes to docs with text/raw.
            ids = cat.text_document_ids(doc_types=[doc_type] if doc_type else None)
        n = sum(1 for sid in ids if self.reparse_document(stable_id=sid).get("reparsed"))
        return {"candidates": len(ids), "reparsed": n}

    def reparse_source(self, *, source: str, workers: int = 12, after_stable_id: str = "",
                       on_progress=None, cancel_check=None) -> dict:
        """Re-derive text + segments for a whole SOURCE from its immutable raw, in
        parallel — the background job behind a parser upgrade reaching an already-harvested
        corpus (e.g. the rii Randnummer / DILA <br/> paragraphing fixes over de-rii's 83k
        and fr-dila's ~2.9M docs). Work is file-read → parse → file-write (I/O-bound), so a
        thread pool of ``workers`` beats the one-doc-at-a-time path many-fold. Reports
        progress by document and checkpoints the last stable_id, so an interrupted or
        cancelled run RESUMES from ``after_stable_id`` rather than restarting."""
        import json as _json
        from concurrent.futures import ThreadPoolExecutor

        from .formats import parse as parse_format
        hints = {"akn", "bwb", "formex-legislation", "rii-xml", "dila-xml"}

        with self._open() as (cat, _rs, ts):
            # KEYSET pagination, not one fetchall: a source with millions of rows would
            # otherwise spend minutes loading (and GBs holding) the whole set before the
            # first parse — no progress, heavy memory. Instead pull ``batch`` rows past a
            # stable_id cursor at a time (PK-indexed, no OFFSET scan); the cursor doubles
            # as the resume checkpoint.
            # the catalogue Row is keyed by column name (not index), so alias the count
            total = cat.conn.execute(
                "SELECT count(*) AS n FROM documents WHERE source=? AND raw_path IS NOT NULL "
                "AND payload_hash IS NOT NULL AND stable_id > ?",
                (source, after_stable_id or "")).fetchone()["n"]
            ok = skip = fail = 0

            def _work(r: dict) -> str:
                try:
                    with open(r["raw_path"], "rb") as fh:
                        raw = fh.read()
                    meta = _json.loads(r["meta_json"]) if r["meta_json"] else {}
                    hint = str(meta.get("format") or "").strip().lower()
                    fmt = hint if hint in hints else _sniff_format(raw)
                    if fmt is None:
                        return "skip"
                    pd = parse_format(fmt, raw)
                    if not pd.text:
                        return "skip"
                    ts.put(r["payload_hash"], pd.text)
                    ts.put_segments(r["payload_hash"], pd.segments)
                    return "ok"
                except Exception:  # noqa: BLE001 — a bad file must never stop the sweep
                    return "fail"

            done = 0
            reanchored = 0
            cursor = after_stable_id or ""
            batch = 2000
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                while True:
                    if cancel_check and cancel_check():
                        break
                    chunk = [dict(r) for r in cat.conn.execute(
                        "SELECT stable_id, raw_path, payload_hash, meta_json FROM documents "
                        "WHERE source=? AND raw_path IS NOT NULL AND payload_hash IS NOT NULL "
                        "AND stable_id > ? ORDER BY stable_id LIMIT ?",
                        (source, cursor, batch)).fetchall()]
                    if not chunk:
                        break
                    results = list(ex.map(_work, chunk))
                    for res in results:
                        done += 1
                        ok += res == "ok"
                        skip += res == "skip"
                        fail += res == "fail"
                    # Re-anchor citation offsets to the text we just rewrote (§1.2): the
                    # regenerated text shifted every char span, so without this the reader
                    # highlights the wrong bytes. Only the reparsed ("ok") docs, whose text
                    # actually changed. Same-transaction as nothing else here writes to the
                    # catalogue, so one commit per batch persists the offset fixes.
                    ok_hashes = {c["stable_id"]: c["payload_hash"]
                                 for c, res in zip(chunk, results) if res == "ok"}
                    fixed, _dc, _miss = self._reanchor_chunk(cat, ts, ok_hashes)
                    reanchored += fixed
                    cat.commit()
                    cursor = chunk[-1]["stable_id"]
                    _progress(on_progress, stage=f"reparsing {source}", done=done, total=total,
                              item=cursor, _checkpoint={"phase": "reparse", "source": source,
                                                        "after_stable_id": cursor})
        return {"source": source, "total": total, "reparsed": ok, "skipped": skip,
                "failed": fail, "offsets_reanchored": reanchored}

    def _reanchor_chunk(self, cat, ts, id_to_hash: dict) -> tuple[int, int, int]:
        """Re-anchor the citation offsets of a batch of documents to their current text.
        ``id_to_hash`` maps stable_id → payload_hash (both callers already hold it, so no
        extra document lookup). Reads each doc's citations in one query, re-locates each
        ``raw`` in the current text, and batches the offset updates (uncommitted — the
        caller commits). Returns ``(offsets_fixed, docs_changed, unlocatable)``."""
        from .citations.reanchor import reanchor

        if not id_to_hash:
            return 0, 0, 0
        by_src: dict[str, list] = {}
        for r in cat.citations_for_many(list(id_to_hash)):
            by_src.setdefault(r["src_id"], []).append(r)
        updates: list[tuple[int, int, int]] = []
        docs_changed = unlocatable = 0
        for sid, rows in by_src.items():
            ph = id_to_hash.get(sid)
            if not ph:
                continue
            try:
                text = ts.get(ph)
            except OSError:
                continue
            ups, miss = reanchor(text or "", rows)
            unlocatable += miss
            if ups:
                updates.extend(ups)
                docs_changed += 1
        cat.reanchor_citation_offsets(updates, commit=False)
        return len(updates), docs_changed, unlocatable

    def reanchor_source(self, *, source: str, after_stable_id: str = "",
                        on_progress=None, cancel_check=None) -> dict:
        """Re-anchor a whole source's stored citation offsets to its CURRENT text — the
        cheap, reliable repair for a corpus that was reparsed (text regenerated) without
        re-extraction, so its ``citations`` char spans drifted (the fr-dila/de-rii
        paragraphing pass). Unlike :meth:`rescan`, this re-runs no grammar, re-resolves
        nothing, and rewrites no edges — the raw strings, candidates, pinpoints and
        resolved targets are all still correct; only ``char_start``/``char_end`` move. One
        citations SELECT + one batched UPDATE per chunk; keyset-paginated and resumable
        from the ``after_stable_id`` checkpoint."""
        with self._open() as (cat, _rs, ts):
            total = cat.conn.execute(
                "SELECT count(*) AS n FROM documents WHERE source=? AND payload_hash IS NOT NULL "
                "AND stable_id > ?", (source, after_stable_id or "")).fetchone()["n"]
            done = fixed = docs_changed = unlocatable = 0
            cursor = after_stable_id or ""
            batch = 2000
            while True:
                if cancel_check and cancel_check():
                    break
                chunk = [dict(r) for r in cat.conn.execute(
                    "SELECT stable_id, payload_hash FROM documents "
                    "WHERE source=? AND payload_hash IS NOT NULL AND stable_id > ? "
                    "ORDER BY stable_id LIMIT ?", (source, cursor, batch)).fetchall()]
                if not chunk:
                    break
                f, dc, miss = self._reanchor_chunk(
                    cat, ts, {r["stable_id"]: r["payload_hash"] for r in chunk})
                cat.commit()
                fixed += f
                docs_changed += dc
                unlocatable += miss
                done += len(chunk)
                cursor = chunk[-1]["stable_id"]
                _progress(on_progress, stage=f"re-anchoring {source}", done=done, total=total,
                          item=cursor, _checkpoint={"phase": "reanchor", "source": source,
                                                    "after_stable_id": cursor})
        return {"source": source, "total": total, "docs_reanchored": docs_changed,
                "offsets_fixed": fixed, "unlocatable": unlocatable}

    def _resolve_seeds(self, cat, seeds: list[str] | None, seed_rule: dict | None) -> set[str]:
        """Turn a seed spec into a concrete set of document/candidate ids. Seeds can
        be given explicitly, or *by rule* — the building blocks for "find cases related
        to X" research:
        - ``{"cites": "32016R0679"}`` → every corpus doc that cites the GDPR
          (add ``"hops": 2`` for "… that cites any case which cites the GDPR");
        - ``{"tag": "data_protection"}`` → a tagged category/collection;
        - ``{"query": "right to erasure"}`` → corpus keyword hits.
        """
        from .resolve.matchers import first_candidate

        out: set[str] = set()
        for s in (seeds or []):
            c = first_candidate(s)
            out.add(c.value if c else s)
        if seed_rule:
            cites = seed_rule.get("cites")
            if cites:
                tgt = cat.find_document_id(cites) or cites
                layer = {tgt}
                for _ in range(int(seed_rule.get("hops", 1))):
                    nxt = set()
                    for t in layer:
                        for r in cat.relations_to(t):  # resolved incoming = who cites t
                            out.add(r["src_id"])
                            nxt.add(r["src_id"])
                    layer = nxt
            if seed_rule.get("tag"):
                for d in cat.list_documents(tag=seed_rule["tag"], limit=10000):
                    out.add(d["stable_id"])
            if seed_rule.get("query"):
                for d in cat.list_documents(query=seed_rule["query"], limit=500):
                    out.add(d["stable_id"])
        return out

    def radiate(self, *, seeds: list[str] | None = None, seed_rule: dict | None = None,
                degrees: int = 2, max_per_degree: int = 40, dry_run: bool = False,
                on_progress=None, cancel_check=None) -> dict:
        """Snowball-sample the citation network from a seed set, ``degrees`` hops out.

        Each hop: take the current frontier's outbound citations, **targeted-harvest**
        the routable ones (fetching exactly those cases/instruments), extract + resolve,
        and make the newly-fetched documents the next frontier. This is the engine
        behind "seed with a case/piece of legislation and radiate three degrees" and
        autosnowball. ``dry_run`` returns the seed set without harvesting."""
        from .citations import extract_corpus

        summary: dict = {"seed_count": 0, "degrees": [], "harvested": []}
        with self._open() as (cat, rs, ts):
            seedset = self._resolve_seeds(cat, seeds, seed_rule)
            summary["seed_count"] = len(seedset)
            if dry_run:
                return {**summary, "seeds": sorted(seedset)[:200]}

            # degree 0 — make sure the seeds themselves are in the corpus
            frontier: set[str] = set()
            for i, s in enumerate(seedset, 1):
                _progress(on_progress, stage="seeding", done=i, total=len(seedset), item=s)
                res = self._fetch_reference(cat, rs, ts, ref=s, candidate=None)
                if "error" not in res:
                    frontier.add(res["candidate"])
            self._extract_ids(cat, ts, frontier)  # only the seeds, not the whole corpus
            Resolver(cat).run_for_documents(frontier)
            seen = set(frontier)

            for deg in range(1, max(1, degrees) + 1):
                # candidates this hop = outbound citations of the current frontier
                cands: set[str] = set()
                for sid in frontier:
                    real = cat.find_document_id(sid) or sid
                    for rel in cat.relations_for(real):
                        c = rel["dst_id"]
                        if c and c not in seen and cat.find_document_id(c) is None:
                            cands.add(c)
                # Fetch until we have max_per_degree *successes* (don't let
                # un-routable / 404 candidates burn the budget); cap total attempts.
                newly: list[str] = []
                attempts = 0
                target = min(max_per_degree, len(cands))
                for c in list(cands):
                    if len(newly) >= max_per_degree or attempts >= max_per_degree * 3:
                        break
                    if cancel_check and cancel_check():
                        summary["cancelled"] = True
                        self._extract_ids(cat, ts, newly)
                        Resolver(cat).run_for_documents(newly)
                        return summary
                    attempts += 1
                    seen.add(c)
                    _progress(on_progress, stage=f"degree {deg}", done=len(newly), total=target, item=c)
                    res = self._fetch_reference(cat, rs, ts, ref=c, candidate=c)
                    ok = bool(res.get("stored") or res.get("present"))
                    if ok:
                        newly.append(res["candidate"])
                    _progress(on_progress, stage=f"degree {deg}", done=len(newly), total=target,
                              item=res.get("candidate") or c, ok=ok)
                self._extract_ids(cat, ts, newly)  # only the newly fetched docs
                Resolver(cat).run_for_documents(newly)
                summary["degrees"].append({"degree": deg, "candidates": len(cands),
                                           "harvested": len(newly)})
                summary["harvested"] += newly
                frontier = set(newly)
                if not frontier:
                    break
        return summary

    # The Convention's article marginal-headings (factual labels) — enough structure for
    # "Article 10 of the Convention" to resolve and pinpoint, without the treaty's text.
    _ECHR_ARTICLES = {
        1: "Obligation to respect human rights", 2: "Right to life", 3: "Prohibition of torture",
        4: "Prohibition of slavery and forced labour", 5: "Right to liberty and security",
        6: "Right to a fair trial", 7: "No punishment without law",
        8: "Right to respect for private and family life",
        9: "Freedom of thought, conscience and religion", 10: "Freedom of expression",
        11: "Freedom of assembly and association", 12: "Right to marry",
        13: "Right to an effective remedy", 14: "Prohibition of discrimination",
        15: "Derogation in time of emergency", 16: "Restrictions on political activity of aliens",
        17: "Prohibition of abuse of rights", 18: "Limitation on use of restrictions on rights",
    }

    def ensure_echr_convention(self) -> dict:
        """Make sure the European Convention on Human Rights exists as a corpus node
        (``echr/convention``) so "Article N of the Convention" citations resolve and
        pinpoint to the right article. Idempotent: pulls the full treaty text once (via
        ``import_echr_convention``), falling back to article *headings* only if offline."""
        with self._open() as (cat, _rs, _ts):
            if cat.get_document("echr/convention") is not None:
                return {"stable_id": "echr/convention", "present": True}
        try:
            return self.import_echr_convention()
        except Exception:  # noqa: BLE001 — offline / source change → headings-only stub
            return self._echr_convention_stub()

    def _echr_convention_stub(self) -> dict:
        from .core.models import DocType, ExtractedVia, Record
        from .core.segmentation import assemble

        with self._open() as (cat, _rs, ts):
            blocks = [(f"Article {n}", "article", f"Article {n} — {title}")
                      for n, title in sorted(self._ECHR_ARTICLES.items())]
            self._store_echr_convention(cat, ts, assemble(blocks))
            return {"stable_id": "echr/convention", "created": True, "source": "headings-stub"}

    def import_echr_convention(self) -> dict:
        """Fetch the European Convention on Human Rights (ETS No. 5) — an official, freely
        reproducible treaty — from Wikisource and store its **full text**, segmented by
        Article (the citable unit), so "Article 10 of the Convention" deep-links to the real
        Article 10. Reproducible: re-run to refresh."""
        import re as _re

        from bs4 import BeautifulSoup

        from .core.http import build_client
        from .core.segmentation import assemble

        client = build_client(timeout=45)
        resp = client.get("https://en.wikisource.org/w/api.php", params={
            "action": "parse", "format": "json", "formatversion": "2", "prop": "text",
            "page": "European_Convention_for_the_Protection_of_Human_Rights_and_Fundamental_Freedoms",
        })
        soup = BeautifulSoup(resp.json()["parse"]["text"], "html.parser")
        for junk in soup.select("sup.reference, .mw-editsection, style, .toc, table.ws-noexport"):
            junk.decompose()
        blocks: list[tuple[str, str, str]] = []
        label, kind, buf = "Preamble", "section", []
        _ART = _re.compile(r"^Article\s+(\d+)\s*[–-]\s*(.+)$")
        for el in soup.find_all(["h2", "h3", "h4", "p", "li"]):
            txt = _re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip().rstrip("¹²³ ")
            if not txt:
                continue
            if el.name in ("h2", "h3", "h4"):
                if buf:
                    blocks.append((label, kind, "\n".join(buf)))
                m = _ART.match(txt)
                if m:
                    label, kind, buf = f"Article {m.group(1)}", "article", [f"Article {m.group(1)} — {m.group(2)}"]
                else:
                    label, kind, buf = txt, "section", []
            else:
                buf.append(txt)
        if buf:
            blocks.append((label, kind, "\n".join(buf)))
        n_articles = sum(1 for b in blocks if b[1] == "article")
        if n_articles < 10:
            raise ValueError(f"ECHR parse looks wrong ({n_articles} articles)")
        with self._open() as (cat, _rs, ts):
            self._store_echr_convention(cat, ts, assemble(blocks))
        return {"stable_id": "echr/convention", "created": True, "source": "wikisource",
                "articles": n_articles}

    def _store_echr_convention(self, cat, ts, parsed) -> None:
        from .core.models import DocType, ExtractedVia, Record

        text, segments = parsed
        rec = Record(
            source="echr", stable_id="echr/convention", doc_type=DocType.LEGISLATION,
            title="European Convention on Human Rights (ETS No. 5)",
            language="en", source_language="en",
            landing_url="https://www.echr.coe.int/documents/d/echr/convention_eng",
            text=text, segments=segments, raw_bytes=text.encode("utf-8"), raw_ext="txt",
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"treaty": "ECHR", "ets": "5", "source_url":
                   "https://en.wikisource.org/wiki/European_Convention_for_the_Protection_of_Human_Rights_and_Fundamental_Freedoms"},
        )
        rec.ensure_payload_hash()
        text_path = str(ts.put(rec.payload_hash, text))
        ts.put_segments(rec.payload_hash, segments)  # the per-article structure for pinpoints
        cat.upsert_document(rec, text_path=text_path)

    def expand_citing_cases(self, *, source: str = "eu-cellar", limit: int = 5000,
                            max_workers: int = 6, on_progress=None, cancel_check=None) -> dict:
        """Find every case that CITES a case already in the corpus, via CELLAR's
        ``work_cites_work`` inverse — recorded as a **deferred** backward-citation edge
        (``cited_by``) WITHOUT downloading the citing case. So the sweep is just one SPARQL
        per held case, run in PARALLEL — not thousands of inline Formex downloads (the slow
        part). The citing cases land in the harvest worklist; their full text is pulled
        later (in parallel) by "Harvest all (eu-cellar)". Idempotent."""
        import re as _re
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from .adapters.eu_cellar import EUCellarAdapter
        from .core.models import ExtractedVia, RelationshipType, ResolutionStatus, TypedRelation

        with self._open() as (cat, _rs, _ts):
            rows = cat.list_documents(source=source, limit=100000)
            seeds: dict[str, str] = {}  # case CELEX -> the held doc's stable_id
            for r in rows:
                if r["doc_type"] not in ("judgment", "opinion"):
                    continue
                sid = r["stable_id"]
                celex = cat.document_meta(sid).get("celex")
                celex = celex if (celex and _re.match(r"^6\d{4}[A-Z]", celex)) else (
                    sid if _re.match(r"^6\d{4}[A-Z]", sid) else None)
                if celex:
                    seeds.setdefault(celex, sid)
        targets = sorted(seeds)[:limit]

        # 1) gather "who cites this" for every seed IN PARALLEL (independent SPARQL calls)
        results: dict[str, list[dict]] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(EUCellarAdapter(cited_by_celex=c, per_page=200).citing_works, c): c
                       for c in targets}
            for fut in as_completed(futures):
                c = futures[fut]
                done += 1
                if cancel_check and cancel_check():
                    break
                try:
                    results[c] = fut.result()
                except Exception:  # noqa: BLE001 — one bad query mustn't stop the sweep
                    results[c] = []
                _progress(on_progress, stage="finding citing cases", done=done, total=len(targets),
                          item=c, ok=True, msg=f"+{len(results[c])} citing")

        # 2) record deferred cited_by edges (held seed -> citing case); the citing case is a
        # dangling dst, so it surfaces in the worklist for a later parallel pull. No downloads.
        _progress(on_progress, stage="recording citation edges", done=0, total=0)
        citers: set[str] = set()
        with self._open() as (cat, _rs, _ts):
            for celex, works in results.items():
                edges = []
                for w in works:
                    cid = (w.get("ecli") or w.get("celex") or "").strip()
                    if not cid or cid == celex:
                        continue
                    citers.add(cid)
                    edges.append(TypedRelation(
                        relationship_type=RelationshipType.CITED_BY,
                        raw_citation_string=cid, dst_id=cid,
                        extracted_via=ExtractedVia.STRUCTURED,
                        resolution_status=ResolutionStatus.PENDING))
                if edges:
                    cat.clear_relations_of_type(seeds[celex], str(RelationshipType.CITED_BY))
                    cat.add_relations(seeds[celex], edges)
            resolved = Resolver(cat).run()  # link any citers already held
            to_harvest = sum(1 for c in citers if cat.find_document_id(c) is None)
        self._invalidate_caches()
        return {"cases_scanned": len(targets), "citing_relations": len(citers),
                "to_harvest": to_harvest, "resolved_edges": resolved.resolved,
                "note": "edges recorded — pull the bodies via Harvest all (eu-cellar)"}

    def detect_citations(self, *, text: str) -> dict:
        """Recognise every citation in a block of pasted text (ECLI, CELEX, neutral
        citation, legislation, CJEU case number, …) and report the routable candidates —
        the preview step before seeding. No fetching."""
        from .citations import extract_citations
        from .citations.snowball import _classify

        seen: dict[str, dict] = {}
        for c in extract_citations(text or ""):
            if not c.candidate_id or c.candidate_id in seen:
                continue
            form, juris, adapter = _classify(c.candidate_id, c.entity_kind)
            seen[c.candidate_id] = {"candidate": c.candidate_id, "raw": c.raw,
                                    "form": form, "adapter": adapter, "routable": adapter is not None}
        with self._open() as (cat, _rs, _ts):
            for d in seen.values():
                d["in_corpus"] = cat.find_document_id(d["candidate"]) is not None
        return {"detected": len(seen), "citations": list(seen.values())}

    def seed_from_text(self, *, text: str, degrees: int = 1, max_per_degree: int = 40,
                       include_citing: bool = True, citing_limit: int = 25, citing_pages: int = 1,
                       on_progress=None, cancel_check=None) -> dict:
        """Paste a block of text → detect every citation in it, harvest those items, then
        radiate ``degrees`` hops over what they cite/link to AND (``include_citing``) pull
        what *cites* them from the live source. The one-shot "seed a set of cases and go
        forwards and backwards from them" — ECLIs, neutral citations, CELEX, legislation
        are all detected and pulled to whatever degree the data sources allow."""
        det = self.detect_citations(text=text)
        cands = [c["candidate"] for c in det["citations"]]
        if not cands:
            return {"detected": 0, "note": "no citations recognised in the text"}

        # 1) seed those candidates + radiate outward (things they cite / link to)
        rad = self.radiate(seeds=cands, degrees=degrees, max_per_degree=max_per_degree,
                           on_progress=on_progress, cancel_check=cancel_check)
        result = {"detected": len(cands), "detected_citations": det["citations"], "radiate": rad}

        # 2) inbound — who cites the seeds (live FCL / CELLAR), one layer, bounded.
        # Backward-discovery is precise for CASES (search by citation) and EU CELEX
        # (CELLAR's "cases interpreting this legislation"), but a UK *statute* title search
        # returns a flood of mostly off-topic judgments — slow and noisy — so we skip
        # UK-legislation seeds here (their relationships come through the forward radiate).
        if include_citing and not (cancel_check and cancel_check()):
            discovered: list[str] = []
            seeds = [c["candidate"] for c in det["citations"]
                     if c["adapter"] != "uk-legislation"][:citing_limit]
            for i, cand in enumerate(seeds, 1):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="finding citing cases", done=i, total=len(seeds), item=cand)
                try:
                    # resolve=False: don't re-resolve the whole graph after each seed —
                    # do it ONCE at the end (below), so 25 seeds cost one resolve, not 25
                    d = self.discover_citing(target=cand, max_pages=citing_pages, resolve=False)
                    discovered += d.get("discovered", [])
                    _progress(on_progress, stage="finding citing cases", done=i, total=len(seeds),
                              item=cand, ok=True, msg=f"+{d.get('count', 0)} citing")
                except Exception:  # noqa: BLE001 — one bad lookup mustn't stop the run
                    pass
            _progress(on_progress, stage="resolving citations", done=0, total=0)
            with self._open() as (cat, _rs, _ts):
                Resolver(cat).run()
            result["citing_discovered"] = sorted(set(discovered))
            result["citing_count"] = len(result["citing_discovered"])
        return result

    def harvest_all_references(self, *, limit: int = 25, min_citing: int = 1,
                               adapter: str | None = None, leg_kind: str | None = None,
                               retry_cooled: bool = False,
                               on_progress=None, cancel_check=None) -> dict:
        """Drain the routable part of the hanging-reference queue in one go: for every
        reference that is high-enough confidence *and* has a targeted adapter, fetch
        its exact item, then extract + resolve **once** at the end. Bounded by
        ``limit`` (most-cited first) so a UI click returns; ``min_citing`` skips
        one-off references. Un-routable / low-confidence references are left for
        manual handling.

        ``retry_cooled`` ignores the cool-down lists, re-attempting references the drain
        recently tried and parked — the "harvest ALL (incl. cooling)" action, for when a
        source was merely unavailable and its items were wrongly written off."""
        # Consider EVERY hanging reference, not just the top-N by frequency — otherwise a
        # category whose items are each cited only a few times (e.g. UK case-law) is starved
        # out of the global ranking by high-frequency legislation, and a per-category harvest
        # only sees a handful. The full grouping is the same scan coverage already does.
        candidates = [r for r in self.unresolved_references(limit=None)
                      if r["suggested_adapter"] and r["confidence"] != "low"
                      and r["citing_count"] >= min_citing and not r["needs_identifier"]
                      # optional category filter: harvest just one source, and within UK
                      # legislation just primary / secondary / assimilated
                      and (not adapter or r["suggested_adapter"] == adapter)
                      and (not leg_kind or r.get("leg_kind") == leg_kind)]
        # Skip references we recently established are ABSENT (a pre-digital UK case, a
        # CELLAR rendition that doesn't exist) so a re-run doesn't re-stall on the same
        # dead item. Two cooldowns, because the two failures mean different things:
        #   harvest-miss  — the source said "no such document". Long TTL (RAGLEX_MISS_TTL_DAYS,
        #                   default 90d): asking again tomorrow will get the same answer.
        #   harvest-retry — we couldn't tell (timeout, 5xx, still-generating). SHORT TTL
        #                   (RAGLEX_RETRY_TTL_HOURS, default 6h): the document probably
        #                   exists and the source was just having a bad afternoon.
        # Conflating these is how a whole worklist gets written off: one slow hour at
        # legislation.gov.uk used to mark thousands of live Acts dead for three months.
        import os as _os
        miss_ttl = float(_os.environ.get("RAGLEX_MISS_TTL_DAYS") or 90)
        retry_ttl_days = float(_os.environ.get("RAGLEX_RETRY_TTL_HOURS") or 6) / 24.0
        if retry_cooled:
            cooled: set[str] = set()  # re-attempt everything, cool-down or not
        else:
            with self._open() as (cat, _rs, _ts):
                cooled = cat.enrichment_misses("harvest-miss", max_age_days=miss_ttl)
                cooled |= cat.enrichment_misses("harvest-retry", max_age_days=retry_ttl_days)
        skipped = sum(1 for r in candidates if r["candidate"] in cooled)
        # honour the requested limit — one click can drain everything now that the run
        # fails-fast on dead items, skips them, stays responsive, and is cancellable.
        rows = [r for r in candidates if r["candidate"] not in cooled][:limit]
        fetched, fetched_ids, failed = [], [], []
        absent, transient, rate_limited = [], [], False
        with self._open() as (cat, rs, ts):
            for i, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="harvesting", done=i, total=len(rows), item=r["ref"])
                res = self._fetch_reference(cat, rs, ts, ref=r["ref"], candidate=r["candidate"])
                outcome = res.get("outcome")
                ok = outcome in ("stored", "present")
                if ok:
                    fetched.append({"ref": r["ref"]})
                    fetched_ids.append(res["candidate"])
                else:
                    failed.append({"ref": r["ref"], "outcome": outcome,
                                   **({} if "error" not in res else {"error": res["error"]})})
                    if outcome in ("absent", "no_adapter"):
                        absent.append(r["candidate"])
                    else:
                        transient.append(r["candidate"])
                _progress(on_progress, stage="harvesting", done=i, total=len(rows),
                          item=res.get("candidate") or r["ref"], ok=ok,
                          msg=res.get("error") if not ok else None)
                if outcome == "rate_limited":
                    # The source is pushing back. Every remaining reference would now
                    # "fail" for reasons that say nothing about it, so stop the batch
                    # rather than cooling off the rest of the worklist (§5a).
                    rate_limited = True
                    _progress(on_progress, stage="rate limited — pausing", done=i,
                              total=len(rows), msg="source is throttling; stopping batch")
                    break
            if absent:
                cat.record_enrichment_misses("harvest-miss", absent)
            if transient:
                cat.record_enrichment_misses("harvest-retry", transient)
            # extract just the newly-fetched docs, then resolve once — both AFTER the
            # fetch loop, so report them as their own stages (this is the phase that
            # looked "stuck" because the progress bar had finished the harvest loop).
            self._extract_ids(cat, ts, fetched_ids, on_progress=on_progress)
            _progress(on_progress, stage="resolving citations",
                      done=0, total=len(fetched_ids))
            # bounded: only the fetched docs' own edges + edges pointing at them can
            # newly resolve — the whole-graph pass here cost minutes per drain batch
            resolved = Resolver(cat).run_for_documents(fetched_ids)
        self._invalidate_caches()  # refresh the worklist's per-source "remaining" counts
        remaining = len(candidates) - skipped - len(fetched)
        return {"attempted": len(rows), "harvested": len(fetched),
                "resolved_edges": resolved.resolved, "failed": failed,
                "absent": len(absent), "retry_later": len(transient),
                "rate_limited": rate_limited,
                # The count the UI must show: a drain that "did nothing" is nearly always
                # a drain whose whole candidate set was still cooling off.
                "skipped_recent_fail": skipped, "remaining": max(remaining, 0)}

    def _extract_ids(self, cat, ts, candidates, *, on_progress=None) -> None:
        """Extract citations from just these (newly-fetched) docs — far cheaper than
        re-extracting the whole corpus on every snowball hop."""
        from .citations import extract_document

        ids = list(set(candidates))
        aliases = cat.named_alias_map() if ids else None  # once, not per document
        for i, cand in enumerate(ids, 1):
            _progress(on_progress, stage="extracting citations", done=i, total=len(ids), item=cand)
            real = cat.find_document_id(cand) or cand
            extract_document(cat, ts, real, aliases=aliases)

    def _fetch_reference(self, cat, rs, ts, *, ref: str, candidate: str | None,
                         patient: bool = False):
        """Fetch one routable reference's exact item into the corpus (no resolve).
        Returns what happened; the caller resolves. Shared by the single- and
        all-reference harvest paths.

        ``outcome`` is the load-bearing field — it tells the drain whether the reference
        is genuinely absent (cool it off for months), merely unreachable right now (retry
        in hours), or whether the source is rate-limiting us (stop the batch immediately,
        before the rest of the worklist is written off as absent)."""
        from .citations.snowball import _classify
        from .pipeline import Pipeline
        from .resolve.matchers import first_candidate

        cand = candidate
        if not cand:
            c = first_candidate(ref)
            cand = c.value if c else ref
        cand = _act_level(cand)  # never fetch a section in isolation — fetch its Act
        if cat.find_document_id(cand) is not None:
            return {"candidate": cand, "present": True, "stored": 0, "outcome": "present"}
        _form, _juris, adapter_key = _classify(cand, "case")
        builder = _TARGETED_HARVEST.get(adapter_key)
        if builder is None:
            return {"error": f"no targeted adapter for {cand!r} (form: {_form}); "
                             f"use upload / scrape / link instead",
                    "candidate": cand, "outcome": "no_adapter"}
        try:
            # only the uk-legislation builder understands patience (giant-Act renders)
            adapter = builder(cand, patient=True) if patient and adapter_key == "uk-legislation" \
                else builder(cand)
        except Exception as exc:  # noqa: BLE001 — a builder may hit the network (CELLAR probe)
            return {"error": f"could not reach {adapter_key} to build a fetch for {cand!r}: {exc}",
                    "candidate": cand, "outcome": "transient"}
        if adapter is None:
            # The builder positively established the item isn't there (e.g. absent from
            # CELLAR under every case-CELEX descriptor) — a genuine absence.
            return {"error": f"could not build a {adapter_key} fetch for {cand!r}",
                    "candidate": cand, "outcome": "absent"}
        # The builder may have resolved the citation to a DIFFERENT real id (a guessed
        # …CJ… descriptor, or a joined case published under its lead number). If we
        # already hold that real document, just mint the alias so the citing edges
        # resolve — no refetch (the pipeline's stub dedup would skip alias minting).
        real = getattr(adapter, "celex", None)
        if real and real.upper() != cand.upper():
            held = cat.find_document_id(real)
            if held is not None:
                cat.put_alias(cand.casefold(), held, source="celex-ecli")
                return {"candidate": cand, "present": True, "stored": 0,
                        "outcome": "present", "aliased_to": held}
        # backfill=False so this one-item fetch never rewrites the source's real
        # watermark; the targeted adapters ignore `since` and yield just our id.
        # record_health=False: a 404 for a single item means "this item isn't available"
        # (pre-digital case, absent CELLAR rendition), not "the source feed is broken" —
        # don't let it increment the source's consecutive_failures counter.
        try:
            stats = Pipeline(cat, rs, textstore=ts).run(
                adapter, max_pages=1, record_health=False)
        except Exception as exc:  # noqa: BLE001
            return {"candidate": cand, "adapter": adapter_key, "stored": 0,
                    "outcome": "transient", "error": str(exc)}
        # Old House of Lords judgments (ukhl/YYYY/N, 1996–2009) often aren't on Find Case
        # Law — fall back to the publications.parliament.uk scrape for those (§5a).
        if (stats.outcome == "absent" and adapter_key == "uk-caselaw"
                and cand.lower().startswith("ukhl/")):
            try:
                hol = _targeted_uk_hol(cand)
                hstats = Pipeline(cat, rs, textstore=ts).run(
                    hol, max_pages=1, record_health=False)
                if hstats.stored:
                    return {"candidate": cand, "adapter": "uk-hol", "stored": hstats.stored,
                            "outcome": "stored"}
            except Exception:  # noqa: BLE001 — the scrape is best-effort here
                pass
        out = {"candidate": cand, "adapter": adapter_key, "stored": stats.stored,
               "outcome": stats.outcome}
        if stats.outcome not in ("stored", "present") and stats.notes:
            out["error"] = stats.notes[-1]
        return out

    def harvest_legislation_at(self, *, stable_id: str, date: str) -> dict:
        """Fetch UK legislation as it stood on ``date`` (YYYY-MM-DD) — the point-in-time
        version, so an old case can be read against the live provisions instead of
        today's (often repealed/blank) text. Stored as ``{id}@{date}`` and linked to
        the base instrument (``point_in_time_of``)."""
        import re as _re

        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            return {"error": "date must be YYYY-MM-DD"}
        base = _act_level(stable_id.split("@")[0])
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline

        adapter = get_adapter("uk-legislation", ids=base, version_date=date)
        with self._open() as (cat, rs, ts):
            stats = Pipeline(cat, rs, textstore=ts).run(adapter, max_pages=1)
            from .citations import extract_corpus
            extract_corpus(cat, ts, stable_id=f"{base}@{date}")
            Resolver(cat).run()
            doc = cat.get_document(f"{base}@{date}")
            return {"stable_id": f"{base}@{date}", "base_id": base, "date": date,
                    "stored": stats.stored, "present": doc is not None,
                    "title": doc["title"] if doc else None}

    def legislation_versions(self, *, stable_id: str) -> dict:
        """Point-in-time versions of a piece of legislation already in the corpus
        (``{id}@{date}`` docs), for the versioning interface."""
        base = _act_level(stable_id.split("@")[0])
        with self._open() as (cat, _rs, _ts):
            rows = cat.list_documents(query=f"{base}@", limit=1000)
            versions = sorted(
                [{"stable_id": r["stable_id"], "date": r["stable_id"].split("@", 1)[1],
                  "title": r["title"]} for r in rows if r["stable_id"].startswith(f"{base}@")],
                key=lambda v: v["date"], reverse=True)
            return {"base_id": base, "versions": versions}

    def outstanding_effects(self, *, limit: int = 500) -> list[dict]:
        """Legislation we hold that has *unapplied amendments* — changes the editors
        know about but haven't yet written into the published text (§0). Each row shows
        how many effects are outstanding, which instruments are amending it, and when
        we'll next re-check. This is the queue that keeps the corpus honest about the
        editorial lag without polling the whole statute book."""
        with self._open() as (cat, _rs, _ts):
            out = []
            for r in cat.list_effects_refresh(limit=limit):
                try:
                    affecting = json.loads(r["affecting"] or "[]")
                except (ValueError, TypeError):
                    affecting = []
                doc = cat.get_document(r["stable_id"])
                out.append({
                    "stable_id": r["stable_id"],
                    "title": doc["title"] if doc else None,
                    "outstanding": r["outstanding"],
                    "affecting": affecting,
                    # which amending instruments we already hold vs. still need to pull
                    "affecting_held": [a for a in affecting if cat.find_document_id(a)],
                    "checks": r["checks"],
                    "first_seen": r["first_seen"],
                    "next_check_at": r["next_check_at"],
                })
            return out

    def effects_caused_by(self, *, stable_id: str) -> list[dict]:
        """What an *amending* instrument changes — read from the same edges, the other
        way round. `amended_by` is directional (affected ← affecting) but the graph is
        bidirectional: this is just the affecting act's *incoming* amended_by edges. So a
        new Act, once harvested, "describes everything it changes" without us storing the
        fact twice. Each row: the affected instrument, the provision touched, and how."""
        with self._open() as (cat, _rs, _ts):
            out: dict[str, dict] = {}
            # affected-side: this act's *incoming* amended_by edges (affected ← affecting)
            for r in cat.relations_to(stable_id):
                if r["relationship_type"] != "amended_by":
                    continue
                affected = cat.get_document(r["src_id"])
                out.setdefault(r["src_id"], {
                    "affected_id": r["src_id"],
                    "affected_title": affected["title"] if affected else None,
                    "affected_provision": r["src_anchor"], "effect_type": r["dst_anchor"]})
            # affecting-side: this act's *outgoing* amends edges (affecting → affected),
            # which also carry applied changes the affected-side backlog has dropped
            for r in cat.relations_for(stable_id):
                if r["relationship_type"] != "amends":
                    continue
                affected = cat.get_document(r["dst_id"])
                out.setdefault(r["dst_id"], {
                    "affected_id": r["dst_id"],
                    "affected_title": affected["title"] if affected else None,
                    "affected_provision": r["dst_anchor"], "effect_type": r["raw_citation_string"]})
            return list(out.values())

    def refresh_effects(self, *, limit: int = 10) -> dict:
        """Re-pull the legislation whose outstanding-effects re-check is *due*, to see
        whether the editors have incorporated the amendments yet (§0). Bounded per call
        so it can run every scheduler tick cheaply — usually nothing is due. Each re-pull
        reschedules (backing off) or, if all effects are now applied, drops the item from
        the queue. Returns what it checked and what got cleared."""
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline
        from .citations import extract_corpus

        with self._open() as (cat, rs, ts):
            due = cat.due_effects_refresh(limit=limit)
            if not due:
                return {"due": 0, "checked": 0, "cleared": 0, "still_outstanding": 0}
            ids = [r["stable_id"] for r in due]
            before = {r["stable_id"]: r["outstanding"] for r in due}
            adapter = get_adapter("uk-legislation", ids=",".join(ids))
            # backfill=True ignores the watermark (the item is already in corpus);
            # refetch_held=True re-pulls it despite being held — the whole point here
            # is to re-read the CURRENT outstanding-amendments state. Each fetch
            # re-records the effects via the pipeline (_ingest), so the queue is
            # rescheduled/cleared as a side effect of the re-pull.
            Pipeline(cat, rs, textstore=ts).run(adapter, backfill=True, refetch_held=True)
            cleared, still = 0, 0
            for sid in ids:
                row = cat.conn.execute(
                    "SELECT outstanding FROM effects_refresh WHERE stable_id = ?", (sid,)
                ).fetchone()
                if row is None:
                    cleared += 1
                    extract_corpus(cat, ts, stable_id=sid)  # text changed → re-extract
                else:
                    still += 1
            Resolver(cat).run()
            return {"due": len(due), "checked": len(ids), "cleared": cleared,
                    "still_outstanding": still, "ids": ids, "before": before}

    def propagate_changes_from(self, *, stable_id: str, max_pages: int = 20) -> dict:
        """Push an amending act's changes OUT to the instruments it affects (§0). Reads
        the affecting-side "Changes to Legislation" feed, mints ``amends`` edges to the
        affected instruments we hold, and — for any change not yet incorporated — flags
        the affected act for re-pull NOW, so the amendment is reflected even though that
        old act might otherwise never be fetched again. This is the steady-state path:
        new amending acts emanate their effects rather than waiting on the affected side."""
        from .adapters.registry import get_adapter
        from .core.models import RelationshipType, ExtractedVia, ResolutionStatus, TypedRelation

        base = _act_level(stable_id.split("@")[0])
        adapter = get_adapter("uk-legislation")
        effects = adapter.changes_affecting(base, max_pages=max_pages)
        with self._open() as (cat, _rs, _ts):
            # group by affected instrument; track distinct effects + any unapplied ones
            by_affected: dict[str, dict] = {}
            for e in effects:
                if not e.affected_id or e.affected_id == base:
                    continue
                g = by_affected.setdefault(e.affected_id, {"effects": [], "unapplied": 0})
                g["effects"].append(e)
                if not e.applied:
                    g["unapplied"] += 1
            cat.clear_relations_of_type(base, str(RelationshipType.AMENDS))  # idempotent
            edges, flagged, held = [], 0, 0
            seen: set[tuple] = set()
            for affected_id, g in by_affected.items():
                present = cat.find_document_id(affected_id)
                if not present:
                    continue  # held-only: don't flood the corpus with every old act touched
                held += 1
                for e in g["effects"]:
                    key = (affected_id, e.affected_provision, e.type)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(TypedRelation(
                        relationship_type=RelationshipType.AMENDS,
                        raw_citation_string=e.type or affected_id, dst_id=affected_id,
                        dst_anchor=e.affected_provision,
                        extracted_via=ExtractedVia.STRUCTURED,
                        resolution_status=ResolutionStatus.RESOLVED,
                    ))
                # a change not yet written into the affected text → re-pull it to track it
                if g["unapplied"]:
                    cat.mark_effects_due(affected_id, [base], count=g["unapplied"])
                    flagged += 1
            if edges:
                cat.add_relations(base, edges)
            return {"act": base, "effects": len(effects), "affected_total": len(by_affected),
                    "affected_held": held, "edges": len(edges), "flagged_for_repull": flagged}

    def propagate_changes(self, *, limit: int = 5, max_age_days: int = 90) -> dict:
        """Scan recently-held legislation we haven't scanned lately for the changes it
        makes (affecting-side), and propagate. Bounded per call for the scheduler; the
        ``changes-feed`` enrichment marker means each act is scanned once per
        ``max_age_days`` rather than every tick.

        Only UK instruments have a "Changes to Legislation" feed: scanning EU legislation
        asks legislation.gov.uk about a CELEX (``/changes/affecting/31964R0038``) and gets
        a guaranteed 404, burning the whole per-tick budget on documents that can never
        yield an effect."""
        with self._open() as (cat, _rs, _ts):
            done = cat.enrichment_misses("changes-feed", max_age_days=max_age_days)
            rows = cat.list_documents(source="uk-legislation", doc_type="legislation", limit=2000)
            todo = [r["stable_id"] for r in rows
                    if r["stable_id"] not in done and "@" not in r["stable_id"]][:limit]
        results = []
        for sid in todo:
            try:
                results.append(self.propagate_changes_from(stable_id=sid))
            except Exception as exc:  # noqa: BLE001 — one bad feed mustn't stop the batch
                results.append({"act": sid, "error": str(exc)})
        with self._open() as (cat, _rs, _ts):
            if todo:
                cat.record_enrichment_misses("changes-feed", todo)
        return {"scanned": len(todo),
                "flagged": sum(r.get("flagged_for_repull", 0) for r in results),
                "edges": sum(r.get("edges", 0) for r in results), "results": results}

    def import_case(self, *, data: bytes, filename: str, neutral_citation: str | None = None,
                    also_cited_as: list[str] | str | None = None, ref: str | None = None,
                    title: str | None = None) -> dict:
        """Import a judgment file (PDF/RTF/HTML/text) as a first-class **case**, keyed by its
        own neutral citation and linked to *every* form the corpus cites it by (§5b, §1.9).

        This is the robust answer to "I have the only available copy of a case TNA doesn't
        hold". Unlike a generic import — which drops an opaque, unlinked commentary blob — it:

        1. extracts clean text (RTF is de-RTF'd, not stored as raw ``{\\rtf1 …}`` markup);
        2. **detects the case's own neutral citation from its header** ("[2021] UKUT 299
           (AAC)" → ``ukut/aac/2021/299``) — so it's keyed the way the corpus cites it;
        3. stores it as a **judgment**, and mints aliases for the report citation(s) it's
           also reported at ("[2022] 1 WLR 2241") and any chamber-less variant — so a
           citation in ANY of those forms resolves to this one document;
        4. extracts the body's own citations and resolves.
        """
        import re as _re

        from .citations import extract_citations
        from .core.models import AddedBy, DocType, ExtractedVia, Record, Segment, sha256_bytes
        from .extraction import extract_bytes
        from .pipeline.runner import _chamberless_alias
        from .resolve.matchers import first_candidate
        from .core.text import fold

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        extracted = extract_bytes(data, ext=ext)
        text = extracted.text or ""
        if not text.strip():
            return {"error": "no text could be extracted (a scanned PDF needs OCR)"}

        # 2. the case's own neutral citation: explicit, else the first case-slug the header
        #    names (the citation printed at the top of every judgment).
        slug = None
        if neutral_citation:
            c = first_candidate(neutral_citation)
            slug = c.value if c else None
        if not slug:
            for cit in extract_citations(text[:1800]):
                if cit.candidate_id and "/" in cit.candidate_id and cit.entity_kind == "case":
                    slug = cit.candidate_id
                    break
        # aliases: everything else this case is cited by — supplied report citations, the
        # worklist ref the user uploaded against, and the chamber-less slug variant.
        alias_srcs: list[str] = []
        if isinstance(also_cited_as, str):
            also_cited_as = [also_cited_as]
        alias_srcs += list(also_cited_as or [])
        if ref:
            alias_srcs.append(ref)
        stable_id = slug or (first_candidate(ref).value if ref and first_candidate(ref) else None) \
            or f"user-case:{sha256_bytes(data)[:16]}"

        payload_hash = sha256_bytes(data)
        segments = [Segment(label=f"p. {n}", char_start=s, char_end=e, kind="page")
                    for n, s, e in (extracted.page_spans or [])]
        from .citations.courts import IRISH_COURTS

        head = stable_id.split("/", 1)[0].lower()
        with self._open() as (cat, rs, ts):
            record = Record(
                source=("ie-caselaw" if head in IRISH_COURTS else "uk-caselaw")
                if slug else "user-import",
                stable_id=stable_id, doc_type=DocType.JUDGMENT,
                title=title or _case_title_from(text) or filename,
                language="en", source_language="en",
                raw_bytes=data, raw_ext=ext or "bin", payload_hash=payload_hash,
                text=text, segments=segments, extracted_via=ExtractedVia.MANUAL,
                added_by=AddedBy.USER, extra={"engine": extracted.engine, "imported": True},
            )
            raw_path = str(rs.path_for(rs.put(data, ext=ext or "bin"), ext or "bin"))
            text_path = str(ts.put(payload_hash, text))
            ts.put_segments(payload_hash, segments)
            cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
            # mint every alias → this document, so all citation forms resolve to it
            aliased = 0
            for a in alias_srcs:
                cand = first_candidate(a)
                key = fold(cand.value) if cand else fold(a)
                if key and key != stable_id.lower():
                    cat.put_alias(key, stable_id, source="import-case", commit=False)
                    aliased += 1
            bare = _chamberless_alias(stable_id)
            if bare and bare != stable_id.lower():
                cat.put_alias(bare, stable_id, source="chamber-alias", commit=False)
                aliased += 1
            cat.commit()
            # extract the judgment's own outgoing citations, then resolve the whole graph
            from .citations import extract_document
            extract_document(cat, ts, stable_id)
            resolved = Resolver(cat).run()
        self._invalidate_caches()
        return {"stable_id": stable_id, "detected_citation": slug, "chars": len(text),
                "aliases": aliased, "resolved_edges": resolved.resolved,
                "engine": extracted.engine}

    # kinds of name variant safe to mint as a blanket alias (single-party is too ambiguous)
    _BAILII_ALIAS_KINDS = frozenset({"exact", "role-form", "abbrev", "drop-tail"})

    def import_bailii_corpus(self, *, jsonl_path: str, names_csv: str | None = None,
                             out_jsonl: str | None = None, batch: int = 500,
                             limit: int | None = None, match_reports: bool = False,
                             on_progress=None, cancel_check=None) -> dict:
        """Bulk-import the BAILII full-text corpus (``all.jsonl``: ``{id, year, text}``),
        recovering each case's name from the BAILII index CSV and keying it by the neutral
        citation its ``id`` path encodes.

        Per record: derive the FCL slug from the path; look up the cleaned case name and
        citations from the index; import the judgment (or, if that slug is already held,
        attach the text as a *secondary* alt-text without disturbing the authoritative one)
        and mint an alias for every distinctive name variant + secondary citation so any
        cited form resolves here. A single ``Resolver`` pass at the end links the graph;
        ``match_reports=True`` then links classic law-report citations against the enlarged
        judgment pool. Idempotent/resumable — a slug already imported is skipped.
        """
        import json as _json
        from datetime import date as _date

        from .adapters.bailii_corpus import (
            bailii_path_to_slug, citation_agrees_with_slug, load_name_index, slug_to_citation,
        )
        from .adapters.uk_caselaw import court_from_slug
        from .citations import extract_document
        from .citations.name_variants import name_variants
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .pipeline.runner import _chamberless_alias
        from .resolve.matchers import first_candidate
        from .core.text import fold

        names = load_name_index(names_csv) if names_csv else {}
        st = {"total": 0, "imported": 0, "secondary": 0, "no_slug": 0, "named": 0,
              "aliases": 0, "citation_mismatch": 0, "extracted": 0}
        out_f = open(out_jsonl, "w", encoding="utf-8") if out_jsonl else None
        try:
            with self._open() as (cat, rs, ts):
                existing = cat.all_stable_ids()
                to_extract: list[str] = []
                n = 0
                with open(jsonl_path, encoding="utf-8") as fh:
                    for line in fh:
                        if cancel_check and cancel_check():
                            break
                        line = line.strip()
                        if not line:
                            continue
                        if limit and st["total"] >= limit:
                            break
                        rec = _json.loads(line)
                        st["total"] += 1
                        slug = bailii_path_to_slug(rec.get("id"))
                        if not slug:
                            st["no_slug"] += 1
                            continue
                        text = rec.get("text") or ""
                        year = rec.get("year")
                        clean = names.get(slug)

                        # -- name + citation ladder --
                        title = clean.title if (clean and clean.title) else None
                        idx_cites = clean.citations if clean else ()
                        if clean and clean.title:
                            st["named"] += 1
                        if not title:
                            title = _case_title_from(text)
                        primary_cite = slug_to_citation(slug)
                        if not title:
                            title = primary_cite or slug

                        # -- sanity check (task 3): the index's citation must agree with the
                        #    path-derived neutral; the path is authoritative on disagreement --
                        mismatch = None
                        if idx_cites and not any(citation_agrees_with_slug(slug, c) for c in idx_cites):
                            mismatch = list(idx_cites)
                            st["citation_mismatch"] += 1
                        secondary = [c for c in idx_cites if not citation_agrees_with_slug(slug, c)]

                        # -- aliases: distinctive name variants + secondary citations + bare slug --
                        variants = name_variants(title)
                        alias_pairs: list[tuple[str, str]] = []
                        for v, kind in variants:
                            if kind not in self._BAILII_ALIAS_KINDS:
                                continue
                            key = fold(v)
                            if key and key != slug:
                                alias_pairs.append((key, f"bailii-name:{kind}"))
                        for c in secondary:
                            cand = first_candidate(c)
                            key = fold(cand.value) if cand else fold(c)
                            if key and key != slug:
                                alias_pairs.append((key, "bailii-report-alias"))
                        bare = _chamberless_alias(slug)
                        if bare and bare != slug:
                            alias_pairs.append((bare, "chamber-alias"))

                        data = text.encode("utf-8")
                        payload_hash = sha256_bytes(data)
                        meta = {"imported": "bailii-corpus", "year": year}
                        if clean and clean.title:
                            meta["bailii_name"] = clean.title
                        if idx_cites:
                            meta["bailii_citations"] = list(idx_cites)
                        if clean and clean.catchwords:
                            meta["catchwords"] = clean.catchwords
                        if mismatch:
                            meta["citation_mismatch"] = mismatch

                        if slug in existing:
                            # already held (Find Case Law / HoL): keep the authoritative text,
                            # attach this one as a non-default secondary, record all metadata.
                            text_path = str(ts.put(payload_hash, text))
                            cur = cat.document_meta(slug)
                            alts = cur.get("alt_texts", [])
                            if not any(a.get("payload_hash") == payload_hash for a in alts):
                                alts.append({"source": "bailii-corpus", "payload_hash": payload_hash,
                                             "text_path": text_path, "chars": len(text), "year": year})
                            cur["alt_texts"] = alts
                            for k, v in meta.items():
                                cur[k] = v
                            cat.set_document_meta(slug, cur, title_if_empty=title, commit=False)
                            st["secondary"] += 1
                            disposition = "secondary"
                        else:
                            record = Record(
                                source="uk-caselaw", stable_id=slug, doc_type=DocType.JUDGMENT,
                                title=title, court=court_from_slug(slug),
                                decision_date=_date(int(year), 1, 1) if str(year).isdigit() else None,
                                language="en", source_language="en",
                                raw_bytes=data, raw_ext="txt", payload_hash=payload_hash, text=text,
                                extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER, extra=meta,
                            )
                            raw_path = str(rs.path_for(rs.put(data, ext="txt"), "txt"))
                            text_path = str(ts.put(payload_hash, text))
                            cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
                            existing.add(slug)
                            to_extract.append(slug)
                            st["imported"] += 1
                            disposition = "imported"

                        for key, source in alias_pairs:
                            cat.put_alias(key, slug, source=source, commit=False)
                            st["aliases"] += 1

                        if out_f:
                            out_f.write(_json.dumps({
                                "id": rec.get("id"), "year": year, "stable_id": slug,
                                "case_name": title, "primary_citation": primary_cite,
                                "secondary_citations": secondary,
                                "name_variants": [v for v, _ in variants],
                                "citation_mismatch": mismatch,
                                "disposition": disposition,
                            }) + "\n")

                        n += 1
                        if n % batch == 0:
                            cat.commit()
                            _progress(on_progress, stage="importing", done=st["total"])
                cat.commit()

                # extract each new judgment's own outgoing citations (pending edges), then
                # resolve in bounded relation ranges. The worklist is this run's imports
                # UNION the durable backlog (stored, never stamped, no citation rows) —
                # an interrupted previous run's imports dedup as "already held" on resume,
                # so an in-memory queue alone would strand them without a citation graph.
                to_extract = list(dict.fromkeys([
                    *to_extract,
                    *cat.text_document_ids(source="uk-caselaw", only_unextracted=True,
                                           only_never_extracted=True),
                ]))
                from .citations import extract_documents_parallel
                ex = extract_documents_parallel(
                    cat, ts, to_extract, on_progress=on_progress,
                    cancel_check=cancel_check)
                st["extracted"] += ex.processed
                resolved = Resolver(cat).run_batched(
                    on_progress=on_progress, cancel_check=cancel_check)
        finally:
            if out_f:
                out_f.close()

        st["resolved_edges"] = resolved.resolved
        if match_reports and not (cancel_check and cancel_check()):
            st["report_matched"] = self.match_report_citations(
                on_progress=on_progress, cancel_check=cancel_check).get("aliased", 0)
        self._invalidate_caches()
        return st

    @staticmethod
    def _bailii_html_supersedes(existing, existing_meta: dict, new_len: int, old_len: int) -> bool:
        """Should a parsed BAILII page REPLACE the held text for its slug? Yes when the
        held copy is a lower-fidelity import (the plain-text bailii-corpus dump, a manual
        RTF upload, a generic user import, or textless); a HoL scrape is replaced only by
        a copy at least comparably long (a truncated save must not beat a full scrape).
        Anything else — above all a Find Case Law XML — stays authoritative."""
        if not existing["has_text"]:
            return True
        if existing_meta.get("imported") in ("bailii-corpus", "bailii-html"):
            return True
        if existing_meta.get("via") == "bailii-upload":
            return True
        if existing["source"] == "user-import":
            return True
        if existing["source"] == "uk-hol":
            return new_len >= 0.8 * old_len
        return False

    def import_bailii_zip(self, *, zip_path: str, limit: int | None = None,
                          on_progress=None, cancel_check=None) -> dict:
        """Import a zip of saved BAILII judgment pages (``.html``) — each parsed for its
        neutral-citation slug (the URL line), case name, decision date, court, numbered
        paragraphs, and the full "Cite as:" list, then **synthesised** with what the
        corpus already holds (§5b):

        * a slug we don't hold → imported as a first-class ``uk-caselaw`` judgment
          (styled HTML kept as the raw, paragraph segments for pinpoints);
        * a slug held as a lower-fidelity copy (the plain-text bailii-corpus dump, a
          manual RTF upload) → **superseded**: the richer page becomes the document's
          text (old version archived, prior text kept as a secondary ``alt_text``);
        * a slug held authoritatively (Find Case Law XML) → the page attaches as a
          secondary ``alt_text`` and only the metadata is merged.

        In every case the name variants, every "Cite as:" report citation, and the
        chamber-less slug are minted as aliases — so report-only citations resolve —
        the case name fills an empty title, and one resolve pass links the graph."""
        import zipfile

        with zipfile.ZipFile(zip_path) as zf:
            infos = [i for i in zf.infolist()
                     if not i.is_dir()
                     and i.filename.lower().endswith((".html", ".htm"))
                     and not i.filename.startswith("__MACOSX")
                     and "/." not in "/" + i.filename]
            if limit:
                infos = infos[:limit]

            def _entries():
                for info in infos:
                    yield info.filename, zf.read(info)

            return self._import_bailii_pages(_entries(), total=len(infos),
                                             on_progress=on_progress, cancel_check=cancel_check)

    def import_bailii_dir(self, *, dir_path: str, limit: int | None = None,
                          on_progress=None, cancel_check=None) -> dict:
        """Same synthesis as :meth:`import_bailii_zip`, but over a **directory** of saved
        ``.html`` pages (recursively) — the no-zip path for a big Finder folder the web UI
        streamed up in batches. The directory is the spool the batched upload wrote to."""
        import os

        paths: list[str] = []
        for root, _dirs, names in os.walk(dir_path):
            for nm in names:
                if nm.lower().endswith((".html", ".htm")) and not nm.startswith("."):
                    paths.append(os.path.join(root, nm))
        paths.sort()
        if limit:
            paths = paths[:limit]

        def _entries():
            for p in paths:
                with open(p, "rb") as fh:
                    yield os.path.basename(p), fh.read()

        return self._import_bailii_pages(_entries(), total=len(paths),
                                         on_progress=on_progress, cancel_check=cancel_check)

    def _import_bailii_pages(self, entries, *, total: int,
                             on_progress=None, cancel_check=None) -> dict:
        """The shared BAILII-page importer: consume ``entries`` (an iterable of
        ``(filename, html_bytes)``), synthesising each against the corpus (import /
        supersede / secondary), then extract + resolve once at the end. Both the zip
        and the directory paths feed it the same stream."""
        from .adapters.bailii_html import parse_bailii_html
        from .adapters.uk_caselaw import court_from_slug
        from .citations import extract_citations, extract_document
        from .citations.courts import IRISH_COURTS
        from .citations.name_variants import name_variants
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .pipeline.runner import _chamberless_alias
        from .resolve.matchers import first_candidate
        from .core.text import fold

        st = {"total": 0, "imported": 0, "superseded": 0, "secondary": 0,
              "unparseable": 0, "aliases": 0, "extracted": 0}
        files: list[dict] = []  # per-file dispositions for the UI
        with self._open() as (cat, rs, ts):
            to_extract: list[str] = []
            for n, (filename, data) in enumerate(entries, 1):
                if cancel_check and cancel_check():
                    break
                st["total"] += 1
                _progress(on_progress, stage="importing", done=n, total=total, item=filename)
                try:
                    parsed = parse_bailii_html(data, filename=filename)
                except Exception as exc:  # noqa: BLE001 — one bad page mustn't sink the batch
                    parsed = None
                    if len(files) < 1000:
                        files.append({"file": filename, "disposition": "error", "error": str(exc)})
                if parsed is None or not parsed.slug:
                    st["unparseable"] += 1
                    if parsed is not None and len(files) < 1000:
                        files.append({"file": filename, "disposition": "unparseable",
                                      "title": parsed.title})
                    continue
                slug, title = parsed.slug, parsed.title

                # aliases: distinctive name variants + every "Cite as:" citation +
                # the chamber-less slug — the same ladder as the corpus import.
                alias_pairs: list[tuple[str, str]] = []
                for v, kind in name_variants(title or ""):
                    if kind not in self._BAILII_ALIAS_KINDS:
                        continue
                    key = fold(v)
                    if key and key != slug:
                        alias_pairs.append((key, f"bailii-name:{kind}"))
                for c in parsed.citations:
                    cand = first_candidate(c)
                    key = fold(cand.value) if cand else fold(c)
                    if key and key != slug:
                        alias_pairs.append((key, "bailii-report-alias"))
                bare = _chamberless_alias(slug)
                if bare and bare != slug:
                    alias_pairs.append((bare, "chamber-alias"))

                # No transcript on the page — either a PDF-only stub (keep its good
                # metadata as a placeholder) or a genuinely empty/unreadable page.
                if not parsed.text.strip():
                    if not parsed.pdf_only:
                        st["unparseable"] += 1
                        if len(files) < 1000:
                            files.append({"file": filename, "disposition": "unparseable",
                                          "title": title})
                        continue
                    disposition = self._import_bailii_pdf_stub(
                        cat, rs, ts, parsed=parsed, data=data, alias_pairs=alias_pairs, st=st)
                    if len(files) < 1000:
                        files.append({"file": filename, "stable_id": slug, "title": title,
                                      "pdf_url": parsed.pdf_url, "disposition": disposition})
                    if n % 100 == 0:
                        cat.commit()
                    continue
                # ICLR-sourced pages open with the report citation the case was
                # published at — usually bare ("12 QBD 271", no year) and often
                # missing from "Cite as:". It names THIS case, so it's an alias,
                # not an outgoing reference (extraction's self-citation guard
                # drops the phantom edge). The report grammar needs a year, so
                # qualify the bare first line with the decision year and mint
                # every form a citer might use: "(1884) …", "[1884] …", bare.
                self_reports = [c.raw for c in extract_citations(parsed.text[:400])
                                if c.entity_kind == "case" and not c.candidate_id]
                year = parsed.decision_date.year if parsed.decision_date else None
                first = parsed.text.split("\n", 1)[0].strip()
                if year and first and not any(first in r for r in self_reports):
                    probe = f"({year}) {first}"
                    got = [c for c in extract_citations(probe) if c.method == "law_report"]
                    if len(got) == 1 and got[0].raw == probe:
                        self_reports += [probe, f"[{year}] {first}", first]
                for r in self_reports:
                    key = fold(r)
                    if key and key != slug and not cat.get_alias(key):
                        alias_pairs.append((key, "bailii-self-report"))

                payload_hash = sha256_bytes(parsed.text.encode("utf-8"))
                new_meta = {"imported": "bailii-html", "bailii_url": parsed.bailii_url,
                            "bailii_citations": list(parsed.citations),
                            "bailii_court": parsed.court_label}
                existing = cat.get_document(slug)
                old_meta = cat.document_meta(slug) if existing is not None else {}

                if existing is not None and existing["payload_hash"] == payload_hash:
                    # the identical text is already the document — just top up aliases
                    for key, source in alias_pairs:
                        cat.put_alias(key, slug, source=source, commit=False)
                        st["aliases"] += 1
                    st["unchanged"] = st.get("unchanged", 0) + 1
                    if len(files) < 1000:
                        files.append({"file": filename, "stable_id": slug,
                                      "title": title, "disposition": "unchanged"})
                    continue

                if existing is None or self._bailii_html_supersedes(
                        existing, old_meta,
                        len(parsed.text), self._text_len(ts, existing) if existing is not None else 0):
                    meta = {**old_meta, **new_meta}
                    if existing is not None and existing["has_text"] and \
                            existing["payload_hash"] != payload_hash:
                        # keep the replaced text reachable as a secondary rendition
                        alts = meta.get("alt_texts", [])
                        if not any(a.get("payload_hash") == existing["payload_hash"] for a in alts):
                            alts.append({"source": existing["source"],
                                         "payload_hash": existing["payload_hash"],
                                         "text_path": existing["text_path"]})
                        meta["alt_texts"] = alts
                    record = Record(
                        source="ie-caselaw" if slug.split("/", 1)[0] in IRISH_COURTS
                        else "uk-caselaw",
                        stable_id=slug, doc_type=DocType.JUDGMENT,
                        title=title or (existing["title"] if existing is not None else None) or slug,
                        court=court_from_slug(slug),
                        decision_date=parsed.decision_date,
                        language="en", source_language="en",
                        landing_url=parsed.bailii_url,
                        raw_bytes=data, raw_ext="html", payload_hash=payload_hash,
                        text=parsed.text, segments=parsed.segments,
                        extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
                        extra=meta,
                    )
                    raw_path = str(rs.path_for(rs.put(data, ext="html"), "html"))
                    text_path = str(ts.put(payload_hash, parsed.text))
                    ts.put_segments(payload_hash, parsed.segments)
                    cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
                    to_extract.append(slug)
                    disposition = "imported" if existing is None else "superseded"
                    st["imported" if existing is None else "superseded"] += 1
                else:
                    # held authoritatively — attach as a secondary text, merge metadata
                    text_path = str(ts.put(payload_hash, parsed.text))
                    alts = old_meta.get("alt_texts", [])
                    if not any(a.get("payload_hash") == payload_hash for a in alts):
                        alts.append({"source": "bailii-html", "payload_hash": payload_hash,
                                     "text_path": text_path, "chars": len(parsed.text)})
                    old_meta["alt_texts"] = alts
                    for k, v in new_meta.items():
                        old_meta.setdefault(k, v)
                    cat.set_document_meta(slug, old_meta, title_if_empty=title, commit=False)
                    disposition = "secondary"
                    st["secondary"] += 1

                for key, source in alias_pairs:
                    cat.put_alias(key, slug, source=source, commit=False)
                    st["aliases"] += 1
                if len(files) < 1000:
                    files.append({"file": filename, "stable_id": slug, "title": title,
                                  "citations": list(parsed.citations),
                                  "disposition": disposition})
                if n % 100 == 0:
                    cat.commit()
            cat.commit()
            for i, sid in enumerate(to_extract):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="extracting citations",
                          done=i + 1, total=len(to_extract), item=sid)
                try:
                    extract_document(cat, ts, sid)
                    st["extracted"] += 1
                except Exception:  # noqa: BLE001
                    pass
                if i % 100 == 0:
                    cat.commit()
            cat.commit()
            _progress(on_progress, stage="resolving citations", done=0, total=0)
            resolved = Resolver(cat).run()
        st["resolved_edges"] = resolved.resolved
        st["files"] = files
        self._invalidate_caches()
        return st

    def _import_bailii_pdf_stub(self, cat, rs, ts, *, parsed, data,
                                alias_pairs: list, st: dict) -> str:
        """A BAILII page with no transcript — the body is only a link to the original
        PDF. Keep the good metadata (title, date, court, "Cite as" citations) as a
        **text-less stub** keyed by the slug, plus the PDF url in meta, so name/report
        citations resolve and the case is visibly held-but-unfetched. Never overwrites
        a real transcript, and being ``has_text=0`` it is superseded the moment the
        full page (or a converted PDF) is imported. Returns the disposition."""
        from .adapters.uk_caselaw import court_from_slug
        from .citations.courts import IRISH_COURTS
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes

        slug, title = parsed.slug, parsed.title
        stub_meta = {"imported": "bailii-pdf-stub", "bailii_url": parsed.bailii_url,
                     "bailii_pdf_url": parsed.pdf_url, "needs_pdf": True,
                     "bailii_citations": list(parsed.citations),
                     "bailii_court": parsed.court_label}
        existing = cat.get_document(slug)
        if existing is not None and existing["has_text"]:
            # we already hold the real judgment — the stub only adds the PDF link + aliases
            meta = cat.document_meta(slug)
            meta.setdefault("bailii_pdf_url", parsed.pdf_url)
            cat.set_document_meta(slug, meta, commit=False)
            disposition = "pdf-stub-skipped"
        else:
            # (re)write the metadata stub — raw HTML kept so /raw serves the "download
            # the PDF" page, but no text/segments (has_text=0 → later import supersedes)
            payload_hash = sha256_bytes(data)
            merged = {**(cat.document_meta(slug) if existing is not None else {}), **stub_meta}
            record = Record(
                source="ie-caselaw" if slug.split("/", 1)[0] in IRISH_COURTS else "uk-caselaw",
                stable_id=slug, doc_type=DocType.JUDGMENT,
                title=title or (existing["title"] if existing is not None else None) or slug,
                court=court_from_slug(slug), decision_date=parsed.decision_date,
                language="en", source_language="en", landing_url=parsed.bailii_url,
                raw_bytes=data, raw_ext="html", payload_hash=payload_hash,
                text=None, segments=[], extracted_via=ExtractedVia.SCRAPE,
                added_by=AddedBy.USER, extra=merged,
            )
            raw_path = str(rs.path_for(rs.put(data, ext="html"), "html"))
            cat.upsert_document(record, raw_path=raw_path, text_path=None)
            disposition = "pdf-stub"
        st["pdf_stub"] = st.get("pdf_stub", 0) + 1
        for key, source in alias_pairs:
            cat.put_alias(key, slug, source=source, commit=False)
            st["aliases"] += 1
        return disposition

    # -- self-healing repair for the Commonwealth register ------------------
    def repair_au_cth(self, *, limit: int = 100, on_progress=None,
                      cancel_check=None) -> dict:
        """Heal ``au-cth`` records that an earlier, worse harvest left incomplete.

        Written as a **bounded, idempotent drain** rather than a one-shot migration, because
        the thing it repairs is "whatever the last version of the adapter couldn't do". Run
        it every so often and the corpus converges on its own after a deploy; run it when
        there is nothing wrong and it does nothing. Two independent repairs:

        **1. Missing bodies.** The adapter used to read a website path that existed for only
        some compilations, so ~1,200 titles were stored as metadata with no text. Those are
        re-fetched through the API's content endpoint, which serves them all. Bounded by
        ``limit`` because each is a real download.

        **2. Canonical-citation aliases.** A title's stable_id is built from the FRL
        *register* id, which carries the year the title was **registered**, not enacted:
        the Privacy Act 1988 is ``C2004A03712`` and so lands at ``au/cth/act/2004/3712``.
        That is faithful to the register's own key and worth keeping as the id — but it means
        a citation naming the Act's real year and number resolves against nothing. The real
        year/number are already stored in the record's metadata, so this mints
        ``au/cth/act/1988/119`` as an **alias** to the held document.

        Aliasing rather than re-keying is deliberate: renaming a stable_id would mean
        rewriting the primary key plus every relation, citation and alias that points at it,
        which is a destructive operation to run automatically on a deploy. An alias reaches
        the same place and cannot lose data."""
        from .adapters.au_legislation import CommonwealthAdapter
        from .core.models import (AddedBy, DocType, ExtractedVia, Record, sha256_bytes)
        from .formats.lawmaker_html import au_id

        st = {"alias_candidates": 0, "aliases_minted": 0, "textless": 0,
              "refetched": 0, "still_textless": 0, "errors": 0}
        with self._open() as (cat, rs, ts):
            rows = cat.conn.execute(
                "SELECT stable_id, meta_json FROM documents "
                "WHERE source = 'au-cth' AND is_latest = 1").fetchall()
            # -- 1. canonical-citation aliases (cheap, no network) --------------
            for r in rows:
                meta = json.loads(r["meta_json"] or "{}")
                year, number = meta.get("year"), meta.get("number")
                series = (meta.get("series_type") or meta.get("collection") or "act").lower()
                if not year or number in (None, ""):
                    continue
                canonical = au_id("cth", series, int(year), str(number))
                if canonical == r["stable_id"]:
                    continue                      # id already carries the real year/number
                st["alias_candidates"] += 1
                if cat.get_alias(canonical) is None:
                    cat.put_alias(canonical, r["stable_id"],
                                  source="au-cth-canonical-id", commit=False)
                    st["aliases_minted"] += 1
            cat.commit()

            # -- 2. re-fetch the bodies an older adapter couldn't reach ----------
            textless = cat.conn.execute(
                "SELECT stable_id FROM documents "
                "WHERE source = 'au-cth' AND is_latest = 1 AND has_text = 0 "
                "ORDER BY stable_id LIMIT ?", (limit,)).fetchall()
            st["textless"] = len(textless)
            if textless:
                adapter = CommonwealthAdapter()
                for n, r in enumerate(textless, 1):
                    if cancel_check and cancel_check():
                        break
                    sid = r["stable_id"]
                    doc_row = cat.get_document(sid)
                    meta = cat.document_meta(sid)
                    tid = meta.get("frl_title_id")
                    if doc_row is None or not tid:
                        continue
                    _progress(on_progress, stage="re-fetching au-cth bodies",
                              done=n, total=len(textless), item=sid)
                    try:
                        doc, as_at = adapter.fetch_body_api(tid)
                    except Exception:  # noqa: BLE001 — one bad title mustn't sink the drain
                        st["errors"] += 1
                        continue
                    if doc is None or not doc.text:
                        st["still_textless"] += 1
                        continue
                    payload_hash = sha256_bytes(doc.text.encode("utf-8"))
                    rec = Record(
                        source="au-cth", stable_id=sid, doc_type=DocType.LEGISLATION,
                        title=doc_row["title"], court=doc_row["court"],
                        language="en", source_language="en",
                        landing_url=doc_row["landing_url"],
                        text=doc.text, segments=doc.segments, payload_hash=payload_hash,
                        extracted_via=ExtractedVia.STRUCTURED, added_by=AddedBy.HARVEST,
                        extra={**meta, "body_repaired": True,
                               "as_at_specification": as_at},
                    )
                    text_path = str(ts.put(payload_hash, doc.text))
                    ts.put_segments(payload_hash, doc.segments)
                    cat.upsert_document(rec, raw_path=doc_row["raw_path"],
                                        text_path=text_path)
                    st["refetched"] += 1
                    if n % 20 == 0:
                        cat.commit()
                cat.commit()
        if st["refetched"] or st["aliases_minted"]:
            self._invalidate_caches()
        return st

    # -- Supreme Court of India (KanoonGPT parquet dump) --------------------
    def import_indian_sci(self, *, dir_path: str, limit: int | None = None,
                          extract: bool = True,
                          on_progress=None, cancel_check=None) -> dict:
        """Import the **Supreme Court of India** slice of the KanoonGPT ``indian-case-laws``
        dump (see :mod:`.adapters.in_caselaw` for why only that slice).

        The dump is ~17M rows across the SCI and 25 High Courts; the predicate
        ``court_code == 'SCI'`` is pushed down to the parquet reader so only the ~43k
        Supreme Court rows are materialised. Those rows are one-per-*report-entry*, so they
        are merged in memory by neutral citation (5,252 citations repeat, up to seven times)
        before anything is written — each judgment becomes one document carrying every
        S.C.R. citation it was reported at.

        What lands: a document keyed ``insc/2020/387`` (the same id the extractor mints for
        "2020 INSC 387"), an alias per Supreme Court Reports citation so report-only
        references resolve, and the judgment PDF's URL in metadata. The headnote is stored
        as text only when it reads as prose — for pre-1960s cases it is garbled OCR — and is
        always flagged ``text_is_headnote`` so a ~600-character snippet is never mistaken
        for the judgment."""
        import pyarrow.compute as pc
        import pyarrow.dataset as pyds

        from .adapters.in_caselaw import SCI_COLUMNS, ParsedSCI, parse_sci_row

        st = {"rows": 0, "judgments": 0, "imported": 0, "updated": 0, "skipped": 0,
              "aliases": 0, "extracted": 0}
        merged: dict[str, ParsedSCI] = {}
        dataset = pyds.dataset(dir_path, format="parquet", partitioning="hive")
        scanner = dataset.scanner(columns=SCI_COLUMNS,
                                  filter=pc.field("court_code") == "SCI",
                                  batch_size=4000)
        for batch in scanner.to_batches():
            if cancel_check and cancel_check():
                break
            d = batch.to_pydict()
            for i in range(batch.num_rows):
                st["rows"] += 1
                parsed = parse_sci_row({c: d[c][i] for c in SCI_COLUMNS})
                if parsed is None:
                    continue
                if parsed.stable_id in merged:
                    merged[parsed.stable_id].merge(parsed)
                else:
                    merged[parsed.stable_id] = parsed
            if st["rows"] % 4000 == 0:
                _progress(on_progress, stage="reading SCI rows", done=st["rows"],
                          total=None, item=f"{len(merged)} judgments")
            if limit and len(merged) >= limit:
                break

        st["judgments"] = len(merged)
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .core.text import fold

        with self._open() as (cat, rs, ts):
            for n, (sid, p) in enumerate(merged.items(), 1):
                if cancel_check and cancel_check():
                    break
                if n % 200 == 0:
                    _progress(on_progress, stage="importing SCI judgments",
                              done=n, total=len(merged), item=sid)
                meta = {
                    "imported": "indian-sci-parquet",
                    "neutral_citation": p.neutral_citation,
                    "report_citations": p.report_citations,
                    "docket_number": p.docket_number, "cnr_number": p.cnr_number,
                    "coram": p.coram, "bench": p.bench, "disposition": p.disposition,
                    "source_pdf_url": p.pdf_url,
                    # The headnote is a truncated (~600 char) snippet, OCR-garbled for older
                    # cases — metadata, never the document's text. Storing it as text would
                    # set has_text and drop every one of these out of the needs-full-text
                    # worklist, which is exactly where they belong until the PDF is fetched.
                    "headnote": p.headnote,
                    "needs_full_text": True,
                }
                # content hash over the metadata that would change on a re-release, so a
                # re-run is a cheap no-op rather than a rewrite of 43k rows.
                fingerprint = sha256_bytes(
                    "|".join([p.title or "", str(p.decision_date or ""), p.pdf_url or "",
                              *sorted(p.report_citations)]).encode("utf-8"))
                existing = cat.get_document(sid)
                if existing is not None and existing["payload_hash"] == fingerprint:
                    st["skipped"] += 1
                else:
                    rec = Record(
                        source="in-caselaw", stable_id=sid, doc_type=DocType.JUDGMENT,
                        title=p.title or sid, court="insc", decision_date=p.decision_date,
                        language="en", source_language="en", landing_url=p.pdf_url,
                        text=None, payload_hash=fingerprint,
                        extracted_via=ExtractedVia.STRUCTURED, added_by=AddedBy.HARVEST,
                        extra={**(cat.document_meta(sid) if existing is not None else {}), **meta},
                    )
                    cat.upsert_document(rec, raw_path=None, text_path=None)
                    st["imported" if existing is None else "updated"] += 1
                # every S.C.R. citation this judgment was reported at resolves to it
                for c in p.report_citations:
                    key = fold(c)
                    if key and key != sid:
                        cat.put_alias(key, sid, source="sci-report-alias", commit=False)
                        st["aliases"] += 1
                if p.neutral_citation:
                    key = fold(p.neutral_citation)
                    if key and key != sid:
                        cat.put_alias(key, sid, source="sci-neutral-alias", commit=False)
                        st["aliases"] += 1
                if n % 200 == 0:
                    cat.commit()
            cat.commit()
            resolved_n = 0
            if extract and not (cancel_check and cancel_check()):
                from .citations import extract_documents_parallel
                aliases = cat.named_alias_map()
                # never-stamped AND no rows: don't re-extract citation-free judgments
                # on every resume (see import_bailii_parquet for the full rationale).
                pending = cat.text_document_ids(source="in-caselaw", only_unextracted=True,
                                                only_never_extracted=True)
                ex = extract_documents_parallel(
                    cat, ts, pending, aliases=aliases,
                    on_progress=on_progress, cancel_check=cancel_check)
                st["extracted"] += ex.processed
                resolved_n = Resolver(cat).run_batched(
                    on_progress=on_progress, cancel_check=cancel_check).resolved
        st["resolved_edges"] = resolved_n
        self._invalidate_caches()
        return st

    # -- Singapore legislation seed (SSO parquet snapshot) ------------------
    def import_sg_seed(self, *, dir_path: str, reconcile: bool = True,
                       limit: int | None = None,
                       on_progress=None, cancel_check=None) -> dict:
        """Seed Singapore legislation from the SSO parquet snapshot (``documents.parquet`` +
        ``sections.parquet``): 2,317 documents, 55,221 sections, parsed from the source PDFs
        so the section text is *complete* (SSO's own HTML lazy-loads large Acts).

        The snapshot's names are **truncated at 50 characters**, which is no good as an
        identity or a stored title. When ``reconcile`` is set (the default) this first pulls
        the live SSO browse listings and matches each truncated name to a full title + the
        SSO act code by prefix — so a document is keyed by its real code (``sg/act/coa1967``),
        carries its full title, and lines up with anything the ongoing harvester later
        fetches. Where a name can't be matched (or matches more than one Act), the document
        falls back to a name-slug id and recovers its full title from its own front matter.

        Idempotent: re-running re-keys nothing already correct and skips unchanged text."""
        import glob
        import os

        import pyarrow.parquet as pq

        from .adapters.sg_legislation import (
            SGLegislationAdapter, name_key, sg_act_id, sg_landing_url, sg_sl_id,
            title_from_frontmatter,
        )
        from .core.models import (AddedBy, DocType, ExtractedVia, Record, Segment,
                                  sha256_bytes)
        from .core.text import fold

        docs_pq = os.path.join(dir_path, "documents.parquet")
        secs_pq = os.path.join(dir_path, "sections.parquet")
        if not (os.path.exists(docs_pq) and os.path.exists(secs_pq)):
            return {"error": f"expected documents.parquet + sections.parquet under {dir_path}"}

        st = {"documents": 0, "imported": 0, "skipped": 0, "sections": 0,
              "reconciled": 0, "frontmatter_title": 0, "unmatched": 0, "aliases": 0}

        # -- 1. build the name→(code,title) index from the live browse listings --
        act_index: dict[str, tuple[str, str]] = {}   # name_key → (code, full_title)
        sl_index: dict[str, tuple[str, str]] = {}
        ambiguous: set[str] = set()
        if reconcile and not (cancel_check and cancel_check()):
            for subsidiary, index in ((False, act_index), (True, sl_index)):
                adapter = SGLegislationAdapter(subsidiary=subsidiary)
                _progress(on_progress, stage="indexing SSO browse listing",
                          done=0, total=0, item="SL" if subsidiary else "Act")
                try:
                    for e in adapter.browse_index():
                        k = name_key(e.title)
                        if k in index and index[k][0] != e.code:
                            ambiguous.add(k)
                        index[k] = (e.code, e.title)
                except Exception:  # noqa: BLE001 — reconciliation is best-effort; seed still lands
                    pass

        def _lookup(name: str, seed_subsidiary: bool) -> tuple[str, str, bool] | None:
            """Match a truncated seed name to (code, full_title, subsidiary).

            Searches **both** the Act and SL indexes rather than trusting the seed's
            ``doc_type``, which is unreliable (it labels some subsidiary legislation as an
            Act). The index a name matches in is the true classification. An exact key wins;
            otherwise a truncated name resolves iff exactly one full title across both
            indexes starts with it — the whole-corpus uniqueness is what makes a 50-char
            prefix safe to trust. The seed's own flag only breaks a tie between two indexes."""
            k = name_key(name)
            if len(k) < 6:
                return None
            candidates: list[tuple[str, str, bool]] = []
            for index, sub in ((act_index, False), (sl_index, True)):
                if k in index and k not in ambiguous:
                    candidates.append((*index[k], sub))
            if len(candidates) == 1:
                return candidates[0]
            if candidates:   # exact match in both indexes → trust the seed's flag
                return next((c for c in candidates if c[2] == seed_subsidiary), candidates[0])
            # prefix match across both indexes, unique
            hits = [(code, title, sub)
                    for index, sub in ((act_index, False), (sl_index, True))
                    for kk, (code, title) in index.items()
                    if len(k) >= 12 and kk.startswith(k)]
            uniq = {c[0]: c for c in hits}
            return next(iter(uniq.values())) if len(uniq) == 1 else None

        # -- 2. read the section rows, grouped by document (file order = document order) --
        wanted = ["doc_name", "doc_type", "parent_act", "section_title", "part",
                  "division", "text"]
        # documents.parquet order is the import order; sections.parquet is grouped by doc.
        from collections import OrderedDict
        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        for batch in pq.ParquetFile(secs_pq).iter_batches(batch_size=8000, columns=wanted):
            d = batch.to_pydict()
            for i in range(len(d["doc_name"])):
                groups.setdefault(d["doc_name"][i], []).append(
                    {k: d[k][i] for k in wanted})
        st["documents"] = len(groups)

        with self._open() as (cat, rs, ts):
            for n, (doc_name, rows) in enumerate(groups.items(), 1):
                if cancel_check and cancel_check():
                    break
                if limit and n > limit:
                    break
                seed_subsidiary = (rows[0].get("doc_type") or "") == "subsidiary_legislation"
                parent = (rows[0].get("parent_act") or "").strip() or None
                if n % 50 == 0:
                    _progress(on_progress, stage="importing SG legislation",
                              done=n, total=len(groups), item=doc_name)

                # text + per-section segments (skip the "Unsectioned" front matter as a
                # section, but keep it for title recovery)
                parts: list[str] = []
                segs: list[Segment] = []
                cursor = 0
                frontmatter = ""
                for r in rows:
                    body = (r.get("text") or "").strip()
                    if not body:
                        continue
                    stitle = (r.get("section_title") or "").strip()
                    if stitle.lower() == "unsectioned" and not frontmatter:
                        frontmatter = body
                    if parts:
                        cursor += 2
                    label = stitle or "section"
                    segs.append(Segment(label=label, char_start=cursor,
                                        char_end=cursor + len(body),
                                        kind="section", level=1))
                    parts.append(body)
                    cursor += len(body)
                text = "\n\n".join(parts)
                if not text:
                    st["skipped"] += 1
                    continue
                st["sections"] += len(segs)

                # identity + full title — the match decides act vs SL (seed doc_type is
                # unreliable); fall back to the seed's flag only when nothing matched.
                match = _lookup(doc_name, seed_subsidiary)
                if match:
                    code, full_title, subsidiary = match
                    stable_id = (sg_sl_id if subsidiary else sg_act_id)(code)
                    landing = sg_landing_url(code, subsidiary=subsidiary)
                    st["reconciled"] += 1
                else:
                    subsidiary = seed_subsidiary
                    full_title = title_from_frontmatter(frontmatter)
                    if full_title:
                        st["frontmatter_title"] += 1
                    else:
                        full_title = doc_name
                    code = None
                    stable_id = f"sg/{'sl' if subsidiary else 'act'}/{fold(name_key(doc_name)).replace(' ', '-')}"
                    landing = None
                    st["unmatched"] += 1

                payload_hash = sha256_bytes(text.encode("utf-8"))
                existing = cat.get_document(stable_id)
                if existing is not None and existing["payload_hash"] == payload_hash:
                    st["skipped"] += 1
                    continue
                meta = {**(cat.document_meta(stable_id) if existing is not None else {}),
                        "jurisdiction": "sg", "imported": "sg-seed",
                        "subsidiary_legislation": subsidiary,
                        "parent_act": parent, "sso_code": code,
                        "seed_name_truncated": doc_name,
                        "is_authoritative": False,
                        "sso_terms": "https://sso.agc.gov.sg/Terms-of-Use"}
                rec = Record(
                    source="sg-legislation", stable_id=stable_id,
                    doc_type=DocType.LEGISLATION, title=full_title, court=None,
                    language="en", source_language="en", landing_url=landing,
                    text=text, segments=segs, payload_hash=payload_hash,
                    extracted_via=ExtractedVia.STRUCTURED, added_by=AddedBy.HARVEST,
                    extra=meta)
                text_path = str(ts.put(payload_hash, text))
                ts.put_segments(payload_hash, segs)
                cat.upsert_document(rec, raw_path=None, text_path=text_path)
                st["imported"] += 1
                # the truncated seed name resolves to the document too
                if code:
                    key = fold(name_key(doc_name))
                    if key and cat.get_alias(key) is None:
                        cat.put_alias(key, stable_id, source="sg-seed-name", commit=False)
                        st["aliases"] += 1
                if n % 100 == 0:
                    cat.commit()
            cat.commit()
        self._invalidate_caches()
        return st

    # -- outbound LII links (§5b) -------------------------------------------
    def lii_links_for(self, stable_id: str) -> list[dict]:
        """Canonical LII URLs for one held document. Prefers the landing URL the importer
        actually recorded (exact, including any case-sensitive filename quirk) and falls
        back to constructing the URL from the slug."""
        from .citations.lii import lii_links

        with self._open() as (cat, _rs, _ts):
            doc = cat.get_document(stable_id)
            meta = cat.document_meta(stable_id) if doc is not None else {}
        out: list[dict] = []
        recorded = (doc["landing_url"] if doc is not None else None) or meta.get("bailii_url")
        if recorded and "bailii.org" in recorded:
            out.append({"site": "bailii", "site_name": "BAILII", "url": recorded,
                        "certainty": "recorded"})
        # CanLII links verified through the API (canlii_enrich / the ca-canlii
        # adapter) beat a constructed guess: the short canlii.ca permalink survives
        # site reorganisations, and the recorded long URL is known to exist.
        for url in (meta.get("canlii_url"), meta.get("canlii_long_url"),
                    recorded if recorded and "canlii.org" in recorded else None):
            if url and not any(o["url"] == url for o in out):
                out.append({"site": "canlii", "site_name": "CanLII", "url": url,
                            "certainty": "recorded"})
        for link in lii_links(stable_id, court=(doc["court"] if doc is not None else None)):
            if not any(o["url"] == link.url for o in out):
                out.append({"site": link.site, "site_name": link.site_name,
                            "url": link.url, "certainty": link.certainty})
        return out

    def reference_links(self, *, ref: str, raw: str | None = None) -> dict:
        """External LII links for a reference that ISN'T held — the sidebar's "read it here"
        for an unfetched or unfetchable case. Constructs the direct LII page(s) from a
        neutral-citation slug where one can be (AustLII / NZLII / CanLII / SAFLII / HKLII /
        PacLII / CommonLII / BAILII), and always adds the single best fallback the harvest
        list uses (a jurisdiction-appropriate search, or a BAILII search for a classic
        report). Pure string work — no network — so it's cheap to call on every peek."""
        from .adapters.bailii import external_link
        from .citations.lii import lii_links

        slug = (ref or "").strip()
        raw_s = (raw or "").strip() or None
        # a slug-shaped ref ("nzsc/2012/12") is a neutral citation we can build direct
        # pages from; a bare name or a raw report citation is not.
        cand = slug if ("/" in slug and not slug.lower().startswith("http")) else None
        out: list[dict] = []
        for link in lii_links(cand or ""):
            out.append({"site": link.site, "site_name": link.site_name, "url": link.url,
                        "certainty": link.certainty, "kind": "lii"})
        best = external_link(cand, raw_s or slug)
        if best and not any(o["url"] == best["url"] for o in out):
            out.append({"site": best.get("site"),
                        "site_name": (best.get("label") or "").replace(" ↗", "").replace(" ↓", ""),
                        "url": best["url"], "certainty": best.get("certainty"),
                        "kind": best.get("kind"), "can_upload": best.get("can_upload")})
        return {"ref": ref, "links": out}

    def lii_link_targets(self, *, scope: str = "unheld", limit: int = 5000,
                         sites: list[str] | None = None) -> list[dict]:
        """The worklist for fetching missing full text from the LIIs.

        ``scope`` picks the target set: ``unheld`` (cases the corpus cites but does not
        hold), ``textless`` (held records that are a name and citation with no judgment
        text), or ``both``. Rows come back most-cited first, so working down the list
        retrieves the cases the corpus actually leans on.

        Each row carries a ``filename`` — the slug with ``/`` replaced by ``_`` — which is
        what makes the manual round-trip work: save each page under that name and the
        importer can recover the document's identity from the filename alone, with no
        mapping file to keep in step."""
        from .citations.lii import lii_links

        rows: list[dict] = []
        want = {s.lower() for s in sites} if sites else None
        with self._open() as (cat, _rs, _ts):
            targets: list[tuple[str, str | None, str | None, int, str]] = []
            if scope in ("unheld", "both"):
                for r in cat.unheld_case_candidates(limit=limit):
                    targets.append((r["candidate"], None, r["raw"], r["citing_count"], "unheld"))
            if scope in ("textless", "both"):
                for r in cat.textless_case_documents(limit=limit):
                    targets.append((r["stable_id"], r["title"], None,
                                    r["citing_count"] or 0, "held-no-text"))
            for slug, title, raw, citing, kind in targets:
                for link in lii_links(slug):
                    if want and link.site not in want:
                        continue
                    rows.append({
                        "stable_id": slug,
                        "title": title,
                        "citation": raw,
                        "status": kind,
                        "citing_count": citing,
                        "site": link.site,
                        "site_name": link.site_name,
                        "url": link.url,
                        "certainty": link.certainty,
                        "filename": slug.replace("/", "_") + ".html",
                    })
        rows.sort(key=lambda r: (-r["citing_count"], r["stable_id"]))
        return rows

    @staticmethod
    def _text_len(ts, doc) -> int:
        """Character length of a held document's primary text (0 if unreadable)."""
        if not doc["payload_hash"]:
            return 0
        try:
            return len(ts.get(doc["payload_hash"]))
        except OSError:
            return 0

    # -- BAILII parquet-dump import (§1.9, the bulk sibling of the saved-page path) ----
    def import_bailii_parquet(self, *, dir_path: str, databases: list[str] | None = None,
                              exclude_databases: list[str] | None = None,
                              limit: int | None = None, start_row: int = 0,
                              batch_size: int = 200, extract: bool = True,
                              on_progress=None, cancel_check=None) -> dict:
        """Import a *BAILII parquet dump* — a bulk Scrapy crawl of bailii.org exported as
        Parquet shards (the ``bailii_260505`` dataset: ~551k rows, columns ``path`` /
        ``title`` / ``citation`` / ``date`` / ``court`` / ``html_content`` …). It is the
        columnar counterpart of :meth:`import_bailii_zip`: same synthesis against the
        corpus (import / supersede / secondary), but fed from parquet rows instead of
        saved pages, because this crawl kept no ``Cite as:`` header to parse.

        What this route adds over the saved-page one:

        * **reporter equivalence at scale** — each case's ICLR parallel-report citations
          (``[2009] 1 WLR 348``) survive as in-body links, decoded and minted as
          self-aliases so report-only references resolve to the neutral-citation case;
        * **identity reconciliation** — an EU judgment's ``ECLI`` (from its ``<meta>``)
          and an ECtHR case's application number are used to attach the BAILII page (often
          an English text of an otherwise French/originating judgment) to the case RAGLex
          already holds under its ECLI, rather than minting a slug-keyed duplicate;
        * **the tribunal long tail** Find Case Law never carried — UKAITUR/UKEAT/UKET, the
          tax tribunals, the Scottish/NI courts, and the Crown-Dependency / offshore
          commercial courts (Jersey, Cayman, DIFC/ADGM, Qatar, St Helena, the SICC).

        ``databases`` / ``exclude_databases`` filter by the dump's ``database_name`` column
        (e.g. ``exclude_databases=["UKAITUR"]`` to skip the asylum-tribunal bulk). Only
        ``/…/cases/…`` rows are imported; legislation and treaty rows are ignored (RAGLex
        sources legislation natively).

        **Resuming.** A half-million-row import is long enough to be interrupted (a restart,
        an OOM kill), so it is built to be re-launched rather than redone:

        * ``start_row`` skips that many rows before doing any work — the dump is a static
          snapshot read in a stable order (shards sorted by name, rows in file order), so
          the ``done`` count a previous run reported is exactly the offset to resume from,
          and skipping costs nothing but the scan;
        * the per-document synthesis is idempotent anyway — an unchanged document short-
          circuits on its payload hash — so an overlapping resume range is safe;
        * extraction is **not** held in memory. The earlier design queued every imported id
          and extracted at the end, which meant an interrupted run lost the whole queue and
          left thousands of documents with text but no edges. Instead the extraction pass
          selects documents that have no citation rows (``only_unextracted``), so it always
          picks up exactly the backlog — including one left by a previous crashed run. Pass
          ``extract=False`` to import only and run the extraction later as its own job.

        ``batch_size`` bounds peak memory: rows are materialised a batch at a time and the
        dump holds documents up to ~6 MB, so a large batch can spike badly (a 2000-row batch
        was enough to OOM the box)."""
        import glob
        import os
        import pyarrow.parquet as pq

        from .adapters.bailii_parquet import parse_parquet_row

        shards = sorted(glob.glob(os.path.join(dir_path, "**", "*.parquet"), recursive=True))
        if not shards:
            return {"total": 0, "error": f"no .parquet shards under {dir_path}"}
        include = {d.lower() for d in databases} if databases else None
        exclude = {d.lower() for d in (exclude_databases or [])}
        total = sum(pq.ParquetFile(s).metadata.num_rows for s in shards)

        cols = ["path", "title", "citation", "date", "court", "database_name", "html_content"]
        st = {"total": 0, "rows_scanned": 0, "resumed_at": start_row, "imported": 0,
              "superseded": 0, "secondary": 0, "enriched": 0, "stub": 0, "skipped": 0,
              "unparseable": 0, "aliases": 0, "extracted": 0}
        files: list[dict] = []
        with self._open() as (cat, rs, ts):
            seen = 0
            for shard in shards:
                if cancel_check and cancel_check():
                    break
                pf = pq.ParquetFile(shard)
                # whole shards before the resume point are skipped without being read
                shard_rows = pf.metadata.num_rows
                if seen + shard_rows <= start_row:
                    seen += shard_rows
                    continue
                for batch in pf.iter_batches(batch_size=batch_size, columns=cols):
                    if cancel_check and cancel_check():
                        break
                    n_rows = batch.num_rows
                    if seen + n_rows <= start_row:      # batch entirely before the cursor
                        seen += n_rows
                        continue
                    d = batch.to_pydict()
                    for i in range(n_rows):
                        seen += 1
                        if seen <= start_row:
                            continue
                        if seen % 500 == 0:
                            _progress(on_progress, stage="importing", done=seen,
                                      total=total, item=d["path"][i])
                        db = (d["database_name"][i] or "").lower()
                        if (include is not None and db not in include) or db in exclude:
                            continue
                        row = {c: d[c][i] for c in cols}
                        try:
                            parsed = parse_parquet_row(row)
                        except Exception as exc:  # noqa: BLE001 — one bad row mustn't sink the batch
                            parsed = None
                            if len(files) < 500:
                                files.append({"path": row["path"], "disposition": "error",
                                              "error": str(exc)})
                        if parsed is None:
                            continue
                        st["total"] += 1
                        self._ingest_bailii_row(
                            cat, rs, ts, parsed=parsed,
                            raw_bytes=(row["html_content"] or "").encode("utf-8"),
                            st=st, files=files)
                        if st["total"] % 200 == 0:
                            cat.commit()
                        if limit and st["total"] >= limit:
                            break
                    d = None                      # release the batch's Python copies
                    if limit and st["total"] >= limit:
                        break
                if limit and st["total"] >= limit:
                    break
            cat.commit()
            st["rows_scanned"] = seen
            # Extraction backlog straight from the database rather than an in-memory queue:
            # every case-law document that has text but no citation rows. That set is exactly
            # what this run imported PLUS anything a previously-interrupted run left behind,
            # so re-launching after a crash converges instead of starting over.
            resolved_n = 0
            if extract and not (cancel_check and cancel_check()):
                from .citations import extract_documents_parallel
                aliases = cat.named_alias_map()
                # The backlog is "never stamped AND no citation rows". The old
                # ``only_unextracted``-only select re-picked every legitimately
                # citation-free judgment on each resume — over the tribunal long tail
                # that re-extracted a large slice of the 551k dump per relaunch. The
                # stamp alone would instead sweep in every pre-stamp-era FCL document
                # (extracted, cited, but never stamped); ANDing both selects exactly
                # the unfinished remainder.
                pending = cat.text_document_ids(doc_types=["judgment"],
                                                only_unextracted=True,
                                                only_never_extracted=True)
                ex = extract_documents_parallel(
                    cat, ts, pending, aliases=aliases,
                    on_progress=on_progress, cancel_check=cancel_check)
                st["extracted"] += ex.processed
                # Bounded, cancellable relation ranges with real progress — not one
                # whole-graph UPDATE in a single transaction reported as "0/0".
                resolved_n = Resolver(cat).run_batched(
                    on_progress=on_progress, cancel_check=cancel_check).resolved
        st["resolved_edges"] = resolved_n
        st["files"] = files
        self._invalidate_caches()
        return st

    def _ingest_bailii_row(self, cat, rs, ts, *, parsed, raw_bytes: bytes,
                           st: dict, files: list) -> None:
        """Synthesise one parsed parquet row against the corpus. Mirrors the saved-page
        importer's disposition ladder (import / supersede / secondary / stub) but keys by
        the row's reconciled identity: an EU case under its ECLI, an ECtHR case matched via
        its application number to the already-held ECLI, everything else by slug."""
        from .adapters.uk_caselaw import court_from_slug
        from .citations import extract_citations
        from .citations.name_variants import name_variants
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .core.text import fold
        from .pipeline.runner import _chamberless_alias
        from .resolve.matchers import first_candidate

        slug, title = parsed.slug, parsed.title

        # -- reconcile identity: is this case already held under another id? --------
        target = parsed.primary_id
        existing = cat.get_document(target)
        if existing is None and target != slug:
            existing = cat.get_document(slug)
            if existing is not None:
                target = slug
        # ECHR pages carry no ECLI, but their application number bridges to the held
        # ECLI:CE:ECHR:… case (the echr adapter mints appno→id aliases).
        if existing is None and parsed.source == "echr" and parsed.appno:
            dst = cat.get_alias(parsed.appno)
            if dst and dst != slug:
                held = cat.get_document(dst)
                if held is not None:
                    target, existing = dst, held

        # -- alias ladder: distinctive name variants + self-citations + chamberless -
        alias_pairs: list[tuple[str, str]] = []
        for v, kind in name_variants(title or ""):
            if kind not in self._BAILII_ALIAS_KINDS:
                continue
            key = fold(v)
            if key and key != target:
                alias_pairs.append((key, f"bailii-name:{kind}"))
        for c in parsed.self_citations:
            cand = first_candidate(c)
            key = fold(cand.value) if cand else fold(c)
            if key and key != target:
                alias_pairs.append((key, "bailii-report-alias"))
        if parsed.appno:
            alias_pairs.append((parsed.appno, "bailii-echr-appno"))
        for extra_id in (slug, parsed.ecli):
            if extra_id and extra_id != target:
                alias_pairs.append((fold(extra_id), "bailii-id"))
        bare = _chamberless_alias(slug)
        if bare and bare != slug and bare != target:
            alias_pairs.append((bare, "chamber-alias"))

        def _mint(dst_id: str) -> None:
            for key, source in alias_pairs:
                cat.put_alias(key, dst_id, source=source, commit=False)
                st["aliases"] += 1

        new_meta = {"imported": "bailii-parquet", "bailii_url": parsed.bailii_url,
                    "bailii_citations": list(parsed.self_citations),
                    "bailii_court": parsed.court_label}

        # -- stub (no transcript): keep identity + aliases, never store junk as text --
        if parsed.pdf_only or not parsed.text.strip():
            if existing is not None and existing["has_text"]:
                meta = cat.document_meta(target)
                if parsed.pdf_url:
                    meta.setdefault("bailii_pdf_url", parsed.pdf_url)
                cat.set_document_meta(target, meta, commit=False)
                disp = "stub-skipped"
            else:
                stub_meta = {**(cat.document_meta(target) if existing is not None else {}),
                             **new_meta, "needs_pdf": bool(parsed.pdf_url),
                             "bailii_pdf_url": parsed.pdf_url}
                rec = Record(
                    source=parsed.source, stable_id=target, doc_type=DocType.JUDGMENT,
                    title=title or (existing["title"] if existing is not None else None) or target,
                    court=court_from_slug(slug), decision_date=parsed.decision_date,
                    language="en", source_language="en", landing_url=parsed.bailii_url,
                    raw_bytes=raw_bytes, raw_ext="html", payload_hash=sha256_bytes(raw_bytes),
                    text=None, segments=[], extracted_via=ExtractedVia.SCRAPE,
                    added_by=AddedBy.USER, extra=stub_meta)
                raw_path = str(rs.path_for(rs.put(raw_bytes, ext="html"), "html"))
                cat.upsert_document(rec, raw_path=raw_path, text_path=None)
                disp = "stub"
            st["stub"] += 1
            _mint(target)
            if len(files) < 500:
                files.append({"path": parsed.bailii_url, "stable_id": target,
                              "title": title, "disposition": disp})
            return

        payload_hash = sha256_bytes(parsed.text.encode("utf-8"))
        old_meta = cat.document_meta(target) if existing is not None else {}

        # already exactly this text — just top up aliases.
        if existing is not None and existing["payload_hash"] == payload_hash:
            _mint(target)
            st["skipped"] += 1
            return

        if existing is None or self._bailii_html_supersedes(
                existing, old_meta, len(parsed.text),
                self._text_len(ts, existing) if existing is not None else 0):
            meta = {**old_meta, **new_meta}
            if existing is not None and existing["has_text"] and \
                    existing["payload_hash"] != payload_hash:
                alts = meta.get("alt_texts", [])
                if not any(a.get("payload_hash") == existing["payload_hash"] for a in alts):
                    alts.append({"source": existing["source"],
                                 "payload_hash": existing["payload_hash"],
                                 "text_path": existing["text_path"]})
                meta["alt_texts"] = alts
            rec = Record(
                source=(existing["source"] if existing is not None else parsed.source),
                stable_id=target, doc_type=DocType.JUDGMENT,
                title=title or (existing["title"] if existing is not None else None) or target,
                court=court_from_slug(slug), decision_date=parsed.decision_date,
                language="en", source_language="en", landing_url=parsed.bailii_url,
                raw_bytes=raw_bytes, raw_ext="html", payload_hash=payload_hash,
                text=parsed.text, segments=parsed.segments,
                extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER, extra=meta)
            raw_path = str(rs.path_for(rs.put(raw_bytes, ext="html"), "html"))
            text_path = str(ts.put(payload_hash, parsed.text))
            ts.put_segments(payload_hash, parsed.segments)
            cat.upsert_document(rec, raw_path=raw_path, text_path=text_path)
            # no in-memory extraction queue: the run's extraction pass finds this document
            # (text, no citation rows) by query, so an interrupted run loses nothing.
            disp = "imported" if existing is None else "superseded"
            st["imported" if existing is None else "superseded"] += 1
        else:
            # held authoritatively (Find Case Law XML, eu-cellar, echr) — attach the BAILII
            # text as a secondary rendition (often the English text of an EU/ECHR case) and
            # merge metadata; the identity + report aliases still land.
            text_path = str(ts.put(payload_hash, parsed.text))
            alts = old_meta.get("alt_texts", [])
            if not any(a.get("payload_hash") == payload_hash for a in alts):
                alts.append({"source": "bailii-parquet", "payload_hash": payload_hash,
                             "text_path": text_path, "chars": len(parsed.text)})
            old_meta["alt_texts"] = alts
            for k, v in new_meta.items():
                old_meta.setdefault(k, v)
            cat.set_document_meta(target, old_meta, title_if_empty=title, commit=False)
            disp = "enriched" if target != slug else "secondary"
            st["enriched" if target != slug else "secondary"] += 1

        _mint(target)
        if len(files) < 500:
            files.append({"path": parsed.bailii_url, "stable_id": target, "title": title,
                          "citations": list(parsed.self_citations), "disposition": disp})

    # -- Westlaw RTF import (§1.9, sibling of the BAILII-page path) ---------
    def import_westlaw_zip(self, *, zip_path: str, limit: int | None = None,
                           on_progress=None, cancel_check=None) -> dict:
        """Import a **zip of Westlaw ``.rtf`` exports** — the counterpart to
        :meth:`import_bailii_zip` for the other big source of older UK judgments. Each
        RTF is parsed (:func:`parse_westlaw_rtf`), keyed by its strongest identity
        (neutral-citation slug → ECLI → Westlaw-id surrogate), synthesised against the
        corpus, then extracted + resolved once at the end."""
        import zipfile

        with zipfile.ZipFile(zip_path) as zf:
            infos = [i for i in zf.infolist()
                     if not i.is_dir()
                     and i.filename.lower().endswith((".rtf", ".doc"))
                     and not i.filename.startswith("__MACOSX")
                     and "/." not in "/" + i.filename]
            if limit:
                infos = infos[:limit]

            def _entries():
                for info in infos:
                    yield info.filename, zf.read(info)

            return self._import_westlaw_files(_entries(), total=len(infos),
                                              on_progress=on_progress, cancel_check=cancel_check)

    def import_westlaw_dir(self, *, dir_path: str, limit: int | None = None,
                           on_progress=None, cancel_check=None) -> dict:
        """Same synthesis as :meth:`import_westlaw_zip`, over a **directory** of ``.rtf``
        exports (recursively) — the no-zip path for a Finder folder the web UI streamed
        up in batches."""
        import os

        paths: list[str] = []
        for root, _dirs, names in os.walk(dir_path):
            for nm in names:
                if nm.lower().endswith((".rtf", ".doc")) and not nm.startswith("."):
                    paths.append(os.path.join(root, nm))
        paths.sort()
        if limit:
            paths = paths[:limit]

        def _entries():
            for p in paths:
                with open(p, "rb") as fh:
                    yield os.path.basename(p), fh.read()

        return self._import_westlaw_files(_entries(), total=len(paths),
                                          on_progress=on_progress, cancel_check=cancel_check)

    @staticmethod
    def _westlaw_supersedes(existing, existing_meta: dict, new_len: int, old_len: int) -> bool:
        """Should a parsed Westlaw RTF REPLACE the held text for its id? Yes when the held
        copy is a lower-fidelity import (a BAILII page/dump, a manual upload, a prior
        Westlaw RTF, or textless). A HoL scrape is replaced only by a comparably long
        copy. An authoritative primary source — Find Case Law XML (uk-caselaw) or CELLAR
        (eu-cellar) — stays; the Westlaw text attaches as a secondary rendition and only
        its rich metadata (parallel citations, counsel, subjects) is merged."""
        if not existing["has_text"]:
            return True
        if existing_meta.get("imported") in (
                "bailii-corpus", "bailii-html", "bailii-pdf-stub", "westlaw-rtf"):
            return True
        if existing_meta.get("via") == "bailii-upload":
            return True
        if existing["source"] == "user-import":
            return True
        if existing["source"] == "uk-hol":
            return new_len >= 0.8 * old_len
        return False

    @staticmethod
    def _westlaw_meta(parsed) -> dict:
        """The structured Westlaw fields worth keeping in ``documents.meta_json`` —
        everything the RTF states that the bare judgment text does not."""
        fields = {
            "party_full": parsed.party_full,
            "also_known_as": list(parsed.also_known_as),
            "court_label": parsed.court_label,
            "report_citations": list(parsed.report_citations),
            "neutral_citation": parsed.neutral_citation,
            "ecli": parsed.ecli,
            "case_number": parsed.case_number,
            "wl_number": parsed.wl_number,
            "judges": list(parsed.judges),
            "counsel": list(parsed.counsel),
            "solicitors": list(parsed.solicitors),
            "subjects": list(parsed.subjects),
            "keywords": list(parsed.keywords),
        }
        return {k: v for k, v in fields.items() if v}

    def _import_westlaw_files(self, entries, *, total: int,
                              on_progress=None, cancel_check=None) -> dict:
        """The shared Westlaw-RTF importer: consume ``entries`` (an iterable of
        ``(filename, rtf_bytes)``), synthesising each against the corpus, then extract +
        resolve once at the end. Both the zip and the directory paths feed it the same
        stream — the exact shape of :meth:`_import_bailii_pages`, differing only in the
        identity ladder (citation-keyed, not FCL-slug-keyed) and the richer metadata."""
        from .adapters.uk_caselaw import court_from_slug
        from .adapters.westlaw_rtf import parse_westlaw_rtf, westlaw_identity
        from .citations import extract_document
        from .citations.courts import IRISH_COURTS
        from .citations.name_variants import name_variants
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .pipeline.runner import _chamberless_alias
        from .resolve.matchers import first_candidate
        from .core.text import fold

        from .adapters.westlaw_legislation import parse_westlaw_legislation

        st = {"total": 0, "imported": 0, "superseded": 0, "secondary": 0,
              "merged": 0, "unparseable": 0, "aliases": 0, "extracted": 0, "legislation": 0}
        files: list[dict] = []
        # A Westlaw folder can mix case law and legislation. Acts are deferred to a second
        # pass so the Act importer opens its own session rather than nesting one.
        leg_entries: list[tuple[str, bytes]] = []
        with self._open() as (cat, rs, ts):
            to_extract: list[str] = []
            for n, (filename, data) in enumerate(entries, 1):
                if cancel_check and cancel_check():
                    break
                st["total"] += 1
                _progress(on_progress, stage="importing", done=n, total=total, item=filename)
                try:
                    if parse_westlaw_legislation(data, filename=filename) is not None:
                        leg_entries.append((filename, data))
                        continue
                except Exception:  # noqa: BLE001 — fall through to the case parser
                    pass
                try:
                    parsed = parse_westlaw_rtf(data, filename=filename)
                except Exception as exc:  # noqa: BLE001 — one bad file mustn't sink the batch
                    parsed = None
                    if len(files) < 1000:
                        files.append({"file": filename, "disposition": "error", "error": str(exc)})
                if parsed is None or not parsed.text.strip():
                    st["unparseable"] += 1
                    if parsed is not None and len(files) < 1000:
                        files.append({"file": filename, "disposition": "unparseable",
                                      "title": parsed.title})
                    continue

                stable_id, id_kind = westlaw_identity(parsed)

                # aliases: distinctive name variants + every parallel citation + the
                # Westlaw/ECLI/CJEU ids + (for a neutral id) the chamber-less slug.
                alias_pairs: list[tuple[str, str]] = []
                for v, kind in name_variants(parsed.title or ""):
                    if kind not in self._BAILII_ALIAS_KINDS:
                        continue
                    key = fold(v)
                    if key and key != stable_id:
                        alias_pairs.append((key, f"westlaw-name:{kind}"))
                for c in parsed.report_citations:
                    cand = first_candidate(c)
                    key = fold(cand.value) if cand else fold(c)
                    if key and key != stable_id:
                        alias_pairs.append((key, "westlaw-report-alias"))
                for ident in (parsed.wl_number, parsed.ecli, parsed.case_number):
                    if ident:
                        key = fold(ident)
                        if key and key != stable_id:
                            alias_pairs.append((key, "westlaw-id"))
                if id_kind == "neutral":
                    bare = _chamberless_alias(stable_id)
                    if bare and bare != stable_id:
                        alias_pairs.append((bare, "chamber-alias"))

                # A pre-neutral case has only a surrogate id to key by (a Westlaw id, a
                # slugged report citation, or a content hash) — but if any of its PRECISE
                # identifiers already points at a held document (the same case from
                # BAILII/ICLR/CELLAR, or a prior import), adopt that id and merge into it
                # rather than minting a duplicate. Precise = a parallel report citation, a
                # Westlaw/ECLI/CJEU id, or the chamber-less slug; a bare party-name variant
                # is deliberately NOT enough to merge on ("Harris v Harris", "Thomas v
                # Thomas" name many distinct cases), so name aliases are skipped.
                if id_kind in ("wl", "report", "hash"):
                    for key, src in alias_pairs:
                        if src.startswith("westlaw-name:"):
                            continue
                        # the key may already be an alias of a held doc, or itself be a
                        # held doc's id (an ECLI / report-slug / neutral slug).
                        held = cat.get_alias(key)
                        if held is None and cat.get_document(key) is not None:
                            held = key
                        if held and held != stable_id and cat.get_document(held) is not None:
                            stable_id, id_kind = held, "merged"
                            break

                payload_hash = sha256_bytes(parsed.text.encode("utf-8"))
                head = stable_id.split("/", 1)[0]
                source = ("eu-cellar" if parsed.is_eu
                          else "ie-caselaw" if head in IRISH_COURTS else "uk-caselaw")
                new_meta = {"imported": "westlaw-rtf", "westlaw": self._westlaw_meta(parsed)}
                existing = cat.get_document(stable_id)
                old_meta = cat.document_meta(stable_id) if existing is not None else {}

                if existing is not None and existing["payload_hash"] == payload_hash:
                    for key, src in alias_pairs:
                        cat.put_alias(key, stable_id, source=src, commit=False)
                        st["aliases"] += 1
                    st["unchanged"] = st.get("unchanged", 0) + 1
                    if len(files) < 1000:
                        files.append({"file": filename, "stable_id": stable_id,
                                      "title": parsed.title, "disposition": "unchanged"})
                    continue

                if existing is None or self._westlaw_supersedes(
                        existing, old_meta, len(parsed.text),
                        self._text_len(ts, existing) if existing is not None else 0):
                    meta = {**old_meta, **new_meta}
                    if existing is not None and existing["has_text"] and \
                            existing["payload_hash"] != payload_hash:
                        alts = meta.get("alt_texts", [])
                        if not any(a.get("payload_hash") == existing["payload_hash"] for a in alts):
                            alts.append({"source": existing["source"],
                                         "payload_hash": existing["payload_hash"],
                                         "text_path": existing["text_path"]})
                        meta["alt_texts"] = alts
                    record = Record(
                        source=source, stable_id=stable_id, doc_type=DocType.JUDGMENT,
                        title=parsed.title or (existing["title"] if existing is not None else None) or stable_id,
                        court=court_from_slug(stable_id) or parsed.court_code,
                        decision_date=parsed.decision_date,
                        language="en", source_language="en",
                        raw_bytes=data, raw_ext="rtf", payload_hash=payload_hash,
                        text=parsed.text, segments=parsed.segments,
                        extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
                        extra=meta,
                    )
                    raw_path = str(rs.path_for(rs.put(data, ext="rtf"), "rtf"))
                    text_path = str(ts.put(payload_hash, parsed.text))
                    ts.put_segments(payload_hash, parsed.segments)
                    cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
                    to_extract.append(stable_id)
                    if existing is None:
                        disposition, key = "imported", "imported"
                    elif id_kind == "merged":
                        disposition, key = "merged", "merged"
                    else:
                        disposition, key = "superseded", "superseded"
                    st[key] += 1
                else:
                    # held authoritatively (FCL XML / CELLAR) — attach as secondary text,
                    # merge the richer Westlaw metadata, keep the parallel-citation aliases.
                    text_path = str(ts.put(payload_hash, parsed.text))
                    alts = old_meta.get("alt_texts", [])
                    if not any(a.get("payload_hash") == payload_hash for a in alts):
                        alts.append({"source": "westlaw-rtf", "payload_hash": payload_hash,
                                     "text_path": text_path, "chars": len(parsed.text)})
                    old_meta["alt_texts"] = alts
                    for k, v in new_meta.items():
                        old_meta.setdefault(k, v)
                    cat.set_document_meta(stable_id, old_meta, title_if_empty=parsed.title, commit=False)
                    disposition = "secondary"
                    st["secondary"] += 1

                for key, src in alias_pairs:
                    cat.put_alias(key, stable_id, source=src, commit=False)
                    st["aliases"] += 1
                if len(files) < 1000:
                    files.append({"file": filename, "stable_id": stable_id, "title": parsed.title,
                                  "citations": list(parsed.report_citations),
                                  "disposition": disposition})
                if n % 100 == 0:
                    cat.commit()
            cat.commit()
            for i, sid in enumerate(to_extract):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="extracting citations",
                          done=i + 1, total=len(to_extract), item=sid)
                try:
                    extract_document(cat, ts, sid)
                    st["extracted"] += 1
                except Exception:  # noqa: BLE001
                    pass
                if i % 100 == 0:
                    cat.commit()
            cat.commit()
            _progress(on_progress, stage="resolving citations", done=0, total=0)
            resolved = Resolver(cat).run()
        st["resolved_edges"] = resolved.resolved
        # second pass: the Acts, each imported under its legislation.gov.uk id
        for filename, data in leg_entries:
            if cancel_check and cancel_check():
                break
            _progress(on_progress, stage="importing legislation", done=0, total=len(leg_entries),
                      item=filename)
            res = self.import_westlaw_legislation(data=data, filename=filename, match_names=False)
            if res.get("error"):
                st["unparseable"] += 1
                disposition, sid = "error", None
            else:
                st["legislation"] += 1
                st["aliases"] += res.get("aliases", 0)
                disposition, sid = res["disposition"], res["stable_id"]
            if len(files) < 1000:
                files.append({"file": filename, "stable_id": sid, "title": res.get("title"),
                              "kind": "legislation", "disposition": disposition,
                              "error": res.get("error")})
        if st["legislation"]:  # one name-match pass links every new Act's hanging references
            st["resolved_edges"] += self.match_named_legislation().get("resolved_edges", 0)
        st["files"] = files
        self._invalidate_caches()
        return st

    # -- unified case-law import (one uploader, routed by extension) --------
    @staticmethod
    def _merge_caselaw_stats(a: dict, b: dict) -> dict:
        """Merge two import runs' stat dicts: sum the counters, concatenate the per-file
        disposition lists, keep the first scalar for anything else."""
        out = dict(a)
        for k, v in b.items():
            if k == "files" and isinstance(v, list):
                out[k] = (out.get(k) or []) + v
            elif isinstance(v, (int, float)) and isinstance(out.get(k, 0), (int, float)):
                out[k] = out.get(k, 0) + v
            else:
                out.setdefault(k, v)
        return out

    def import_westlaw_legislation(self, *, data: bytes, filename: str | None = None,
                                   match_names: bool = True) -> dict:
        """Import a Westlaw **legislation** export (an RTF, often named ``.doc``) as a real,
        citable Act — the route for statutes legislation.gov.uk only holds as a scanned PDF
        (the Interpretation Act 1889 and its vintage), where Westlaw is the only
        machine-readable text and the Act would otherwise stay a hanging reference forever.

        Keyed by the legislation.gov.uk id the resolver already routes to
        (``ukpga/1889/63``), with one ``Segment`` per provision so "section 38 of the
        Interpretation Act 1889" lands on s. 38. The as-enacted/as-amended banner is kept in
        meta — an as-enacted text of a much-amended Act is not current law and must not
        silently pose as it. Never overwrites an authoritative legislation.gov.uk copy that
        already has text; it supersedes only a textless/PDF-only stub."""
        from .adapters.westlaw_legislation import parse_westlaw_legislation
        from .citations import extract_document
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .core.text import fold

        parsed = parse_westlaw_legislation(data, filename=filename)
        if parsed is None:
            return {"error": "not a recognisable Westlaw legislation export", "file": filename}
        if not parsed.stable_id:
            return {"error": f"no legislation id derivable from {parsed.title!r}",
                    "file": filename}

        sid = parsed.stable_id
        payload_hash = sha256_bytes(parsed.text.encode("utf-8"))
        meta = {
            "imported": "westlaw-legislation",
            "westlaw_legislation": {k: v for k, v in {
                "chapter": parsed.chapter, "long_title": parsed.long_title,
                "version": parsed.version_note, "provisions": len(parsed.provisions),
                "crossheadings": parsed.crossheadings or None,
            }.items() if v},
        }
        with self._open() as (cat, rs, ts):
            existing = cat.get_document(sid)
            old_meta = cat.document_meta(sid) if existing is not None else {}
            # an authoritative copy WITH text wins; a textless/PDF-only stub is superseded
            authoritative = (
                existing is not None and existing["has_text"]
                and old_meta.get("imported") not in ("westlaw-legislation",)
                and existing["source"] not in ("user-import",))
            if authoritative:
                text_path = str(ts.put(payload_hash, parsed.text))
                alts = old_meta.get("alt_texts", [])
                if not any(a.get("payload_hash") == payload_hash for a in alts):
                    alts.append({"source": "westlaw-legislation", "payload_hash": payload_hash,
                                 "text_path": text_path, "chars": len(parsed.text)})
                old_meta["alt_texts"] = alts
                for k, v in meta.items():
                    old_meta.setdefault(k, v)
                cat.set_document_meta(sid, old_meta, title_if_empty=parsed.title)
                disposition = "secondary"
            else:
                record = Record(
                    source="uk-legislation", stable_id=sid, doc_type=DocType.LEGISLATION,
                    title=parsed.title, decision_date=parsed.enacted_date,
                    language="en", source_language="en",
                    raw_bytes=data, raw_ext="rtf", payload_hash=payload_hash,
                    text=parsed.text, segments=parsed.segments,
                    extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER,
                    extra={**old_meta, **meta},
                )
                raw_path = str(rs.path_for(rs.put(data, ext="rtf"), "rtf"))
                text_path = str(ts.put(payload_hash, parsed.text))
                ts.put_segments(payload_hash, parsed.segments)
                cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
                disposition = "imported" if existing is None else "superseded"
            # the Act's short title is how it is actually cited — alias it so
            # "the Interpretation Act 1889" resolves without a section pinpoint
            aliases = 0
            key = fold(parsed.title)
            if key and key != sid:
                cat.put_alias(key, sid, source="westlaw-legislation", commit=False)
                aliases += 1
            cat.commit()
            if disposition != "secondary":
                try:
                    extract_document(cat, ts, sid)
                except Exception:  # noqa: BLE001
                    pass
            resolved = Resolver(cat).run().resolved
        # Name-only references ("section 38 of the Interpretation Act 1889") carry no
        # candidate id, so the plain resolver can't reach the new Act — the statute
        # name-matcher does, indexing the held Act's title and minting the alias. That's
        # what turns the hanging edges live, so run it unless a batch defers one pass to the end.
        if match_names:
            resolved += self.match_named_legislation().get("resolved_edges", 0)
        self._invalidate_caches()
        return {"stable_id": sid, "title": parsed.title, "disposition": disposition,
                "provisions": len(parsed.provisions), "chars": len(parsed.text),
                "version": parsed.version_note, "aliases": aliases,
                "resolved_edges": resolved}

    def refix_westlaw_imports(self, *, apply: bool = False, limit: int | None = None,
                              on_progress=None, cancel_check=None) -> dict:
        """Repair already-imported Westlaw documents whose id predates the current identity
        rules — chiefly the opaque ``westlaw:<hash>`` keys minted before WL-less law reports
        keyed by their report citation. Recompute each doc's identity from its stored
        ``meta_json`` (no re-parse of the raw RTF needed) and, where it differs, re-key the
        document in place (:meth:`Catalogue.rekey_document`, cascading every reference). Also
        folds a doc into a held record that shares a **precise** alias (report citation or
        WL/ECLI/CJEU id) — never a bare party name. ``apply=False`` is a dry run that just
        reports the planned changes."""
        import json

        from .adapters.westlaw_rtf import ParsedWestlaw, westlaw_identity
        from .resolve.matchers import first_candidate
        from .core.text import fold

        st = {"scanned": 0, "rekeyed": 0, "merged": 0, "unchanged": 0, "applied": apply}
        changes: list[dict] = []
        with self._open() as (cat, _rs, _ts):
            rows = cat.conn.execute(
                "SELECT stable_id, meta_json FROM documents WHERE meta_json LIKE ?",
                ('%"imported": "westlaw-rtf"%',)).fetchall()
            if limit:
                rows = rows[:limit]
            # Only the opaque content-hash surrogates need repair — a doc already keyed by
            # an ECLI, a neutral slug, a WL id or a report slug is authoritative and must be
            # left alone (its meta_json may be incomplete after a merge, so recomputing from
            # meta could wrongly demote a good id).
            hash_id = re.compile(r"^westlaw:[0-9a-f]{16}$")
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                st["scanned"] += 1
                cur = r["stable_id"]
                if not hash_id.match(cur):
                    st["unchanged"] += 1
                    continue
                try:
                    wl = (json.loads(r["meta_json"]) or {}).get("westlaw") or {}
                except (ValueError, TypeError):
                    st["unchanged"] += 1
                    continue
                p = ParsedWestlaw(
                    title=None, text="",
                    report_citations=tuple(wl.get("report_citations") or ()),
                    neutral_citation=wl.get("neutral_citation"),
                    ecli=wl.get("ecli"), wl_number=wl.get("wl_number"),
                    case_number=wl.get("case_number"))
                # only ever re-key TO a citation-derived identity — never to a fresh hash
                if not (p.neutral_citation or p.ecli or p.wl_number or p.report_citations):
                    st["unchanged"] += 1
                    continue
                target, kind = westlaw_identity(p)
                # fold into a held record sharing a precise alias (report cite / id)
                if kind in ("wl", "report", "hash"):
                    precise = list(p.report_citations) + [
                        x for x in (p.wl_number, p.ecli, p.case_number) if x]
                    for c in precise:
                        cand = first_candidate(c)
                        key = fold(cand.value) if cand else fold(c)
                        held = cat.get_alias(key)
                        if held is None and cat.get_document(key) is not None:
                            held = key
                        if held and held != cur and cat.get_document(held) is not None:
                            target, kind = held, "merged"
                            break
                if target == cur or not target:
                    st["unchanged"] += 1
                    continue
                changes.append({"old": cur, "new": target, "kind": kind})
                if apply:
                    action = cat.rekey_document(cur, target, commit=False)
                    st["merged" if action == "merge" else "rekeyed"] += 1
                    if n % 100 == 0:
                        cat.commit()
                _progress(on_progress, stage="refix westlaw", done=n, total=len(rows), item=cur)
            if apply:
                cat.commit()
        if apply:
            self._invalidate_caches()
        st["changes"] = changes[:5000]
        return st

    def repair_ecr_aliases(self, *, apply: bool = False, limit: int | None = None,
                           on_progress=None, cancel_check=None) -> dict:
        """Repair 'dead' European Court Reports aliases — an ``ECR → CELEX`` alias whose
        CELEX names no held document, because the mint-time chain to the case's ECLI didn't
        fire (the CELEX→ECLI alias was minted later). Follow that second hop now and, when
        it lands on a **held** judgment whose court is consistent with the ECR series
        (:func:`_ecr_series_ok` — "ECR II-" must be General Court, not Court of Justice),
        re-point the ECR alias straight at the ECLI so a bare "[2000] ECR II-491" resolves.
        A chain that fails the series guard is left dead rather than resolved to the wrong
        decision. ``apply=False`` is a dry run. Follow with :meth:`resolve`."""
        st = {"scanned": 0, "repaired": 0, "already_ok": 0,
              "skipped_series": 0, "skipped_unheld": 0, "applied": apply}
        changes: list[dict] = []
        with self._open() as (cat, _rs, _ts):
            rows = cat.conn.execute(
                "SELECT alias, dst_id FROM citation_aliases WHERE alias LIKE ? OR alias LIKE ?",
                ("%ecr %", "%e.c.r%")).fetchall()
            if limit:
                rows = rows[:limit]
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                alias, dst = r["alias"], r["dst_id"]
                st["scanned"] += 1
                # already lands on a held document (by stable_id or ECLI)? nothing to do.
                if cat.get_document(dst) is not None:
                    st["already_ok"] += 1
                    continue
                hop = cat.get_alias(dst.lower()) if dst else None
                if not hop or cat.get_document(hop) is None:
                    st["skipped_unheld"] += 1
                    continue
                if not _ecr_series_ok(alias, hop):
                    st["skipped_series"] += 1
                    continue
                changes.append({"alias": alias, "was": dst, "now": hop})
                st["repaired"] += 1
                if apply:
                    cat.put_alias(alias, hop, source="ecr-repair", commit=False)
                    if n % 500 == 0:
                        cat.commit()
                _progress(on_progress, stage="repair ecr", done=n, total=len(rows), item=alias)
            if apply:
                cat.commit()
        if apply:
            self._invalidate_caches()
        st["changes"] = changes[:5000]
        return st

    def import_caselaw_zip(self, *, zip_path: str, limit: int | None = None,
                           on_progress=None, cancel_check=None) -> dict:
        """Import a zip that may mix saved BAILII ``.html`` pages and Westlaw ``.rtf``
        exports — each entry routed to its own parser by extension (:meth:`import_bailii_zip`
        for HTML, :meth:`import_westlaw_zip` for RTF), the two runs' stats merged. A
        single-source zip simply no-ops the other importer."""
        import zipfile

        with zipfile.ZipFile(zip_path) as zf:
            names = [i.filename.lower() for i in zf.infolist() if not i.is_dir()]
        has_html = any(n.endswith((".html", ".htm")) for n in names)
        has_rtf = any(n.endswith((".rtf", ".doc")) for n in names)
        if not has_html and not has_rtf:
            return {"total": 0, "note": "no .html or .rtf files in the zip"}
        stats: dict = {}
        if has_html and not (cancel_check and cancel_check()):
            stats = self._merge_caselaw_stats(stats, self.import_bailii_zip(
                zip_path=zip_path, limit=limit, on_progress=on_progress, cancel_check=cancel_check))
        if has_rtf and not (cancel_check and cancel_check()):
            stats = self._merge_caselaw_stats(stats, self.import_westlaw_zip(
                zip_path=zip_path, limit=limit, on_progress=on_progress, cancel_check=cancel_check))
        return stats

    def import_caselaw_dir(self, *, dir_path: str, limit: int | None = None,
                           on_progress=None, cancel_check=None) -> dict:
        """Import a folder that may mix BAILII ``.html`` pages and Westlaw ``.rtf`` exports
        — the no-zip counterpart of :meth:`import_caselaw_zip`. Each importer walks the
        same directory and picks up only its own extension."""
        import os

        has_html = has_rtf = False
        for _root, _dirs, nms in os.walk(dir_path):
            for nm in nms:
                low = nm.lower()
                has_html = has_html or low.endswith((".html", ".htm"))
                has_rtf = has_rtf or low.endswith((".rtf", ".doc"))
        if not has_html and not has_rtf:
            return {"total": 0, "note": "no .html or .rtf files in the folder"}
        stats: dict = {}
        if has_html and not (cancel_check and cancel_check()):
            stats = self._merge_caselaw_stats(stats, self.import_bailii_dir(
                dir_path=dir_path, limit=limit, on_progress=on_progress, cancel_check=cancel_check))
        if has_rtf and not (cancel_check and cancel_check()):
            stats = self._merge_caselaw_stats(stats, self.import_westlaw_dir(
                dir_path=dir_path, limit=limit, on_progress=on_progress, cancel_check=cancel_check))
        return stats

    def harvest_reference(self, *, ref: str, candidate: str | None = None) -> dict:
        """The one-click resolution for a *routable* hanging reference: fetch exactly
        that item from the adapter that holds it, then resolve. ``ref`` is a row from
        ``unresolved_references``; we normalise it to a candidate id, pick the adapter
        from the candidate's shape, and run a **targeted single-item harvest**
        (uk-legislation by id, eu-legislation by CELEX, uk-caselaw by document URI)
        rather than making the user upload or scrape what the system already knows
        how to fetch."""
        with self._open() as (cat, rs, ts):
            # patient: the user asked for exactly this item — wait out a giant-Act render
            # rather than fast-failing like the bulk drain does
            res = self._fetch_reference(cat, rs, ts, ref=ref, candidate=candidate, patient=True)
            if "error" in res:
                return {"ref": ref, **res}
            # extract only the newly-fetched doc (NOT the whole 20k-doc corpus), then
            # resolve — the same fix as harvest(); a single click shouldn't re-mine everything.
            self._extract_ids(cat, ts, [res["candidate"]])
            resolved = Resolver(cat).run_for_documents([res["candidate"]])
            now = cat.find_document_id(res["candidate"])
        self._invalidate_caches()
        return {"ref": ref, "candidate": res["candidate"],
                "adapter": res.get("adapter"), "stored": res.get("stored", 0),
                "resolved_edges": resolved.resolved,
                "resolved": now is not None, "document": now}

    # -- neutral-citation gap-fill (completeness) --------------------------
    # Where a probed neutral citation comes back empty, we remember it so we don't re-probe
    # forever. A completed *past* year is contiguous — a missing number was never issued (or
    # isn't digitised) and never will be, so the miss is permanent. The current year is still
    # being filled, so its misses are 'not yet published' and re-probed later.
    _GAP_PERMANENT = "gap-permanent"
    _GAP_RETRY = "gap-retry"

    def gap_scan(self, *, court: str, year: int, start: int = 1, max_probes: int = 400,
                 stop_after_misses: int = 25, on_progress=None, cancel_check=None) -> dict:
        """Probe a UK court's neutral-citation numbering for one year and pull what's missing.

        ``court`` is the slug head of the neutral citation (``ewca/civ``, ``uksc``,
        ``ewhc/admin`` …); candidate ids are ``{court}/{year}/{n}``. Present numbers are
        skipped; existing ones are fetched (and extracted + resolved so they integrate — a
        new case's own citations then surface on the worklist for onward pulling); empty
        numbers are recorded as gaps. Probing stops after ``stop_after_misses`` consecutive
        empties (past the highest hit) — a completed year is contiguous. Idempotent: held
        numbers and recorded permanent gaps are skipped, so a re-run only does what's left.
        """
        import datetime as _dt

        court = (court or "").strip().strip("/").lower()
        year = int(year)
        historic = year < _dt.datetime.now(_dt.timezone.utc).year
        result = {"court": court, "year": year, "historic": historic,
                  "present": 0, "fetched": 0, "absent": 0, "highest": 0,
                  "fetched_ids": [], "gap_numbers": []}
        fetched_ids: list[str] = []
        with self._open() as (cat, rs, ts):
            permanent = {k for k in cat.enrichment_misses(self._GAP_PERMANENT, max_age_days=36500)
                         if k.startswith(f"{court}/{year}/")}
            new_perm: list[str] = []
            new_retry: list[str] = []
            consecutive = 0
            n = start
            probed = 0
            while probed < max_probes:
                if cancel_check and cancel_check():
                    result["cancelled"] = True
                    break
                cand = f"{court}/{year}/{n}"
                probed += 1
                if cand in permanent:
                    # an already-recorded empty — counts toward the contiguous-miss run so a
                    # re-scan doesn't creep past the end of the year on every pass.
                    consecutive += 1
                    if consecutive >= stop_after_misses:
                        result["stopped_at_run_end"] = True
                        break
                    n += 1
                    continue
                if cat.find_document_id(cand) is not None:
                    result["present"] += 1
                    result["highest"] = n
                    consecutive = 0
                    n += 1
                    continue
                res = self._fetch_reference(cat, rs, ts, ref=cand, candidate=cand)
                outcome = res.get("outcome")
                if outcome in ("stored", "present"):
                    result["fetched"] += 1
                    result["highest"] = n
                    fetched_ids.append(cand)
                    consecutive = 0
                elif outcome in ("absent", "no_adapter"):
                    result["absent"] += 1
                    result["gap_numbers"].append(n)
                    (new_perm if historic else new_retry).append(cand)
                    consecutive += 1
                else:  # transient / rate-limited — don't record as a gap, just stop soon
                    result.setdefault("transient", 0)
                    result["transient"] += 1
                    consecutive += 1
                _progress(on_progress, stage=f"{court} {year}", done=probed, total=max_probes,
                          item=cand, ok=outcome in ("stored", "present"),
                          msg=f"{result['fetched']} fetched · {result['absent']} gap")
                if consecutive >= stop_after_misses:
                    result["stopped_at_run_end"] = True
                    break
                n += 1
            if new_perm:
                cat.record_enrichment_misses(self._GAP_PERMANENT, new_perm)
            if new_retry:
                cat.record_enrichment_misses(self._GAP_RETRY, new_retry)
            # integrate what we pulled: extract the new docs' own citations, then resolve so
            # their edges (and any onward hanging references) enter the graph.
            if fetched_ids:
                self._extract_ids(cat, ts, fetched_ids)
                resolved = Resolver(cat).run_for_documents(fetched_ids)
                result["resolved_edges"] = resolved.resolved
        result["fetched_ids"] = fetched_ids
        if fetched_ids:
            self._invalidate_caches()
        return result

    def gap_status(self, *, court: str, year: int) -> dict:
        """Completeness of one court+year: which neutral-citation numbers are held, which are
        recorded as permanent gaps, and which are pending a re-probe."""
        court = (court or "").strip().strip("/").lower()
        year = int(year)
        prefix = f"{court}/{year}/"
        with self._open() as (cat, _rs, _ts):
            held = sorted(int(r["stable_id"].rsplit("/", 1)[1])
                          for r in cat.list_documents(id_prefix=court, limit=100000)
                          if r["stable_id"].startswith(prefix) and r["stable_id"].rsplit("/", 1)[1].isdigit())
            perm = {k for k in cat.enrichment_misses(self._GAP_PERMANENT, max_age_days=36500) if k.startswith(prefix)}
            retry = {k for k in cat.enrichment_misses(self._GAP_RETRY, max_age_days=36500) if k.startswith(prefix)}
        highest = max(held) if held else 0
        gaps = sorted(int(k.rsplit("/", 1)[1]) for k in perm if k.rsplit("/", 1)[1].isdigit())
        return {"court": court, "year": year, "held": len(held), "highest": highest,
                "permanent_gaps": len(gaps), "pending_reprobe": len(retry),
                "gap_numbers": gaps[:200],
                "complete": highest > 0 and (len(held) + len(gaps)) >= highest}

    def clear_gap_markers(self, *, court: str | None = None, year: int | None = None) -> dict:
        """Forget recorded gaps so they're re-probed (e.g. after a source backfilled old
        judgments). Clears both permanent and retry markers for the court/year, or all."""
        with self._open() as (cat, _rs, _ts):
            if court is None:
                cat.clear_enrichment_misses(self._GAP_PERMANENT)
                cat.clear_enrichment_misses(self._GAP_RETRY)
                return {"cleared": "all"}
            prefix = f"{court.strip().strip('/').lower()}/{year}/" if year else f"{court.strip().strip('/').lower()}/"
            for kind in (self._GAP_PERMANENT, self._GAP_RETRY):
                keys = [k for k in cat.enrichment_misses(kind, max_age_days=36500) if k.startswith(prefix)]
                for k in keys:
                    cat.conn.execute("DELETE FROM enrichment_misses WHERE kind = ? AND key = ?", (kind, k))
            cat.conn.commit()
            return {"cleared": prefix}

    # -- write / augment (the agent surface) -------------------------------
    def import_bytes(
        self, *, data: bytes, filename: str, doc_type: str = "commentary",
        title: str | None = None, link_to: str | None = None, relationship: str | None = None,
    ) -> dict:
        with self._open() as (cat, rs, ts):
            res = import_file(
                cat, rs, ts, data=data, filename=filename,
                doc_type=_doc_type(doc_type, DocType.COMMENTARY), title=title,
                link_to=link_to, relationship=_rel_type(relationship),
            )
            return asdict(res)

    def import_base64(self, *, content_base64: str, filename: str, **kw) -> dict:
        """Posting mode for an agent that holds the bytes (e.g. a PDF it generated
        or downloaded with another tool)."""
        return self.import_bytes(data=base64.b64decode(content_base64), filename=filename, **kw)

    def import_url(
        self, *, url: str, doc_type: str = "commentary", title: str | None = None,
        link_to: str | None = None, relationship: str | None = None,
    ) -> dict:
        with self._open() as (cat, rs, ts):
            res = import_url(
                cat, rs, ts, url=url, doc_type=_doc_type(doc_type, DocType.COMMENTARY),
                title=title, link_to=link_to, relationship=_rel_type(relationship),
            )
            return asdict(res)

    def import_bailii_file(
        self, *, stable_id: str, data: bytes, title: str | None = None,
    ) -> dict:
        """Import a BAILII RTF as a UK case-law judgment keyed by the FCL stable_id.

        The user downloads the RTF manually from BAILII (no scraping), drops it into
        the UI, and this method stores it under the same ``stable_id`` that all the
        pending citations already reference — so they resolve immediately.

        Args:
            stable_id: The Find Case Law stable_id, e.g. ``ewca/civ/2006/717``.
            data: Raw RTF bytes from the downloaded file.
            title: Optional display title (defaults to the stable_id).

        Returns a dict with ``stable_id``, ``chars`` (text length) and
        ``resolved_edges`` (citations this import resolved).
        """
        from .formats.rtf import strip_rtf
        from .core.models import DocType as _DT, ExtractedVia as _EV, Record as _Rec, Segment
        from .citations import extract_document as _extract_doc
        from datetime import date as _date

        parsed = strip_rtf(data)

        # Extract year from slug: ewca/civ/2006/717 → 2006
        decision_date: _date | None = None
        for part in stable_id.split("/"):
            if len(part) == 4 and part.isdigit():
                try:
                    decision_date = _date(int(part), 1, 1)
                except ValueError:
                    pass
                break

        # Derive court label from first slug segment (e.g. "ewca" → "Court of Appeal")
        court_slug = stable_id.split("/")[0].lower()
        _COURT_LABELS: dict[str, str] = {
            "ewca": "Court of Appeal",
            "ewhc": "High Court",
            "uksc": "Supreme Court",
            "ukhl": "House of Lords",
            "ukpc": "Privy Council",
            "ukftt": "First-tier Tribunal",
            "ukut": "Upper Tribunal",
            "csoh": "Court of Session (Outer House)",
            "csih": "Court of Session (Inner House)",
            "iesc": "Supreme Court of Ireland",
            "ieca": "Court of Appeal of Ireland",
            "iehc": "High Court of Ireland",
            "iecca": "Court of Criminal Appeal of Ireland",
        }
        court = _COURT_LABELS.get(court_slug, court_slug.upper())
        from .citations.courts import IRISH_COURTS

        record = _Rec(
            source="ie-caselaw" if court_slug in IRISH_COURTS else "uk-caselaw",
            stable_id=stable_id,
            doc_type=_DT.JUDGMENT,
            title=title or stable_id,
            language="en",
            source_language="en",
            landing_url=None if court_slug in IRISH_COURTS
            else f"https://caselaw.nationalarchives.gov.uk/{stable_id}",
            raw_bytes=data,
            raw_ext="rtf",
            text=parsed or None,
            segments=[],
            extracted_via=_EV.UNSTRUCTURED,
            decision_date=decision_date,
            court=court,
            extra={"via": "bailii-upload"},
        )
        record.ensure_payload_hash()

        with self._open() as (cat, rs, ts):
            from .storage.raw import RawStore as _RS  # already open via rs
            digest = rs.put(data, ext="rtf")
            raw_path = str(rs.path_for(digest, "rtf"))
            text_path: str | None = None
            if parsed and record.payload_hash:
                text_path = str(ts.put(record.payload_hash, parsed))
            cat.upsert_document(record, raw_path=raw_path, text_path=text_path)
            if parsed:
                _extract_doc(cat, ts, stable_id)
            resolved = Resolver(cat).run()

        self._invalidate_caches()
        return {
            "stable_id": stable_id,
            "stored": True,
            "chars": len(parsed) if parsed else 0,
            "resolved_edges": resolved.resolved,
        }

    def add_note(
        self, *, text: str, title: str | None = None, link_to: str | None = None,
        relationship: str = "summarises",
    ) -> dict:
        with self._open() as (cat, _rs, ts):
            res = add_note(
                cat, ts, text=text, title=title, link_to=link_to,
                relationship=_rel_type(relationship, RelationshipType.SUMMARISES),
            )
            return asdict(res)

    def attach(self, *, doc_id: str, data: bytes, filename: str, kind: str = "exhibit") -> dict:
        with self._open() as (cat, rs, _ts):
            asset_id = attach_asset(cat, rs, doc_id=doc_id, data=data, filename=filename, kind=kind)
            return {"asset_id": asset_id, "doc_id": doc_id, "kind": kind}

    def attach_base64(self, *, doc_id: str, content_base64: str, filename: str, kind: str = "exhibit") -> dict:
        return self.attach(doc_id=doc_id, data=base64.b64decode(content_base64), filename=filename, kind=kind)

    def link(self, *, src_id: str, dst_id: str, relationship: str,
             src_anchor: str | None = None, dst_anchor: str | None = None) -> dict:
        with self._open() as (cat, _rs, _ts):
            rel = _rel_type(relationship, RelationshipType.ANALYSES)
            resolved = link_documents(cat, src_id=src_id, dst_id=dst_id, relationship=rel,
                                      src_anchor=src_anchor, dst_anchor=dst_anchor)
            return {"src_id": src_id, "dst_id": dst_id, "relationship": rel.value,
                    "src_anchor": src_anchor, "dst_anchor": dst_anchor, "resolved": resolved}

    def tag(self, *, doc_id: str, tag: str) -> dict:
        with self._open() as (cat, _rs, _ts):
            written = tag_document(cat, doc_id, tag)
            return {"doc_id": doc_id, "tag": tag, "written": written}

    # -- named aliases / shorthand rules (e.g. "UK GDPR" → a document) ------
    def create_named_alias(self, *, phrase: str, target_id: str, apply: bool = False) -> dict:
        """Define a shorthand *rule*: every occurrence of ``phrase`` (e.g. "UK GDPR")
        links to ``target_id``. It propagates across the corpus on the next extraction;
        ``apply=True`` re-extracts now (can be slow on a big corpus)."""
        phrase = (phrase or "").strip()
        if not phrase or not target_id:
            return {"error": "phrase and target_id required"}
        with self._open() as (cat, _rs, ts):
            present = cat.find_document_id(target_id)
            cat.put_alias(phrase, target_id, source="named")
            result = {"phrase": phrase, "target_id": target_id, "target_present": present is not None}
            if apply:
                from .citations import extract_corpus
                extract_corpus(cat, ts)
                Resolver(cat).run()
                result["applied"] = True
        return result

    def list_named_aliases(self) -> list[dict]:
        """All shorthand rules (with whether the target is in the corpus)."""
        with self._open() as (cat, _rs, _ts):
            out = []
            for r in cat.list_named_aliases():
                out.append({"phrase": r["alias"], "target_id": r["dst_id"],
                            "target_present": cat.find_document_id(r["dst_id"]) is not None})
            return out

    def delete_named_alias(self, *, phrase: str) -> dict:
        with self._open() as (cat, _rs, _ts):
            cat.delete_alias(phrase)
            return {"phrase": phrase, "deleted": True}

    def apply_rules(self, *, source: str | None = None, run_id: str | None = None,
                    on_progress=None, cancel_check=None) -> dict:
        """Re-extract document text with the current grammars + user rules — the "re-scan the
        corpus for new potential citations" action. Run this after a new adapter/grammar
        lands (e.g. the law-report grammars, ECHR app numbers) so already-stored docs pick
        them up. ``source`` scopes it (e.g. just ``uk-caselaw``) — reports are cited by case
        law, so a scoped re-scan is far faster than the whole corpus. Heavy → run as a job."""
        from .citations import extract_documents_parallel

        with self._open() as (cat, _rs, ts):
            aliases = cat.named_alias_map()
            ids = cat.text_document_ids(source=source,
                                        exclude_extraction_run_id=run_id)
            ex = extract_documents_parallel(
                cat, ts, ids, aliases=aliases, run_id=run_id,
                stage="re-scanning citations",
                checkpoint_fn=lambda done, sid: {"phase": "extract", "completed": done,
                                                 "last_id": sid, "run_id": run_id},
                on_progress=on_progress, cancel_check=cancel_check)
            docs, cites, cancelled = ex.documents, ex.citations, ex.cancelled
            # don't run the (long, un-interruptible) resolve if the user cancelled —
            # so a cancel actually stops promptly instead of grinding to completion.
            if cancelled:
                return {"documents": docs, "citations": cites, "cancelled": True, "resolved_edges": 0}
            _progress(on_progress, stage="resolving citations", done=0, total=0)
            resolved = Resolver(cat).run()
            return {"documents": docs, "citations": cites, "resolved_edges": resolved.resolved}

    def untag(self, *, doc_id: str, tag: str) -> dict:
        """Remove a manual tag (a mis-tag correction). Rule tags are re-derived, so
        they're corrected by editing the rule, not here."""
        with self._open() as (cat, _rs, _ts):
            removed = cat.remove_document_tag(doc_id, tag, method="manual")
            return {"doc_id": doc_id, "tag": tag, "removed": removed}

    def tag_many(self, *, doc_ids: list[str], tag: str) -> dict:
        """Bulk-tag a selection — the academic's "drop these into a collection" gesture
        (a collection is just a shared manual tag)."""
        with self._open() as (cat, _rs, _ts):
            n = sum(1 for d in doc_ids if tag_document(cat, d, tag))
            return {"tag": tag, "documents": len(doc_ids), "written": n}

    # -- corrections (fix misclassification; human curation wins) -----------
    def update_document(self, *, stable_id: str, doc_type: str | None = None,
                        title: str | None = None, court: str | None = None,
                        source_language: str | None = None) -> dict:
        """Correct a misclassified document's metadata (type / title / court /
        language)."""
        if doc_type is not None:
            try:
                doc_type = DocType(doc_type).value
            except ValueError:
                valid = ", ".join(t.value for t in DocType)
                return {"error": f"unknown doc_type {doc_type!r}; valid: {valid}"}
        with self._open() as (cat, _rs, _ts):
            ok = cat.update_document_fields(stable_id, {
                "doc_type": doc_type, "title": title, "court": court,
                "source_language": source_language,
            })
            doc = cat.get_document(stable_id)
            return {"stable_id": stable_id, "updated": ok,
                    "document": dict(doc) if doc else None}

    def correct_citation(self, *, relation_id: int, treatment: str | None = None,
                         dst_id: str | None = None, suppress: bool = False) -> dict:
        """Fix one citation edge: ``suppress`` a false positive (it won't come back on
        re-extraction); re-point a wrong resolution to ``dst_id`` (an existing doc);
        or correct the ``treatment`` (e.g. follows → distinguishes). All record the
        edit as ``manual`` so the automatic passes never overwrite it (§1.3a)."""
        with self._open() as (cat, _rs, _ts):
            rel = cat.get_relation(relation_id)
            if rel is None:
                return {"error": f"no relation {relation_id}"}
            if suppress:
                cat.suppress_relation(relation_id)
                return {"relation_id": relation_id, "action": "suppressed"}
            if dst_id is not None:
                if cat.get_document(dst_id) is None:
                    return {"error": f"no document {dst_id!r} in corpus", "relation_id": relation_id}
                cat.resolve_relation(relation_id, dst_id)
                cat.set_relationship_type(relation_id, rel["relationship_type"], extracted_via="manual")
                return {"relation_id": relation_id, "action": "repointed", "dst_id": dst_id}
            if treatment is not None:
                rel_type = _rel_type(treatment, RelationshipType.MENTIONS)
                cat.set_relationship_type(relation_id, rel_type.value, extracted_via="manual")
                return {"relation_id": relation_id, "action": "reclassified",
                        "relationship_type": rel_type.value}
            return {"error": "nothing to do — pass treatment, dst_id, or suppress"}

    def embed(self, *, limit: int | None = None, on_progress=None, cancel_check=None) -> dict:
        """Embed/index documents that have text but no vectors in the current embedding
        family — the lexical (FTS) + semantic (vector) index both search reads. Resumable
        and cancellable; run as the ``embed`` background job so it shows progress and can be
        stopped. Returns per-run stats (documents, chunks, skipped)."""
        with self._open() as (cat, _rs, ts):
            stats = asdict(EmbedStage(cat, self._provider(), textstore=ts).run(
                limit=limit, on_progress=on_progress, cancel_check=cancel_check))
        self._invalidate_caches()  # has_embedding changed → coverage/search availability
        return stats

    def embedding_backlog(self) -> dict:
        """How much of the corpus is indexed in the current embedding family — the number
        a UI shows next to the 'Embed' button so it's clear how much work remains."""
        p = self._provider()
        with self._open() as (cat, _rs, _ts):
            pending = len(cat.pending_embedding(p.name, p.model, p.model_version))
            total = cat.count_documents()
        return {"provider": p.name, "model": p.model,
                "pending": pending, "indexed": max(total - pending, 0), "total": total}

    def resolve(self) -> dict:
        with self._open() as (cat, _rs, _ts):
            stats = asdict(Resolver(cat).run())
        self._invalidate_caches()  # edges flipped → worklist/unfetchable/dashboard are stale
        return stats

    def _llm_passes(self, use_llm: bool | None):
        """Build the optional LLM extractor + treatment classifier. ``use_llm``:
        None → auto (use them iff an LLM endpoint is configured & reachable);
        True → require; False → off (grammars + heuristics only). Returns
        ``(citation_extractor_or_None, treatment_classifier)``."""
        from .treatment import HeuristicTreatmentClassifier

        if use_llm is False:
            return None, HeuristicTreatmentClassifier()
        from .citations import LLMCitationExtractor
        from .llm import get_llm_client
        from .treatment import LLMTreatmentClassifier

        client = get_llm_client()
        if use_llm is None and not client.available():
            return None, HeuristicTreatmentClassifier()
        return LLMCitationExtractor(client), LLMTreatmentClassifier(client)

    def extract_citations(self, *, stable_id: str | None = None, limit: int | None = None,
                          use_llm: bool | None = None) -> dict:
        """Extract citations from document text into hanging edges (§5), classify
        treatments (§1.3a), then resolve them. A judgment that cites "Article 17
        GDPR" gets a pinpoint edge to the GDPR (resolving when it's in the corpus).
        When an LLM endpoint is configured, an extra batched LLM pass adds
        narrative citations and refines treatments (``use_llm`` forces on/off)."""
        from .citations import extract_corpus
        from .treatment import classify_corpus

        llm_cite, classifier = self._llm_passes(use_llm)
        with self._open() as (cat, _rs, ts):
            stats = extract_corpus(cat, ts, stable_id=stable_id, limit=limit, llm=llm_cite)
            treat = classify_corpus(cat, ts, stable_id=stable_id, classifier=classifier)
            resolved = Resolver(cat).run()
            return {**asdict(stats), "reclassified": treat.reclassified,
                    "resolved_edges": resolved.resolved,
                    "llm": llm_cite is not None}

    # -- watches (saved harvest plans + scheduler, §5a) --------------------
    def source_catalog(self) -> list[dict]:
        """Per-source capabilities (what it pulls, keyword-search vs post-filter,
        options) — the morphing-UI metadata."""
        from .adapters.registry import source_catalog

        return source_catalog()

    def create_watch(self, *, name: str, spec: dict, cadence_minutes: int = 1440,
                     enabled: bool = True) -> dict:
        """Save a harvest plan. ``spec`` keys: ``source`` (+ ``source_options``),
        ``keywords`` (list), ``seed_rule`` (e.g. {"cites": "32016R0679"}),
        ``degrees`` (autosnowball hops), ``max_pages``, ``max_per_degree``,
        ``tag`` (label everything brought in), ``backfill``."""
        with self._open() as (cat, _rs, _ts):
            wid = cat.add_watch(name, json.dumps(spec), cadence_minutes, enabled=enabled)
            return self._watch_dict(cat.get_watch(wid))

    def list_watches(self) -> list[dict]:
        with self._open() as (cat, _rs, _ts):
            return [self._watch_dict(w) for w in cat.list_watches()]

    def get_watch(self, watch_id: int) -> dict:
        with self._open() as (cat, _rs, _ts):
            return self._watch_dict(cat.get_watch(watch_id))

    def update_watch(self, *, watch_id: int, name: str | None = None, spec: dict | None = None,
                     cadence_minutes: int | None = None, enabled: bool | None = None) -> dict:
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if spec is not None:
            fields["spec_json"] = json.dumps(spec)
        if cadence_minutes is not None:
            fields["cadence_minutes"] = cadence_minutes
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        with self._open() as (cat, _rs, _ts):
            cat.update_watch(watch_id, fields)
            return self._watch_dict(cat.get_watch(watch_id))

    def delete_watch(self, *, watch_id: int) -> dict:
        with self._open() as (cat, _rs, _ts):
            cat.delete_watch(watch_id)
            return {"watch_id": watch_id, "deleted": True}

    @staticmethod
    def _watch_dict(row) -> dict:
        if row is None:
            return {}
        d = dict(row)
        d["spec"] = json.loads(d.pop("spec_json", "{}") or "{}")
        if d.get("last_result_json"):
            try:
                d["last_result"] = json.loads(d.pop("last_result_json"))
            except (ValueError, TypeError):
                d["last_result"] = None
        d["enabled"] = bool(d.get("enabled"))
        return d

    def _keyword_seed_docs(self, source: str, keywords: list[str] | None, *, limit: int = 60) -> list[str]:
        """Documents from ``source`` matching the watch keywords — the universal
        keyword limiter (works regardless of API search support): scans title + text
        for any term. No keywords → the source's most-recent docs.

        Keywords are un-quoted first: a phrase keyword ('"unfair dismissal"', quoted for
        the source API's exact-match search) must post-filter as the phrase itself —
        the quote characters never appear in a document, so the quoted form matches
        nothing and the watch silently seeds zero documents."""
        terms = [k.strip().strip("\"'“”‘’").lower() for k in (keywords or []) if k.strip()]
        terms = [t for t in terms if t]
        out: list[str] = []
        with self._open() as (cat, _rs, ts):
            for r in cat.list_documents(source=source, limit=1000):
                if not terms:
                    out.append(r["stable_id"])
                else:
                    hay = (r["title"] or "").lower()
                    if not any(t in hay for t in terms) and r["has_text"] and r["payload_hash"]:
                        try:
                            hay = (ts.get(r["payload_hash"]) or "").lower()
                        except OSError:
                            hay = ""
                    if any(t in hay for t in terms):
                        out.append(r["stable_id"])
                if len(out) >= limit:
                    break
        return out

    _CELEX_FULL = re.compile(r"^\d{5}[A-Z]{1,2}\d{4}$")

    def _search_query_for(self, cat, target: str) -> str:
        """A full-text search string that finds cases *citing* ``target``. Cases cite by
        NEUTRAL CITATION ("[2021] UKSC 12"), not by the case name — so for a UK case slug
        we rebuild the citation (searching the title only finds the case itself). Falls
        back to the title, then the raw target (already a citation like '[2014] UKSC 38')."""
        nc = _neutral_citation_from_slug(target.split("@")[0])
        if nc:
            return nc
        doc = cat.get_document(target) or (
            cat.get_document(cat.find_document_id(target)) if cat.find_document_id(target) else None)
        if doc and doc["title"]:
            return doc["title"]
        return target

    def backfill_titles(self, *, limit: int = 500, reset_misses: bool = False) -> dict:
        """Augment already-harvested CJEU judgments/opinions from the authoritative
        EUR-Lex webservice with everything the free CELLAR RDF omits — the official
        **case name** and the **subject-matter / EuroVoc** classification (added as
        tags). **Quota-friendly**: one CELLAR SPARQL maps every ECLI→CELEX, then the
        metadata comes back in batches of 50 per credentialed call; CELEXes the
        webservice has nothing for are flagged so they're not retried daily. Needs
        EURLEX_USERNAME/PASSWORD (Settings); without them it's a no-op."""
        from .adapters.eu_cellar import (EUCellarAdapter, clean_case_display_title,
                                         concise_case_title, eurlex_metadata)
        from .adapters.eu_legislation import _is_generic_title, celex_title

        with self._open() as (cat, _rs, _ts):
            if reset_misses:
                cat.clear_enrichment_misses("cjeu_title")
            rows = [dict(r) for r in cat.list_documents(source="eu-cellar", limit=limit)]
            missed = cat.enrichment_misses("cjeu_title")  # don't re-query daily failures
            # First, locally shorten any already-stored *long* EXPRESSION_TITLEs to
            # "parties (case no)" — no webservice quota needed.
            shortened = 0
            for r in rows:
                t = r["title"]
                clean = clean_case_display_title(t)
                if clean and clean != t:
                    cat.update_document_fields(r["stable_id"], {"title": clean}, curate=False)
                    r["title"] = t = clean
                    shortened += 1
                if t and ("—" in t or "#" in t) and len(t) > 90:
                    short = concise_case_title(t)
                    if short and short != t:
                        cat.update_document_fields(r["stable_id"], {"title": short}, curate=False)
                        r["title"] = short
                        shortened += 1
            # And give EU-legislation docs a real name where the source gave a generic
            # one ("EUR-Lex - 12008E267 - EN", "ANNEX", an OJ filename) — derived from
            # the CELEX (e.g. "Article 267 TFEU"). Local, no webservice.
            for r in cat.list_documents(source="eu-legislation", limit=100000):
                if _is_generic_title(r["title"]):
                    name = celex_title(r["stable_id"])
                    if name:
                        cat.update_document_fields(r["stable_id"], {"title": name}, curate=False)
                        shortened += 1
        # needs the case name OR has never been enriched (no subjects/tags yet)
        targets = [r for r in rows if r["doc_type"] in ("judgment", "opinion")
                   and (not r["title"] or r["title"] == r["stable_id"]
                        or str(r["title"]).startswith("ECLI:"))]
        if not targets:
            return {"candidates": 0, "updated": 0, "shortened": shortened}

        cellar = EUCellarAdapter()
        eclis = [r["stable_id"] for r in targets if r["stable_id"].startswith("ECLI:")]
        celex_by_ecli = cellar.celex_for_eclis(eclis)  # 1 SPARQL for all
        want: dict[str, str] = {}
        for r in targets:
            sid = r["stable_id"]
            celex = celex_by_ecli.get(sid) if sid.startswith("ECLI:") else (
                sid if re.fullmatch(r"\d{5}[A-Z]{1,2}\d{4}", sid) else None)
            if celex and celex not in missed:
                want[celex] = sid
        meta = eurlex_metadata(list(want))  # batched: ⌈N/50⌉ credentialed calls
        titled = tagged = 0
        with self._open() as (cat, _rs, _ts):
            for celex, sid in want.items():
                m = meta.get(celex) or {}
                clean_title = clean_case_display_title(m.get("title"))
                if clean_title and clean_title != sid:
                    cat.update_document_fields(sid, {"title": clean_title}, curate=False)
                    titled += 1
                for subj in (m.get("subjects") or []):
                    if cat.upsert_document_tag(sid, subj, method="eurlex"):
                        tagged += 1
            # Only flag misses when the call actually *worked* (returned some data) —
            # otherwise an auth/network outage would poison every CELEX permanently.
            if meta:
                cat.record_enrichment_misses("cjeu_title", [c for c in want if c not in meta])
        return {"candidates": len(targets), "mapped_celex": len(want), "shortened": shortened,
                "webservice_calls": -(-len(want) // 50), "titled": titled,
                "subject_tags_added": tagged,
                # We asked for CELEXes and got nothing at all back: the webservice is down
                # or the credentials are wrong. Distinct from "it answered, and had no data
                # for these" — the scheduler backs off on the former, not the latter.
                "provider_down": bool(want) and not meta,
                "flagged_no_data": len([c for c in want if c not in meta])}

    def harvest_house_of_lords(self, *, ids: str | None = None, limit: int | None = None,
                               match_reports: bool = True, on_progress=None, cancel_check=None) -> dict:
        """Scrape the House of Lords archive (publications.parliament.uk, 1996–2009) and,
        after, link the classic-reporter citations to what was harvested (§5a/§5b).

        Post-2001 cases resolve every "[YYYY] UKHL N"; pre-2001 cases become documents a
        "[1998] AC 1" can be matched to. ``ids`` scopes to specific stable_ids (e.g. from the
        worklist); otherwise the whole index is walked."""
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline

        adapter = get_adapter("uk-hol", ids=ids) if ids else get_adapter("uk-hol")
        stored_ids: list[str] = []
        with self._open() as (cat, rs, ts):
            _progress(on_progress, stage="scraping House of Lords index", done=0, total=0)
            before = cat.all_stable_ids()
            stats = Pipeline(cat, rs, textstore=ts).run(
                adapter, max_pages=limit, record_health=True)
            stored_ids = [s for s in cat.all_stable_ids() - before]
            self._extract_ids(cat, ts, stored_ids, on_progress=on_progress)
            resolved = Resolver(cat).run_for_documents(stored_ids)
        matched = {}
        if match_reports and not (cancel_check and cancel_check()):
            matched = self.match_report_citations(on_progress=on_progress, cancel_check=cancel_check)
        self._invalidate_caches()
        return {"stored": stats.stored, "extracted_docs": len(stored_ids),
                "resolved_edges": resolved.resolved, "report_match": matched}

    def match_report_citations(self, *, limit: int = 8000, on_progress=None, cancel_check=None) -> dict:
        """Link reporter-only citations ("[1998] AC 1") to harvested cases by matching the
        case name the citing text puts beside the report against a harvested judgment of the
        right year (§5b, citations.report_match). Mints an alias per confident, unambiguous
        match, then resolves — so the citation and all its siblings go live."""
        import re as _re
        from collections import Counter, defaultdict

        from .citations.report_match import (
            HOL_PLAUSIBLE_SERIES, extract_preceding_name, match_report,
        )
        from .citations.reporters import report_series
        from .core.text import fold

        def _year(d):
            return int(d[:4]) if d and len(d) >= 4 and d[:4].isdigit() else None

        def _report_year(raw):
            m = _re.search(r"[\[(](1[6-9]\d{2}|20\d{2})[\])]", raw or "")
            return int(m.group(1)) if m else None

        with self._open() as (cat, _rs, ts):
            pool = [{"stable_id": r["stable_id"], "title": r["title"], "year": _year(r["decision_date"])}
                    for r in cat.judgment_pool()]
            # index the pool by year for a cheap "any candidate this year?" pre-filter
            pool_years: set[int] = {p["year"] for p in pool if p["year"] is not None}
            contexts = cat.report_citation_contexts(limit=limit)

            # group occurrences by raw string, and pre-filter BEFORE any text I/O: keep only
            # report strings a HoL case could actually be in (plausible series) AND for which
            # the pool holds a judgment in the reporting-lag window. This skips reading text
            # for the ~majority of report citations that can't match, which was the cost.
            by_raw: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for c in contexts:
                if c["char_start"] is not None:
                    by_raw[c["raw"]].append((c["src_id"], c["char_start"]))
            viable = []
            for raw in by_raw:
                series = report_series(raw)
                ry = _report_year(raw)
                if series in HOL_PLAUSIBLE_SERIES and ry is not None \
                        and any(y in pool_years for y in (ry, ry - 1, ry - 2, ry + 1)):
                    viable.append(raw)

            text_cache: dict[str, str | None] = {}

            def _text(src_id: str) -> str | None:
                if src_id not in text_cache:
                    doc = cat.get_document(src_id)
                    ph = doc["payload_hash"] if doc else None
                    try:
                        text_cache[src_id] = ts.get(ph) if ph else None
                    except OSError:
                        text_cache[src_id] = None
                return text_cache[src_id]

            aliased = 0
            for i, raw in enumerate(viable):
                if cancel_check and cancel_check():
                    break
                # read the name from up to a few citing occurrences; take the most common
                names: Counter = Counter()
                for src_id, start in by_raw[raw][:5]:
                    txt = _text(src_id)
                    if txt:
                        nm = extract_preceding_name(txt[max(0, start - 200): start])
                        if nm:
                            names[nm] += 1
                if names:
                    name, _ = names.most_common(1)[0]
                    hit = match_report(raw, name, pool, confirm_text=False)
                    if hit:
                        # key the alias on the folded raw so the resolver's raw_fold rung
                        # links this citation and every sibling occurrence at once. Tag the
                        # source by match kind so abbrev/single-party matches stay auditable.
                        stable, _score, kind = hit
                        source = "report-match" if kind == "exact" else f"report-match:{kind}"
                        cat.put_alias(fold(raw), stable, source=source, commit=False)
                        aliased += 1
                if on_progress and i % 100 == 0:
                    _progress(on_progress, stage="matching reporter citations", done=i, total=len(viable))
            cat.commit()
            resolved = Resolver(cat).run()
        return {"report_strings": len(by_raw), "viable": len(viable),
                "aliased": aliased, "resolved_edges": resolved.resolved}

    def rescan(self, *, limit: int | None = None, coref: bool = True, parallel: bool = True,
               doc_types: list[str] | None = None, source: str | None = None,
               only_unextracted: bool = False, stale_days: int | None = None,
               run_id: str | None = None, on_progress=None, cancel_check=None) -> dict:
        """Full fresh relink of the corpus: re-extract every text document with the current
        grammars, then run the whole resolution chain — so every fix (statute-name grammar,
        carry-forward cue/kind, the enlarged case pool, name/EHRR/EU matchers, parallel
        mining) takes effect and its contribution is visible in one report.

        Efficient for the whole corpus (unlike ``extract_citations``, which caps at 100k):
        the user-alias map is loaded once, ids stream from a single-column scan, writes are
        per-document durable (idempotent → the run is restartable), and progress/cancel are
        honoured. Order matters — extraction first (regenerates edges), then the matchers
        that alias name-only references to what's held, then parallel mining last.

        ``source`` scopes the re-extraction to one adapter's documents — e.g. re-extract
        just a freshly-imported corpus after a new grammar lands, rather than re-running
        the whole 700k-doc corpus. The relink chain afterwards still operates corpus-wide
        on the pending references (that's where the new edges get resolved).

        ``only_unextracted`` makes the run a **resume** rather than a redo: it takes only
        the documents that have no citation rows yet. A bulk import (or a rescan) that is
        interrupted — an OOM kill, a container restart — leaves a backlog of text documents
        with no edges; without this, picking up where it left off means re-extracting the
        entire source from scratch. With it, a killed 200k-document run can simply be
        re-launched and will process only what never finished.

        ``stale_days`` scopes the re-extraction to documents **not extracted in the last N
        days** — the "avoid re-doing the whole corpus on restart" set. It reads freshness
        from the ``last_extracted_at`` stamp OR the newest ``citations.created_at``, so it
        works retroactively against an in-flight or just-finished rescan (which is stamping
        those timestamps as it goes): running "rescan stale (>1 week)" now targets only
        what the current run hasn't already reached."""
        from .citations import extract_documents_parallel

        report: dict = {}

        def _cancelled() -> bool:
            return bool(cancel_check and cancel_check())

        with self._open() as (cat, _rs, ts):
            aliases = cat.named_alias_map()          # user shorthand rules — loaded ONCE
            ids = cat.text_document_ids(limit=limit, doc_types=doc_types, source=source,
                                        only_unextracted=only_unextracted, stale_days=stale_days,
                                        exclude_extraction_run_id=run_id)
            total = len(ids)
            # the pooled bulk extractor: regex on N cores, writes overlapped in the
            # parent, commits batched. Resume-safe under the SAME contract as the old
            # serial loop — the run_id-scoped last_extracted_at stamp — so a rescan
            # interrupted under the old code continues under this one and vice versa.
            ex = extract_documents_parallel(
                cat, ts, ids, aliases=aliases, run_id=run_id,
                stage="re-extracting corpus", report_every=100,
                checkpoint_fn=lambda done, sid: {"phase": "extract", "completed": done,
                                                 "last_id": sid, "run_id": run_id},
                on_progress=on_progress, cancel_check=cancel_check)
            docs, cites = ex.documents, ex.citations
            # Large rescans regenerate millions of pending edges; resolve them in
            # bounded, cancellable ranges rather than one whole-graph transaction.
            if total >= 10000:
                resolved = Resolver(cat).run_batched(
                    on_progress=on_progress, cancel_check=cancel_check)
            else:
                resolved = Resolver(cat).run()
        report["extract"] = {"docs_reextracted": docs, "citations": cites,
                             "resolved_edges": resolved.resolved, "total": total}
        self._invalidate_caches()

        # relink chain — each pass aliases name-only references to held targets and resolves
        if not _cancelled():
            report["legislation"] = self.match_named_legislation(
                on_progress=on_progress, cancel_check=cancel_check)
        if not _cancelled():
            report["reports"] = self.match_report_citations(
                on_progress=on_progress, cancel_check=cancel_check)
        if not _cancelled():
            report["echr"] = self.match_echr_reports(
                on_progress=on_progress, cancel_check=cancel_check)
        if parallel and not _cancelled():
            report["parallel"] = self.mine_parallel_citations(
                coref=coref, on_progress=on_progress, cancel_check=cancel_check)
        return report

    def match_named_legislation(self, *, limit: int | None = None, on_progress=None,
                                cancel_check=None) -> dict:
        """Resolve name-only statute references ("the Police and Criminal Evidence Act
        1984", "section 32 of the Limitation Act 1980") against the titles of legislation
        the corpus **already holds** (§5b). This is the self-updating counterpart to the
        bundled offline gazetteer: the index is rebuilt from harvested legislation each run,
        so it never goes stale and covers every Act that's been fetched — including recent
        ones the offline list predates. Mints an alias per confident match, then resolves."""
        from .citations.statute_gazetteer import normalise_title, reference_key
        from .core.text import fold

        with self._open() as (cat, _rs, _ts):
            # held-legislation title index, keyed by normalised title; keep only the
            # unambiguous ones (one held id per title) so a match can't pick the wrong Act.
            index: dict[str, str | None] = {}
            for r in cat.held_legislation_titles():
                key = normalise_title(r["title"])
                if not key:
                    continue
                if key in index and index[key] != r["stable_id"]:
                    index[key] = None  # ambiguous title → refuse to guess
                else:
                    index.setdefault(key, r["stable_id"])

            refs = cat.pending_statute_refs(limit=limit)
            aliased = 0
            for i, row in enumerate(refs):
                if cancel_check and cancel_check():
                    break
                raw = row["raw"]
                sid = index.get(reference_key(raw))
                if sid:
                    cat.put_alias(fold(raw), sid, source="legislation-name", commit=False)
                    aliased += 1
                if on_progress and i % 500 == 0:
                    _progress(on_progress, stage="matching named legislation", done=i, total=len(refs))
            cat.commit()
            resolved = Resolver(cat).run()

        self._invalidate_caches()
        return {"held_titles": len(index), "candidates": len(refs),
                "aliased": aliased, "resolved_edges": resolved.resolved}

    def harvest_missing_echr(self, *, limit: int = 500, match_after: bool = True,
                             on_progress=None, cancel_check=None) -> dict:
        """Queue the ECtHR cases the corpus cites (by name/EHRR) but doesn't hold, and fetch
        them from HUDOC by docname search (§5a). Each pending ``echr:<name>`` candidate — the
        form the EHRR grammar leaves for a case like "Chahal v United Kingdom" — is looked up
        on HUDOC, harvested, and (``match_after``) linked to its EHRR citations so the whole
        family of references goes live. Most-cited missing cases first."""
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline

        chunk = 20  # harvest in small batches so progress ticks (a single Pipeline.run over
        # 500 rate-limited HUDOC lookups reports nothing for minutes → the stall detector
        # wrongly flags the job frozen).
        with self._open() as (cat, rs, ts):
            names = cat.pending_echr_name_refs(limit=limit)
            if not names:
                return {"queued": 0, "stored": 0, "harvested_docs": 0, "resolved_edges": 0}
            before = cat.all_stable_ids()
            total = len(names)
            stored = 0
            for i in range(0, total, chunk):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="harvesting ECtHR from HUDOC", done=i, total=total)
                adapter = get_adapter("echr", ids=names[i: i + chunk])
                stored += Pipeline(cat, rs, textstore=ts).run(
                    adapter, record_health=False).stored
            stored_ids = list(cat.all_stable_ids() - before)
            self._extract_ids(cat, ts, stored_ids, on_progress=on_progress)
            resolved = Resolver(cat).run_for_documents(stored_ids)
        matched = {}
        if match_after and not (cancel_check and cancel_check()):
            matched = self.match_echr_reports(on_progress=on_progress, cancel_check=cancel_check)
        self._invalidate_caches()
        return {"queued": len(names), "stored": stored,
                "harvested_docs": len(stored_ids), "resolved_edges": resolved.resolved,
                "echr_match": matched}

    def match_echr_reports(self, *, limit: int = 8000, on_progress=None, cancel_check=None) -> dict:
        """Link an EHRR citation ("Soering v United Kingdom (1989) 11 EHRR 349") to a held
        ECtHR case by matching the applicant name + year the citing text puts beside it
        against the held-case pool — grouping the EHRR (and the case's application number)
        as alternative reference forms (§5c). The respondent state normalises away via the
        abbreviation table (UK ⇄ United Kingdom), leaving the applicant as the distinctive
        token. Returns the still-unmatched names so they can be queued for the ECtHR
        extractor's HUDOC docname search."""
        import re as _re

        from .citations.report_match import score_echr_candidate, surnames
        from .core.text import fold

        def _year(d):
            return int(d[:4]) if d and len(d) >= 4 and d[:4].isdigit() else None

        def _report_year(raw):
            m = _re.search(r"[\[(](1[6-9]\d{2}|20\d{2})[\])]", raw or "")
            return int(m.group(1)) if m else None

        def _clean_title(t):
            return _re.sub(r"^case of\s+", "", (t or "").strip(), flags=_re.IGNORECASE)

        with self._open() as (cat, _rs, _ts):
            pool = [{"stable_id": r["stable_id"], "title": _clean_title(r["title"]),
                     "year": _year(r["decision_date"]), "appno": r["appno"]}
                    for r in cat.echr_pool()]
            refs = cat.echr_report_refs(limit=limit)
            aliased = 0
            missing: list[dict] = []
            for i, r in enumerate(refs):
                if cancel_check and cancel_check():
                    break
                raw, cand = r["raw"], r["candidate_id"]
                ry = _report_year(raw)
                # the case name is carried in the "echr:<name>" candidate the grammar set
                name = cand[5:] if cand and cand.lower().startswith("echr:") else raw
                if ry is None or not surnames(name):
                    continue
                best = second = 0.0
                pick = None
                for p in pool:
                    if p["year"] is None or not (ry - 3 <= p["year"] <= ry + 1):
                        continue
                    # respondent-neutral scorer: "HL v UK" must not auto-alias to
                    # whichever single UK case sits in the year window
                    s = score_echr_candidate(name, p["title"], p["year"], ry)
                    if s > best:
                        best, second, pick = s, best, p
                    elif s > second:
                        second = s
                if pick and best >= 0.5 and best - second >= 0.08:
                    # alias the raw AND the echr:<name> candidate to the held case's ECLI,
                    # and record the application number as another form of reference.
                    cat.put_alias(fold(raw), pick["stable_id"], source="echr-report", commit=False)
                    if cand:
                        cat.put_alias(fold(cand), pick["stable_id"], source="echr-report", commit=False)
                    if pick["appno"]:
                        cat.put_alias(fold(pick["appno"]), pick["stable_id"],
                                      source="echr-report", commit=False)
                    aliased += 1
                else:
                    missing.append({"name": name, "year": ry, "raw": raw})
                if on_progress and i % 200 == 0:
                    _progress(on_progress, stage="matching EHRR citations", done=i, total=len(refs))
            cat.commit()
            resolved = Resolver(cat).run()

        self._invalidate_caches()
        return {"ehrr_strings": len(refs), "aliased": aliased, "missing": len(missing),
                "resolved_edges": resolved.resolved, "missing_refs": missing[:500]}

    def suggest_matches(self, *, report_limit: int = 8000, statute_limit: int = 20000,
                        max_report_refs: int = 1500, on_progress=None, cancel_check=None) -> dict:
        """Populate the human-confirmable "Possibly: …?" suggestions (§5b).

        The automatic matchers act only on confident, unambiguous matches; everything
        sub-threshold used to be silently dropped and sat in the worklist forever. This
        pass keeps the near-misses as *suggestions* a person confirms with one click:

        - **legislation-nested**: the cited title is the tail of a real act's title in the
          same year — a judge's shorthand ("Harassment Act 1997" for the Protection from
          Harassment Act 1997). Candidates come from held legislation AND the offline
          gazetteer (a gazetteer hit is fetchable — accepting it harvests the act).
        - **legislation-year**: same title, year off by one (report/assent-year slips).
        - **case-name**: a report citation ("[1998] AC 1") whose auto-extracted party
          names score against a held judgment in the reporting-lag year window, but not
          confidently enough to auto-alias. The extracted parties are stored for audit;
          the held case's id/neutral citation is shown so the human can verify.
        - **echr-name**: the EHRR matcher's sub-threshold candidates, likewise.

        Confident matches found on the way (e.g. after the duplicate-holdings tie-break)
        are aliased directly, exactly as the automatic passes would."""
        import re as _re

        from .citations.report_match import (
            extract_name_candidates, match_report, score_candidate, score_echr_candidate,
            surnames,
        )
        from .citations.statute_gazetteer import _index as _gz_index, reference_key, normalise_title
        from .core.text import fold

        st = {"statute": 0, "report": 0, "echr": 0, "auto_aliased": 0}

        def _cancelled() -> bool:
            return bool(cancel_check and cancel_check())

        def _year(d):
            return int(d[:4]) if d and len(d) >= 4 and d[:4].isdigit() else None

        def _report_year(raw):
            m = _re.search(r"[\[(](1[6-9]\d{2}|20\d{2})[\])]", raw or "")
            return int(m.group(1)) if m else None

        with self._open() as (cat, _rs, ts):
            # ---- legislation: nested titles + year slips -----------------------
            entries: list[tuple[tuple[str, ...], str, str, bool]] = []  # (tokens, sid, title, held)
            seen_sids: set[str] = set()
            for r in cat.held_legislation_titles():
                key = normalise_title(r["title"])
                if key:
                    entries.append((tuple(key.split()), r["stable_id"], r["title"], True))
                    seen_sids.add(r["stable_id"])
            for (t, y), sid in _gz_index().items():
                if y and sid and sid not in seen_sids:
                    entries.append((tuple(f"{t} {y}".split()), sid, f"{t.title()} {y}", False))
            exact: dict[tuple[str, ...], list] = {}
            by_year: dict[str, list] = {}
            for e in entries:
                exact.setdefault(e[0], []).append(e)
                if e[0] and e[0][-1].isdigit():
                    by_year.setdefault(e[0][-1], []).append(e)

            refs = cat.pending_statute_refs(limit=statute_limit)
            for i, row in enumerate(refs):
                if _cancelled():
                    break
                raw = row["raw"]
                key = tuple(reference_key(raw).split())
                if len(key) < 3 or not key[-1].isdigit() or key in exact:
                    continue  # too thin, or the exact matcher's territory
                # keyed by the raw string — exactly how the worklist groups candidate-less rows
                ref_key = raw
                year, base = key[-1], key[:-1]
                # year slip: identical title, ±1 year, unambiguous
                for y2 in (str(int(year) - 1), str(int(year) + 1)):
                    hits = exact.get(base + (y2,), [])
                    if len(hits) == 1:
                        _t, sid, title, held = hits[0]
                        if cat.put_suggestion(ref_key, sid, kind="legislation-year",
                                              reason=f"same title; the act is {y2}, cited as {year}",
                                              context=title, held=held, score=0.6, commit=False):
                            st["statute"] += 1
                # nested: cited name is the TAIL of a longer real title, same year
                if len(base) >= 2:
                    nested = [e for e in by_year.get(year, [])
                              if len(e[0]) > len(key) and e[0][-len(key):] == key]
                    if len(nested) > 2:
                        nested = []  # three+ acts end the same way — too ambiguous to ask
                    for toks, sid, title, held in nested:
                        score = round(len(key) / len(toks), 2)
                        if cat.put_suggestion(ref_key, sid, kind="legislation-nested",
                                              reason=f"cited name is the tail of “{title}”",
                                              context=title, held=held, score=score, commit=False):
                            st["statute"] += 1
                if on_progress and i % 1000 == 0:
                    _progress(on_progress, stage="suggesting legislation", done=i, total=len(refs))
            cat.commit()

            # ---- report citations: extracted parties vs held judgments --------
            pool = [{"stable_id": r["stable_id"], "title": r["title"],
                     "year": _year(r["decision_date"]),
                     "jur": self._jurisdiction_of(r["source"])} for r in cat.judgment_pool()]
            pool_by_year: dict[int, list] = {}
            for p in pool:
                if p["year"] is not None:
                    pool_by_year.setdefault(p["year"], []).append(p)
            # a report series that names its jurisdiction (ALR → Australia, NZLR → NZ,
            # SCR → Canada…) must only score against that jurisdiction's candidates —
            # an "(1997) 145 ALR 169" was being offered an Irish High Court case
            # because one party surname coincided. Ambiguous/travelling series get no
            # gate (jurisdiction honestly unknown), and are flagged at review instead.
            from .citations.reporters import report_series as _series_name, reporter_jurisdiction
            _REPORTER_LABEL = {"AU": "Australia", "CA": "Canada", "NZ": "New Zealand",
                               "SG": "Singapore", "HK": "Hong Kong", "IN": "India",
                               "IE": "Ireland"}

            from collections import defaultdict
            by_raw: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for c in cat.report_citation_contexts(limit=report_limit):
                if c["char_start"] is not None:
                    by_raw[c["raw"]].append((c["src_id"], c["char_start"]))
            raws = sorted(by_raw, key=lambda r: -len(by_raw[r]))[:max_report_refs]

            text_cache: dict[str, str | None] = {}

            def _text(sid: str) -> str | None:
                if sid not in text_cache:
                    doc = cat.get_document(sid)
                    ph = doc["payload_hash"] if doc else None
                    try:
                        text_cache[sid] = ts.get(ph) if ph else None
                    except OSError:
                        text_cache[sid] = None
                return text_cache[sid]

            for i, raw in enumerate(raws):
                if _cancelled():
                    break
                if on_progress and i % 100 == 0:
                    _progress(on_progress, stage="suggesting report matches", done=i, total=len(raws))
                ref_key = raw  # the worklist's group key for candidate-less rows
                if "\n" in raw or cat.get_alias(fold(raw)):
                    continue  # a raw with a newline is a mis-parsed span, not a citation
                series = _series_name(raw)
                if series is None:
                    continue  # "[1976]" alone etc. — nothing to match on
                ry = _report_year(raw)
                if ry is None:
                    continue
                names: list[str] = []
                for src_id, start in by_raw[raw][:4]:
                    txt = _text(src_id)
                    if txt:
                        for nm in extract_name_candidates(txt[max(0, start - 220): start]):
                            if nm not in names:
                                names.append(nm)
                if not names:
                    continue
                window = [p for y in (ry - 2, ry - 1, ry, ry + 1) for p in pool_by_year.get(y, [])]
                rj = reporter_jurisdiction(series)
                if rj is not None:
                    label = _REPORTER_LABEL.get(rj)
                    window = [p for p in window if p["jur"] == label] if label else []
                    if not window:
                        continue  # the right jurisdiction isn't held — better silent than wrong
                # a confident, unambiguous match found here is acted on, not just suggested
                hit = match_report(raw, names[0], window, confirm_text=False)
                if hit:
                    stable, _score, kind = hit
                    cat.put_alias(ref_key, stable,
                                  source="report-match" if kind == "exact" else f"report-match:{kind}",
                                  commit=False)
                    st["auto_aliased"] += 1
                    continue
                # sub-threshold: score full names AND each side's tokens alone
                variants: list[set] = []
                for nm in names[:3]:
                    full = surnames(nm)
                    if full and full not in variants:
                        variants.append(full)
                    parts = _re.split(r"\s+v\.?\s+", nm, maxsplit=1)
                    if len(parts) == 2:
                        for side in parts:
                            s = surnames(side)
                            if s and s not in variants:
                                variants.append(s)
                scored: list[tuple[float, dict]] = []
                for p in window:
                    s = max((score_candidate(v, p["title"] or "", p["year"], ry)
                             for v in variants), default=0.0)
                    if s >= 0.3:
                        scored.append((s, p))
                scored.sort(key=lambda t: -t[0])
                parties = "; ".join(names[:3])
                for s, p in scored[:2]:
                    if cat.put_suggestion(ref_key, p["stable_id"], kind="case-name",
                                          reason=f"party match “{names[0]}” near {raw}",
                                          extracted_parties=parties,
                                          context=f"{p['title']} · {p['stable_id']}",
                                          held=True, score=s, commit=False):
                        st["report"] += 1
            cat.commit()

            # ---- EHRR / ECtHR names: the matcher's sub-threshold band ---------
            epool = [{"stable_id": r["stable_id"],
                      "title": _re.sub(r"^case of\s+", "", (r["title"] or "").strip(), flags=_re.IGNORECASE),
                      "year": _year(r["decision_date"]), "appno": r["appno"]}
                     for r in cat.echr_pool()]
            for i, r in enumerate(cat.echr_report_refs(limit=report_limit)):
                if _cancelled():
                    break
                raw, cand = r["raw"], r["candidate_id"]
                # keyed exactly as the worklist keys the row (candidate_id, unfolded),
                # so the suggestion attaches to the row the user is looking at
                ref_key = cand if cand else fold(raw)
                if cat.get_alias(fold(ref_key)):
                    continue
                ry = _report_year(raw)
                name = cand[5:] if cand and cand.lower().startswith("echr:") else raw
                if ry is None or not surnames(name):
                    continue
                scored = []
                for p in epool:
                    if p["year"] is None or not (ry - 3 <= p["year"] <= ry + 1):
                        continue
                    # respondent-neutral: only the applicant side identifies the case
                    s = score_echr_candidate(name, p["title"], p["year"], ry)
                    if s >= 0.3:
                        scored.append((s, p))
                scored.sort(key=lambda t: -t[0])
                # the confident unambiguous ones are match_echr_reports' job — suggest the rest
                if scored and not (scored[0][0] >= 0.5 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08)):
                    for s, p in scored[:2]:
                        ctx = f"{p['title']}" + (f" · app no {p['appno']}" if p["appno"] else "")
                        if cat.put_suggestion(ref_key, p["stable_id"], kind="echr-name",
                                              reason=f"name match “{name}” · EHRR {ry}",
                                              extracted_parties=name, context=ctx,
                                              held=True, score=s, commit=False):
                            st["echr"] += 1
                if on_progress and i % 500 == 0:
                    _progress(on_progress, stage="suggesting ECHR matches", done=i)
            cat.commit()
            resolved = Resolver(cat).run()
            pending = cat.count_pending_suggestions()

        self._invalidate_caches()
        return {**st, "resolved_edges": resolved.resolved, "pending_suggestions": pending}

    def decide_suggestion(self, *, ref: str, suggested_id: str, accept: bool,
                          resolve: bool = True) -> dict:
        """Apply a human's tick/cross on a suggestion. Accept mints the alias (so every
        sibling citation resolves), harvests the target if it isn't held yet (a gazetteer
        suggestion), and resolves. Reject just records the decision so the suggester
        never re-asks. ``resolve=False`` defers the resolver pass — the bulk accept-all
        sweep decides many rows then runs :meth:`resolve` once at the end."""
        from .core.text import fold

        with self._open() as (cat, rs, ts):
            n = cat.set_suggestion_status(ref, suggested_id, "accepted" if accept else "rejected")
            out: dict = {"updated": n, "accepted": accept}
            if accept:
                cat.put_alias(fold(ref), suggested_id, source="user-confirm")
                if cat.find_document_id(suggested_id) is None:
                    out["harvest"] = self._fetch_reference(
                        cat, rs, ts, ref=suggested_id, candidate=suggested_id, patient=True)
                if resolve:
                    # bounded: only edges keyed on the confirmed alias/target can flip
                    resolved = Resolver(cat).run_for_documents([suggested_id])
                    out["resolved_edges"] = resolved.resolved
        self._invalidate_caches()
        return out

    # reporter-jurisdiction code → the Explore jurisdiction label (only codes whose
    # corpora can actually be held; the rest simply produce no gate/label)
    _REPORTER_JUR_LABEL = {"AU": "Australia", "CA": "Canada", "NZ": "New Zealand",
                           "SG": "Singapore", "HK": "Hong Kong", "IN": "India",
                           "IE": "Ireland", "ZA": "South Africa", "MY": "Malaysia",
                           "KE": "Kenya", "GH": "Ghana", "NG": "Nigeria"}

    def list_pending_suggestions(self, *, limit: int = 500) -> dict:
        """Every pending "Possibly: …?" naming candidate, best score first, ENRICHED
        with what a reviewer needs to judge each one in context:

        - ``target``: the suggested document's title / court / date / jurisdiction;
        - ``occurrences`` + ``citing_jurisdictions``: how often (and from where) the
          corpus actually cites the hanging reference — the impact of accepting;
        - ``flags``: red/amber warnings computed from the systematic error classes
          seen in the wild — a report series that names a different jurisdiction
          than the match (ALR is Australian, the match is Irish), legislation cited
          mostly from another jurisdiction's documents (an Irish judgment's
          "Companies Act 1990" is the Irish Act, not the UK's 1989 one), report-year
          vs decision-year disagreement, and initials-only extracted names whose
          token matches are unreliable.

        Flags are computed here at read time, so they apply to suggestions minted
        before the flagging existed."""
        import re as _re

        from .citations.report_match import surnames
        from .citations.reporters import report_series, reporter_jurisdiction
        from .core.text import fold

        def _yr(s: str | None) -> int | None:
            m = _re.search(r"[\[(](1[6-9]\d{2}|20\d{2})[\])]", s or "")
            if m:
                return int(m.group(1))
            m = _re.search(r"\bEHRR (\d{4})\b", s or "")
            return int(m.group(1)) if m else None

        with self._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.pending_suggestions(limit=limit)]
            total = cat.count_pending_suggestions()

            ids = sorted({r["suggested_id"] for r in rows if r["suggested_id"]})
            targets: dict[str, dict] = {}
            if ids:
                qs = ",".join("?" * len(ids))
                for d in cat.conn.execute(
                        "SELECT stable_id, title, court, decision_date, doc_type, source "
                        f"FROM documents WHERE stable_id IN ({qs})", ids).fetchall():
                    targets[d["stable_id"]] = dict(d)

            refs = sorted({r["ref"] for r in rows if r["ref"]})
            fold_of = {ref: fold(ref) for ref in refs}
            by_fold: dict[str, str] = {}
            for ref in refs:
                by_fold.setdefault(fold_of[ref], ref)
            evidence: dict[str, dict] = {ref: {"n": 0, "jurs": {}} for ref in refs}
            if refs:
                qs = ",".join("?" * len(refs))
                for row in cat.conn.execute(
                        "SELECT r.candidate_id AS cand, r.raw_fold AS rf, d.source AS src, "
                        "COUNT(*) AS n FROM relations r "
                        "JOIN documents d ON d.stable_id = r.src_id "
                        "WHERE r.resolution_status = 'pending' "
                        f"AND (r.candidate_id IN ({qs}) OR r.raw_fold IN ({qs})) "
                        "GROUP BY r.candidate_id, r.raw_fold, d.source",
                        (*refs, *[fold_of[r] for r in refs])).fetchall():
                    ref = row["cand"] if row["cand"] in evidence else by_fold.get(row["rf"])
                    if ref is None:
                        continue
                    ev = evidence[ref]
                    ev["n"] += row["n"]
                    jur = self._jurisdiction_of(row["src"])
                    ev["jurs"][jur] = ev["jurs"].get(jur, 0) + row["n"]

            for r in rows:
                kind = r.get("kind") or ""
                t = targets.get(r["suggested_id"])
                tj = self._doc_bucket(t["source"], t["court"]) if t else None
                ty = int(str(t["decision_date"])[:4]) if t and t["decision_date"] \
                    and str(t["decision_date"])[:4].isdigit() else None
                if t:
                    r["target"] = {
                        "title": t["title"], "court": t["court"],
                        "court_label": self.court_label(t["court"], t["source"]) if t["court"] else None,
                        "date": str(t["decision_date"])[:10] if t["decision_date"] else None,
                        "doc_type": t["doc_type"], "jurisdiction": tj,
                        "source_label": self.source_label(t["source"]),
                    }
                ev = evidence.get(r["ref"]) or {"n": 0, "jurs": {}}
                r["occurrences"] = ev["n"]
                r["citing_jurisdictions"] = ev["jurs"]

                flags: list[dict] = []
                if kind == "case-name":
                    series = report_series(r["ref"])
                    sj = reporter_jurisdiction(series) if series else None
                    sj_label = self._REPORTER_JUR_LABEL.get(sj) if sj else None
                    if sj_label and tj and sj_label != tj:
                        flags.append({"id": "series-jurisdiction", "level": "red",
                                      "note": f"{series} is a {sj_label} report series, "
                                              f"but the suggested match is {tj}"})
                if kind.startswith("legislation") and ev["jurs"] and tj:
                    top_j, top_n = max(ev["jurs"].items(), key=lambda kv: kv[1])
                    if top_j != tj and top_n >= 2 and top_n / sum(ev["jurs"].values()) >= 0.6:
                        flags.append({"id": "citing-jurisdiction", "level": "red",
                                      "note": f"cited almost only by {top_j} documents, but the "
                                              f"suggested match is {tj} legislation — same-name "
                                              f"acts exist across jurisdictions"})
                if kind in ("case-name", "echr-name"):
                    ry = _yr(r["ref"]) or _yr(r.get("reason"))
                    if ry and ty and not (ry - 3 <= ty <= ry + 1):
                        flags.append({"id": "year", "level": "amber",
                                      "note": f"reported {ry} but the match was decided {ty} — "
                                              f"outside the reporting-lag window"})
                    parties = (r.get("extracted_parties") or "").strip()
                    applicant = _re.split(r"\s+v\.?\s+", parties, maxsplit=1)[0] if parties else ""
                    if applicant and not surnames(applicant):
                        flags.append({"id": "weak-name", "level": "amber",
                                      "note": "the extracted name is initials-only — name-token "
                                              "matching is unreliable, check the context"})
                r["flags"] = flags
            return {"total": total, "suggestions": rows}

    def reference_context(self, ref: str, *, limit: int = 5) -> dict:
        """The passages where the corpus actually cites a hanging reference — the
        evidence a human needs to judge a near-miss suggestion. Each snippet is the
        citing sentence-neighbourhood (from the edge's stored context span) with
        the citing document's citation form."""
        from .core.text import fold

        with self._open() as (cat, _rs, ts):
            out: list[dict] = []
            for occ in cat.reference_occurrences(ref, fold(ref), limit=limit):
                sdoc = cat.get_document(occ["src_id"])
                snippet = None
                cs, ce = occ["context_start"], occ["context_end"]
                if sdoc and sdoc["payload_hash"] and cs is not None:
                    try:
                        text = ts.get(sdoc["payload_hash"])
                        a = max(0, cs - 140)
                        b = min(len(text), (ce or cs) + 240)
                        snippet = text[a:b].strip()
                    except OSError:
                        snippet = None
                out.append({
                    "src_id": occ["src_id"],
                    "src_oscola": _oscola_cite(sdoc, _row_meta(sdoc)) if sdoc else None,
                    "src_title": sdoc["title"] if sdoc else None,
                    "raw": occ["raw_citation_string"],
                    "snippet": snippet,
                })
            return {"ref": ref, "occurrences": out}

    # -- refinement flags (reader passages flagged for linking-logic review) --
    def flag_refinement(self, *, doc_id: str, selected_text: str, anchor: str | None = None,
                        context: str | None = None, current_links: str | None = None,
                        note: str | None = None) -> dict:
        with self._open() as (cat, _rs, _ts):
            cat.add_refinement_flag(doc_id=doc_id, selected_text=selected_text, anchor=anchor,
                                    context=context, current_links=current_links, note=note)
        return {"flagged": True}

    def list_refinement_flags(self, *, status: str | None = "open", limit: int = 500) -> list[dict]:
        with self._open() as (cat, _rs, _ts):
            return [dict(r) for r in cat.refinement_flags(status=status, limit=limit)]

    def resolve_refinement_flag(self, *, flag_id: int, status: str = "resolved") -> dict:
        with self._open() as (cat, _rs, _ts):
            return {"updated": cat.set_refinement_flag(flag_id, status)}

    def mine_parallel_citations(self, *, limit_docs: int | None = None, coref: bool = True,
                                on_progress=None, cancel_check=None) -> dict:
        """Recover the neutral-citation ↔ law-report map from the corpus text (§5c).

        Within each judgment, runs of citations separated only by ``;`` / ``,`` / pinpoints
        are *parallel* citations of one case (``adjacency_groups``); those runs are unioned
        into global clusters. A weaker name+year rung (``coref=True``) links citations
        across judgments. Each cluster is anchored to the held document its (single) neutral
        citation names, and every other member is aliased to it — so a citation in any
        parallel form resolves to that one case. The one-neutral-per-cluster invariant
        vetoes bad merges. Aliases are tagged ``parallel:adjacency`` / ``parallel:coref``.
        """
        from collections import defaultdict

        from .citations.parallel import (
            ClusterIndex, Occurrence, adjacency_groups, coref_key, link_eu_reports, occ_neutral,
        )
        from .citations.report_match import extract_preceding_name
        from .core.text import fold

        idx = ClusterIndex()
        adjacency_keys: set[str] = set()
        coref_buckets: dict[tuple, list[str]] = defaultdict(list)
        eu_report_links: dict[str, str] = {}  # folded ECR string → CJEU case candidate
        st = {"docs": 0, "adjacency_groups": 0, "clusters": 0, "anchored": 0,
              "pending_clusters": 0, "aliased": 0, "eu_report_links": 0}

        with self._open() as (cat, _rs, ts):
            src_ids = cat.docs_with_citations(min_count=2, limit=limit_docs)
            text_cache: dict[str, str | None] = {}

            def _text(sid: str) -> str | None:
                if sid not in text_cache:
                    doc = cat.get_document(sid)
                    ph = doc["payload_hash"] if doc else None
                    try:
                        text_cache[sid] = ts.get(ph) if ph else None
                    except OSError:
                        text_cache[sid] = None
                return text_cache[sid]

            for i, sid in enumerate(src_ids):
                if cancel_check and cancel_check():
                    break
                text = _text(sid)
                if not text:
                    continue
                # Only case-like citation strings may join a cluster. Act/instrument rows
                # include carry-forward pinpoints whose raw is a bare "para 8" / "s.689" —
                # the SAME folded key across the whole corpus. Fed to the union-find they
                # weld unrelated cases into one mega-cluster: its first neutral then vetoes
                # every later (correct) merge, and the anchoring step mints nonsense aliases
                # ("para 98" → a random judgment) that misdirect resolution corpus-wide.
                occs = [Occurrence(r["raw"], r["char_start"], r["char_end"],
                                   candidate=(r["candidate_id"] if r["entity_kind"] == "case" else None))
                        for r in cat.citation_occurrences(sid)
                        if r["entity_kind"] in ("case", "echr_case")]
                for o in occs:
                    idx.add(fold(o.raw), neutral=occ_neutral(o))
                # Stage A — adjacency runs within this judgment
                for group in adjacency_groups(text, occs):
                    st["adjacency_groups"] += 1
                    keys = [fold(g) for g in group]
                    for k in keys[1:]:
                        idx.union(keys[0], k)
                    adjacency_keys.update(keys)
                # EU report rung — an ECR citation following a CJEU case number is that
                # case's alternative reference form ("Case 25/62 Plaumann v Commission
                # [1963] ECR 95").
                for ecr_raw, case_cand in link_eu_reports(text, occs):
                    eu_report_links[fold(ecr_raw)] = case_cand
                # Stage C — name+year coreference key per occurrence
                if coref:
                    for o in occs:
                        if o.char_start is None:
                            continue
                        name = extract_preceding_name(text[max(0, o.char_start - 200): o.char_start])
                        ck = coref_key(name, o.raw)
                        if ck:
                            coref_buckets[ck].append(fold(o.raw))
                st["docs"] += 1
                text_cache.pop(sid, None)  # bounded memory: one judgment's text at a time
                if on_progress and i % 500 == 0:
                    _progress(on_progress, stage="mining parallel citations",
                              done=i, total=len(src_ids))

            # apply the coreference unions (the neutral-veto guards each merge)
            if coref:
                for keys in coref_buckets.values():
                    uniq = list(dict.fromkeys(keys))
                    for k in uniq[1:]:
                        idx.union(uniq[0], k)

            # clusters are rebuilt from scratch each run, so previous parallel-mined
            # aliases are stale output, not state — drop them (in the same transaction
            # as the re-mint) so a bad alias from an earlier run self-heals
            st["cleared"] = cat.delete_aliases_by_source(
                ("parallel:adjacency", "parallel:coref", "eu-report"), commit=False)

            # anchor each cluster to its held document and alias the rest to it
            for members in idx.clusters():
                st["clusters"] += 1
                canonical = idx.neutral_of(members[0])
                if not canonical:
                    for m in members:  # a member may already alias to a held case
                        dst = cat.get_alias(m)
                        if dst:
                            canonical = dst
                            break
                if not canonical:
                    continue
                if not cat.find_document_id(canonical):
                    st["pending_clusters"] += 1  # cluster real but its case isn't held (yet)
                    continue
                st["anchored"] += 1
                canon_key = fold(canonical)
                for m in members:
                    if m == canon_key:
                        continue
                    source = "parallel:adjacency" if m in adjacency_keys else "parallel:coref"
                    cat.put_alias(m, canonical, source=source, commit=False)
                    st["aliased"] += 1

            # EU report links: alias each ECR string to its CJEU case, chaining one level
            # through a CELEX→ECLI alias when the case is held under its ECLI. The series
            # guard rejects a chain whose court contradicts the ECR series ("ECR II-" is
            # the General Court → an ECLI:EU:C: target is a mis-chain, so keep the raw
            # case candidate rather than resolve to the wrong decision).
            for ecr_key, case_cand in eu_report_links.items():
                chained = cat.get_alias(fold(case_cand))
                target = chained if (chained and _ecr_series_ok(ecr_key, chained)) else case_cand
                cat.put_alias(ecr_key, target, source="eu-report", commit=False)
                st["eu_report_links"] += 1
            cat.commit()
            resolved = Resolver(cat).run()

        self._invalidate_caches()
        st["resolved_edges"] = resolved.resolved
        return st

    def discover_citing(self, *, target: str, via: str = "auto", query: str | None = None,
                        max_pages: int = 1, resolve: bool = True) -> dict:
        """Forward-citation discovery — find **new** cases that cite ``target``, by
        querying the live source (this is what genuinely grows over time):
        - an EU instrument (CELEX) → CELLAR structured "cases interpreting this
          legislation" (``eu-cellar``);
        - a UK act/case → Find Case Law **full-text search** for its citation/title
          (``uk-caselaw``), which surfaces judgments that mention it.
        Returns the ids of newly-harvested citing documents (seeds for enrichment)."""
        t = target.strip()
        if via == "auto":
            via = "eu-cellar" if self._CELEX_FULL.match(t.upper()) else "uk-caselaw"
        if via not in ("eu-cellar", "uk-caselaw"):
            return {"error": f"unknown discovery source {via!r}"}

        with self._open() as (cat, _rs, _ts):
            before = {r["stable_id"] for r in cat.list_documents(source=via, limit=100000)}
            search = (query or t) if via == "eu-cellar" else (query or self._search_query_for(cat, t))

        # ignore_watermark: this is a SEARCH for citing cases, not an incremental crawl —
        # the newest-first recency cutoff would otherwise drop every older match (the bug
        # behind "find citing cases" always reporting +0).
        if via == "eu-cellar":
            # a CJEU *case* CELEX (sector 6, e.g. 62020CJ0245) → cases CITING it; a piece of
            # EU *legislation* (sector 3) → cases interpreting it. Using the legislation
            # query on a case CELEX is why CJEU seeds always reported "+0 citing".
            opts = {"cited_by_celex": t} if re.match(r"^6\d{4}[A-Z]", t.upper()) else {"legislation_celex": t}
            h = self.harvest("eu-cellar", options=opts, max_pages=max_pages,
                             resolve=resolve, ignore_watermark=True)
        else:
            h = self.harvest("uk-caselaw", options={"query": search}, max_pages=max_pages,
                             resolve=resolve, ignore_watermark=True)

        with self._open() as (cat, _rs, _ts):
            after = {r["stable_id"] for r in cat.list_documents(source=via, limit=100000)}
        discovered = sorted(after - before)
        return {"via": via, "query": search, "harvested": h.get("stored", 0),
                "discovered": discovered, "count": len(discovered)}

    def run_watch(self, *, watch_id: int, on_progress=None, cancel_check=None) -> dict:
        """Execute one watch: (1) harvest its source (keywords searched at the API
        where supported, else used to limit the seeds); (2) gather seeds (the keyword-
        matching source docs + any ``seed_rule`` set); (3) autosnowball ``degrees``
        hops; (4) tag everything brought in. Records the result + last-run time.

        Runnable as a background job (``on_progress``/``cancel_check``) so it appears in
        the Jobs panel with per-stage progress instead of blocking a request."""
        def _emit(stage: str, **kw):
            _progress(on_progress, stage=stage, **kw)

        with self._open() as (cat, _rs, _ts):
            w = cat.get_watch(watch_id)
        if w is None:
            return {"error": f"no watch {watch_id}"}
        spec = json.loads(w["spec_json"] or "{}")
        from .adapters.registry import SOURCE_INFO

        source = spec.get("source")
        keywords = spec.get("keywords") or []
        result: dict = {"watch_id": watch_id, "name": w["name"]}
        seed_ids: list[str] = []

        if source:
            _emit(f"harvesting {source}")
            opts = dict(spec.get("source_options") or {})
            info = SOURCE_INFO.get(source)
            if keywords and info and info.keyword_search and "query" not in opts:
                opts["query"] = " ".join(keywords)  # search at the source API
            # Each watch keeps its OWN cursor: two watches on one source see different
            # slices of the feed (different query/court), so sharing the source-wide
            # watermark let whichever ran last blind the others. A brand-new watch
            # starts from the top of the feed (bounded by max_pages) and then follows.
            wm_key = f"watch:{watch_id}:{source}"
            with self._open() as (cat, _rs, _ts):
                has_cursor = cat.get_watermark(wm_key) is not None
            # Once a cursor exists, the cursor bounds the crawl — page until we reach
            # it (with a generous safety cap) rather than stopping at max_pages. A page
            # cap on an incremental crawl silently loses everything between the cap and
            # the cursor: the watermark still jumps to the newest item seen.
            max_pages = (spec.get("max_pages_incremental", 40) if has_cursor
                         else spec.get("max_pages", 1))
            # ``backfill`` means "the FIRST run walks deep" — not "ignore the cursor
            # forever". A backfill harvest reads no watermark at all, so a recurring
            # watch spec with backfill:true re-walked its entire upstream register on
            # every cadence tick (the NL Rechtspraak daily sync re-paged a million-row
            # SRU feed from 0 each day). Once a cursor exists the walk has happened;
            # every later run follows it incrementally.
            h = self.harvest(source, backfill=bool(spec.get("backfill")) and not has_cursor,
                             max_pages=max_pages, options=opts, watermark_key=wm_key,
                             use_llm=spec.get("use_llm"), on_progress=on_progress)
            result["harvest"] = h

        # Forward-citation discovery: NEW cases citing a target (the renewing seed).
        disc = spec.get("discover")
        if disc and disc.get("citing"):
            _emit(f"discovering cases citing {disc['citing']}")
            d = self.discover_citing(target=disc["citing"], via=disc.get("via", "auto"),
                                     query=disc.get("query"), max_pages=spec.get("max_pages", 1))
            result["discover"] = {k: d.get(k) for k in ("via", "query", "count")}
            seed_ids = list({*seed_ids, *d.get("discovered", [])})

        # NO snowballing. A watch is the systematic path now: harvest the register's
        # delta, extract, resolve — done. The old radiate stage re-fetched every
        # keyword-matched "seed" one at a time (the mysterious "seeding 1/23" a watch
        # froze on at WAF pace) and then chased citations ``degrees`` hops, even when
        # degrees was 0. Full-register backfills + set-based resolution made that
        # expansion obsolete; ``radiate`` survives only as an explicit one-off job.
        if spec.get("tag"):
            brought = list({*seed_ids,
                            *self._keyword_seed_docs(source, keywords,
                                                     limit=spec.get("max_seeds", 60))})
            if brought:
                self.tag_many(doc_ids=brought, tag=spec["tag"])
                result["tagged"] = len(brought)

        with self._open() as (cat, _rs, _ts):
            cat.update_watch(watch_id, {"last_run_at": _now_iso(),
                                        "last_result_json": json.dumps(result)})
        return result

    def due_watch_ids(self) -> list[int]:
        """The enabled watches whose cadence is due now — the scheduler starts a job per id
        (so each shows in the Jobs panel), rather than running them inline invisibly.

        Due-ness is **staggered** per watch (see :func:`watch_is_due`) so that watches
        sharing a cadence — every daily register sync, every weekly source — don't all
        come due in the same tick and stampede the pipeline. Each fires once per cadence
        window, at a deterministic phase offset from its neighbours."""
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        return [w["watch_id"] for w in self.list_watches()
                if w["enabled"]
                and watch_is_due(w["watch_id"], w["cadence_minutes"], w.get("last_run_at"), now)]

    def tick_watches(self) -> dict:
        """Run every enabled watch whose cadence is due (the scheduler's unit of
        work). Idempotent and safe to call on a timer."""
        ran = [self.run_watch(watch_id=wid) for wid in self.due_watch_ids()]
        return {"ran": len(ran), "results": ran}

    def harvest(
        self, source: str, *, backfill: bool = False, since: str | None = None,
        max_pages: int | None = 1, options: dict | None = None, resolve: bool = True,
        ignore_watermark: bool = False, watermark_key: str | None = None,
        refetch_held: bool = False, use_llm: bool | None = None,
        postprocess_after_relation_id: int = 0,
        on_progress=None, cancel_check=None,
    ) -> dict:
        """Run one source through the pipeline, then resolve + tag — the §8
        "trigger a backfill / re-run a source from the browser" action. ``options``
        are passed to the adapter (e.g. ``{"query": "unfair dismissal"}`` for the
        Find Case Law keyword search, ``{"court": "ewca/civ"}``). Foreground and
        bounded by ``max_pages`` so a UI click returns; large backfills run via the
        CLI/cron."""
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline
        from .tagging import RuleEngine

        try:
            adapter = get_adapter(source, **(options or {}))
        except (KeyError, TypeError) as exc:
            return {"error": str(exc)}

        # Keep the durable discovery cursor visible on EVERY later phase's checkpoint.
        # Discovery persists {"phase": "discover", "resume_offset": N}; without merging,
        # the first extract/resolve/tag checkpoint OVERWRITES it — so a job interrupted
        # after discovery would resume by replaying the entire upstream walk from 0
        # (the exact failure the offset exists to prevent). Seeded from the restored
        # ``start_offset`` because a resumed discovery that lands exactly at the feed's
        # end yields zero stubs — and therefore zero discover checkpoints to merge from
        # (observed in production: the NL job's extract checkpoint lost its offset).
        discover_cursor: dict = {}
        if (options or {}).get("start_offset"):
            # adapter.source, not the registry key: "fr-dila-legi" resolves to an
            # adapter whose checkpoints (and _resume_row's comparison) say "fr-dila".
            discover_cursor.update(source=adapter.source,
                                   resume_offset=int(options["start_offset"]))

        def _phase_progress(**p) -> None:
            ck = p.get("_checkpoint")
            if isinstance(ck, dict):
                if ck.get("phase") == "discover":
                    discover_cursor.update(
                        {k: ck[k] for k in ("source", "resume_offset") if ck.get(k) is not None})
                elif discover_cursor:
                    for k, v in discover_cursor.items():
                        ck.setdefault(k, v)
            _progress(on_progress, **p)

        with self._open() as (cat, rs, ts):
            pipe = Pipeline(cat, rs, textstore=ts)
            stats = pipe.run(adapter, backfill=backfill, since=since, max_pages=max_pages,
                             refetch_held=refetch_held,
                             ignore_watermark=ignore_watermark, watermark_key=watermark_key,
                             on_progress=_phase_progress, cancel_check=cancel_check)
            # Extract + classify ONLY the newly-fetched documents — NOT the whole corpus.
            # (Re-extracting all ~20k docs on every harvest was O(minutes) of pure-CPU
            # grammar work; resolution already links existing pending edges to the new
            # nodes without re-mining their text.) Upstream-REVISED documents the crawl
            # re-fetched (contenthash changed) aren't "new" but their text changed, so
            # they get the same re-extract/classify pass.
            new_ids = list(dict.fromkeys([*stats.stored_ids, *stats.refreshed_ids]))
            # Bulk primary-law/caselaw sources: no guidance can occur in them (skip the
            # per-document classification PK lookups) and their post-processing must be
            # rebuilt from durable state on restart (see below).
            primary_bulk_sources = {
                "fr-dila", "fr-dila-legi", "de-rii", "de-gii", "de-gesetze",
                "de-gesetze-im-internet", "nl-rechtspraak", "nl-legislation",
            }
            # Rebuild the extraction worklist from durable state instead of an in-memory
            # list two ways of losing it:
            # - a cursor-resumed discovery (``start_offset``) intentionally skips the
            #   already-walked prefix, so ``stored_ids`` misses everything stored before
            #   the restart;
            # - ECLI-keyed bulk sources (de-rii, the DILA jurisprudence funds) key their
            #   stubs on the FILE name but store under the ECLI resolved at fetch, so the
            #   pipeline's held-but-unextracted carry-forward never matches them and a
            #   restart would strand the whole stored backlog with no citation graph.
            # ``last_extracted_at`` is stamped even for citation-free documents, so this
            # selects precisely the stored-but-unfinished backlog and converges.
            if (options or {}).get("start_offset") or adapter.source in primary_bulk_sources:
                new_ids = list(dict.fromkeys([
                    *cat.text_document_ids(source=adapter.source, only_never_extracted=True),
                    *new_ids,
                ]))
            from .citations import extract_documents_parallel
            from .treatment import classify_corpus
            llm_cite, classifier = self._llm_passes(use_llm)
            aliases = cat.named_alias_map()
            # The pooled extractor: regex on N cores, batched commits, progress
            # throttled for bulk seeds (per-doc callbacks alone cost ~90 minutes over
            # 1.7m documents). Resume-safe like the loop it replaces: the backlog
            # select above is stamp-driven, so a crash re-extracts at most one
            # uncommitted batch. With an LLM pass it falls back to the serial path
            # (the extractor is unpicklable and network-bound anyway).
            extract_documents_parallel(
                cat, ts, new_ids, aliases=aliases, llm=llm_cite,
                stage="extracting citations",
                checkpoint_fn=lambda done, sid: {"phase": "extract", "done": done},
                post_fn=lambda sid: classify_corpus(cat, ts, classifier=classifier,
                                                    stable_id=sid),
                on_progress=_phase_progress, cancel_check=cancel_check)
            # Guidance classification (§1.9/§4a): every guidance-typed document — and
            # every EDPB publication regardless of doc_type (binding decisions and
            # opinions carry the same citable series numbers) — gets its issuer /
            # identity / version / status / regime fields the moment it lands. NOT
            # edpb-oss: those are national DPA decisions, not Board guidance.
            # Bulk primary-law/caselaw sources cannot contain guidance. Avoid millions
            # of pointless PK lookups after DILA, RII/GII, or Rechtspraak imports.
            if adapter.source not in primary_bulk_sources:
                for i, sid in enumerate(new_ids, 1):
                    if cancel_check and cancel_check():
                        break
                    if i == 1 or i % 1000 == 0 or i == len(new_ids):
                        _phase_progress(stage="classifying harvested documents", done=i,
                                        total=len(new_ids), item=sid)
                    doc = cat.get_document(sid)
                    if doc is not None and (doc["doc_type"] == "guidance" or doc["source"] == "edpb"):
                        self._classify_guidance_into(cat, ts, sid)
            # ``resolve=False`` lets a batch caller (e.g. seed-from-text over many seeds)
            # resolve ONCE at the end instead of re-resolving the whole graph per call.
            # Ingest changes only two bounded sets: edges emitted BY each new document,
            # and old pending edges pointing TO it.  A whole-graph Resolver.run() here
            # made even a one-document LEGI smoke import scan millions of relations and
            # hit the three-minute statement timeout.  Reserve whole-graph resolution
            # for its explicit maintenance job; harvest is incremental and durable.
            resolved_n = 0
            if resolve:
                resolver = Resolver(cat)
                rules = RuleEngine(cat)
                # Per-document resolution issues one target-side UPDATE per imported doc,
                # so it is only ever worth it for a genuine handful — beyond that the
                # set-based ``run_batched`` (one bounded relation-id sweep for the whole
                # import) wins by orders of magnitude. A GDPRhub-scale backfill (~3.7k docs)
                # crawling for an hour under the per-doc loop is the symptom of setting
                # this too high; keep the per-doc path for small incremental ticks only.
                bulk_threshold = int(os.environ.get("RAGLEX_BULK_POSTPROCESS_THRESHOLD") or 200)
                if len(new_ids) >= bulk_threshold:
                    # Never issue one target-side UPDATE per imported document. At DILA
                    # scale that meant 1.7m scans and a months-long "silent" phase.
                    # ``postprocess_after_relation_id`` is the relation cursor a resumed
                    # job restores so an interrupted resolve continues instead of
                    # rescanning already-committed ranges.
                    bulk = resolver.run_batched(
                        after_id=postprocess_after_relation_id,
                        on_progress=_phase_progress, cancel_check=cancel_check,
                    )
                    resolved_n += bulk.resolved
                    if not (cancel_check and cancel_check()):
                        rules.run_on_documents(
                            new_ids, on_progress=_phase_progress, cancel_check=cancel_check,
                        )
                else:
                    for i, sid in enumerate(new_ids, 1):
                        if cancel_check and cancel_check():
                            break
                        # every 25, not every 1000: the job runner already throttles
                        # heartbeats to 1/s, and a 1000-doc gap meant HOURS with the
                        # display stuck on "1/4143" whenever per-doc resolution was
                        # slow — indistinguishable from a hang.
                        if i == 1 or i % 25 == 0 or i == len(new_ids):
                            _phase_progress(stage="resolving harvested citations", done=i,
                                            total=len(new_ids), item=sid)
                        doc = cat.get_document(sid)
                        resolved_n += cat.resolve_pending_from(sid)
                        resolved_n += resolver.run_for(sid, doc["ecli"] if doc else None)
                        rules.run_on_document(sid)
            result = asdict(stats)
            result.pop("stored_ids", None)  # internal and potentially hundreds of thousands
            return {**result, "resolved_edges": resolved_n,
                    "new_documents": len(new_ids)}

    def finish_bulk_postprocess(self, *, source: str | None = None, resolve: bool = True,
                                tag: bool = True, batch_size: int = 50000,
                                after_relation_id: int = 0, tag_start: int = 0,
                                on_progress=None, cancel_check=None) -> dict:
        """Complete the resolve/tag phases of a bulk import WITHOUT re-running discovery
        or citation extraction — the recovery path for a large harvest whose
        post-processing was interrupted (or ran under the old one-UPDATE-per-document
        algorithm and had to be cancelled).

        Resolution runs set-wise over bounded relation-id ranges
        (:meth:`Resolver.run_batched`), committing and checkpointing each range;
        ``after_relation_id`` restores the persisted cursor so a resumed job continues
        rather than rescanning. Tagging applies the enabled rules once over ``source``'s
        text documents in stable (sorted) id order; ``tag_start`` skips the prefix a
        previous attempt completed. Both phases are idempotent, so replaying the last
        bounded range after an interruption is safe.

        The invariant this protects: a large import must NEVER perform incoming-target
        resolution once per imported document — that is what turned the 1.7m-document
        DILA import's final phase into months of repeated pending-edge scans.
        """
        from .tagging import RuleEngine

        out: dict = {"source": source or "*"}
        with self._open() as (cat, _rs, _ts):
            if resolve:
                stats = Resolver(cat).run_batched(
                    batch_size=batch_size, after_id=after_relation_id,
                    on_progress=on_progress, cancel_check=cancel_check)
                out["resolved_edges"] = stats.resolved
                if stats.still_pending:
                    out["still_pending"] = stats.still_pending
            if tag and not (cancel_check and cancel_check()):
                rules = RuleEngine(cat)
                # sorted() pins the order: text_document_ids orders by the extraction
                # stamp, which the NEXT extraction pass would reshuffle under a resumed
                # tag cursor. No extraction runs inside this job, but sorting makes the
                # ``tag_start`` offset stable against that hazard for free.
                ids = sorted(cat.text_document_ids(source=source))
                out["tag_total"] = len(ids)
                out["tagged"] = rules.run_on_documents(
                    ids[tag_start:], start=tag_start,
                    on_progress=on_progress, cancel_check=cancel_check)
        self._invalidate_caches()
        return out

    def list_sources(self) -> list[str]:
        from .adapters.registry import ADAPTERS

        return sorted(ADAPTERS)

    def provider_health(self) -> dict:
        """Whether the configured embedding provider is usable (key present etc.)."""
        p = self._provider()
        return {"provider": p.name, "model": p.model, "dimensions": p.dimensions,
                "healthy": p.health()}

    def create_index(self) -> dict:
        """Build the pgvector HNSW index for the configured provider's dimension
        (§7). No-op on SQLite."""
        with self._open() as (cat, _rs, _ts):
            dims = self._provider().dimensions
            created = cat.create_vector_index(dims)
            return {"backend": cat.backend, "dimensions": dims, "created": created}

    # -- guidance classification (§1.9/§4a): rules are DATA, fields carry EVIDENCE --

    def _guidance_rules_file(self):
        from pathlib import Path

        return Path(self.config.data_dir) / "guidance_rules.json"

    def guidance_rules(self) -> dict:
        """The effective classification rules: built-in defaults merged with the
        user's overlay file. What the rules UI renders and edits."""
        from .citations.guidance_class import merge_rules

        overlay = None
        try:
            overlay = json.loads(self._guidance_rules_file().read_text())
        except (OSError, ValueError):
            pass
        merged = merge_rules(overlay)
        merged["path"] = str(self._guidance_rules_file())
        return merged

    def update_guidance_rules(self, payload: dict) -> dict:
        """Persist the user's rules overlay (issuers merge by code over the defaults;
        collection mappings are overlay-only), then return the new effective rules —
        edit → save → re-classify is the improvement loop."""
        issuers = [i for i in (payload.get("issuers") or []) if i.get("code")]
        collections = {k: v for k, v in (payload.get("collections") or {}).items() if k}
        f = self._guidance_rules_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"issuers": issuers, "collections": collections}, indent=1))
        return self.guidance_rules()

    def classify_guidance_preview(self, *, stable_id: str | None = None,
                                  title: str | None = None, url: str | None = None,
                                  text: str | None = None) -> dict:
        """Dry-run the classifier and SHOW THE WORKING — per field: value, the rule
        that fired, and the text it matched. With a ``stable_id`` the held document
        supplies title/url/text and its citations supply the dominant-regime signal;
        with pasted title/url/text this is the rules test-bench (edit a rule, paste
        a cover page, see what would happen — no writes either way)."""
        from .citations.guidance_class import classify_guidance, dominant_regime

        rules = self.guidance_rules()
        regime = None
        current = None
        with self._open() as (cat, _rs, ts):
            if stable_id:
                doc = cat.get_document(stable_id)
                if doc is None:
                    return {"error": f"unknown document {stable_id!r}"}
                title = title or doc["title"]
                meta = cat.document_meta(stable_id)
                url = url or meta.get("url") or meta.get("bailii_url") or doc["landing_url"]
                if text is None and doc["payload_hash"]:
                    try:
                        text = ts.get(doc["payload_hash"])[:3000]
                    except OSError:
                        text = None
                regime = dominant_regime(cat.citations_for(stable_id))
                current = meta.get("guidance")
        fields = classify_guidance(title=title, text=text, url=url, rules=rules)
        aliases = fields.pop("aliases", [])
        if regime:
            fields["regime"] = regime
        elif "regime_default" in fields:
            fields["regime"] = fields.pop("regime_default")
        fields.pop("regime_default", None)
        return {"fields": fields, "aliases": aliases,
                **({"current": current} if current else {}),
                **({"stable_id": stable_id} if stable_id else {})}

    def _classify_guidance_into(self, cat, ts, stable_id: str, *,
                                issuer_default: str | None = None) -> dict:
        """Classify one held guidance document and persist the result: evidence-carrying
        fields into ``meta.guidance`` (a field a human set — method 'manual' — is never
        overwritten), the citation-form aliases, and one ``interprets`` edge to the
        regime when the document's own citations settle it."""
        from .citations import extract_document
        from .citations.guidance_class import classify_guidance, dominant_regime
        from .core.models import (ExtractedVia, RelationshipType, ResolutionStatus,
                                  TypedRelation)

        doc = cat.get_document(stable_id)
        if doc is None:
            return {"error": "unknown document"}
        meta = cat.document_meta(stable_id)
        text = None
        if doc["payload_hash"]:
            try:
                text = ts.get(doc["payload_hash"])
            except OSError:
                text = None
        # the dominant-regime signal needs the document's citations — extract if new
        if text and not cat.citations_for(stable_id):
            extract_document(cat, ts, stable_id)
        fields = classify_guidance(
            title=doc["title"], text=(text or "")[:3000],
            url=meta.get("url") or meta.get("bailii_url") or doc["landing_url"],
            rules=self.guidance_rules())
        aliases = fields.pop("aliases", [])
        regime = dominant_regime(cat.citations_for(stable_id))
        if regime:
            fields["regime"] = regime
        elif "regime_default" in fields:
            fields["regime"] = fields.pop("regime_default")
        fields.pop("regime_default", None)
        if issuer_default and "issuer" not in fields:
            fields["issuer"] = {"value": issuer_default, "method": "rule",
                                "rule": "collection-mapping",
                                "evidence": "the Zotero intake collection's saved issuer"}

        cur = meta.get("guidance") or {}
        for k, v in fields.items():
            if cur.get(k, {}).get("method") != "manual":  # human corrections always win
                cur[k] = v
        meta["guidance"] = cur
        cat.set_document_meta(stable_id, meta, commit=False)
        for a in aliases:
            if a and not cat.get_alias(a):
                cat.put_alias(a, stable_id, source="guidance-alias", commit=False)
        # one interprets edge to the regime (idempotent; survives re-extraction —
        # extract_document only clears regex/inferred edges)
        reg = cur.get("regime", {}).get("value")
        if reg and not any(r["relationship_type"] == str(RelationshipType.INTERPRETS)
                           and (r["dst_id"] == reg or r["raw_citation_string"] == reg)
                           for r in cat.relations_for(stable_id)):
            cat.add_relations(stable_id, [TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=reg, dst_id=reg,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING)])
        cat.commit()
        return {"fields": cur, "aliases": aliases}

    def set_guidance_field(self, *, stable_id: str, field: str, value: str | None) -> dict:
        """A human's correction of one classification field — recorded as method
        'manual' so no re-classify pass ever overwrites it. Empty value clears the
        field (back to eligible-for-rules)."""
        with self._open() as (cat, _rs, _ts):
            meta = cat.document_meta(stable_id)
            g = meta.get("guidance") or {}
            if value:
                g[field] = {"value": value, "method": "manual", "rule": "user-edit",
                            "evidence": ""}
            else:
                g.pop(field, None)
            meta["guidance"] = g
            cat.set_document_meta(stable_id, meta)
        self._invalidate_caches()
        return {"stable_id": stable_id, "guidance": g}

    def reclassify_guidance(self, *, limit: int | None = None,
                            on_progress=None, cancel_check=None) -> dict:
        """Re-run classification over every guidance document with the CURRENT rules —
        the second half of the improvement loop (edit a rule, re-classify, see what
        changed). Manual fields are untouched; a resolve pass links the new edges."""
        st = {"documents": 0, "classified": 0}
        with self._open() as (cat, _rs, ts):
            rows = cat.list_documents(doc_type="guidance", limit=limit or 100000)
            for i, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="classifying", done=i, total=len(rows),
                          item=r["stable_id"])
                st["documents"] += 1
                res = self._classify_guidance_into(cat, ts, r["stable_id"])
                if res.get("fields"):
                    st["classified"] += 1
            resolved = Resolver(cat).run()
        st["resolved_edges"] = resolved.resolved
        self._invalidate_caches()
        return st

    def _zotero_importer(self, *, library_id=None, api_key=None, library_type=None, http=None):
        """Build a ZoteroImporter from stored credentials. ONE field is enough: with
        just the API key, the numeric library id is derived from ``/keys/current``
        and persisted — nobody should have to find their userID by hand."""
        from .core.http import build_client

        api_key = api_key or self.settings.resolve("ZOTERO_API_KEY")
        if not api_key:
            return None, {"connected": False, "reason": "no_api_key",
                          "hint": "Create a key at zotero.org/settings/keys/new "
                                  "(read access is enough) and paste it here."}
        library_id = library_id or self.settings.resolve("ZOTERO_LIBRARY_ID")
        library_type = library_type or self.settings.resolve("ZOTERO_LIBRARY_TYPE") or "users"
        client = http or build_client(timeout=60)  # proxy-aware (§5a)
        importer = ZoteroImporter(client, library_id or "", api_key, library_type)
        if not library_id:
            info = importer.key_info()
            if not info:
                return None, {"connected": False, "reason": "bad_key",
                              "hint": "Zotero rejected the API key — re-check it."}
            importer.library_id = str(info["userID"])
            self.settings.update({"ZOTERO_LIBRARY_ID": importer.library_id})
        return importer, None

    def zotero_status(self, *, http=None) -> dict:
        """Is Zotero connected, as whom, and what collections exist — everything the
        intake UI needs to render a picker instead of asking for pasted keys."""
        importer, err = self._zotero_importer(http=http)
        if err:
            return err
        info = importer.key_info()
        if not info:
            return {"connected": False, "reason": "bad_key",
                    "hint": "Zotero rejected the API key — re-check it in Settings."}
        return {"connected": True, "username": info.get("username"),
                "library_id": importer.library_id, "library_type": importer.library_type,
                "collections": importer.list_collections()}

    def import_zotero(
        self, *, library_id: str | None = None, api_key: str | None = None,
        library_type: str | None = None, limit: int = 50, fetch_pdfs: bool = False,
        collection: str | None = None, doc_type: str | None = None, http=None,
    ) -> dict:
        """``collection`` + ``doc_type`` make Zotero the guidance-intake channel: the
        Zotero browser connector clips an EDPB/Ofcom page (with its PDF) into a
        designated collection from the user's real browser session — no bot-blocking
        to fight — and this pulls that collection in as ``guidance`` documents. A
        collection with a saved intake mapping (guidance rules) supplies doc_type and
        issuer defaults; imported guidance is auto-classified (with evidence) on the
        way in."""
        from .core.models import DocType as _DT

        importer, err = self._zotero_importer(library_id=library_id, api_key=api_key,
                                              library_type=library_type, http=http)
        if err:
            return {"error": err["hint"], **err}
        # a saved intake mapping for this collection supplies the defaults
        mapping = (self.guidance_rules().get("collections") or {}).get(collection or "", {})
        doc_type = doc_type or mapping.get("doc_type")
        dt = None
        if doc_type:
            try:
                dt = _DT(doc_type)
            except ValueError:
                return {"error": f"unknown doc_type {doc_type!r}"}
        with self._open() as (cat, rs, ts):
            ids = importer.import_into(cat, rs, ts, limit=limit, fetch_pdfs=fetch_pdfs,
                                       collection=collection or None, doc_type=dt)
            classified = 0
            for sid in ids:
                doc = cat.get_document(sid)
                if doc is not None and doc["doc_type"] == str(_DT.GUIDANCE):
                    res = self._classify_guidance_into(cat, ts, sid,
                                                       issuer_default=mapping.get("issuer"))
                    classified += 1 if res.get("fields") else 0
            if classified:
                Resolver(cat).run()  # the new interprets edges / aliases may resolve
        self._invalidate_caches()
        return {"imported": len(ids), "stable_ids": ids, "classified": classified}
