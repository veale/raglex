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
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator


from .citations.snowball import TARGETED_ADAPTERS as _SNOWBALL_TARGETED

log = logging.getLogger("raglex.facade")

# The bounded French grammar-refresh scope requested for the EU digital acquis.
# These are base CELEX identities (citations resolve to them even when the reader
# defaults to a dated consolidation).  Keep this explicit and reviewable: selecting
# every sector-3 instrument would turn a focused 8k-document repair back into a
# multi-million-document French rescan.
# What an editorial provision-to-provision mapping asserts. The distinction is
# substantive, not decorative: a repealed instrument's provision is the current one's
# ancestry, so citations to it read as that provision's history; a companion instrument
# drafted in the same package (GDPR / EUDPR / LED) is in force alongside, so its citations
# are a parallel provision's, never the current provision's past. Rows are labelled by
# this in the reader, and callers may not assert descent by accident — an unrecognised
# value is refused rather than coerced.
PROVISION_MAPPING_TYPES = {
    "functional_predecessor":
        "the other provision is an earlier iteration this one succeeds",
    "equivalent":
        "a parallel provision in a companion instrument, both in force",
    # A national provision implementing an EU one. Not descent and not a companion
    # instrument: the two are in force in different legal orders, and the EU case law
    # interpreting the directive is what makes the link worth having.
    #
    # It gates ITSELF. Where the transposing provision is UK, only retained EU case law
    # is inherited — CJEU judgments up to IP completion day bind UK courts (subject to
    # the higher courts' power to depart), later ones do not — so the cutoff is derived
    # from the jurisdiction rather than from the operator remembering a special type
    # name. Getting that wrong would present post-Brexit Luxembourg authority as
    # governing a domestic provision: a legal error, not an untidy result.
    "transposition":
        "this national provision transposes the other (EU) provision into domestic law; "
        "a UK transposition inherits retained EU case law only (pre-IP completion day)",
}

# IP completion day — 31 December 2020, 11pm. Date granularity is enough: the cutoff
# falls on a year boundary, which is what lets a document dated only by its ECLI year
# be placed on the right side of it.
RETAINED_EU_CASELAW_CUTOFF = "2020-12-31"
# Jurisdictions whose transpositions carry a cutoff, and what it is. Only the UK has
# one: no other member state left.
_TRANSPOSITION_CUTOFF_BY_JURISDICTION = {"United Kingdom": RETAINED_EU_CASELAW_CUTOFF}
# A UK instrument by its identifier. Checked alongside the jurisdiction bucket because
# the bucket is derived from the ADAPTER that imported the document — a hand-imported
# ukpga is no less a UK Act, and the cutoff must not depend on how it arrived.
_UK_INSTRUMENT_ID_RE = re.compile(
    r"^(?:ukpga|ukla|ukcm|uksi|ukmo|ukci|asp|ssi|anaw|asc|wsi|nia|nisr|apni|aosp|aep|mnia)/",
    re.IGNORECASE,
)

EU_DIGITAL_ACQUIS_IDS = (
    # data protection / data economy
    "31995L0046", "32016R0679", "32016L0680", "32002L0058",
    "32018R1807", "32019L1024", "32022R0868", "32023R2854",
    # platforms, intermediary services, markets and media
    "32000L0031", "32019R1150", "32022R1925", "32022R2065",
    "32010L0013", "32018L1972", "32024R1083", "32024R0903",
    # digital consumer acquis
    "32005L0029", "32006L0114", "32011L0083", "32017R2394",
    "32019L0770", "32019L0771", "32019L2161", "32019L0882",
    # copyright, cyber and emerging technology
    "32001L0029", "32019L0790", "32021R0784", "32022L2555",
    "32022L2557", "32024R1689", "32024R2847",
    # trust services, network security and communications privacy — the older
    # instruments national courts still cite heavily (Data Retention survives its own
    # annulment in the case law; NIS1 predates NIS2; the 2009 amendment is where the
    # ePrivacy consent rule actually lives)
    "32014R0910", "32024R1183", "32019R0881", "32016L1148",
    "32018R1725", "32006L0024", "32015R2120", "32013L0040",
    "32009L0136",
)


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


def _locate_span(text: str, selected: str, context: str | None) -> tuple[int, int] | None:
    """Find the char span of a reader selection in the stored document text. The rendered
    DOM collapses whitespace (newlines → spaces), so match whitespace-flexibly; when the
    selection occurs more than once, disambiguate by the enclosing segment ``context``."""
    if not text or not (selected or "").strip():
        return None
    words = selected.split()
    if not words:
        return None
    pat = re.compile(r"\s+".join(re.escape(w) for w in words), re.S)
    matches = [(m.start(), m.end()) for m in pat.finditer(text)]
    if not matches:
        i = text.find(selected)
        return (i, i + len(selected)) if i >= 0 else None
    if len(matches) == 1 or not context:
        return matches[0]
    cwords = context.split()[:40]
    if cwords:
        cpat = re.compile(r"\s+".join(re.escape(w) for w in cwords), re.S)
        cm = cpat.search(text)
        if cm:
            inside = [sp for sp in matches if cm.start() <= sp[0] <= cm.end()]
            if inside:
                return inside[0]
    return matches[0]


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

from .citations.extractor import SHORTHAND_MIN_DOCS as _SHORTHAND_MIN_DOCS
from .citations.extractor import valid_shorthand as _valid_shorthand
from .citations.oscola import cite as _oscola_cite
from .config import Config
from .core.models import (
    DocType,
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from .embeddings import EmbedStage

# The line under the free-text box. Editable in settings, because only the operator
# knows what the index currently covers and a search box that lies about its scope is
# worse than one that says nothing.
def _decades(years: list[dict] | None) -> dict:
    """Year counts rolled to decades — an agent wants the shape of the result set in
    time, not 120 individual years."""
    out: dict[str, int] = {}
    for y in years or []:
        try:
            d = f"{int(y['year']) // 10 * 10}s"
        except (ValueError, TypeError, KeyError):
            continue
        out[d] = out.get(d, 0) + y["n"]
    return dict(sorted(out.items()))


#: A reparse below this many segments, against a document that had many, is a
#: FLATTENING rather than an improvement. One segment means the parser found no
#: structure at all and handed back the whole document as a single block.
_FLATTEN_FLOOR = 2


def _would_flatten(ts, payload_hash: str | None, fresh: list) -> bool:
    """Would writing ``fresh`` replace real structure with one undifferentiated block?

    A reparse is supposed to be a projection refresh, and it is normally an improvement —
    but it overwrites unconditionally, and a parser that fails to recognise a document's
    shape returns the whole text as ONE segment rather than raising. Run over a corpus
    that is a rescue for the majority, that silently destroys the minority: the UK GDPR's
    base act went from its articles to a single 197,522-character blob, which is what the
    reader then displayed.

    The raw is immutable, so nothing is lost permanently — but the projection is what is
    served, and a whole-source sweep can flatten thousands of documents before anyone
    notices one. Refuse the write and report it; a genuine improvement never has to pass
    through one segment to get there.
    """
    if len(fresh) >= _FLATTEN_FLOOR or not payload_hash:
        return False
    try:
        held = ts.get_segments(payload_hash) or []
    except OSError:
        return False
    return len(held) >= _FLATTEN_FLOOR


def _segment_at(ts, doc, char_start: int | None) -> str | None:
    """The label of the structural unit containing ``char_start`` ("para 42",
    "Article 6"), so a free-text hit can be linked to the passage it matched."""
    if char_start is None or not doc["payload_hash"]:
        return None
    try:
        segments = ts.get_segments(doc["payload_hash"]) or []
    except OSError:
        return None
    if len(segments) <= 1 and (not segments or segments[0].kind in {"section", "body"}):
        try:
            from .core.segmentation import recover_numbered_segments
            segments, _recovered = recover_numbered_segments(
                ts.get(doc["payload_hash"]), segments)
        except OSError:
            pass
    for seg in segments:
        if seg.char_start <= char_start < seg.char_end and seg.label:
            return seg.label
    return None


_DEFAULT_FTS_NOTE = (
    "Searches the full text of the sources selected below. "
    "Put a phrase in \"quotation marks\" to match it literally.")
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

    # The canonical fold, which is what makes this agree with every other anchor
    # comparison in the system: "section 167", "s. 167" and "s167" are one provision, and
    # a UK segment label carries its title ("s. 167 Compliance orders") so a bare pinpoint
    # never equals it outright. Restricted to a SIMPLE anchor — a bare unit+number, with
    # optional sub-parts — because a compound label like "Sch 2 Pt 2 para 7" folds to just
    # "sch:2" and would otherwise match any part of Schedule 2.
    if _re.fullmatch(r"\s*[a-z]*\.?\s*\d+[a-z]?\s*(?:\([^()]+\)\s*)*", anchor.strip(),
                     _re.IGNORECASE):
        key = _anchor_key(anchor)
        if key:
            for i, s in enumerate(segs):
                if _anchor_key(s.label) == key:
                    return i

    # Last resort, and number-guarded. A bare substring test answered "s. 16" with
    # s. 166 — silently quoting the wrong provision, which is worse than finding
    # nothing — so a match may not continue the number we were given.
    if len(a) > 2:
        for i, s in enumerate(segs):
            label = norm(s.label)
            at = label.find(a)
            if at < 0:
                continue
            after = label[at + len(a):at + len(a) + 1]
            if a[-1].isdigit() and after.isdigit():
                continue
            return i
    return -1


#: A provision number carrying a letter suffix — "Article 12A", "Article 22B",
#: "Article 8ZA", "s. 164A", "Article 4a". Legislative drafting inserts new provisions
#: by suffixing the number of the one they follow, precisely so the existing numbering
#: survives; an ORIGINAL enacted text therefore does not contain them. Finding them in a
#: served body is direct evidence that the text has been amended, whatever the edges say.
_INSERTED_UNIT = re.compile(
    r"^\s*(?:articles?|arts?\.?|sections?|ss?\.?|regulations?|regs?\.?|"
    r"paragraphs?|paras?\.?)\s*(\d+[A-Za-z]{1,3})\b", re.IGNORECASE)


#: Sources whose base identifier serves a CONTINUOUSLY REVISED text rather than the
#: instrument as originally enacted. For these there is no separate consolidation to
#: import and none is missing: the editors amend the published text in place, and date
#: it. Dated expressions exist too, but as point-in-time *snapshots of* the revised text
#: — the reverse of the EU model, where the base act stays frozen and each consolidation
#: is its own document.
_REVISED_IN_PLACE_SOURCES = frozenset({"uk-legislation"})


def _revised_in_place(source: str | None) -> bool:
    return (source or "") in _REVISED_IN_PLACE_SOURCES


def _currency_from_raw(source: str | None, stable_id: str, raw: bytes) -> dict | None:
    """Currency facts re-derivable from a document's stored raw, or None.

    Only what the raw literally states — never an inference. For UK legislation that is
    the date the publisher says the served expression is the law as at
    (FRBRExpression/@validFrom), which older harvests dropped, leaving as_at null on all
    100,027 acts and the reader looking at an "undated legislation record" over text the
    source had dated precisely.

    Dated expressions (``id@date``) are skipped: their as_at is their identity, set when
    they were fetched, and must not be overwritten by whatever the file happens to say.
    """
    if source != "uk-legislation" or "@" in stable_id:
        return None
    from .formats.akoma_ntoso import expression_valid_from

    as_at = expression_valid_from(raw)
    return {"as_at": as_at} if as_at else None


def _inserted_provisions(labels) -> list[str]:
    """The letter-suffixed provision numbers among a document's segment labels."""
    found: list[str] = []
    for label in labels:
        m = _INSERTED_UNIT.match(label or "")
        if m:
            found.append(" ".join((label or "").split())[:40])
    return sorted(dict.fromkeys(found))


#: A party name written immediately before a citation ("… in Valero Energy Ltd v Persons
#: Unknown [2025] EWHC 134 KB"). The citation's own opening bracket is optional because
#: the run-up may or may not include it, depending on where the matched span begins.
_NAME_RUN_UP = re.compile(
    r"([A-Z][A-Za-z'’()\-.& ]{1,60}?\s+v\.?\s+[A-Z][A-Za-z'’()\-.& ]{1,60}?)"
    r"\s*[,\[(]?\s*$")

#: Lowercase words that genuinely belong inside a party name, so the lead-in trim below
#: walks past them instead of cutting the name short at "and others".
_NAME_CONNECTORS = frozenset({"and", "&", "of", "the", "others", "ors", "anor",
                              "another", "on", "behalf", "for", "in", "re"})


def _trim_party_lead_in(name: str) -> str:
    """Drop the sentence that introduces a case name, keeping the name.

    The regex above matches leftmost, so it swallows the prose ("In the more recent case
    of Valero Energy Ltd v …"). Trimming is done by walking BACK from the "v" through
    capitalised words and the connectors that belong inside a party name — not by
    shortening until the pattern stops matching, which ate "Valero Energy" and left the
    reader looking at "Limited and others v Persons Unknown"."""
    parts = re.split(r"\s+v\.?\s+", name, maxsplit=1)
    if len(parts) != 2:
        return name
    left, right = parts
    tokens = left.split()
    i = len(tokens)
    while i > 0 and (tokens[i - 1][:1].isupper()
                     or tokens[i - 1].lower() in _NAME_CONNECTORS):
        i -= 1
    tokens = tokens[i:] or left.split()
    while tokens and tokens[0].lower() in _NAME_CONNECTORS:
        tokens = tokens[1:]
    return f"{' '.join(tokens)} v {right}".strip() if tokens else name


def _cited_name_conflict(target_title: str | None, snippet: dict) -> dict | None:
    """Does the party name written beside a citation contradict what it resolved to?

    A UK neutral citation is a number, and a number is all a bare-citation match has to
    go on — so a typo in the citing judgment ("[2025] EWHC 134" for a 2024 case) mints a
    confident edge to a real but unrelated judgment. The citing court wrote the parties
    down right next to it; that is corroborating evidence, and it is free here because
    the snippet already carries the run-up.

    Only ever a FLAG. Deleting the edge on this evidence would lose the genuine citer
    that names a case some other way; in a citator precision is what matters, and a
    reader told "the name beside this citation is a different case" can judge it."""
    from .ops.uk_identity import _words

    title = target_title or ""
    mark = snippet.get("mark")
    body = snippet.get("text") or ""
    if " v " not in f" {title} " or not mark:
        return None                      # not a case name: nothing to corroborate against
    run_up = body[:mark[0]].rstrip()
    m = _NAME_RUN_UP.search(run_up)
    if not m:
        return None
    named = _trim_party_lead_in(m.group(1).strip())
    name_words, title_words = _words(named), _words(title)
    # Corroboration is tested against the WHOLE run-up, not just the abutting name.
    # A joined appeal puts two names before one citation — "Ittihadieh v 5-11 Cheyne
    # Gardens RTM Company Limited / Deer v University of Oxford [2017] EWCA Civ 121" —
    # and only the second abuts it, so testing that one alone called a correct edge a
    # contradiction. String citations behave the same way. Any mention of the target in
    # the run-up vindicates the edge; only the abutting name is quoted back.
    #
    # Both sides must carry a distinctive word, or "R v Secretary of State" against
    # "Regina v SSHD" reduces to two empty sets and reads as a contradiction.
    if not name_words or not title_words or (_words(run_up) & title_words):
        return None
    return {"named_beside_citation": named, "resolved_to": title,
            "why": ("the citing document names a different case beside this citation — "
                    "a bare neutral citation matches on the number alone, so a "
                    "mis-typed year or division lands on a real but unrelated judgment")}


#: VERBATIM phrases a judgment uses when it says how it is treating an authority, with
#: the direction each points. Deliberately NOT a treatment classifier: no phrase here is
#: read as a holding, the matched words are quoted back so the reader judges them, and a
#: cue found in a passage may belong to a different authority named in the same sentence.
#: What it buys is the thing that otherwise costs opening every citer — knowing which
#: citing document is the one that declined to follow.
_TREATMENT_CUE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), signal) for pattern, signal in (
        (r"\bdeclin\w+ to follow\b", "negative"),
        (r"\brefus\w+ to follow\b", "negative"),
        (r"\b(?:wrongly|incorrectly) decided\b", "negative"),
        (r"\bplainly wrong\b", "negative"),
        (r"\bper incuriam\b", "negative"),
        (r"\bno longer good law\b", "negative"),
        (r"\bnot binding on (?:this|the) (?:court|tribunal)\b", "negative"),
        (r"\boverrul\w+\b", "negative"),
        (r"\bdisapprov\w+\b", "negative"),
        (r"\bdepart\w* from\b", "negative"),
        (r"\bcannot stand\b", "negative"),
        (r"\bdoubt(?:ed|s|ful)\b", "doubted"),
        (r"\bdistinguish\w+\b", "distinguished"),
        (r"\bbound by\b", "positive"),
        (r"\bbinding on (?:this|the) (?:court|tribunal)\b", "positive"),
        (r"\b(?:approv\w+|endors\w+)\b", "positive"),
        (r"\bcorrectly (?:stated|decided|held)\b", "positive"),
        # Anchored to a VERB, not the bare word. "following" and "applied" occur
        # constantly as ordinary prose — "the cases of X following Y", "applied for
        # permission" — and a cue that fires on those is worse than no cue: it fills the
        # signals roll-up with a majority verdict nobody checked.
        (r"\b(?:I|we|the court|the tribunal|his lordship|her ladyship)\s+"
         r"(?:respectfully\s+)?(?:follow|adopt|appl(?:y|ied))\b", "positive"),
        (r"\b(?:is|am|are)\s+(?:therefore\s+)?bound to follow\b", "positive"),
        (r"\bfollowing (?:the (?:decision|reasoning|approach) (?:in|of)|that approach)\b",
         "positive"),
        # German treatment and interpretive-method vocabulary.  These remain verbatim
        # cues, not holdings: a reader sees the phrase and decides what it means in its
        # sentence, exactly as for the English patterns above.
        (r"\b(?:entgegen der Auffassung|abweichend von|nicht zu folgen|"
         r"Aufgabe der bisherigen Rechtsprechung)\b", "negative"),
        (r"\b(?:offengelassen|dahinstehen)\b", "uncertain"),
        (r"\b(?:in Fortführung|Anschluss an)\b", "positive"),
        (r"\bVorlage an den EuGH\b", "reference"),
        (r"\b(?:nach der Gesetzesbegründung|der Gesetzgeber wollte|"
         r"dem Willen des Gesetzgebers|ausweislich der Begründung|Regelungsabsicht)\b",
         "legislative-intent"),
        (r"\b(?:verfassungskonforme Auslegung|unionsrechtskonforme Auslegung|"
         r"teleologische Reduktion|analoge Anwendung|Wortlaut|Systematik)\b",
         "interpretive-method"),
        # SUBSEQUENT HISTORY. There is no appellate edge in the graph — no source the
        # corpus harvests publishes one — but where the appeal IS held, the appellate
        # judgment says so in the passage where it cites the decision below. That is the
        # only evidence available for "has this been appealed", and it is better than the
        # nothing the citator returned before.
        (r"\bon appeal from\b", "appeal"),
        (r"\bappeal (?:against|from) (?:the )?(?:decision|judgment|order|ruling)\b",
         "appeal"),
        (r"\bpermission to appeal (?:was |had been )?(?:granted|refused)\b", "appeal"),
        (r"\b(?:revers\w+|set aside)\b", "reversed"),
        (r"\b(?:affirm\w+|uph(?:eld|olding))\b", "affirmed"),
    ))

#: The cue signals that speak to what happened to a decision on appeal, rather than to
#: how a later court treated it as authority. Reported separately because they answer a
#: different question — "is this still the last word?" against "how was it received?".
_HISTORY_SIGNALS = frozenset({"appeal", "reversed", "affirmed"})


def _treatment_cues(text: str) -> list[dict]:
    """The treatment cues visible in one citing passage, quoted verbatim."""
    found: list[dict] = []
    seen: set[str] = set()
    for pattern, signal in _TREATMENT_CUE_PATTERNS:
        m = pattern.search(text or "")
        if not m:
            continue
        phrase = m.group(0)
        if phrase.lower() in seen:
            continue
        seen.add(phrase.lower())
        found.append({"phrase": phrase, "signal": signal})
    return found[:4]


def _label_help(segs) -> dict:
    """What a caller who missed needs in order to hit: the document's own labelling
    convention, not its opening forty labels.

    A long judgment starts with structural headers ("Introduction", "The facts"), so a
    head-of-document sample answers "what do the labels look like?" with the one part
    of the document that does not use the paragraph convention — and the convention is
    exactly what varies between judgments of the same court ("para 27" against "161.").
    So: name the numbered run explicitly, and spread the sample over the whole thing."""
    import re as _re

    labels = [(s.label or "").strip() for s in segs]
    labels = [x for x in labels if x]
    numbered = [x for x in labels if _re.fullmatch(r"\[?\d+[.\]]?", x)]
    # evenly spread rather than the first N, so the sample describes the document
    step = max(1, len(labels) // 40)
    sample = labels[::step][:40]
    out: dict = {"labels_sample": sample, "label_count": len(labels)}
    if numbered:
        out["paragraph_labels"] = {
            "convention": numbered[0],
            "first": numbered[0], "last": numbered[-1], "count": len(numbered),
        }
        out["hint"] = (
            f"this document numbers its paragraphs {numbered[0]!r} … {numbered[-1]!r}. "
            "A BARE INTEGER resolves against whatever convention a document uses — "
            "label='161' finds '161.', '[161]' or 'para 161' alike.")
    return out


def _doc_type(value: str | None, default: DocType) -> DocType:
    if not value:
        return default
    try:
        return DocType(value)
    except ValueError:
        return default


def _import_jurisdiction_labels() -> tuple[tuple[str, str], ...]:
    from .imports.service import JURISDICTIONS

    return JURISDICTIONS


_IMPORT_JURISDICTION_LABELS = _import_jurisdiction_labels()


def _as_date(value: str | None) -> date | None:
    """An ISO date typed into a form field, or nothing. A half-typed date must not fail
    the import it was optional to."""
    text = (value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _sniff_format(raw: bytes) -> str | None:
    """Infer the structural format of stored raw bytes (for re-parsing) — a zip or
    Formex ``<ACT>`` → Formex; Akoma Ntoso; a BWB ``<toestand>`` → BWB."""
    head = raw[:4096]
    if raw[:2] == b"PK":
        return "formex-legislation"  # CELLAR Formex zip
    low = head.lower()
    if b"akomantoso" in low:
        return "akoma-ntoso"
    # CLML (legislation.gov.uk data.xml). Its root is <Legislation> in the
    # …/namespaces/legislation NS — checked after AKN (whose files also carry that NS
    # as xmlns:ukl). Needed for assimilated EU regs, whose AKN body is empty while the
    # CLML carries the articles (see formats.clml_xml).
    if b"<legislation" in low and b"namespaces/legislation" in low:
        return "clml"
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


@lru_cache(maxsize=1)
def _registered_jurisdictions() -> dict[str, str]:
    """source key → the natural-language jurisdiction its ``SourceInfo`` declares.

    Imported lazily and cached: the registry imports every adapter module, and the facade
    is imported by some of them. Rebuilt only if the process restarts, which is the same
    lifetime the registry itself has.
    """
    from .adapters.registry import JURISDICTION_LABELS, SOURCE_INFO

    out: dict[str, str] = {}
    for key, info in SOURCE_INFO.items():
        label = JURISDICTION_LABELS.get(info.jurisdiction or "")
        if label and label != "Other":
            out[key.lower()] = label
    return out


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
# The worklist decides what to offer the drain from the same set (a citation whose
# source has no id-fetch is reported, but never queued) — keep the two in step.
assert set(_TARGETED_HARVEST) == set(_SNOWBALL_TARGETED), (
    "TARGETED_ADAPTERS and _TARGETED_HARVEST have drifted: "
    f"{set(_TARGETED_HARVEST) ^ set(_SNOWBALL_TARGETED)}")


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


_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def _roman_to_int(numeral: str) -> int:
    total = 0
    values = [_ROMAN_VALUES[ch] for ch in numeral.lower()]
    for i, value in enumerate(values):
        total += -value if any(v > value for v in values[i + 1:]) else value
    return total


def _int_to_roman(value: int) -> str:
    out = ""
    for amount, numeral in ((100, "c"), (90, "xc"), (50, "l"), (40, "xl"), (10, "x"),
                            (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while value >= amount:
            out += numeral
            value -= amount
    return out


def _anchor_key(text: str | None) -> str | None:
    t = (text or "").strip().lower().lstrip("[(")
    # An annex is numbered in ROMAN — "Annex I", "Annexe II" — which the arabic matcher
    # below cannot see at all, so every annex anchor and every annex segment label folded
    # to no key: the citations existed but could never join the annex they were about.
    # The tail is dropped, so "Annex I, point 29" keys to the annex family exactly as
    # "Article 28(3)" keys to art:28, and the exact anchor keeps the point.
    annex = re.match(r"^annexe?\.?\s+([ivxlc]+)(?![a-z0-9])", t)
    if annex:
        # Folded to ARABIC, because the two spellings occur for the same annex: Wind Tre
        # writes "Annex I, point 29" twelve times and "Annex 1, point 29" once, and the
        # UCPD's own segment label is "ANNEX I". Keying them apart would scatter a
        # provision's citers across two keys for a typographic difference.
        return f"annex:{_roman_to_int(annex.group(1))}"
    # The number may be MULTI-LEVEL: a code of practice is cited by "paragraph 3.19",
    # a rule of court by "r 3.1". Stopping at the first dot folded 3.19 and 3.2 onto the
    # same key as 3 — every paragraph of a chapter answering to its chapter number. The
    # dot only counts when a digit follows it, so a trailing "s. 7." is unaffected.
    m = re.match(r"^([a-z]+)?\.?\s*(\d+(?:\.\d+)*[a-z]?)", t)
    if not m or not m.group(2):
        return None
    typ = _ANCHOR_TYPES.get(m.group(1) or "", "")
    return f"{typ}:{m.group(2)}" if typ else m.group(2)


#: Reading order of an instrument's own units, for listing the provisions a document
#: cites: recitals precede the enacting terms, annexes follow them. Anything unrecognised
#: sorts last, alphabetically, rather than being silently dropped.
_PROVISION_ORDER = {"rec": 0, "art": 1, "annex": 3}


def _provision_sort_key(anchor: str) -> tuple:
    """Sort pinpoints the way the instrument reads — Recital 42, Article 5(1),
    Article 22, Annex I — rather than by the string, which puts Article 22 before
    Article 5 and files every recital after the articles."""
    key = _anchor_key(anchor) or ""
    unit, _, number = key.partition(":")
    if not number:
        unit, number = "", unit
    parts = tuple(int(p) if p.isdigit() else 0
                  for p in re.findall(r"\d+", number)) or (0,)
    return (_PROVISION_ORDER.get(unit, 2), parts, anchor.lower())


#: A paragraph pinpoint that SPANS a range — "para 135-140", "[135]-[140]", "paras 16
#: to 18". 369,230 of the corpus's 2,395,763 paragraph pinpoints are written this way
#: (15%), because a court citing a passage cites the passage, not its first line.
_PARA_RANGE = re.compile(
    r"^\s*(?:paras?\.?|paragraphs?)?\s*\[?(\d+)\]?\s*(?:[-–—]+|\bto\b)\s*\[?(\d+)\]?\s*$",
    re.IGNORECASE)
_PARA_SINGLE = re.compile(
    r"^\s*(?:paras?\.?|paragraphs?)?\s*\[?(\d+)\]?[.\]]?\s*$", re.IGNORECASE)
#: Beyond this a "range" is a parse artefact, not a citation; it names its first
#: paragraph and nothing else rather than swallowing a whole judgment.
_PARA_SPAN_MAX = 200


def _paragraph_span(anchor: str | None) -> tuple[int, int] | None:
    """(first, last) paragraph an anchor covers, or None if it is not a paragraph.

    A single paragraph is a span of one, so containment is the only test either side
    needs. Multi-level numbering ("para 3.19") deliberately returns None — it is a
    paragraph of a code of practice, not a judgment paragraph, and 3.19 is not a
    number to compare with < ."""
    text = (anchor or "").strip()
    if not text:
        return None
    m = _PARA_RANGE.match(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi <= lo + _PARA_SPAN_MAX:
            return lo, hi
        return lo, lo
    m = _PARA_SINGLE.match(text)
    return (int(m.group(1)), int(m.group(1))) if m else None


#: Above this width a range is guarded by "every paragraph pinpoint" rather than by
#: enumerating each paragraph — the enumeration is the tighter guard, but a hundred
#: LIKE branches is not.
_PARA_ENUMERATE_MAX = 40


def _paragraph_anchor_like(span: tuple[int, int]) -> list[str]:
    """Whole LIKE patterns admitting every pinpoint that could overlap ``span``.

    Two shapes have to survive the SQL guard and neither is reachable by a prefix of
    the request: a RANGE that contains the paragraph asked for ("para 135-140" for
    [138]), and — when a range is what was asked for — each single paragraph inside
    it. Coarse on purpose; ``_paragraph_spans_overlap`` is what decides."""
    lo, hi = span
    if hi - lo >= _PARA_ENUMERATE_MAX:
        return ["para%"]
    # EVERY dash a court uses. LIKE is literal, so a pattern built with the ASCII
    # hyphen alone silently misses "para 80–81" — and the corpus writes both, in the
    # same document. That mistake is this whole class of bug in miniature.
    out = [f"para%{dash}%" for dash in ("-", "–", "—")]
    for n in range(lo, hi + 1):
        out += [f"para{n}%", f"{n}%"]       # "para 138" and the bare "[138]" form
    return out


def _paragraph_spans_overlap(want: tuple[int, int], stored: str | None) -> bool:
    """Does a stored pinpoint touch the paragraphs asked for?

    Overlap, not equality: "who cites [138]" must find the court that wrote
    "[135]-[140]", and "who cites [135]-[140]" must find the one that wrote "[138]".
    Without this the paragraph-level citer count is a floor, not a count — and it
    reads as a count."""
    span = _paragraph_span(stored)
    return span is not None and span[0] <= want[1] and want[0] <= span[1]


def _anchor_key_variants(key: str | None) -> set[str]:
    """The keys one pinpoint can legitimately be stored under.

    A judgment paragraph is the citable unit of case law — practitioners cite Ittihadieh
    at [110], not Ittihadieh — and it gets written both with its unit word and without.
    The citing document's extracted pinpoint is "para 110" (key ``para:110``) while the
    reader types "[110]" or "110" (key ``110``, no unit at all), so the two never met and
    every judgment pinpoint answered "nothing cites that". Folding them is safe in the
    direction that matters: a bare number carries no unit, so it cannot collide with
    ``art:110`` or ``s:110``, which keep their own keys."""
    if not key:
        return set()
    typ, sep, num = key.partition(":")
    if not sep:                       # a bare number — "[110]", "110"
        return {key, f"para:{key}"}
    if typ == "para":
        return {key, num}
    return {key}


# Every way each unit gets written, keyed by the canonical type ``_anchor_key`` folds to.
# The database guard needs the spellings, not the fold: the corpus stores whichever form
# the citing document used ("s. 13"), and the caller types whichever they know
# ("section 13") — a guard built from one spelling silently drops the other.
_ANCHOR_SPELLINGS: dict[str, tuple[str, ...]] = {}
for _spelling, _canonical in _ANCHOR_TYPES.items():
    _ANCHOR_SPELLINGS.setdefault(_canonical, ())
    _ANCHOR_SPELLINGS[_canonical] += (_spelling,)


def _anchor_sql_prefixes(anchor: str | None) -> list[str]:
    """Normalised prefixes for the coarse database-side anchor guard.

    "s. 13", "section 13" and "§ 13" all yield ``["section13", "sec13", "s13"]``, which
    match the stored pinpoint whichever way it was punctuated. Returning *nothing* means
    "don't narrow in SQL" — never "match nothing" — so an anchor shape this doesn't
    understand degrades to the (correct, slower) unfiltered path.
    """
    key = _anchor_key(anchor)
    if not key:
        return []
    typ, _, number = key.partition(":")
    # The database guard compares against a normalisation that removes dots, so the
    # prefix must too, or "para 3.19" would be looked up as "para3.19" against a stored
    # "para319" and match nothing. Coarseness is fine here — "para 31.9" normalises the
    # same way, and the exact matcher behind the guard rejects it.
    number = number.replace(".", "")
    if not number:
        # A bare numeral — a judgment paragraph pinpoint ("[110]"). It has no unit to
        # spell, but the corpus stores what the CITING document wrote, and for a
        # paragraph that is "para 110". Guarding on the bare number alone matched
        # nothing, which is why pinpointing a judgment returned an empty citer list.
        bare = typ.replace(".", "")
        return [bare, *(f"{sp}{bare}" for sp in _ANCHOR_SPELLINGS.get("para", ()))]
    numbers = [number]
    if typ == "para":
        # …and the mirror image: an anchor written "para 110" against an edge stored as
        # the bare "[110]".
        return [f"{spelling}{number}" for spelling in _ANCHOR_SPELLINGS.get(typ, (typ,))
                ] + [number]
    if typ == "annex" and number.isdigit():
        # The KEY is arabic (see _anchor_key) but the corpus stores what the citing
        # document wrote, and for an annex that is nearly always roman. Guarding on the
        # arabic spelling alone would match nothing at all — the silent-zero failure this
        # whole path exists to avoid — so guard on both.
        numbers.append(_int_to_roman(int(number)))
    return [f"{spelling}{n}" for spelling in _ANCHOR_SPELLINGS.get(typ, (typ,))
            for n in numbers]


_SUBDIVISION_RE = re.compile(r"\(([^()]{1,8})\)")


def _subdivision_note(label: str | None, segment, text: str) -> str | None:
    """Was a requested SUBDIVISION actually found, or only its parent provision?

    Anchor keys deliberately fold to unit+number — "s. 7(1)(c)", "s. 7(99)" and "s. 7" all
    key as ``s:7`` — because that is what makes a pinpoint match a segment whose label
    carries a title. The cost is that a subsection which does not exist resolves to its
    parent and is reported as a hit: ``s. 45(99)`` and ``s. 7(99)`` both came back
    ``resolved: true``, so a bogus pinpoint was indistinguishable from a real one.

    Sections are rarely segmented below the section, so returning the parent is the right
    ANSWER — it just must not be presented as an exact match. Say so when the subdivision
    appears neither in the segment's label nor anywhere in its text.
    """
    wanted = _SUBDIVISION_RE.findall(label or "")
    if not wanted or segment is None:
        return None
    body = (segment.label or "") + " " + text[segment.char_start:segment.char_end]
    missing = [w for w in wanted if f"({w})" not in body]
    if not missing:
        return None
    return (f"{label} — the segment returned is {segment.label or 'this provision'}; "
            f"({'), ('.join(missing)}) was not found within it, so this is the parent "
            "provision rather than an exact match for the subdivision.")


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


# Words that stay lower-case inside a French/Dutch court name when a SHOUTED one is cased
# for display, and the particles that keep their own capital.
_COURT_LOWER = {
    # connectives + articles
    "de", "d'", "du", "des", "la", "le", "les", "et", "en", "van", "der", "voor",
    "bij", "het", "op", "à", "au", "aux",
    # the generic vocabulary of a court's name — everything left over is a PLACE, which
    # is the only part that takes a capital ("Cour administrative d'appel de Lyon")
    "cour", "tribunal", "conseil", "chambre", "administrative", "administratif",
    "appel", "cassation", "instance", "grande", "commerciale", "sociale", "civile",
    "criminelle", "correctionnelle", "judiciaire", "prud'hommes", "assises",
    "rechtbank", "gerechtshof", "raad", "beroep", "college", "bedrijfsleven",
}


def _sentence_case_court(name: str) -> str:
    """"COUR ADMINISTRATIVE D'APPEL DE LYON" → "Cour administrative d'appel de Lyon".

    Only the first word and proper nouns take a capital; the register's own mixed-case
    spellings of the same courts are the model. A place name is anything that isn't a
    connective, which over-capitalises nothing in practice because the connectives are
    exactly the words that repeat."""
    words = name.split()
    out: list[str] = []
    for i, w in enumerate(words):
        low = w.lower()
        if i and (low in _COURT_LOWER or low.rstrip("'") in _COURT_LOWER):
            out.append(low)
        elif "'" in low and len(low.split("'", 1)[0]) <= 2:      # d'APPEL → d'appel
            head, tail = low.split("'", 1)
            keep = tail in _COURT_LOWER or not i
            out.append(f"{head}'{tail if keep and i else tail.capitalize()}")
        else:
            out.append(low.capitalize() if i else low.capitalize())
    return " ".join(out)


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
        # Per-document view cache (the citator panel: cited-by counts + PageRank-ranked
        # incoming edges). Assembling it for a mega-authority (Data Protection Act 2018 has
        # ~10k resolved citers) touches tens of thousands of buffer pages, which on a
        # RAM-starved box is ~1s warm and tens of seconds when a background job is evicting
        # the cache. Reads dominate writes and a citer count seconds-stale is harmless, so
        # cache the assembled view: instant re-opens, and only the first open per document
        # pays the cost. Cleared wholesale on any local graph mutation (see
        # _invalidate_caches); a short TTL bounds staleness from harvests in the OTHER
        # (scheduler) process, which can't reach this process's cache. Bounded LRU so the
        # cache can't itself grow into the memory pressure it exists to relieve.
        import threading as _threading
        self._doc_cache: dict[str, tuple[float, dict]] = {}
        self._doc_cache_lock = _threading.Lock()

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

    # Aggregates dropped the instant the citation graph changes (harvest/resolve/edit), so
    # their "remaining" counts don't serve a pre-op snapshot. Explore's per-slice drill lists
    # and the shape table are DELIBERATELY excluded: they're expensive to recompute (a cold
    # slice is a ~16s scan) and only warmed at startup, so wiping them on every op left the
    # homepage recomputing a slice on each click after any background job. Their 1h TTL +
    # stale-while-revalidate keeps them fresh enough (top-authority lists barely move per
    # harvest), and they refresh in the background on next access rather than blocking a click.
    _VOLATILE_CACHE_PREFIXES = ("coverage", "stats", "corpus_map", "queues", "worklist",
                                "snowball", "unfetchable", "unresolved")

    def _invalidate_caches(self) -> None:
        """Drop the cached dashboard aggregates after an op that changes the citation
        graph (harvest/resolve), so the worklist's per-source "remaining" counts and
        coverage refresh instead of serving the pre-harvest snapshot."""
        for key in [k for k in self._cache
                    if k.startswith(self._VOLATILE_CACHE_PREFIXES)]:
            self._cache.pop(key, None)
            self._refreshing.discard(key)
        # A graph change (new citers, re-resolution, an edit) invalidates every cached
        # document view — the cited-by counts and incoming-edge lists it holds may all have
        # moved. Cheap to refill on next access; correctness beats keeping a stale panel.
        with self._doc_cache_lock:
            self._doc_cache.clear()

    def warm_caches(self) -> None:
        """Pre-compute the heavy dashboard aggregates in the background (called on app
        startup) so the first page load after a restart is instant, not a cold scan."""
        import threading
        import time as _t

        def _warm():
            # NOTE: unresolved is deliberately NOT warmed here — at limit=5000 it runs ~5000
            # citing sub-queries, and forcing that on every startup stampedes an IO-bound DB.
            # It warms on first access (non-blocking placeholder) and in the nightly refresh.
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
                # Warm each jurisdiction's default drill slices — every kind toggle
                # (all / cases / legislation / guidance / admin decisions) AND both of the
                # sorts the explore panel lands on first (most authoritative, most cited) — so
                # switching kind OR sort is instant, not a cold 16s scan. Sequential, so a
                # restart doesn't stampede the pool; each also warms PG's buffers.
                for row in self._cache["corpus-shape"][1].get("jurisdictions", []):
                    for kind in (None, "cases", "legislation", "guidance", "administrative"):
                        for sort in ("authority", "cited"):
                            key = self._drill_key(row["jurisdiction"], None, kind, None, sort, 25)
                            if key not in self._cache:
                                self._cache[key] = (_t.time(), self._drill_uncached(
                                    row["jurisdiction"], kind=kind, sort=sort))
            except Exception:  # noqa: BLE001 — warming is best-effort
                pass
        threading.Thread(target=_warm, daemon=True).start()

    def start_daily_refresh(self, *, hour_uk: int = 1) -> None:
        """Once a day at ~01:00 UK time (low traffic), fully recompute + re-warm every heavy
        dashboard aggregate — the Explore per-jurisdiction slices, corpus shape, stats,
        coverage AND the unresolved queue — so the first visitor of the day gets fresh,
        already-warm caches instead of triggering a cold/stale recompute. Runs in the API
        process (where the in-memory cache lives), on a daemon thread, independent of the
        pausable job scheduler."""
        import threading
        import time as _t
        from datetime import datetime, timedelta

        def _sleep_secs() -> float:
            try:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo("Europe/London"))
            except Exception:  # noqa: BLE001 — tz db missing → fall back to server local
                now = datetime.now()
            target = now.replace(hour=hour_uk, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return max(60.0, (target - now).total_seconds())

        def _loop():
            while True:
                _t.sleep(_sleep_secs())
                try:
                    # drop EVERYTHING (incl. the non-volatile drill/shape) then re-warm from
                    # scratch, so the day starts on freshly-computed figures. This is the one
                    # place the heavy unresolved queue is pre-warmed (1am is quiet enough to
                    # absorb its ~5000 citing sub-queries).
                    self._cache.clear()
                    self.warm_caches()
                    try:
                        self.unresolved_references_cached()
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    log.warning("daily cache refresh failed", exc_info=True)

        threading.Thread(target=_loop, daemon=True).start()

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

    # legislative-change relationship types, split by what they say about an act's currency.
    _LEG_INCOMING_REPEAL = ("repeals", "recasts")          # src did this TO this act → repealed
    _LEG_OUTGOING_REPEAL = ("repealed_by",)                # this act repealed_by src
    _LEG_INCOMING_AMEND = ("amends",)
    _LEG_OUTGOING_AMEND = ("amended_by",)
    _LEG_INCOMING_CORRECT = ("corrects",)
    _LEG_OUTGOING_CORRECT = ("corrected_by",)
    # Carried in the status payload as a weaker "also affected by" signal, never as a
    # repeal: CELLAR's implicitly_repeals marks an act superseding a REFERENCE to another.
    _LEG_INCOMING_IMPLICIT = ("implicitly_repeals",)
    _LEG_OUTGOING_IMPLICIT = ("implicitly_repealed_by",)
    _LEG_CHANGE_TYPES = tuple({
        "repeals", "recasts", "repealed_by", "amends", "amended_by", "corrects",
        "corrected_by", "consolidates", "legal_basis", "supersedes", "point_in_time_of",
        "implicitly_repeals", "implicitly_repealed_by",
    })

    def enrich_eu_legislation(self, *, limit: int = 100000, workers: int = 8,
                              on_progress=None, cancel_check=None) -> dict:
        """Harvest each held EU act's act-to-act CDM relationships from CELLAR (repeals /
        amends / corrects / legal-basis, both directions) and store them — so an old
        directive learns it was repealed/recast, and the legislative-status banner + MCP
        lights up. Resumable (skips acts already carrying a change-edge), so a re-run picks up
        wherever an interrupted run left off; dangling edges to unheld acts feed the worklist.

        The work is per-act CELLAR SPARQL — network-bound — so the lookups run across a small
        thread pool (``workers``); the DB writes are serialised in the main thread. ``limit``
        defaults high enough to drain the whole backlog in one run. Needs network to CELLAR."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .adapters.eu_cellar import harvest_act_relations
        change_types = ("repeals", "amends", "corrects", "consolidates", "legal_basis",
                        "repealed_by", "amended_by", "corrected_by")
        qs = ",".join("?" * len(change_types))
        with self._open() as (cat, _rs, _ts):
            # EU legislation is held under source 'eu-legislation' (the CELEX is the
            # stable_id); 'eu-cellar' is the case-law surface. The old filter named only
            # 'eu-cellar' AND doc_type='legislation' — a combination that matches ZERO rows,
            # so every run was a silent no-op (scanned 0). Cover both sources.
            rows = cat.conn.execute(
                f"SELECT stable_id FROM documents d "
                f"WHERE d.source IN ('eu-legislation', 'eu-cellar') "
                f"AND d.doc_type = 'legislation' AND NOT EXISTS ("
                f"  SELECT 1 FROM relations r WHERE r.src_id = d.stable_id "
                f"  AND r.relationship_type IN ({qs})) LIMIT ?",
                (*change_types, limit)).fetchall()
        ids = [r["stable_id"] for r in rows]
        total = len(ids)
        enriched = edges = done = 0
        # one connection held for the write side; the harvest (network) is what's parallel
        with self._open() as (cat, _rs, _ts):
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futures = {ex.submit(harvest_act_relations, sid): sid for sid in ids}
                for fut in as_completed(futures):
                    if cancel_check and cancel_check():
                        for f in futures:
                            f.cancel()
                        break
                    sid = futures[fut]
                    done += 1
                    _progress(on_progress, stage="enriching EU legislation",
                              done=done, total=total, item=sid)
                    try:
                        rels = fut.result()
                    except Exception:  # noqa: BLE001 — one act's CELLAR failure must not stop the sweep
                        rels = None
                    if rels:
                        cat.add_relations(sid, rels)  # stable_id is the CELEX for EU legislation
                        enriched += 1
                        edges += len(rels)
        self._invalidate_caches()
        return {"scanned": total, "enriched": enriched, "edges": edges}

    def repair_eu_implicit_repeals(self, *, limit: int = 100000, workers: int = 8,
                                   dry_run: bool = False,
                                   on_progress=None, cancel_check=None) -> dict:
        """Re-type the EU repeal edges that were never repeals.

        CELLAR exposes two predicates, and we mapped both to ``repeals``. The second,
        ``implicitly_repeals``, marks an act that supersedes a REFERENCE to another — five
        acts "implicitly repeal" the Unfair Commercial Practices Directive, which is in
        force and was amended in 2024. 14,294 held EU acts currently read as repealed on
        the strength of it.

        The two are indistinguishable once stored, so this re-asks CELLAR per act and
        replaces that act's structured repeal edges with the freshly-typed ones. An act
        whose CELLAR lookup fails or returns nothing is left exactly as it was — a network
        blip must never delete a repeal."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from .adapters.eu_cellar import harvest_act_relations

        repeal_types = ("repeals", "repealed_by", "implicitly_repeals",
                        "implicitly_repealed_by")
        qs = ",".join("?" * len(repeal_types))
        with self._open() as (cat, _rs, _ts):
            rows = cat.conn.execute(
                f"SELECT DISTINCT src_id FROM relations WHERE relationship_type IN ({qs}) "
                f"AND extracted_via = 'structured' ORDER BY src_id LIMIT ?",
                (*repeal_types, limit)).fetchall()
        ids = [r["src_id"] for r in rows]
        st = {"scanned": len(ids), "rechecked": 0, "edges_replaced": 0,
              "acts_no_longer_repealed": 0, "unreachable": 0}
        if dry_run:
            return st
        done = 0
        with self._open() as (cat, _rs, _ts):
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futures = {ex.submit(harvest_act_relations, sid): sid for sid in ids}
                for fut in as_completed(futures):
                    if cancel_check and cancel_check():
                        for f in futures:
                            f.cancel()
                        break
                    sid = futures[fut]
                    done += 1
                    _progress(on_progress, stage="re-typing EU repeal edges",
                              done=done, total=len(ids), item=sid)
                    try:
                        rels = fut.result()
                    except Exception:  # noqa: BLE001
                        rels = None
                    if not rels:
                        st["unreachable"] += 1
                        continue
                    fresh = [r for r in rels
                             if str(r.relationship_type) in repeal_types]
                    had_repeal = cat.conn.execute(
                        "SELECT 1 FROM relations WHERE src_id = ? AND relationship_type "
                        "IN ('repeals','repealed_by') AND extracted_via = 'structured' LIMIT 1",
                        (sid,)).fetchone() is not None
                    with cat._atomic():
                        st["edges_replaced"] += cat.conn.execute(
                            f"DELETE FROM relations WHERE src_id = ? AND extracted_via = "
                            f"'structured' AND relationship_type IN ({qs})",
                            (sid, *repeal_types)).rowcount
                    if fresh:
                        cat.add_relations(sid, fresh)
                    st["rechecked"] += 1
                    if had_repeal and not any(
                            str(r.relationship_type) in ("repeals", "repealed_by")
                            for r in fresh):
                        st["acts_no_longer_repealed"] += 1
        self._invalidate_caches()
        return st

    def legislative_status(self, stable_id: str) -> dict:
        """Currency of a piece of legislation, from its change-edges (source-agnostic — UK
        legislation.gov.uk amendments, CELLAR/CDM repeals/corrigenda, and consolidation
        links all feed it). A user browsing an old act sees at a glance whether it is in
        force, amended, repealed/recast, corrected, or a consolidated snapshot — with the
        acts that did it. Dangling (not-yet-held) edges still count, so "repealed by
        <CELEX>" shows before the repealing act is harvested."""
        from .eu_law import is_consolidation, consolidation_base, consolidation_date
        from .leg_currency import Currency, Provision, more_severe, status_meta, CanonStatus
        cons_base = consolidation_base(stable_id)
        is_cons = bool(is_consolidation(stable_id))
        pit_match = re.fullmatch(r"(.+)@(\d{4}-\d{2}-\d{2})", stable_id)
        pit_base = pit_match.group(1) if pit_match else None
        pit_date = pit_match.group(2) if pit_match else None
        lineage_base = cons_base or pit_base or stable_id
        qs = ",".join("?" * len(self._LEG_CHANGE_TYPES))
        with self._open() as (cat, _rs, _ts):
            doc = cat.get_document(stable_id)
            meta = _row_meta(doc) if doc is not None else {}
            source = (doc["source"] if doc is not None else None)
            # What the SERVED TEXT says about its own currency. This tool's answer is
            # otherwise derived entirely from edges, so an instrument with no recorded
            # amendments reads as unamended even while the body being served carries
            # inserted provisions — see the conflict check below.
            seg_labels: list[str] = []
            if doc is not None and doc["payload_hash"]:
                try:
                    seg_labels = [s.label for s in
                                  (_ts.get_segments(doc["payload_hash"]) or []) if s.label]
                except OSError:
                    seg_labels = []
            # A sector-0 identifier hides whether its base came from sector 1, 2,
            # 3 or 4. The adapter resolves and records the actual base from Cellar;
            # only legacy records need the sector-3 string fallback above.
            if is_cons and meta.get("consolidation_of"):
                cons_base = str(meta["consolidation_of"])
                lineage_base = cons_base
            held_versions = cat.legislative_versions(lineage_base)
            # editorial-lag backlog (UK unapplied effects), if this act is on the re-check queue
            eff = cat.conn.execute(
                "SELECT outstanding FROM effects_refresh WHERE stable_id = ?", (stable_id,)).fetchone()
            out = [dict(r) for r in cat.conn.execute(
                f"SELECT relationship_type, dst_id, candidate_id, raw_citation_string, dst_anchor "
                f"FROM relations WHERE src_id = ? AND relationship_type IN ({qs})",
                (stable_id, *self._LEG_CHANGE_TYPES)).fetchall()]
            inc = [dict(r) for r in cat.conn.execute(
                f"SELECT relationship_type, src_id, raw_citation_string, dst_anchor "
                f"FROM relations WHERE (dst_id = ? OR candidate_id = ?) "
                f"AND relationship_type IN ({qs})",
                (stable_id, stable_id, *self._LEG_CHANGE_TYPES)).fetchall()]

        def _tgt(r):  # a display id for an outgoing edge's target
            return r.get("dst_id") or r.get("candidate_id") or r.get("raw_citation_string")

        repealed_by = ([r["src_id"] for r in inc if r["relationship_type"] in self._LEG_INCOMING_REPEAL]
                       + [_tgt(r) for r in out if r["relationship_type"] in self._LEG_OUTGOING_REPEAL])
        amended_by = ([r["src_id"] for r in inc if r["relationship_type"] in self._LEG_INCOMING_AMEND]
                      + [_tgt(r) for r in out if r["relationship_type"] in self._LEG_OUTGOING_AMEND])
        corrected_by = ([r["src_id"] for r in inc if r["relationship_type"] in self._LEG_INCOMING_CORRECT]
                        + [_tgt(r) for r in out if r["relationship_type"] in self._LEG_OUTGOING_CORRECT])
        # consolidations that snapshot this act (a consolidation CONSOLIDATES its base)
        consolidations = [r["src_id"] for r in inc if r["relationship_type"] == "consolidates"]
        legal_basis = [_tgt(r) for r in out if r["relationship_type"] == "legal_basis"]
        repeals = [_tgt(r) for r in out if r["relationship_type"] in self._LEG_INCOMING_REPEAL]
        # Recorded, shown, and deliberately NOT part of the status calculation.
        implicitly_affected_by = (
            [r["src_id"] for r in inc if r["relationship_type"] in self._LEG_INCOMING_IMPLICIT]
            + [_tgt(r) for r in out if r["relationship_type"] in self._LEG_OUTGOING_IMPLICIT])

        edge_status = ("repealed" if repealed_by else
                       "amended" if amended_by else
                       "corrected" if corrected_by else None)
        # A consolidation's own incoming edge set does not contain its siblings. Build the
        # version message from every held lineage expression so the reader can tell whether
        # this snapshot is historical, future, or the latest one actually held by RagLex.
        consolidation_versions = [
            {"stable_id": sid, "as_at": version_date}
            for sid, version_date in held_versions if is_consolidation(sid)
        ]
        # ``is_consolidation`` is a CELEX test (sector 0 + date), so it answers "is this an
        # EU consolidation", not "is this a dated version". Held UK point-in-time copies
        # (``ukpga/2018/12@2020-01-01``, linked by point_in_time_of) were fetched into
        # ``held_versions`` and then dropped by that filter — so an act could show its
        # dated versions in the versions panel while the banner above it said none were
        # held. Keep them, under their own name; they are not consolidations.
        point_in_time_versions = [
            {"stable_id": sid, "as_at": version_date}
            for sid, version_date in held_versions if not is_consolidation(sid)
        ]
        consolidations = sorted(set(consolidations) | {
            row["stable_id"] for row in consolidation_versions
        })
        today = datetime.now(timezone.utc).date().isoformat()
        latest_held = consolidation_versions[-1] if consolidation_versions else None
        applicable_versions = [
            row for row in consolidation_versions if row["as_at"] <= today
        ]
        latest_applicable = applicable_versions[-1] if applicable_versions else None
        if pit_match:
            version_state = "point_in_time"
        elif is_cons and (consolidation_date(stable_id) or "") > today:
            version_state = "future_consolidation"
        elif is_cons and latest_applicable and \
                latest_applicable["stable_id"] == stable_id:
            version_state = "latest_applicable_consolidation"
        elif is_cons and latest_applicable:
            version_state = "historical_consolidation"
        elif is_cons:
            version_state = "unverified_consolidation"
        elif latest_applicable:
            version_state = "base_with_consolidation"
        elif _revised_in_place(source):
            # A source that REVISES ITS TEXT IN PLACE has no separate consolidation to
            # import: legislation.gov.uk's base URI *is* the consolidated text, kept
            # current by its editors and stamped with the date it is current as at. The
            # EU-shaped fallback below told every UK reader they were looking at an
            # "undated legislation record" for which "RagLex has not imported a dated
            # consolidation" — describing a gap that cannot exist in this model, over
            # text the publisher had dated precisely.
            version_state = "revised_in_place"
        else:
            version_state = "base_without_consolidation"
        # Native currency the adapter/format parser stowed (FR états, DE force, NL WTI, UK
        # status). Merge it with the edge-derived picture: the more-severe of the two wins, so a
        # source that says "repealed" is never hidden behind a mild edge signal, and vice-versa.
        native = (Currency.from_meta(meta) or Currency()).normalized()
        merged = more_severe(edge_status, native.status)
        if merged is None:
            merged = str(CanonStatus.CONSOLIDATED) if is_cons else str(CanonStatus.IN_FORCE)
        # a dated consolidation is a *manifestation* fact; only let it be the headline when no
        # force signal (repeal/amend/etc.) outranks it.
        if is_cons:
            merged = more_severe(merged, str(CanonStatus.CONSOLIDATED))
        meta_info = status_meta(merged)

        # per-article change markers: edges that carry a pinpoint (dst_anchor), grouped by
        # article → the change kinds touching it (UK provision-level effects; recast
        # correlation-table CORRESPONDS_TO). Reliable only where the source pinpointed it.
        by_article: dict[str, list[str]] = {}
        for r in inc + out:
            anchor = (r.get("dst_anchor") or "").strip()
            if anchor:
                by_article.setdefault(anchor, [])
                if r["relationship_type"] not in by_article[anchor]:
                    by_article[anchor].append(r["relationship_type"])
        # Unified provision view: native per-provision currency (FR états / DE / NL windows)
        # merged with the edge-derived change markers, keyed on the anchor. Native carries dated
        # in-force windows; edges carry which instruments touched it.
        prov: dict[str, dict] = {}
        for p in native.provisions:
            prov[p.anchor] = {**p.to_dict(), "anchor": p.anchor}
        for anchor, kinds in by_article.items():
            row = prov.setdefault(anchor, {"anchor": anchor})
            row["change_types"] = sorted(set(row.get("change_types", [])) | set(kinds))
        provisions = sorted(prov.values(), key=lambda r: r["anchor"])

        # up-to-date / editorial lag: a UK effects-refresh row means known-but-unapplied changes.
        unapplied = int(eff["outstanding"]) if eff else (native.unapplied_count or 0)
        up_to_date = (False if unapplied else native.up_to_date)
        # Low confidence when we're calling it "in force" purely from absence of edges + no
        # native confirmation — the banner uses this to hedge rather than over-claim currency.
        degraded = (native.status is None and edge_status is None and not is_cons)
        # The held text is the ENACTED text of an act we know has been amended, and no
        # consolidation exists to diff it against. ePrivacy read `unapplied_count: 0,
        # degraded: false` in exactly this state while its held Article 5(3) still said
        # "offered the right to refuse" — the pre-2009 opt-out, the opposite of the law.
        # Zero there meant "nothing to compare", and it read as reassurance.
        uncomparable = bool(
            version_state == "base_without_consolidation"
            and amended_by and not eff
        )
        if uncomparable:
            degraded = True
            unapplied = None
            up_to_date = False
        # The mirror image of `uncomparable`, and the more dangerous one: the edges
        # record NO amendment, so every field says "unamended, nothing outstanding",
        # while the body actually being served carries inserted provisions. The UK GDPR
        # held via uk-legislation reads exactly like this — degraded=true,
        # amended_by=[], unapplied_count=0 — over a body containing Articles 12A, 22A-22D
        # and 45A, all inserted by the Data (Use and Access) Act 2025. A reader asking
        # this tool "is this current?" was told the one thing it could not know.
        #
        # Never a status change: a letter-suffixed number is strong evidence, not a
        # recorded effect, and the honest output is a contradiction rather than a
        # guessed amendment list.
        inserted = _inserted_provisions(seg_labels)
        conflict = bool(inserted and not amended_by and not corrected_by
                        and not repealed_by and not is_cons)
        if conflict:
            degraded = True
            unapplied = None            # unknown — not zero
            up_to_date = False
        return {
            "stable_id": stable_id, "status": merged,
            "status_label": meta_info["label"], "status_icon": meta_info["icon"],
            "status_tone": meta_info["tone"], "source": source,
            "native_status": native.native_status, "scheme": native.scheme,
            "in_force_from": native.in_force_from, "in_force_to": native.in_force_to,
            "repealed_by": sorted(set(filter(None, repealed_by))),
            "amended_by": sorted(set(filter(None, amended_by))),
            "corrected_by": sorted(set(filter(None, corrected_by))),
            "consolidations": sorted(set(filter(None, consolidations))),
            "legal_basis": sorted(set(filter(None, legal_basis))),
            "repeals": sorted(set(filter(None, repeals))),
            # CELLAR's implicitly_repeals — an act superseding a REFERENCE to this one.
            # Reported so the fact isn't lost, but it never moves the status.
            "implicitly_affected_by": sorted(set(filter(None, implicitly_affected_by))),
            # this doc IS a dated consolidation snapshot → its base + as-at date
            "is_consolidation": is_cons,
            "consolidation_of": cons_base,
            "as_at": consolidation_date(stable_id) or native.as_at,
            "is_point_in_time": bool(pit_match),
            "point_in_time_of": pit_base,
            "point_in_time_date": pit_date,
            "version_state": version_state,
            "latest_held_consolidation": latest_held,
            "latest_applicable_consolidation": latest_applicable,
            "consolidation_versions": consolidation_versions,
            "point_in_time_versions": point_in_time_versions,
            "consolidations_checked_at": meta.get("consolidations_checked_at"),
            # Two distinct clocks: when the official rendition says it changed, and
            # when this exact RagLex copy was fetched. Showing both avoids presenting
            # an old fetch date as the law's currency date (or vice versa).
            "source_last_modified": meta.get("source_last_modified"),
            "raglex_fetched_at": doc["fetched_at"] if doc is not None else None,
            # A property of the SOURCE, not of this stored record. legislation.gov.uk
            # serves a dated version of everything it publishes, assimilated EU law
            # included; reading the flag off harvested metadata alone made it false for
            # every instrument stored before the adapter recorded it — the UK GDPR among
            # them, which is the most date-sensitive instrument in UK data protection.
            "point_in_time_capable": bool(native.point_in_time_capable
                                          or source == "uk-legislation"),
            "point_in_time_how": (
                "harvest_legislation_at(stable_id, date='YYYY-MM-DD') fetches the text "
                "as it stood, stored as {id}@{date}. Read it with lookup() or "
                "get_provision() on that dated id."
            ) if source == "uk-legislation" else None,
            "unapplied_count": unapplied, "up_to_date": up_to_date,
            "by_article": by_article, "provisions": provisions,
            "degraded": degraded,
            # Explicit, because `unapplied_count: null` alone doesn't say WHY.
            "amendments_uncomparable": uncomparable,
            # The served text contradicts the recorded currency. Named, listed and
            # dated (or explicitly undated) so a reader can see WHICH provisions say so.
            "text_contradicts_metadata": conflict,
            "inserted_provisions_in_text": inserted[:20] if inserted else [],
            "text_conflict_note": (
                f"The body served for this instrument contains {len(inserted)} "
                "letter-suffixed provision(s) — " + ", ".join(inserted[:6]) +
                ("…" if len(inserted) > 6 else "") + " — which drafting only produces "
                "by INSERTION into an existing text. So this text has been amended, and "
                "no amending instrument is recorded against it: amended_by is empty "
                "because nothing was recorded, not because nothing happened. "
                + ("The text is also undated (as_at is null), so which commencement "
                   "dates it reflects cannot be determined from the corpus. "
                   if not (consolidation_date(stable_id) or native.as_at) else "")
                + "Treat unapplied_count as unknown and verify against the source "
                  "before relying on any provision's currency."
            ) if conflict else None,
            "currency_note": (
                "The held text is the act as enacted. It has been amended and no "
                "consolidation is held, so there is nothing to compare it against: "
                "unapplied_count is unknown, not zero. Read the amending instruments "
                f"({', '.join(sorted(set(filter(None, amended_by)))[:5])}) before "
                "relying on any provision."
            ) if uncomparable else None,
        }

    def canonical_read_target(self, stable_id: str, *, original: bool = False) -> dict:
        """Resolve an ordinary legislation read to today's applicable consolidation.

        Explicit dated versions are stable. For legislation.gov.uk, ``original=True``
        means the publisher's *enacted* rendition — not its revised-in-place base, which
        can be a wholly-repealed wall of dots. The small provenance envelope lets
        web/MCP callers redirect without concealing what was requested.
        """
        fetch_enacted = False
        with self._open() as (cat, _rs, _ts):
            doc = cat.get_document(stable_id)
            if doc is None:
                return {"requested_stable_id": stable_id, "stable_id": stable_id,
                        "redirected": False}
            if original and doc["source"] == "uk-legislation" \
                    and doc["doc_type"] == "legislation" \
                    and not cat.consolidation_base_for(stable_id):
                enacted_id = f"{stable_id.split('@', 1)[0]}@enacted"
                enacted = cat.get_document(enacted_id)
                if enacted is not None and enacted["has_text"]:
                    return {"requested_stable_id": stable_id, "stable_id": enacted_id,
                            "as_enacted": True, "redirected": enacted_id != stable_id}
                fetch_enacted = True
            elif original or doc["doc_type"] != "legislation" \
                    or cat.consolidation_base_for(stable_id):
                return {"requested_stable_id": stable_id, "stable_id": stable_id,
                        "redirected": False}
            current = None if fetch_enacted else cat.applicable_consolidation(stable_id)
            # A consolidation that holds NO TEXT must never take over the read. 1,965 EU
            # consolidations are metadata stubs — a CELEX row minted from the linked data
            # with no body ever fetched — and redirecting to one replaced the instrument
            # with nothing: opening the AI Act showed only the recitals it inherits from
            # its base act, because the expression the reader had been sent to was empty.
            # The dated version stays reachable by asking for it; it just cannot silently
            # stand in for the act.
            if current and not (cat.get_document(current[0]) or {})["has_text"]:
                current = None
        if fetch_enacted:
            result = self.ensure_uk_legislation_original(stable_id=stable_id)
            enacted_id = result.get("stable_id")
            if enacted_id and not result.get("error"):
                return {"requested_stable_id": stable_id, "stable_id": enacted_id,
                        "as_enacted": True, "redirected": enacted_id != stable_id}
            return {"requested_stable_id": stable_id, "stable_id": stable_id,
                    "redirected": False, "original_unavailable": result.get("error")}
        return {
            "requested_stable_id": stable_id,
            "stable_id": current[0] if current else stable_id,
            "as_at": current[1] if current else None,
            "redirected": bool(current),
        }

    def get_document(self, stable_id: str) -> dict:
        """The reader/citator payload for one document. Cached (see _doc_cache): assembling
        a mega-authority's cited-by panel is expensive, and it's re-opened far more often
        than the graph changes under it."""
        import os as _os
        import time as _time
        try:
            ttl = max(0.0, float(_os.environ.get("RAGLEX_DOC_CACHE_TTL_S") or 120.0))
        except (TypeError, ValueError):
            ttl = 120.0
        if ttl > 0.0:
            now = _time.time()
            with self._doc_cache_lock:
                hit = self._doc_cache.get(stable_id)
                if hit is not None and now - hit[0] < ttl:
                    # LRU touch: move to newest so the bound evicts genuinely-cold entries.
                    self._doc_cache[stable_id] = self._doc_cache.pop(stable_id)
                    return {**hit[1], "_cached": True}
        view = self._get_document_uncached(stable_id)
        if ttl > 0.0 and "error" not in view:  # never pin a transient not-found
            with self._doc_cache_lock:
                self._doc_cache[stable_id] = (_time.time(), view)
                self._doc_cache[stable_id] = self._doc_cache.pop(stable_id)
                while len(self._doc_cache) > 256:
                    self._doc_cache.pop(next(iter(self._doc_cache)))
        return view

    def _get_document_uncached(self, stable_id: str) -> dict:
        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(stable_id)
            if doc is None:
                return {"error": "not found", "stable_id": stable_id}
            meta = cat.document_meta(stable_id)  # adapter extras (celex, origin_country, …)
            version_base = (
                cat.consolidation_base_for(stable_id)
                if doc["doc_type"] == "legislation" else None
            )
            canonical_version = (
                cat.applicable_consolidation(stable_id)
                if doc["doc_type"] == "legislation" and not version_base else None
            )
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
            ids_self = cat.document_identity_ids(stable_id)
            direct_edges = [dict(r) for r in cat.top_citing_edges(ids_self, limit=600)]
            version_edges: list[dict] = []
            for row in cat.version_inherited_mentions_for(stable_id, limit=5000):
                projected = dict(row)
                projected["version_inherited"] = True
                projected["dst_id"] = stable_id
                version_edges.append(projected)
            # A citer can name both the base CELEX and the dated expression in the same
            # passage. Prefer its literal direct-to-version edge and do not paint it twice.
            seen_edges = {
                (r["src_id"], r["dst_anchor"], r["context_start"], r["context_end"])
                for r in direct_edges
            }
            version_edges = [
                r for r in version_edges
                if (r["src_id"], r["dst_anchor"], r["context_start"], r["context_end"])
                not in seen_edges
            ]
            combined_edges = self._collapse_version_citers(
                cat, [*direct_edges, *version_edges])
            combined_edges.sort(
                key=lambda r: float(r.get("src_pagerank") or 0.0), reverse=True)
            incoming = self._assemble_cited_by(cat, combined_edges, cap=200)
            collapsed_version_edges = self._collapse_version_citers(cat, version_edges)
            version_inherited_incoming = self._assemble_cited_by(
                cat, collapsed_version_edges, cap=200)
            cited_by_total = cat.cited_by_stats(ids_self)["documents"]
            version_cited_by_total = cat.cited_by_family_count(
                [*ids_self, *([version_base] if version_base else [])])
            mapping_rows = [dict(r) for r in cat.provision_mappings(stable_id)]
            inherited_edges = [dict(r) for r in cat.inherited_mentions_for(
                stable_id, limit=1200)]
            inherited_incoming = self._assemble_cited_by(
                cat, inherited_edges, cap=400)
            direct_citer_ids = {row["src_id"] for row in incoming}
            inherited_citer_ids = {row["src_id"] for row in inherited_incoming}
            inherited_by_mapping: dict[int, set[str]] = {}
            for row in inherited_edges:
                inherited_by_mapping.setdefault(int(row["mapping_id"]), set()).add(
                    row["src_id"])
            for row in mapping_rows:
                row["mentioned_by_count"] = len(
                    inherited_by_mapping.get(int(row["mapping_id"]), set()))
            inferred_total = cat.inferred_citer_count(ids_self)
            preparatory_count = cat.citer_count_by_doc_type(ids_self, "preparatory")
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
            inherited_recitals = (
                self._inherited_recitals(
                    cat, ts, stable_id, include_citations=False)
                if version_base else None
            )
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
                "cited_by_count": version_cited_by_total + len(
                    inherited_citer_ids - direct_citer_ids),
                "direct_cited_by_count": cited_by_total,
                "version_cited_by_count": version_cited_by_total,
                "version_inherited_incoming": version_inherited_incoming,
                "version_inherited_cited_by_count": len({
                    row["src_id"] for row in collapsed_version_edges
                }),
                "inherited_incoming": inherited_incoming,
                "inherited_cited_by_count": len(inherited_citer_ids),
                "provision_mappings": mapping_rows,
                # Reader/MCP canonicalisation is explicit and reversible. A base act
                # opens at the latest consolidation applicable today; a dated snapshot
                # never redirects merely because a newer/future one exists.
                "canonical_read": (
                    {"stable_id": canonical_version[0], "as_at": canonical_version[1],
                     "requested_stable_id": stable_id}
                    if canonical_version else None
                ),
                "original_act": (
                    {"stable_id": version_base,
                     "title": (cat.get_document(version_base) or {})["title"]
                     if cat.get_document(version_base) else version_base}
                    if version_base else None
                ),
                "inherited_recitals": (
                    {
                        key: inherited_recitals[key]
                        for key in (
                            "count", "source_stable_id", "source_title",
                            "source_url", "base_stable_id", "source_is_base_act",
                            "unchanged", "virtual", "note",
                        )
                    }
                    if inherited_recitals else None
                ),
                "preparatory_documents": {
                    "available": bool(preparatory_count),
                    "count": preparatory_count,
                    "message": (f"Preparatory documents exist for this item — "
                                f"{preparatory_count} available."
                                if preparatory_count else None),
                    "retrieve_with": "document_mentions",
                },
                "inferred_by_count": max(0, inferred_total),
                # the other half of a CJEU case — judgment ↔ AG Opinion (see _cjeu_companion)
                "companion": self._cjeu_companion(cat, doc, meta),
                # UK assimilated text ↔ the EU original it was made from. Live-service
                # only: a static edition is a snapshot of ONE jurisdiction's law and
                # must not sprout links into the other (see _eu_uk_counterpart).
                "counterpart": self._eu_uk_counterpart(cat, doc),
                "assets": [dict(a) for a in cat.assets_for(stable_id)],
                "versions": [dict(v) for v in cat.list_versions(stable_id)],
            }

    # A CJEU case is published as two documents with the SAME case number, differing only
    # in the CELEX descriptor: the Court's judgment (…CJ…) and the Advocate General's
    # Opinion (…CC…). Reading one, you almost always want to know the other
    # exists — so pair them deterministically off the CELEX rather than relying on the
    # opinion_in edge, which only 368 of the 8,553 held opinions actually carry (most were
    # pulled by a path that never minted it).
    # …and the same pairing serves an ORDER (CO) and a still-pending application notice
    # (CN): while the case is pending, the Opinion is the only thing there is to read, so
    # a notice that has one must say so and link to it. The Opinion never retires the
    # notice — only a judgment or order does (Catalogue.retire_pending_eu_notice).
    _CELEX_CASE_RE = re.compile(r"^(6\d{4})(CJ|CC|CO|CN)(\d+)$", re.IGNORECASE)

    def _eu_uk_counterpart(self, cat, doc) -> dict | None:
        """The same instrument on the other side of the 2020 split: a UK assimilated text
        ↔ the EU original it was made from.

        The two texts are separate law and diverge with every amendment — which is
        exactly why a reader of one wants a route to the other. Someone reading the
        assimilated UK GDPR needs to check what the CJEU said about the article they are
        on (persuasive, not binding, since s.6 EUWA 2018), and someone reading Regulation
        2016/679 needs to see whether the UK still says the same words.

        Derived from the durable ``assimilated_version_of`` edge in both directions, so
        an unheld counterpart still yields a link to the official source rather than
        nothing. This is a LIVE-SERVICE affordance: ``static_export`` drops it, because a
        static edition of UK law cannot carry working links into an EU corpus it does
        not contain.
        """
        from .eu_law import consolidation_base
        from .resolve.matchers import assimilated_celex, assimilated_leg_path
        stable_id = doc["stable_id"]
        # UK assimilated text → the EU original (its own outgoing edge; the identifier
        # form is authoritative enough to derive when the edge predates this feature).
        celex = next((r["raw_citation_string"] for r in cat.relations_for(stable_id)
                      if r["relationship_type"] == "assimilated_version_of"
                      and r["raw_citation_string"]), None) or assimilated_celex(stable_id)
        if celex and doc["source"] != "eu-legislation":
            held = cat.find_document_id(celex)
            row = cat.get_document(held) if held else None
            return {"role": "eu_original", "celex": celex, "stable_id": held,
                    "title": row["title"] if row is not None else None,
                    "url": ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:"
                            + celex),
                    "note": "The EU original. Its CJEU case law is persuasive, not "
                            "binding, on the assimilated text (s. 6 EUWA 2018)."}
        # EU original → the UK assimilated text (an incoming edge from uk-legislation).
        # A reader is normally on a DATED consolidation (02016R0679-20160504), and the
        # assimilated edge points at the base act — so ask the base act's identity, or
        # the link only ever appeared on the undated record nobody reads.
        own_celex = str(_row_meta(doc).get("celex") or stable_id)
        own_celex = consolidation_base(own_celex) or own_celex
        if not re.fullmatch(r"[0-9]{5}[A-Z]{1,2}[0-9]{4}", own_celex.upper()):
            return None
        src_id = cat.relation_src_of_type(own_celex, "assimilated_version_of")
        uk = cat.get_document(src_id) if src_id else None
        if uk is not None:
            path = assimilated_leg_path(src_id) or src_id
            return {"role": "uk_assimilated", "stable_id": src_id,
                    "title": uk["title"],
                    "url": f"https://www.legislation.gov.uk/{path}",
                    "note": "The UK assimilated version of this instrument. It is "
                            "separate law and has been amended separately since 2020."}
        return None

    def _cjeu_companion(self, cat, doc, meta: dict) -> dict | None:
        """``{"role": "ag_opinion"|"judgment", …}`` for the counterpart document, when the
        corpus holds it. None for anything that isn't half of a CJEU pair."""
        celex = (meta or {}).get("celex") or doc["stable_id"]
        m = self._CELEX_CASE_RE.match(str(celex).upper())
        if not m:
            return None
        year, kind, num = m.groups()
        # A judgment, an order and a pending notice all want the AG's Opinion; the
        # Opinion wants whichever of those the corpus holds — preferring the judgment,
        # because once it exists the notice it superseded is no longer the answer.
        wanted = ([("CC", "ag_opinion")] if kind in ("CJ", "CO", "CN")
                  else [("CJ", "judgment"), ("CO", "order"), ("CN", "pending_notice")])
        for desc, role in wanted:
            other = cat.find_document_id(f"{year}{desc}{num}")
            if not other or other == doc["stable_id"]:
                continue
            row = cat.get_document(other)
            if row is None:
                continue
            # A retired notice is not the counterpart of anything: it was replaced by
            # the very judgment this pairing should have found (an Opinion announcing
            # "Pending: X" as its judgment, months after the Court gave one).
            if role == "pending_notice" and row["search_excluded"]:
                continue
            return {"role": role, "stable_id": other,
                    "title": row["title"], "celex": f"{year}{desc}{num}",
                    "oscola": _oscola_cite(row, _row_meta(row)),
                    "pending": bool(_row_meta(row).get("pending")),
                    "advocate_general": _row_meta(row).get("advocate_general"),
                    "date": str(row["decision_date"])[:10] if row["decision_date"] else None}
        return None

    _TREATMENT_RANK = {"overrules": 0, "distinguishes": 1, "applies": 2, "follows": 3,
                       "considers": 4, "mentions": 5}

    @staticmethod
    def _collapse_version_citers(cat, edge_rows) -> list[dict]:
        """Collapse repeated incoming evidence from snapshots of one citing act.

        Consolidated texts reproduce most of their base act verbatim. Extracting each
        snapshot is still correct and auditable, but displaying every snapshot as a
        separate citer makes a third statute appear increasingly cited whenever Cellar
        publishes another version. For each citing lineage choose the latest member
        applicable today that actually carries this target citation. If only future
        members carry it, choose the earliest future member. Non-versioned documents are
        untouched. The stored relation rows are never deleted or rewritten.
        """
        rows = [dict(r) for r in edge_rows]
        families = cat.consolidation_families_for([r["src_id"] for r in rows])
        if not families:
            return rows
        today = datetime.now(timezone.utc).date().isoformat()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            family = families.get(row["src_id"])
            grouped.setdefault(family[0] if family else row["src_id"], []).append(row)
        out: list[dict] = []
        for family_id, family_rows in grouped.items():
            dated = {
                row["src_id"]: families[row["src_id"]][1]
                for row in family_rows if row["src_id"] in families
            }
            applicable = sorted(
                ((d, sid) for sid, d in dated.items() if d and d <= today),
            )
            if applicable:
                chosen = applicable[-1][1]
            elif any(row["src_id"] == family_id for row in family_rows):
                chosen = family_id
            else:
                future = sorted((d or "9999-99-99", sid) for sid, d in dated.items())
                chosen = future[0][1] if future else family_rows[0]["src_id"]
            selected = [row for row in family_rows if row["src_id"] == chosen]
            # Exact repeats inside a package can still occur through parallel structured
            # and extracted edges; keep one evidential occurrence per passage/anchor/type.
            seen: set[tuple] = set()
            for row in selected:
                key = (
                    row["dst_id"], row["dst_anchor"], row["relationship_type"],
                    row["context_start"], row["context_end"],
                    row["raw_citation_string"],
                )
                if key not in seen:
                    seen.add(key)
                    row["citing_version_family"] = family_id
                    out.append(row)
        return out

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
                # src_id/src_anchor ride along: each extra passage is a link to the CITING
                # document, and without the id the UI had nothing to open — it pushed a
                # peek for `undefined` and the reader got "not held, fetch it?" on a
                # document sitting right there (the "→ para 39 opens item-not-found" bug).
                {"src_id": sid,
                 "src_anchor": demoted.get("src_anchor"),
                 "dst_anchor": demoted.get("dst_anchor"),
                 "relationship_type": demoted.get("relationship_type")})
        incoming: list[dict] = []
        page_ids = list(best.items())[:cap]
        # one grouped aggregate for the whole page, not one query per row — and one
        # batched document read for the same reason: 200 sequential single-row lookups
        # per page view is the N+1 that made get_document time out on a mega-authority.
        citer_counts = cat.cited_by_counts([sid for sid, _ in page_ids])
        sources = cat.get_documents([sid for sid, _ in page_ids])
        for sid, r in page_ids:
            src = sources.get(sid)
            # OSCOLA citation for the citing document, so "cited by / mentioned by"
            # reads in proper form. meta_json is on the row → no extra query.
            src_oscola = _oscola_cite(src, _row_meta(src)) if src else None
            incoming.append({**r, "src_title": src["title"] if src else None,
                             "src_court": src["court"] if src else None,
                             "src_date": (src["decision_date"] or src["effective_date"]) if src else None,
                             "src_authority": r.get("src_pagerank") or 0.0,
                             # jurisdiction × kind, so the cited-by list can be
                             # sliced the way a lawyer actually reads it
                             # ("UK cases", "EU legislation")
                             "src_jurisdiction": self._doc_bucket(
                                 src["source"], src["court"]) if src else None,
                             "src_kind": self._doc_kind(
                                 src["source"], src["doc_type"], src["court"],
                                 **self._pending_flags(_row_meta(src))) if src else None,
                             # how heavily THIS citer is itself cited — a subtle
                             # authority cue next to each name
                             "src_cited_by": citer_counts.get(sid),
                             "src_oscola": src_oscola,
                             # the other passages in this document that cite it
                             "other_passages": others.get(sid) or []})
        return incoming

    # The AG's Opinion (CC) and View (CV) in the same case. A pending notice's box says
    # so, because an Opinion delivered is the strongest public signal of where a pending
    # reference is going — and it is readable now, months before the judgment.
    _AG_DESCRIPTORS = ("CC", "CV", "CA", "CP")

    #: CELLAR's ``type_procedure`` code → what that proceeding is called in English. The
    #: OJ notice's own heading ("Action brought", "Appeal brought") says how it started
    #: but not what it IS: 802 of the corpus's live notices have no heading label at all,
    #: and "Action brought" alone cannot tell an annulment from a staff case or an
    #: infringement action. Two codes mean a preliminary reference — the ordinary one and
    #: the urgent procedure (PPU) — and reading only the first filed 10 urgent references,
    #: the fastest-moving cases the Court hears, as though they were something else.
    _PROCEDURE_LABEL = {
        "PREJ": "Preliminary reference",
        "REFER_PREL_URG": "Preliminary reference (urgent, PPU)",
        "ANNU": "Action for annulment",
        "PVOI": "Appeal",
        "FONC": "Staff case",
        "CONS": "Infringement action",
        "CARE": "Action for failure to act",
        "RESP": "Damages action",
        "COMP": "Damages action",
    }

    @classmethod
    def _is_preliminary(cls, procedure: str | None) -> bool:
        """Both preliminary-ruling codes, urgent included."""
        code = str(procedure or "").upper()
        return code.startswith("PREJ") or code.startswith("REFER_PREL")

    @classmethod
    def _procedure_label(cls, procedure: str | None, proceeding: str | None) -> str:
        # CELLAR occasionally returns a URL-encoded qualifier ("ANNU%3DRI"); the code
        # before it is still the procedure.
        code = str(procedure or "").upper().split("%")[0].strip()
        return (cls._PROCEDURE_LABEL.get(code)
                or (proceeding or "").strip()
                or (code.title() if code else "Pending proceeding"))

    #: After this long, a notice still marked pending is almost certainly not a live
    #: question: the reference was withdrawn or removed from the register, or its
    #: judgment exists and we failed to pair them. Measured on this corpus's own
    #: resolved pairs, an Article 267 reference takes a median 1.5 years and 2.6 years
    #: at the 99th percentile (the CJEU's own reported average is ~16 months); the
    #: longest observed here is 2.9. Five years is nearly double that tail, so the
    #: filter cannot hide a genuinely slow case — it hides zombies. They are COUNTED
    #: and returned separately (``stale``), never silently dropped.
    _PENDING_STALE_YEARS = 5

    def pending_references(self, stable_id: str, *, limit: int = 200) -> dict:
        """Live CJEU proceedings citing this instrument — what is still an open question
        about this statute, and on which provisions.

        Article 267 references (``preliminary``) are reported apart from the other
        pending proceedings (``other``: annulment actions, appeals, staff cases), because
        only the first asks the Court what this text MEANS. Each entry carries the
        articles and recitals that notice cites, so a reader can see at a glance that
        four pending references turn on Article 22 — and the Advocate General's Opinion
        where one has been delivered, which is readable long before the judgment.

        A notice retired by its full English judgment is not here: it is no longer a
        question, and the judgment carries a ``supersedes`` edge to it.
        """
        def _compute() -> dict:
            from .adapters.eu_cellar import celex_case_number
            from .eu_law import consolidation_base
            with self._open() as (cat, _rs, _ts):
                doc = cat.get_document(stable_id)
                if doc is None:
                    return {"error": "not found", "stable_id": stable_id}
                # Every key this act may receive citations under: the base act, its dated
                # consolidations, and the ECLI/alias identities. A reference lodged in
                # 2019 cites "Regulation 2016/679", which lands on the base act, while the
                # reader is looking at a 2021 consolidation of it.
                base = consolidation_base(stable_id) or stable_id
                ids = {stable_id, base, *(doc["ecli"] and [doc["ecli"]] or [])}
                ids.update(cat.document_identity_ids(stable_id))
                ids.update(sid for sid, _d in cat.legislative_versions(base))
                notices: dict[str, dict] = {}
                for row in cat.pending_eu_citers(sorted(i for i in ids if i)):
                    meta = _row_meta(row)
                    entry = notices.get(row["stable_id"])
                    if entry is None:
                        celex = str(meta.get("celex") or row["stable_id"])
                        courts = meta.get("referring_courts") or []
                        entry = notices[row["stable_id"]] = {
                            "stable_id": row["stable_id"],
                            "title": row["title"],
                            "case_number": celex_case_number(celex),
                            "date": str(row["decision_date"] or "")[:10] or None,
                            "court": self.court_label(row["court"], "eu-cellar")
                                     if row["court"] else None,
                            "procedure": meta.get("pending_procedure"),
                            "proceeding": meta.get("pending_proceeding"),
                            "procedure_label": self._procedure_label(
                                meta.get("pending_procedure"),
                                meta.get("pending_proceeding")),
                            "referring_court": courts[0] if courts else None,
                            "origin_country": meta.get("origin_country"),
                            "preliminary": self._is_preliminary(
                                meta.get("pending_procedure")),
                            "anchors": [],
                        }
                        # The Opinion, if the AG has delivered one. It does NOT end the
                        # case (only a judgment or order retires the notice), so the
                        # reference stays listed — annotated, not removed.
                        for descriptor in self._AG_DESCRIPTORS:
                            # 62026CN0449 → 62026CC0449. Court of Justice cases only:
                            # the General Court has no Advocate General.
                            if not re.fullmatch(r"6\d{4}CN\d{4}", celex, re.IGNORECASE):
                                break
                            opinion_id = cat.find_document_id(
                                f"{celex[:5]}{descriptor}{celex[7:]}")
                            if not opinion_id or opinion_id == row["stable_id"]:
                                continue
                            op = cat.get_document(opinion_id)
                            if op is None:
                                continue
                            entry["ag_opinion"] = {
                                "stable_id": opinion_id,
                                # The descriptor that MATCHED — an urgent reference is
                                # answered by a View (CV), not an Opinion (CC), so this
                                # cannot be reconstructed from the notice afterwards. The
                                # corpus often holds the document under its ECLI, which
                                # is no use as a EUR-Lex address.
                                "celex": f"{celex[:5]}{descriptor}{celex[7:]}",
                                "date": str(op["decision_date"] or "")[:10] or None,
                                "advocate_general":
                                    _row_meta(op).get("advocate_general"),
                            }
                            break
                    if row["dst_anchor"]:
                        entry["anchors"].append(row["dst_anchor"])
                entries = []
                for entry in notices.values():
                    # Which provisions the reference turns on, deduplicated and ordered
                    # the way the instrument itself is: recitals, then articles, then
                    # annexes — not by how often the notice happened to repeat them.
                    entry["anchors"] = sorted(dict.fromkeys(entry["anchors"]),
                                              key=_provision_sort_key)
                    entries.append(entry)
                entries.sort(key=lambda e: (e["date"] or "", e["stable_id"]), reverse=True)
                cutoff = (datetime.now(timezone.utc).date()
                          - timedelta(days=round(self._PENDING_STALE_YEARS * 365.25))
                          ).isoformat()
                live = [e for e in entries if (e["date"] or "9999") >= cutoff]
                stale = [e for e in entries if (e["date"] or "9999") < cutoff]
                preliminary = [e for e in live if e["preliminary"]]
                other = [e for e in live if not e["preliminary"]]
                return {
                    "stable_id": stable_id,
                    # One list, references first: a pending T-case on this instrument is
                    # as much "what is before the Court" as a reference is, and splitting
                    # them into separate boxes hid the direct actions behind a toggle.
                    # Each row carries its own procedure label, so they stay legible.
                    "pending": (preliminary + other)[:limit],
                    "preliminary": preliminary[:limit],
                    "other": other[:limit],
                    "preliminary_count": len(preliminary),
                    "other_count": len(other),
                    "with_ag_opinion": sum(1 for e in preliminary if e.get("ag_opinion")),
                    # Counted, not listed: see _PENDING_STALE_YEARS.
                    "stale_count": len(stale),
                    "stale": stale[:limit],
                    "stale_after_years": self._PENDING_STALE_YEARS,
                }
        return self._cached(f"pending-references:{stable_id}", 3600, _compute,
                            placeholder={"stable_id": stable_id, "preliminary": [],
                                         "other": [], "preliminary_count": None,
                                         "other_count": None, "with_ag_opinion": 0},
                            sync_wait=2.5)

    def retire_resolved_pending_notices(self, *, limit: int = 5000) -> dict:
        """Retire every CN/TN notice whose deciding judgment or order is now held in
        full English — the order-independent sweep behind the feed's own retirement.

        The feed can only retire a notice when it happens to re-enumerate the resolving
        decision; a judgment that arrived through the ordinary CJEU feed left its notice
        reading "Pending:" forever, fronting a case the corpus had already decided. This
        pass pairs them from what is held, so the two harvest orders converge.
        """
        with self._open() as (cat, _rs, _ts):
            pairs = cat.resolved_pending_eu_notices(limit=limit)
            retired = [(n, d) for n, d in pairs if cat.retire_pending_eu_notice(n, d)]
        if retired:
            self._invalidate_caches()
        return {"examined": len(pairs), "retired": len(retired),
                "notices": [n for n, _d in retired][:200]}

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
                           self._doc_kind(r["source"], r["doc_type"], r["court"],
                                          pending=bool(r["pending"]),
                                          preliminary=bool(r["prej"])))
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
                and (not kind or self._doc_kind(r["source"], r["doc_type"], r["court"],
                                                pending=bool(r["pending"]),
                                                preliminary=bool(r["prej"])) == kind)})
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

    def refresh_eu_rendition(self, *, stable_id: str) -> dict:
        """Recheck one held CELLAR decision for a newly published English rendition.

        This is deliberately a one-document operation for the reader's language banner,
        not a source-wide harvest.  If English is still absent, the held French body and
        its labelled English OJ operative part remain unchanged.
        """
        from .adapters.eu_cellar import CELEX_BASE, EUCellarAdapter
        from .core.models import Stub
        from .pipeline import Pipeline
        from .pipeline.runner import RunStats

        with self._open() as (cat, rs, ts):
            real = cat.find_document_id(stable_id) or stable_id
            doc = cat.get_document(real)
            if doc is None:
                return {"error": "document not found", "stable_id": stable_id}
            if doc["source"] != "eu-cellar":
                return {"error": "language refresh is available only for CELLAR documents",
                        "stable_id": real}
            old_meta = _row_meta(doc)
            celex = str(old_meta.get("celex") or "").upper()
            if not re.fullmatch(r"6\d{4}[CTF][JOVC]\d{4}", celex):
                return {"error": "document has no refreshable CJEU CELEX identifier",
                        "stable_id": real}
            adapter = EUCellarAdapter(with_citations=True)
            stub = Stub(
                stable_id=real,
                title=doc["title"],
                court=doc["court"],
                hint_date=(date.fromisoformat(str(doc["decision_date"])[:10])
                           if doc["decision_date"] else None),
                landing_url=(doc["landing_url"]
                             or f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}"),
                raw_url=f"{CELEX_BASE}/{celex}",
                hints={"celex": celex},
            )
            record = adapter.fetch(stub)
            if record is None or not record.text:
                return {"stable_id": real, "refreshed": False,
                        "source_language": doc["source_language"],
                        "english_available": doc["source_language"] == "en",
                        "reason": "no current rendition returned by EUR-Lex"}
            # Preserve unrelated enrichment/provenance already accumulated on this row;
            # fresh rendition facts win where they overlap.
            record.extra = {**old_meta, **(record.extra or {})}
            stored = Pipeline(cat, rs, textstore=ts)._ingest(
                record, RunStats(source=adapter.source))
            if stored:
                from .citations import extract_document
                extract_document(cat, ts, real)
                Resolver(cat).run_for(real, record.ecli)
            result = {
                "stable_id": real,
                "refreshed": stored,
                "source_language": record.source_language,
                "english_available": record.source_language == "en",
                "english_oj_notice": bool(record.extra.get("english_oj_notice_celex")),
            }
        self._invalidate_caches()
        return result

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

    def _inherited_recitals(
        self, cat, ts, stable_id: str, *, include_citations: bool = True,
    ) -> dict | None:
        """Return a consolidation's unchanged base-act recitals as a virtual body.

        EUR-Lex consolidated expressions omit the preamble because amendments do not
        rewrite recitals.  Copying those recitals into every dated expression would
        duplicate text and citation edges, so readers instead project the base act's
        recital segments at read time.  Offsets and outgoing citation spans are rebased
        onto a compact recital-only string; the stored consolidation remains untouched.
        Incoming mentions need no copying either:
        ``version_inherited_mentions_for`` already projects base-act anchors (including
        ``Recital N``) onto every consolidation.
        """
        base_id = cat.consolidation_base_for(stable_id)
        if not base_id:
            return None
        target = cat.get_document(stable_id)
        if target is not None and target["payload_hash"]:
            target_segments = ts.get_segments(target["payload_hash"])
            if any(
                (segment.kind or "").casefold() == "recital"
                or re.match(r"^\s*recitals?\b", segment.label or "", re.I)
                for segment in target_segments
            ):
                return None
        base = cat.get_document(base_id)

        def _recitals_for(row):
            if row is None or not row["payload_hash"]:
                return None
            try:
                candidate_text = ts.get(row["payload_hash"])
            except OSError:
                return None
            candidate_segments = ts.get_segments(row["payload_hash"])
            candidate_recitals = [
                segment for segment in candidate_segments
                if (segment.kind or "").casefold() == "recital"
                or re.match(r"^\s*recitals?\b", segment.label or "", re.I)
            ]
            return (
                (candidate_text, candidate_recitals)
                if candidate_recitals else None
            )

        source = base
        source_body = _recitals_for(base)
        source_is_base_act = bool(source_body)
        # Old imports sometimes hold the sector-3 act only as flattened HTML whose
        # parser dropped its preamble. Recitals are immutable across consolidations, so
        # the earliest held sector-0 expression with structured recitals is a safe
        # temporary projection source while the reverse sweep refreshes the base Formex.
        if source_body is None and re.fullmatch(r"3\d{4}[A-Z]+\d+", base_id):
            siblings = cat.conn.execute(
                """
                SELECT d.*
                FROM relations r JOIN documents d ON d.stable_id = r.src_id
                WHERE r.relationship_type = 'consolidates'
                  AND (r.dst_id = ? OR r.candidate_id = ?)
                  AND d.payload_hash IS NOT NULL
                ORDER BY d.stable_id
                """,
                (base_id, base_id),
            ).fetchall()
            for sibling in siblings:
                source_body = _recitals_for(sibling)
                if source_body is not None:
                    source = sibling
                    break
        if source is None or source_body is None:
            return None
        source_text, recital_segments = source_body
        source_id = str(source["stable_id"])

        source_citations = (
            list(cat.citations_for(source_id)) if include_citations else [])
        text_parts: list[str] = []
        segments: list[dict] = []
        citations: list[dict] = []
        cursor = 0
        for segment in sorted(
                recital_segments, key=lambda item: (item.char_start, item.char_end)):
            source_start = max(0, min(int(segment.char_start), len(source_text)))
            source_end = max(source_start, min(int(segment.char_end), len(source_text)))
            body = source_text[source_start:source_end].strip()
            if not body:
                continue
            if text_parts:
                text_parts.append("\n\n")
                cursor += 2
            start = cursor
            text_parts.append(body)
            cursor += len(body)
            segments.append({
                "label": segment.label,
                "kind": "recital",
                "level": segment.level,
                "char_start": start,
                "char_end": cursor,
                "inherited": True,
                "source_stable_id": source_id,
            })

            # ``strip`` may remove whitespace before the segment body. Account for it
            # when rebasing exact citation/highlight spans.
            raw_body = source_text[source_start:source_end]
            leading = len(raw_body) - len(raw_body.lstrip())
            content_source_start = source_start + leading
            content_source_end = content_source_start + len(body)
            for citation in source_citations:
                citation_start = citation["char_start"]
                citation_end = citation["char_end"]
                if citation_start is None or citation_end is None:
                    continue
                if not (content_source_start <= citation_start
                        and citation_end <= content_source_end):
                    continue
                candidate = citation["candidate_id"]
                resolved = self._resolved_target(
                    cat, candidate, citation["raw"])
                citations.append({
                    "char_start": start + citation_start - content_source_start,
                    "char_end": start + citation_end - content_source_start,
                    "raw": citation["raw"],
                    "candidate_id": candidate,
                    "pinpoint": citation["pinpoint"],
                    "entity_kind": citation["entity_kind"],
                    "resolved_id": resolved,
                    "method": citation["method"],
                    "state": (
                        "resolved" if resolved
                        else ("pending" if candidate else "maybe")
                    ),
                    "inherited": True,
                    "source_stable_id": source_id,
                })

        if not segments:
            return None
        return {
            "text": "".join(text_parts),
            "segments": segments,
            "citations": citations,
            "count": len(segments),
            "source_stable_id": source_id,
            "source_title": source["title"] or source_id,
            "source_url": source["landing_url"],
            "base_stable_id": base_id,
            "source_is_base_act": source_is_base_act,
            "unchanged": True,
            "virtual": True,
            # Written for a reader, not for the pipeline. The old wording ("Recitals are
            # inherited unchanged … without being copied into this consolidated
            # expression") described our storage model and left the actual question —
            # why does a consolidated text have no recitals of its own? — unanswered.
            "note": (
                (
                    "A consolidated text does not normally reproduce the recitals: "
                    "they belong to the act as originally adopted. These are that "
                    "act's own recitals, shown here for reference — they are "
                    "unchanged, and are not part of this consolidated text."
                    if source_is_base_act else
                    "A consolidated text does not normally reproduce the recitals: "
                    "they belong to the act as originally adopted. These are taken "
                    "from the earliest expression RagLex holds with a structured "
                    "preamble (the original act's own rendition is being refreshed), "
                    "are unchanged, and are not part of this consolidated text."
                )
            ),
        }

    # Characters of text returned when the caller asked for no window and the document is
    # too large to return whole. Deliberately conservative: the payload carries segments
    # and inline citations too, and their density varies enormously between a flat
    # judgment and a heavily-cross-referenced act, so the budget is set for the dense case.
    _BODY_DEFAULT_WINDOW = 120_000

    def document_body(self, stable_id: str, *, offset: int = 0,
                      limit: int | None = None, segments_only: bool = False,
                      max_chars: int | None = None) -> dict:
        """The document's extracted text + structural segments (§6b) for the reader.
        Segments carry kind/level so legislation renders as a hierarchy. Consolidated
        EU expressions also expose ``inherited_recitals``: a virtual, provenance-marked
        projection of the original act's unchanged recitals and their citation links.

        ``segments_only`` returns the STRUCTURE alone — labels, kinds, levels, offsets —
        and no text, no citations. That is what structural work (picking a provision to
        pincite, building a correlation table) actually needs, and it turns a document
        that cannot be returned at all into one cheap call: the DPA 2018 is 1,222
        segments and its full body exceeds the 1 MB tool ceiling outright.

        ``offset``/``limit`` window the text in characters, carrying only the segments
        and citations that overlap the window, so a large document can be read in
        pieces instead of not at all.
        """
        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(stable_id)
            if doc is None or not doc["payload_hash"]:
                return {"text": None, "segments": [], "doc_type": doc["doc_type"] if doc else None}
            ph = doc["payload_hash"]
            try:
                text = ts.get(ph)
            except OSError:
                text = None
            if segments_only:
                from .core.segmentation import recover_numbered_segments
                segments, synthesised = recover_numbered_segments(
                    text or "", ts.get_segments(ph))
                return {
                    "stable_id": stable_id,
                    "title": doc["title"],
                    "doc_type": doc["doc_type"],
                    "segments_only": True,
                    "segment_count": len(segments),
                    "segments_total": len(segments),
                    "segmentation": ("none" if (text and not segments)
                                     else "synthesised" if synthesised else "structural"),
                    "text_chars": len(text or ""),
                    "segments": [
                        {"label": s.label, "kind": s.kind, "level": s.level,
                         "char_start": s.char_start, "char_end": s.char_end}
                        for s in segments
                    ],
                    "how_to_read": (
                        "structure only. get_provision(stable_id, label=…) reads one "
                        "provision; get_document_body(offset=…, limit=…) reads a window "
                        "of the text."),
                }
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
            from .core.segmentation import recover_numbered_segments
            segments, synthesised = recover_numbered_segments(
                text or "", ts.get_segments(ph))
            total_chars = len(text or "")
            segments_total = len(segments)
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
                from .core.structure import line_structure

                def _spans(body: str, base: int) -> list[dict]:
                    # ``anchor`` = the line's marker path ("(2)", "(2)(a)") so the reader can
                    # place its sub-provision mention badge at the end of that line, not bunched
                    # at the section foot. Empty for continuation/lead-in lines.
                    return [{"start": base + a, "end": base + b, "depth": d,
                             **({"anchor": path} if path else {})}
                            for a, b, d, path in line_structure(body)]

                for s in segs:
                    body = text[s["char_start"]:s["char_end"]]
                    if "\n" in body:
                        s["lines"] = _spans(body, s["char_start"])
                # unsegmented legislation (flat-text imports) renders as one block,
                # which still wants indenting
                if not segs and "\n" in text:
                    flat_lines = _spans(text, 0)
            # A caller BOUND BY A SIZE CEILING (``max_chars``) and asking for no window
            # gets the FIRST PAGE rather than a transport error: the DPA 2018 assembles to
            # 2.4 MB against the 1 MB MCP tool ceiling, so an unwindowed call could not be
            # answered at all, and the failure named none of the ways through.
            #
            # ONLY that caller. This defaulted for everyone once, and the web reader — which
            # has no ceiling, renders the whole act, and knows nothing about next_offset —
            # silently began serving the first 120k characters of every long instrument.
            # For the AI Act that is 180 recitals and not one article: the reader looked
            # exactly as broken as the bug this was shipped alongside.
            defaulted = False
            skipped_recitals = None
            if (text and max_chars and not offset and limit is None
                    and len(text) > max_chars):
                limit, defaulted = max_chars, True
                # …and it starts at the ARTICLES. An EU instrument opens with its
                # recitals — the AI Act has 180 of them, ~120k characters, so a window
                # from character zero is entirely preamble and not one operative
                # provision. The recitals are worth reading and are one call away
                # (offset=0); they are just not what "read me this regulation" means.
                first_article = next(
                    (s for s in segs if (s.get("kind") or "") not in ("recital", "header")),
                    None)
                if first_article and first_article["char_start"] > 0:
                    recitals = [s for s in segs if (s.get("kind") or "") == "recital"]
                    if recitals:
                        offset = first_article["char_start"]
                        skipped_recitals = len(recitals)
            window = None
            if text and (offset or limit):
                # A character window, with segments/citations narrowed to what OVERLAPS
                # it — a segment straddling the boundary still belongs to both pages, so
                # its offsets stay absolute and the caller can stitch pages by char_start.
                start = max(0, min(int(offset), len(text)))
                end = len(text) if limit is None else min(len(text), start + max(1, int(limit)))
                window = {"offset": start, "limit": end - start,
                          "text_chars": len(text), "has_more": end < len(text),
                          "next_offset": end if end < len(text) else None}
                if defaulted:
                    window["defaulted"] = True
                    window["note"] = (
                        f"{len(text):,} characters is too large to return whole, so this "
                        "is one window of it. Walk it with next_offset, or call "
                        "segments_only=True for the structure alone, or "
                        "get_provision(label=…) for one provision.")
                    if skipped_recitals:
                        window["starts_at"] = "first operative provision"
                        window["recitals_skipped"] = skipped_recitals
                        window["note"] += (
                            f" It starts at the first operative provision: the {skipped_recitals} "
                            "recitals before it are NOT included — read them with offset=0.")
                segs = [s for s in segs
                        if s["char_end"] > start and s["char_start"] < end]
                citations = [c for c in citations
                             if (c["char_end"] or 0) > start
                             and (c["char_start"] or 0) < end]
                text = text[start:end]
            # The truncation signal sits at the TOP level as well as inside ``window``.
            # A reader scanning the keys of a body response sees ``text`` and reads it;
            # nesting "this is 68% of the judgment" one level down inside an object
            # named for the windowing mechanism is how a 213-paragraph judgment gets
            # quoted as if it ended at paragraph 140. It should not be possible to hold
            # this response and not know the text is partial.
            truncated = bool(window and window["has_more"])
            return {
                "text": text,
                "truncated": truncated,
                "text_chars": total_chars,
                "char_range": ([window["offset"], window["offset"] + window["limit"]]
                               if window else [0, total_chars]),
                **({"incomplete": (
                    f"PARTIAL TEXT — characters {window['offset']:,}–"
                    f"{window['offset'] + window['limit']:,} of {total_chars:,}. "
                    f"The rest is at offset={window['next_offset']}.")}
                   if truncated else {}),
                "segments": segs,
                # How much structure this document HAS, against how much of it this
                # window carries — "three recitals and nothing operative" is otherwise
                # indistinguishable from "a commencement SI that says little", and the
                # difference is whether ingestion dropped the operative part.
                "segment_count": len(segs),
                "segments_total": segments_total,
                "segmentation": ("none" if (text and not segments_total)
                                 else "synthesised" if synthesised else "structural"),
                "lines": flat_lines,
                "citations": citations,
                **({"window": window} if window else {}),
                "doc_type": doc["doc_type"],
                "language": doc["language"],
                "source_language": doc["source_language"],
                "language_fallback": meta.get("language_fallback"),
                "english_oj_notice": (
                    {
                        "celex": meta.get("english_oj_notice_celex"),
                        "url": meta.get("english_oj_notice_url"),
                        "anchor": "English Official Journal notice — operative part",
                    }
                    if meta.get("english_oj_notice_celex") else None
                ),
                "title": doc["title"],
                "oscola": _oscola_cite(doc, meta),
                # the reader offers an "original" pane when the ingested file is stored
                "raw_ext": (raw_path.rsplit(".", 1)[-1].lower()
                            if raw_path and "." in raw_path else None),
                # a BAILII PDF-only stub: no transcript here, but a link to the original
                # PDF on bailii.org the reader can offer (source_url is the landing page)
                "external_pdf": meta.get("bailii_pdf_url"),
                "source_url": doc["landing_url"] or meta.get("bailii_url"),
                "inherited_recitals": self._inherited_recitals(
                    cat, ts, stable_id) if doc["doc_type"] == "legislation" else None,
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
                          sort: str = "pagerank", jurisdiction: str | None = None,
                          kind: str | None = None) -> dict:
        """Who mentions this document (and, optionally, one paragraph of it), grouped by the
        citing document and ranked by ``sort`` (default: the citer's own PageRank).

        Powers the reader's per-paragraph "Mentioned by …" line (``by_anchor``) and the
        "See all mentions" tray (``groups`` — each citing document with the passages, drawn
        from the citation's context span, where it cites this one, and its OSCOLA citation).
        Heuristic carry-forward (inferred) edges are excluded — they aren't citations.

        ``jurisdiction`` (ISO code or name) and ``kind`` ("cases" | "administrative" |
        "legislation" | "guidance") narrow the citing set; ``facets`` in the reply always
        report the WHOLE (unfiltered) anchor-scoped set so the caller can see what else it
        could narrow to.
        """
        with self._open() as (cat, _rs, ts):
            anchor_exact = anchor if anchor and exact else None
            # Segment labels may include their title ("Article 17 Right to erasure",
            # "s. 13 Compensation for failure to comply"), whereas citation edges carry
            # only the bare pinpoint ("Article 17(2)", "s. 13"). Give the catalogue every
            # spelling of the canonical unit+number as a coarse guard; the exact family
            # matcher below remains authoritative.
            # A range pinpoint shares no prefix with the paragraph it covers, so the
            # prefix guard dropped every one of them before the span test could run —
            # 15% of the corpus's paragraph pinpoints, silently, making a
            # paragraph-level citer count a floor that read as a count.
            want_span = (_paragraph_span(anchor) if anchor and not exact else None)
            anchor_prefixes = ([] if want_span else
                               _anchor_sql_prefixes(anchor) if anchor and not exact
                               else [])
            anchor_like = _paragraph_anchor_like(want_span) if want_span else []
            identity_ids = cat.document_identity_ids(stable_id)
            rels = []
            # The heuristic edges are excluded from the answer — they aren't citations —
            # but they must not be excluded SILENTLY. When a provision has none of the
            # one kind and plenty of the other, "no citer pins to s. 16" is a false
            # negative dressed as a finding, and the reader has no way to tell.
            # ``_anchor_matched`` is applied to these below, on the same anchor rules.
            dropped_inferred = []
            seen_relation_ids: set[int] = set()
            for identity_id in identity_ids:
                for row in cat.relations_to(
                    identity_id,
                    anchor_exact=anchor_exact,
                    anchor_prefixes=anchor_prefixes,
                    anchor_like=anchor_like,
                ):
                    rid = int(row["relation_id"])
                    if rid in seen_relation_ids:
                        continue
                    seen_relation_ids.add(rid)
                    if row["extracted_via"] == "inferred":
                        dropped_inferred.append(dict(row))
                    else:
                        rels.append(dict(row))
            # A law or judgment can cite another rendition/identifier of itself. That
            # is an internal cross-reference, not a later document citing it. The
            # lookup count already excludes these; dropping them here keeps
            # citing_documents().total on the same evidenced-document definition.
            identity_set = set(identity_ids)
            rels = [row for row in rels if row["src_id"] not in identity_set]
            dropped_inferred = [row for row in dropped_inferred
                                if row["src_id"] not in identity_set]
            direct_keys = {
                (r["src_id"], r["dst_anchor"], r["context_start"], r["context_end"])
                for r in rels
            }
            version_base = cat.consolidation_base_for(stable_id)
            for row in cat.version_inherited_mentions_for(
                stable_id, limit=20000,
                anchor_exact=anchor_exact,
                anchor_prefixes=anchor_prefixes,
                anchor_like=anchor_like,
            ):
                projected = dict(row)
                key = (
                    projected["src_id"], projected["dst_anchor"],
                    projected["context_start"], projected["context_end"],
                )
                if key in direct_keys:
                    continue
                projected["dst_id"] = stable_id
                projected["version_inherited"] = True
                rels.append(projected)
            rels = self._collapse_version_citers(cat, rels)

            def _anchor_matched(rows: list[dict]) -> list[dict]:
                """``rows`` narrowed to the ones pinned to ``anchor``. Factored out so the
                heuristic edges can be tested against exactly the same rules — a count of
                what was withheld is only meaningful if it answers the same question."""
                if anchor and exact:
                    # A specific SUB-provision: the sub-paragraph mention badges want only
                    # the documents pinned to exactly this pinpoint (Article 47(1)), not
                    # the whole Article 47 family. Match on a whitespace/case-normalised
                    # anchor so "Article 47(1)" and "article 47 (1)" coincide.
                    def _norm(a: str | None) -> str:
                        return re.sub(r"\s+", "", (a or "")).lower()
                    want = _norm(anchor)
                    return [r for r in rows if _norm(r["dst_anchor"]) == want]
                if anchor and want_span:
                    # A JUDGMENT PARAGRAPH — the citable unit of case law, and the one the
                    # string matcher below cannot handle. The unit word is optional on both
                    # sides ("[110]" / "para 110"), and a pinpoint may span a RANGE, so the
                    # test is numeric overlap rather than a match on the written form.
                    return [r for r in rows
                            if _paragraph_spans_overlap(want_span, r["dst_anchor"])]
                if anchor and any(k.startswith("para:") for k in
                                  _anchor_key_variants(_anchor_key(anchor))):
                    # Multi-level paragraph numbering ("para 3.19" of a code of practice):
                    # not a judgment paragraph, so it folds on the canonical key, which
                    # keeps 3.19 apart from 3.
                    keys = _anchor_key_variants(_anchor_key(anchor))
                    return [r for r in rows
                            if _anchor_key_variants(_anchor_key(r["dst_anchor"])) & keys]
                if anchor:
                    # A provision heading represents its whole family. "Mentions of
                    # Article 22" includes citations pinned to Article 22(1), 22(2), …;
                    # exact string equality made the UI inherit whichever subparagraph
                    # happened to appear first and hid the rest.
                    parent = re.sub(r"(?:\([^()]+\))+\s*$", "", anchor).strip()
                    family = re.compile(rf"^{re.escape(parent)}(?:\([^()]+\))*$",
                                        re.IGNORECASE)
                    matched = [r for r in rows
                               if family.match((r["dst_anchor"] or "").strip())]
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
                        #
                        # Compared as VARIANTS, so a judgment paragraph matches whichever
                        # way each side spelled it ("[110]" against a stored "para 110").
                        keys = _anchor_key_variants(_anchor_key(anchor))
                        if keys:
                            matched = [
                                r for r in rows
                                if _anchor_key_variants(_anchor_key(r["dst_anchor"]))
                                & keys]
                    return matched
                return rows

            rels = _anchor_matched(rels)
            withheld_inferred = (
                len({r["src_id"] for r in _anchor_matched(dropped_inferred)})
                if anchor and dropped_inferred else 0)
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
                    "src_court": sdoc["court"],
                    # decision_date where the source gave one, else the year the
                    # identifier carries — otherwise a newest-first sort buries every
                    # undated judgment at the bottom regardless of its year
                    "src_date": sdoc["decision_date"] or sdoc["effective_date"],
                    # name the citing court and its jurisdiction, as the explorer does
                    "src_court_label": self.court_label(sdoc["court"], sdoc["source"]) if sdoc["court"] else None,
                    "src_jurisdiction": self._doc_bucket(sdoc["source"], sdoc["court"]),
                    "src_kind": self._doc_kind(sdoc["source"], sdoc["doc_type"], sdoc["court"],
                                               **self._pending_flags(_row_meta(sdoc))),
                    "src_doc_type": sdoc["doc_type"],
                    # what makes it pending, for the tray's own labelling — the reader
                    # should see "Request for a preliminary ruling" on the row, not have
                    # to infer it from the chip it was filed under
                    **self._pending_meta(sdoc, _row_meta(sdoc)),
                    "authority": _authority(sid, sdoc), "count": len(rs),
                    "version_inherited_count": sum(
                        bool(r.get("version_inherited")) for r in rs),
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
            # Split on what the document IS, not on the browse bucket it sits in: a law-
            # reform report files under Guidance/Reports for browsing (nobody looks for the
            # Law Commission under "travaux"), but as a CITER of an Act it is still
            # legislative history and belongs in this section.
            preparatory_groups = [g for g in groups if g["src_doc_type"] == "preparatory"]
            groups = [g for g in groups if g["src_doc_type"] != "preparatory"]

            # facet counts over the WHOLE anchor-scoped set (before any jurisdiction/kind
            # filter) so the caller sees exactly what it could narrow to — this is what
            # makes the citing list browsable instead of a wall of rows.
            juris_facets: dict[str, int] = {}
            kind_facets: dict[str, int] = {}
            crossed: dict[tuple[str, str], int] = {}
            for g in groups:
                juris_facets[g["src_jurisdiction"]] = juris_facets.get(g["src_jurisdiction"], 0) + 1
                kind_facets[g["src_kind"]] = kind_facets.get(g["src_kind"], 0) + 1
                if g["src_jurisdiction"] and g["src_kind"]:
                    key = (g["src_jurisdiction"], g["src_kind"])
                    crossed[key] = crossed.get(key, 0) + 1
            facets = {
                "jurisdiction": [{"jurisdiction": j, "documents": n}
                                 for j, n in sorted(juris_facets.items(), key=lambda kv: -kv[1])],
                "kind": [{"kind": k, "documents": n}
                         for k, n in sorted(kind_facets.items(), key=lambda kv: -kv[1])],
                # The crossed bucket ("UK cases 512") is what the reader's mentions tray
                # puts on its chips. It has to come from here: the tray holds one PAGE of
                # citers, so counting its own rows described 40 documents while claiming to
                # summarise 912 — and a jurisdiction that sorts below the first page read
                # as absent entirely.
                "jurisdiction_kind": [{"jurisdiction": j, "kind": k, "documents": n}
                                      for (j, k), n in sorted(crossed.items(),
                                                              key=lambda kv: -kv[1])],
            }
            # apply the narrowing filters (jurisdiction accepts an ISO code OR a name)
            want_j = self._norm_jurisdiction(jurisdiction)
            if want_j:
                wl = want_j.lower()
                groups = [g for g in groups if (g["src_jurisdiction"] or "").lower() == wl]
            if kind:
                kl = kind.strip().lower()
                groups = [g for g in groups if (g["src_kind"] or "").lower() == kl]

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
                    from .citations.reanchor import aligned_span

                    for r in g["_rels"]:
                        cs, ce = r["context_start"], r["context_end"]
                        if cs is None:
                            continue
                        aligned = aligned_span(
                            text, r["raw_citation_string"], cs, ce)
                        if aligned is None:
                            # Do not confidently highlight unrelated bytes when an old
                            # parser projection has drifted and the raw citation cannot
                            # be confirmed nearby.  The surrounding preview remains useful.
                            aligned_cs = max(0, min(int(cs), len(text)))
                            aligned_ce = aligned_cs
                        else:
                            aligned_cs, aligned_ce = aligned
                        # The RUN-UP carries the evidence about the citation, not just
                        # context for it: the party name the citing court wrote beside it
                        # (checked against what it resolved to) and, in an appellate
                        # judgment, the "ON APPEAL FROM THE HIGH COURT …" header that
                        # names the decision below. 90 characters cut both off — the
                        # Dawson-Damer appeal's own header ran to ~105 before the
                        # citation — so this is sized to the sentence, not the phrase.
                        a = max(0, aligned_cs - 170)
                        b = min(len(text), aligned_ce + 200)
                        # offsets of the citation itself within the snippet, so the
                        # tray can mark the words that actually made the connection
                        # ("Arbitration Act s 7"). context_start/end is the matched
                        # citation's own span, not a wider window.
                        window = text[a:b]
                        lead = len(window) - len(window.lstrip())
                        body = window.strip()
                        ms = min(max(0, aligned_cs - a - lead), len(body))
                        me = min(max(ms, aligned_ce - a - lead), len(body))
                        # the anchor labels WHERE IN THE CITING DOCUMENT the passage
                        # sits, so the reader can place the quote. Never fall back to
                        # dst_anchor: that is the paragraph of the *cited* document the
                        # user just clicked, so every snippet would be labelled with
                        # the thing they already know.
                        snippets.append({"anchor": r["src_anchor"], "text": body,
                                         "start": aligned_cs,
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
                        "version_inherited": bool(r.get("version_inherited")),
                    }
                elif r.get("version_inherited"):
                    seen[r["src_id"]]["version_inherited"] = True
            # …plus the citers of a MAPPED provision in another instrument, against the
            # provision they were mapped to. The bottom-of-page panel already listed
            # them, behind a dropdown; the article you are reading is where they answer
            # a question. Marked ``inherited`` with the instrument they actually cite,
            # so the rail can say so — they are not citations of this Article and the
            # reader never implies they are. Deliberately confined to this roll-up: the
            # ``groups``/``total`` half of this reply is the citation record, and
            # citing_documents() reads it.
            if offset == 0:
                inherited_srcs: dict[str, object] = {}
                for row in cat.inherited_mentions_for(stable_id, limit=1200):
                    lab = row["inherited_current_anchor"]
                    seen = by_anchor.setdefault(lab, {})
                    if not lab or row["src_id"] in seen:
                        continue
                    sid = row["src_id"]
                    if sid not in inherited_srcs:
                        inherited_srcs[sid] = cat.get_document(sid)
                    sdoc = inherited_srcs[sid]
                    seen[sid] = {
                        "src_id": sid,
                        "src_oscola": (_oscola_cite(sdoc, _row_meta(sdoc))
                                       if sdoc else None),
                        "authority": _authority(sid, sdoc),
                        "version_inherited": False,
                        "inherited": True,
                        "mapping_type": row["mapping_type"],
                        "from_id": row["inherited_from_id"],
                        "from_anchor": row["inherited_from_anchor"],
                        "from_title": row["inherited_from_title"],
                    }
            by_anchor = {lab: sorted(v.values(), key=lambda x: -x["authority"])
                         for lab, v in by_anchor.items() if v}
            end = (offset + limit) if limit else total_groups
            return {"target": stable_id, "anchor": anchor,
                    "version_inheritance": (
                        {"from_base_act": version_base}
                        if version_base else None
                    ),
                    "jurisdiction": want_j, "kind": kind,
                    "facets": facets,
                    "total": total_groups, "groups": page,
                    # documents that pin to this anchor ONLY through a heuristic
                    # (carry-forward) edge, which is excluded from the answer above
                    "withheld_inferred_citers": withheld_inferred or None,
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
                    # How many of this document's citers could EVER appear under an
                    # anchor. Without it a provision-level total is read against the
                    # document-level one as though the difference were courts that
                    # considered the provision and passed over it.
                    **(self._pinpoint_coverage(cat, stable_id) if anchor else {}),
                    "by_anchor": by_anchor if offset == 0 else {}}

    def _pinpoint_coverage(self, cat, stable_id: str) -> dict:
        """How many citing DOCUMENTS pinpoint anything at all, and how many never do.

        Partitioned per citing document, not per edge: a judgment that cites this one
        four times and pinpoints once is a citer that CAN appear under an anchor. Counting
        edges instead put such a document in both columns, and the two numbers then
        summed to more than the citer total — which is exactly the arithmetic a reader
        uses them for."""
        row = cat.conn.execute(
            "SELECT SUM(CASE WHEN has_pin = 1 THEN 1 ELSE 0 END) AS pinned,"
            "       SUM(CASE WHEN has_pin = 0 THEN 1 ELSE 0 END) AS unpinned FROM ("
            "  SELECT src_id, MAX(CASE WHEN COALESCE(dst_anchor, '') <> ''"
            "                          THEN 1 ELSE 0 END) AS has_pin"
            "  FROM relations WHERE dst_id = ? AND resolution_status = 'resolved'"
            "    AND relationship_type <> 'cited_by' AND extracted_via <> 'inferred'"
            "  GROUP BY src_id) t",
            (stable_id,)).fetchone()
        if row is None:
            return {}
        return {"pinpointed_citers": row["pinned"] or 0,
                "unpinpointed_citers": row["unpinned"] or 0}

    def _resolve_held_id(self, raw: str) -> tuple[str | None, str | None]:
        """Resolve a citation string / stable_id to (held_id, candidate_id). Shared by
        lookup() and citing_documents() so both accept the same identifiers: a neutral
        citation, an ECLI/CELEX, a statute-by-name, or a stable_id passed straight through."""
        from .citations import extract_citations
        from .resolve.matchers import first_candidate

        raw = (raw or "").strip()
        if not raw:
            return None, None
        cand: str | None = None
        hits = extract_citations(raw)
        if hits and hits[0].candidate_id:
            cand = hits[0].candidate_id
        if not cand:
            fc = first_candidate(raw)
            cand = fc.value if fc else None
        with self._open() as (cat, _rs, _ts):
            held = cat.find_document_id(cand) if cand else None
            if held is None:
                if cat.get_document(raw) is not None:
                    held, cand = raw, raw
            return held, cand

    # How many citing rows a browsable page carries, and how much snippet text each — kept
    # small so a page of citers is token-cheap; the agent pages/narrows to see more.
    _CITING_PAGE = 20

    def citing_documents(self, target: str | list[str], *, anchor: str | None = None,
                         sort: str = "pagerank", jurisdiction: str | None = None,
                         kind: str | None = None, offset: int = 0,
                         limit: int | None = None, snippets: bool = True,
                         mode: str = "union") -> dict:
        """The browsable list of who cites ``target`` — optionally pinned to ONE provision
        (``anchor`` = "Article 15", "s. 45", "[42]") so you get exactly the documents that
        cite THAT article, not the whole instrument. Sortable, filterable by jurisdiction
        (ISO code or name) and kind, paginated, with an inline snippet per row and facet
        counts telling you what you can narrow to. Re-callable with the same arguments — this
        IS the results list to come back to; there is no hidden state."""
        limit = limit or self._CITING_PAGE
        if isinstance(target, (list, tuple)):
            if mode != "intersection":
                return {"error": "multiple targets require mode='intersection'"}
            return self._citing_intersection(
                list(target), sort=sort, jurisdiction=jurisdiction, kind=kind,
                offset=offset, limit=limit)
        held, cand = self._resolve_held_id(target)
        if held is None:
            return {"target": target, "held": False,
                    "note": ("Not held, so there is nothing in the corpus citing it. "
                             "lookup() it first (it will fetch the authority if it can), "
                             "then browse its citers here.")}
        read_target = self.canonical_read_target(held)
        held = read_target["stable_id"]
        # Cache per full arg tuple: loading a mega-authority's incoming edges costs seconds
        # (see cited_by_breakdown), and the whole point of this tool is to be re-called as
        # the agent pages / re-sorts / returns to the list — so the repeats must be instant.
        key = f"citing:{held}:{anchor}:{sort}:{jurisdiction}:{kind}:{offset}:{limit}:{int(snippets)}"
        def build() -> dict:
            result = self._citing_documents(
                held, anchor, sort, jurisdiction, kind, offset, limit, snippets)
            result["read_target"] = read_target
            return result
        return self._cached(key, 180, build)

    def _citing_intersection(self, targets: list[str], *, sort: str,
                             jurisdiction: str | None, kind: str | None,
                             offset: int, limit: int) -> dict:
        resolved: list[str] = []
        unknown: list[str] = []
        for target in dict.fromkeys(targets):
            held, _cand = self._resolve_held_id(target)
            if held:
                resolved.append(self.canonical_read_target(held)["stable_id"])
            else:
                unknown.append(target)
        resolved = list(dict.fromkeys(resolved))
        if unknown or len(resolved) < 2:
            return {"targets": targets, "mode": "intersection", "held_targets": resolved,
                    "unheld_targets": unknown,
                    "error": "every target must resolve to a distinct held document"}
        with self._open() as (cat, _rs, _ts):
            ids = cat.documents_citing_all(resolved)
            meta = cat.documents_meta(ids)
        want_j = self._norm_jurisdiction(jurisdiction)
        want_kind = self._KIND_ALIASES.get((kind or "").lower()) if kind else None
        rows: list[dict] = []
        for d in meta:
            j = self._doc_bucket(d.get("source", ""), d.get("court"))
            k = self._doc_kind(d.get("source", ""), d.get("doc_type", ""), d.get("court"))
            if want_j and j.lower() != want_j.lower():
                continue
            if want_kind and k != want_kind:
                continue
            rows.append({
                "stable_id": d["stable_id"], "title": d.get("title"),
                "court": self.court_label(d.get("court"), d.get("source")) if d.get("court") else None,
                "jurisdiction": j, "kind": k,
                "date": str(d.get("decision_date") or d.get("effective_date") or "")[:10] or None,
                "authority": d.get("pagerank") or 0, "cited_by_count": d.get("cited_by") or 0,
            })
        if sort == "newest":
            rows.sort(key=lambda r: (r.get("date") or "", r["stable_id"]), reverse=True)
        elif sort == "oldest":
            rows.sort(key=lambda r: (r.get("date") or "9999", r["stable_id"]))
        elif sort == "cited":
            rows.sort(key=lambda r: (r["cited_by_count"], r["authority"]), reverse=True)
        else:
            rows.sort(key=lambda r: (r["authority"], r["cited_by_count"]), reverse=True)
        total = len(rows)
        page = rows[offset:offset + limit]
        return {
            "targets": resolved, "mode": "intersection", "total": total,
            "offset": offset, "showing": [offset + 1 if page else 0, offset + len(page)],
            "has_more": offset + len(page) < total, "sort": sort,
            "jurisdiction": want_j, "kind": want_kind, "results": page,
            "count_note": "documents with resolved, non-inferred citations to every target",
        }

    def _citing_documents(self, held, anchor, sort, jurisdiction, kind, offset, limit,
                          snippets) -> dict:
        m = self.document_mentions(held, anchor=anchor, sort=sort, offset=offset,
                                   limit=limit, jurisdiction=jurisdiction, kind=kind,
                                   snippet_docs=limit if snippets else 0)
        doc = self.get_document(held).get("document", {}) or {}
        rows = []
        disputed = 0
        for g in m.get("groups", []):
            snip = None
            conflict = None
            cues: list[dict] = []
            if snippets and g.get("snippets"):
                s0 = g["snippets"][0]
                snip = {"where": s0.get("anchor"), "text": (s0.get("text") or "")[:320]}
                conflict = _cited_name_conflict(doc.get("title"), s0)
                if conflict:
                    disputed += 1
                cues = _treatment_cues(s0.get("text") or "")
            rows.append({k: v for k, v in {
                "stable_id": g["src_id"], "cite": g.get("src_oscola"),
                "court": g.get("src_court_label"), "jurisdiction": g.get("src_jurisdiction"),
                "kind": g.get("src_kind"), "date": str(g.get("src_date") or "")[:10] or None,
                "authority": g.get("authority"), "passages": g.get("count"),
                "cites_provisions": g.get("anchors"), "snippet": snip,
                # a row the reader should check before treating it as a citer
                "name_conflict": conflict,
                # verbatim cues, NOT a treatment classification — see _treatment_cues
                "treatment_cues": cues or None,
            }.items() if v is not None})
        total = m.get("total", 0)
        shown_to = offset + len(rows)
        # concrete, copy-pasteable next steps — the nudge that keeps an agent from getting
        # lost: how to page, how to narrow, how to re-sort, how to open a case, how to widen.
        nav: list[str] = []
        if m.get("has_more"):
            nav.append(f"More: call again with offset={shown_to} (showing {offset+1}-{shown_to} "
                       f"of {total}).")
        narrowable = [f["jurisdiction"] for f in m.get("facets", {}).get("jurisdiction", [])
                      if not jurisdiction][:6]
        if narrowable and total > limit:
            nav.append("Narrow by jurisdiction=" + " / ".join(repr(j) for j in narrowable[:5])
                       + " (ISO codes work too), or kind='cases'|'administrative'|'legislation'.")
        if not rows:
            if anchor and offset == 0:
                # nothing pins to THIS provision — the document may still have citers that
                # cite it as a whole, so say so rather than implying it is uncited
                nav.append(f"No citer pins specifically to {anchor!r}. Drop `anchor` to see "
                           "every document citing this instrument, or check the provision "
                           "label with lookup() / get_document_body().")
                # …but "none" must not be reported as a finding when the only reason is
                # that the pinpoints came from the carry-forward heuristic. Those are
                # excluded from this list on purpose (they are guesses, not citations),
                # and a reader who is not told cannot distinguish "no court has construed
                # this provision" from "the evidence is here but unverified".
                held_back = m.get("withheld_inferred_citers")
                if held_back:
                    nav.append(
                        f"BUT {held_back} document(s) DO pin to {anchor!r} through "
                        "carry-forward — a bare 'section N' attached to the last "
                        "instrument named nearby. Those are heuristic guesses, so they "
                        "are excluded here rather than shown as citations; treat this as "
                        "'unverified evidence exists', not as 'nothing engages this "
                        f"provision'. search_text() for the provision, or read the "
                        "candidates, to confirm.")
            elif (jurisdiction or kind) and m.get("facets"):
                have = ", ".join(f"{x['jurisdiction']} ({x['documents']})"
                                 for x in m["facets"].get("jurisdiction", [])[:6])
                nav.append(f"No citer matches that filter. Available: {have or '—'}. "
                           "Widen by dropping jurisdiction/kind.")
            else:
                nav.append("No documents in the corpus cite this yet.")
        nav.append("Re-sort with sort=" + "|".join(self.MENTION_SORTS))
        if anchor:
            # A pinpoint count cannot be read against the document count without this.
            # Lloyd v Google is cited by 47 documents; 73 of its 84 incoming edges carry
            # NO pinpoint at all. "1 citer of [138]" against "47 citers" invites the
            # reading that 46 courts considered the paragraph and declined to engage,
            # when in fact most never pinpointed anything.
            pinned = m.get("pinpointed_citers")
            unpinned = m.get("unpinpointed_citers")
            if unpinned:
                nav.append(
                    f"{unpinned} of this document's citers cite it with NO pinpoint, so "
                    f"they can never appear under any anchor; {pinned} pinpoint "
                    "something. A provision-level total is a count of pinpointed "
                    "citations, not of courts that engaged with the provision.")
        nav.append("Open any row with lookup(citation=<its stable_id>) or get_document(<stable_id>).")
        if disputed:
            nav.append(
                f"{disputed} row(s) carry `name_conflict`: the citing document writes a "
                "DIFFERENT case name beside the citation. Read the snippet before "
                "counting them as citers.")
        return {
            "target": held, "title": doc.get("title"), "oscola": self.get_document(held).get("oscola"),
            "provision": anchor,
            "is_floor": bool(anchor),
            "count_note": ("minimum evidenced count: only resolved citations carrying "
                           "this provision anchor are included" if anchor else None),
            "sort": sort, "sorts": dict(self.MENTION_SORTS),
            "jurisdiction": m.get("jurisdiction"), "kind": kind,
            "total": total, "offset": offset, "showing": [offset + 1 if rows else 0, shown_to],
            "has_more": m.get("has_more", False),
            "facets": m.get("facets"),
            "results": rows,
            "how_to_browse": nav,
        }

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

    # kind → the doc_type set _doc_kind() maps back to (for post-filtering title hits)
    _KIND_ALIASES = {"cases": "cases", "case": "cases", "caselaw": "cases",
                     "legislation": "legislation", "statute": "legislation",
                     "law": "legislation", "act": "legislation",
                     "guidance": "guidance", "administrative": "administrative",
                     "decision": "administrative", "dpa": "administrative"}

    # Words which do not make a failed descriptive query distinctive enough to widen
    # into an OR search.  The relaxed route is for party/place/fact discovery (common in
    # civil-law records whose titles are only ECLIs or docket numbers), not a pretence at
    # semantic question answering.
    _FIND_RELAX_STOP = {
        "and", "are", "article", "case", "cases", "court", "for", "from", "how",
        "law", "of", "on", "right", "rights", "rule", "rules", "section", "the",
        "to", "under", "what", "when", "where", "which", "who", "why", "access",
    }

    @classmethod
    def _relaxed_find_query(cls, query: str) -> str | None:
        """A conservative OR query for descriptive searches whose all-terms pass failed."""
        # Preserve explicit query-language intent: callers who used operators, quotes or
        # grouping asked for that shape and should not have it silently widened.
        if re.search(r'(?i)\b(?:AND|OR|NOT)\b|["“”()|&]', query):
            return None
        terms: list[str] = []
        for word in re.findall(r"[^\W_]+", query, flags=re.UNICODE):
            folded = word.casefold()
            if len(folded) < 3 or folded.isdigit() or folded in cls._FIND_RELAX_STOP:
                continue
            if folded not in {t.casefold() for t in terms}:
                terms.append(word)
        if len(terms) < 2:
            return None
        return " OR ".join(terms[:8])

    def find(self, query: str, *, k: int = 10, jurisdiction: str | None = None,
             kind: str | None = None, source: str | None = None,
             doc_type: str | None = None, tag: str | None = None,
             year_from: str | None = None) -> dict:
        """Locate documents by CITATION or TITLE — the reliable, embedding-independent way
        in. Two passes: (1) read the query AS A CITATION with the grammar and, if it
        resolves, hand you straight to that authority (call lookup() on it); (2) match your
        words against document TITLES / ids, filtered by jurisdiction (ISO code or name),
        kind ("cases" | "administrative" | "legislation" | "guidance"), court, year.

        It does NOT do concept/semantic search — that needs the embedding pass, which is
        incomplete on this corpus — so search by the NAME of a case or an act, or by a
        citation, NOT by a legal question. For a specific provision and who cites it, use
        lookup(citation, pincite=…) → citing_documents()."""
        q = (query or "").strip()
        if not q:
            return {"query": q, "error": "empty query",
                    "hint": "give a case name, an act title, or a citation"}
        out: dict = {"query": q}
        want_j = self._norm_jurisdiction(jurisdiction)
        # 1. citation pass — is this actually a citation? then resolution beats title match.
        held, cand = self._resolve_held_id(q)
        # …but never across the jurisdiction the caller asked for. Scoped to Ireland,
        # "Data Protection Act 2018" was still answered with a citation_match on the UK
        # Act — the one document the filter existed to exclude — sitting above the Irish
        # Act in the results below it.
        if want_j:
            scoped = self._held_instrument_titled(q, want_j)
            if scoped:
                held, cand = scoped, scoped
            elif held and self._bucket_of_id(held) != want_j:
                held = cand = None
        if cand:
            hit = {"candidate": cand, "held": held is not None}
            if held:
                d = self.get_document(held)
                hit.update({"stable_id": held, "title": (d.get("document") or {}).get("title"),
                            "oscola": d.get("oscola")})
                hit["next"] = f"lookup(citation={q!r}) — add pincite='<Article/section>' for a provision + its citers"
            else:
                hit["next"] = f"lookup(citation={q!r}) — it will fetch the authority if it can"
            out["citation_match"] = hit
        # 2. title pass — tokenised title/id match, jurisdiction/kind applied to the pool
        want_kind = self._KIND_ALIASES.get((kind or "").strip().lower()) if kind else None
        pool = max(k * 6, 60)
        # A collective citation names a body of law, so nothing is titled it — search for
        # what its members ARE titled (see _singularised_collective).
        titled = self._singularised_collective(q) or q
        if titled != q:
            out["searched_as"] = titled
        rows = self.list_documents(query=titled, source=source, doc_type=doc_type, tag=tag,
                                   year_from=year_from, limit=pool)
        results = []
        for r in rows:
            j = r.get("jurisdiction")
            kd = self._doc_kind(r.get("source", ""), r.get("doc_type", ""), r.get("court"))
            if want_j and (j or "").lower() != want_j.lower():
                continue
            if want_kind and kd != want_kind:
                continue
            results.append({
                "stable_id": r["stable_id"], "title": r.get("title"),
                "jurisdiction": j, "kind": kd, "court": r.get("court_label"),
                "date": str(r.get("decision_date") or "")[:10] or None,
                "doc_type": r.get("doc_type"), "source": r.get("source"),
            })
            if len(results) >= k:
                break
        out["results"] = results
        out["total_shown"] = len(results)
        # Civil-law sources often title a judgment only by docket/ECLI. If metadata
        # search has no route in, fall back to the indexed body: GDPRhub's translated
        # Facts/Holding text is where party names such as Uber and Ola live.
        if not results and "citation_match" not in out:
            fulltext = self.freetext_search(
                q, exact=False, limit=k,
                jurisdictions=[jurisdiction] if jurisdiction else None,
                doc_type=[kind] if kind else None, with_network=False,
            )
            if not fulltext.get("items"):
                relaxed = self._relaxed_find_query(q)
                if relaxed:
                    fulltext = self.freetext_search(
                        relaxed, exact=False, limit=k,
                        jurisdictions=[jurisdiction] if jurisdiction else None,
                        doc_type=[kind] if kind else None, with_network=False,
                    )
                    if fulltext.get("items"):
                        out["relaxed_query"] = relaxed
            for r in fulltext.get("items", []):
                results.append({
                    "stable_id": r["stable_id"], "title": r.get("title"),
                    "jurisdiction": r.get("jurisdiction"),
                    "kind": self._doc_kind(r.get("source", ""), r.get("doc_type", ""),
                                           r.get("court")),
                    "court": r.get("court_label"), "date": r.get("decision_date"),
                    "doc_type": r.get("doc_type"), "source": r.get("source"),
                    "match": "full_text", "snippet": r.get("snippet"),
                })
            out["total_shown"] = len(results)
            if results:
                out["search_route"] = (
                    "relaxed indexed body (ranked any-term match)"
                    if out.get("relaxed_query") else
                    "indexed body (title/citation search had no match)"
                )
        # honest note about what search can and can't do here
        with self._open() as (cat, _rs, _ts):
            semantic_on = cat.has_vector_index(self._provider().dimensions)
        out["how_search_works"] = (
            "Matches CITATIONS and document TITLES/ids first; when those find nothing, "
            "searches the indexed document body" +
            ("" if semantic_on else " (lexically — semantic embeddings are incomplete)") +
            ". For a provision and who cites it: lookup(citation, pincite=…) then "
            "citing_documents().")
        if not results and "citation_match" not in out:
            out["nothing_found"] = (
                "No title/citation match. Try fewer/among-title words, a party name, or a "
                "citation; or overview() to see what jurisdictions are held, then "
                "list_documents(source=…) to browse.")
        return out

    def list_documents(self, **filters) -> list[dict]:
        # SEARCHING collapses an instrument's point-in-time expressions to the one a
        # reader wants; BROWSING (no query) does not, because iterating a source or an
        # id prefix is how the versions themselves are reached. Callers that need every
        # version of a searched name can still say collapse_versions=False.
        filters.setdefault("collapse_versions", bool(filters.get("query")))
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
    # ``id_or`` is not user-supplied — search_corpus derives it from the query (alias hits).
    # Search result counts stop here; the UI shows "N+" past it (see search_corpus).
    _SEARCH_COUNT_CAP = 1000

    #: Grammars that recognise an instrument by its NAME rather than by an identifier.
    #: A name is not a key: Ireland and the UK both have a Data Protection Act 2018, and
    #: the gazetteer behind these grammars only holds the UK one.
    _NAME_GRAMMARS = frozenset({
        "uk_statute_named", "uk_act_section", "uk_si_short_name", "uk_si_acronym",
        "eu_named", "eu_named_full", "recital_eu_named", "recital_eu_named_full",
    })

    def _citation_query_ids(self, cat, query: str) -> tuple[list[str], bool]:
        """``(ids, exact)`` — the document id(s) this search text resolves to as a
        citation, and whether it did so by an IDENTIFIER rather than by a name.

        "[2011] IESC 26", an ECLI and a report citation are identifiers: they name one
        document, and matching it by primary key beats substring-scanning (the id slug
        omits the brackets, so the trigram OR would miss it). A statute short title is
        not. Searching "Data Protection Act 2018" resolved through the UK gazetteer to
        ``ukpga/2018/12`` and REPLACED the query with that id — so the Irish Act of the
        same name, held and titled with those exact words, could not be found at all.
        With ``exact`` false the caller keeps the text search and merely ranks the
        resolved document first.

        Empty for an ordinary keyword query, which falls through to the title search.
        """
        q = (query or "").strip()
        if not q:
            return [], False
        from .core.text import fold
        ids: list[str] = []
        exact = False
        dst, alias_source = cat.alias_with_source(fold(q))
        if dst:
            ids.append(dst)
            # An alias minted FROM A NAME is still a name. "data protection act 2018" is
            # in the table pointing at the UK Act — which is right for a UK document and
            # is why the alias exists — so believing it as an identifier hid Ireland's
            # Act from a search for the words in its own title. A report citation or a
            # retired id is an identifier and still short-circuits.
            exact = not str(alias_source or "").endswith("-name")
        try:
            from .citations import extract_citations
            for c in extract_citations(q):
                if c.candidate_id:
                    ids.append(c.candidate_id)  # the slug may itself be the stable_id
                    hit = cat.find_document_id(c.candidate_id)
                    if hit:
                        ids.append(hit)
                    exact = exact or c.method not in self._NAME_GRAMMARS
        except Exception:  # noqa: BLE001 — never let citation parsing break search
            pass
        return list(dict.fromkeys(i for i in ids if i)), exact

    #: A COLLECTIVE citation — "the Data Protection Acts", "the Data Protection Acts 1988
    #: to 2018", "the Companies Acts". Nothing is TITLED that, because it is the name of
    #: a body of law rather than of an instrument: section 1(2) of Ireland's Data
    #: Protection Act 2018 provides that it and the 1988 and 2003 Acts "may be cited
    #: together as the Data Protection Acts 1988 to 2018". Typed into search it matched
    #: nothing at all, not even as you typed — the plural noun is a substring of no
    #: title, and the year range belongs to no single Act.
    _COLLECTIVE_QUERY = re.compile(
        r"(?i)^\s*(?:the\s+)?(?P<stem>.+?)\s+"
        r"(?P<noun>Acts|Measures|Regulations|Orders|Rules)"
        r"(?:\s+(?:1[6-9]|20)\d{2}\s*(?:to|and|-|–|—)\s*(?:1[6-9]|20)\d{2})?\s*$")

    @classmethod
    def _singularised_collective(cls, query: str | None) -> str | None:
        """A collective citation reduced to the words its member Acts are titled with.

        "the Data Protection Acts 1988 to 2018" → "Data Protection Act", which every
        member's title contains. Returns None for anything that is not one, including
        the plural instrument nouns that ARE real titles ("Rules of the Superior
        Courts", "The Environmental Information Regulations 2004") — those keep a year
        or trailing words and never reach the end-anchored pattern with a bare noun.
        """
        m = cls._COLLECTIVE_QUERY.match(query or "")
        if not m or m.group("noun").lower() in ("regulations", "rules"):
            # Only Acts/Measures/Orders are collectively cited this way; "Regulations"
            # and "Rules" end far too many genuine instrument titles to be rewritten.
            return None
        return f"{m.group('stem').strip()} {m.group('noun')[:-1]}"

    def search_corpus(self, *, sort: str | None = None, limit: int = 50, offset: int = 0,
                      facets: bool = True, **filters) -> dict:
        """Unified metadata search: filtered, sortable results plus the facet distribution of
        the whole match set (counts per source / doc_type / court and a year histogram) so the
        sidebar can offer refine tick-boxes with live counts. Each result carries its OSCOLA
        citation and a cited-by count for display and 'most-cited' ranking.

        With no ``sort`` given, a query searches by RELEVANCE and a bare filter browse still
        goes newest-first. Date was the default for both, which meant a search ranked by
        when a document was published rather than by how well it matched what was typed.
        """
        jurisdiction = str(filters.pop("jurisdiction", "") or "").strip()
        f = {k: v for k, v in filters.items() if k in self._SEARCH_FILTERS and v not in (None, "")}
        if jurisdiction:
            # Use the same source/court bucketing as Explore itself. In particular, EU
            # one-stop-shop decisions live under an EU source but their dpa-xx court
            # token belongs to the named country. This remains an indexed IN filter and
            # avoids fetching a broad pool merely to discard it in Python.
            sources = [s for s in self._all_sources()
                       if self._jurisdiction_of(s).casefold() == jurisdiction.casefold()]
            court_codes = [c for c, name in self._DPA_COUNTRY.items()
                           if name.casefold() == jurisdiction.casefold()]
            courts = [f"{prefix}-{c}" for c in court_codes for prefix in ("dpa", "court")]
            f["source_or_court"] = (sources, courts)
        collective = self._singularised_collective(f.get("query"))
        if collective:
            f["query"] = collective
        if not sort:
            sort = "relevance" if str(f.get("query") or "").strip() else "date"
        boost: list[str] = []
        with self._open() as (cat, _rs, _ts):
            # Citation-format query ("[2011] IESC 26", an ECLI, a report cite) → resolve to
            # the exact document id(s) and match by PK, instead of substring-scanning (the
            # id slug omits the brackets, so the trigram OR would miss it). Ordinary keyword
            # queries resolve to nothing and fall through to the fast title/id/ECLI search.
            shorthand_for: dict[str, str] = {}
            if f.get("query"):
                short = cat.shorthand_matches(f["query"])
                short_ids = list(dict.fromkeys(r["candidate_id"] for r in short))
                for match in short:
                    shorthand_for.setdefault(match["candidate_id"], match["shorthand"])
                ids, exact = self._citation_query_ids(cat, f["query"])
                if ids and exact:
                    f = {k: v for k, v in f.items() if k != "query"}
                    f["id_in"] = ids
                else:
                    # …and a name the case is known by rather than titled with ("Dun &
                    # Bradstreet Austria") lives in the alias table: resolve it there and OR
                    # those documents into the title match, so the "also cited as" line is
                    # searchable, not just displayable.
                    alias_ids = cat.documents_by_alias_text(f["query"])
                    # An ABBREVIATION is the third way an authority gets named, and the
                    # one search could not follow: no statute is titled "CPIA". The
                    # corpus-wide shorthand store holds exactly those names — gated on
                    # several documents having independently agreed on each — so a typed
                    # abbreviation resolves to what practitioners mean by it, ranked
                    # ahead of the documents that merely contain the letters.
                    if short_ids or ids or alias_ids:
                        f["id_or"] = list(dict.fromkeys([*short_ids, *ids, *alias_ids]))
                    # Shorthand matches are intentional names and lead inferred title-name
                    # grammar hits; both lead ordinary substring results.
                    boost = list(dict.fromkeys([*short_ids, *ids]))
            rows = cat.search_documents(sort=sort, limit=limit, offset=offset,
                                        id_boost=boost, **f)
            items = []
            for r in rows:
                d = dict(r)
                d["oscola"] = _oscola_cite(r, _row_meta(r))
                # the retrieval-jurisdiction bucket, so result rows (and the hero
                # autocomplete) can show the same circular flag the explorer uses
                d["jurisdiction"] = self._doc_bucket(r["source"], r["court"])
                if r["stable_id"] in shorthand_for:
                    d["matched_shorthand"] = shorthand_for[r["stable_id"]]
                items.append(d)
            # Cap the exact total (a common-word match set is millions of rows; counting all
            # is the only slow part of an otherwise sub-second search). Beyond the cap the UI
            # shows "N+" via total_capped; an unfiltered browse still gets the exact count.
            cap = self._SEARCH_COUNT_CAP
            raw_total = cat.count_documents(cap=cap, **f)
            total_capped = raw_total > cap
            out = {"items": items, "total": min(raw_total, cap), "total_capped": total_capped,
                   "limit": limit, "offset": offset, "sort": sort}
            if facets:
                out["facets"] = cat.document_facets(**f)
            return out

    def corpus_facet_values(self) -> dict:
        """The available values for each advanced-search facet (sources, doc types, courts,
        tags) with counts — populates the field dropdowns / autocomplete.

        Cached stale-while-revalidate: each facet is a full GROUP BY over ~5M documents
        (seconds, and uncached it stampeded — a second explore-page pool-killer), so the
        request never blocks on it — a cold call returns the placeholder + ``_warming`` and
        a single background pass fills the cache."""
        rj = [{"key": k, "label": lb} for k, lb in RETRIEVAL_JURISDICTIONS]

        def _compute():
            with self._open() as (cat, _rs, _ts):
                return {
                    "sources": [{"key": k, "n": v} for k, v in cat._count_by("source").items()],
                    "doc_types": [{"key": k, "n": v} for k, v in cat._count_by("doc_type").items()],
                    "courts": [{"key": r["k"], "n": r["n"]} for r in cat.distinct_courts()],
                    "tags": [{"key": k, "n": v} for k, v in cat.tag_counts().items()],
                    "retrieval_jurisdictions": rj,
                }
        return self._cached("facet-values", 300, _compute, sync_wait=1.5,
                            placeholder={"sources": [], "doc_types": [], "courts": [],
                                         "tags": [], "retrieval_jurisdictions": rj})

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
                     "extracted_via": n.extracted_via, "authority": n.authority,
                     # One row per (document, relationship, direction); ``passages``
                     # says how many edges it stands for and ``anchor_pairs`` names
                     # them, so per-provision edges can be verified.
                     "passages": n.passages,
                     "anchor_pairs": [
                         {"src_anchor": src, "dst_anchor": dst}
                         for src, dst in n.anchor_pairs
                     ]}
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

    #: How many citing passages the cue scan reads. Bounded on purpose: this is a
    #: reconnaissance signal over the most authoritative citers, not a survey.
    _CUE_SCAN_CITERS = 25

    def _treatment_cue_rollup(self, stable_id: str) -> dict:
        """Which citing documents use language that says how they treated this one.

        Answers "how has this been received?" well enough to decide WHICH citers to
        read, which is the question a citator is asked and the one it has so far
        refused — the docstring's honest "treatment classification is not reliable"
        left the reader opening every citer by hand. The cues are quoted, attributed
        and never counted as holdings."""
        try:
            page = self.citing_documents(stable_id, sort="pagerank",
                                         limit=self._CUE_SCAN_CITERS, snippets=True)
        except Exception:  # noqa: BLE001 — a reconnaissance extra must never 500 the citator
            return {}
        rows = [r for r in (page.get("results") or []) if r.get("treatment_cues")]
        if not rows:
            return {}
        scanned = len(page.get("results") or [])

        def _row(r: dict, cues: list[dict]) -> dict:
            return {"stable_id": r["stable_id"], "cite": r.get("cite"),
                    "court": r.get("court"), "date": r.get("date"), "cues": cues,
                    "passage": (r.get("snippet") or {}).get("text")}

        treatment: list[dict] = []
        history: list[dict] = []
        signals: dict[str, int] = {}
        for r in rows:
            hist = [c for c in r["treatment_cues"] if c["signal"] in _HISTORY_SIGNALS]
            treat = [c for c in r["treatment_cues"] if c["signal"] not in _HISTORY_SIGNALS]
            for cue in treat:
                signals[cue["signal"]] = signals.get(cue["signal"], 0) + 1
            if treat:
                treatment.append(_row(r, treat))
            if hist:
                history.append(_row(r, hist))
        out: dict = {}
        if treatment:
            out["treatment_cues"] = {
                "scanned": scanned, "of_total": page.get("total", 0),
                "signals": dict(sorted(signals.items(), key=lambda kv: -kv[1])),
                "documents": treatment[:8],
                "caveat": (
                    "HEURISTIC, NOT A HOLDING. These are verbatim phrases found in the "
                    "citing passage, matched by pattern — the cue may belong to another "
                    "authority named in the same sentence, and 'distinguished' says "
                    "nothing about whether the distinction held. They tell you WHICH "
                    "citers to read, not how this authority stands. Read the passage."),
            }
        if history:
            out["subsequent_history_cues"] = {
                "scanned": scanned, "of_total": page.get("total", 0),
                "documents": history[:8],
                "caveat": (
                    "HEURISTIC. The corpus holds NO appellate edge — no harvested source "
                    "publishes one — so this is language spotted in citing passages "
                    "('on appeal from', 'reversed', 'permission to appeal refused'). "
                    "It can only ever find an appeal whose judgment is HELD and cites "
                    "this one; silence here is NOT evidence that a decision stands. "
                    "Open the named document to see what it actually did."),
            }
        return out

    def citator(self, stable_id: str) -> dict:
        """The "how does this authority stand" report an agent or the UI asks for
        first: citation volume + recency, network-authority percentile, the most
        significant citing documents, and (for legislation) version/effects state.
        Treatment CLASSIFICATIONS are deliberately ABSENT — the classifier isn't
        reliable enough to present Shepard's-style signals yet (design §6c caveat) —
        but the verbatim cues are not, because without them the only way to learn how
        an authority was received is to open every citer."""
        # Before the connection is taken: citing_documents opens its own.
        cue_rows = self._treatment_cue_rollup(stable_id)
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
                **cue_rows,
            }
            if doc["doc_type"] == "legislation":
                out["versions"] = [
                    {"version": v["version"], "archived_at": v["archived_at"]}
                    for v in cat.list_versions(stable_id)]
            return out

    # -- the agent's front door: resolve a citation, fetch it if we can, return it -----
    def lookup(self, *, citation: str, pincite: str | None = None, context: int = 1,
               cited_by: bool = True, similar: bool = True, autofetch: bool = True,
               full: bool = False, original: bool = False,
               outline_kind: str | None = None, as_at: str | None = None) -> dict:
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
        from .core.text import fold_citation
        from .resolve.matchers import first_candidate

        raw = (citation or "").strip()
        if not raw:
            return {"error": "empty citation"}
        # 0. a jurisdiction the caller SPELLED OUT ("Data Protection Act 2018 (Ireland)",
        # "the Irish Data Protection Act 2018") settles a statute-name collision that no
        # grammar can. Strip it before reading the citation, and hold it to correct the
        # answer if the grammar resolves the name somewhere else.
        raw, want_bucket = self._jurisdiction_qualifier(raw)
        # 1. resolve to a candidate id — the citation as written, an ECLI/CELEX, or a slug
        cand: str | None = None
        hits = extract_citations(raw)
        # the grammar often recovers a PINPOINT from the citation itself
        # ("Article 15 GDPR", "s. 45 of the DPA 2018"). Adopt it as the pincite when the
        # caller didn't pass one (and isn't asking for the full text) — so "who cites
        # Article 15" Just Works from one string, the thing agents kept failing to do.
        inferred_pin = hits[0].pinpoint if hits else None
        pincite_inferred = False
        if not pincite and not full and inferred_pin:
            pincite, pincite_inferred = inferred_pin, True
        if hits and hits[0].candidate_id:
            cand = hits[0].candidate_id
        if not cand:
            fc = first_candidate(raw)
            cand = fc.value if fc else None
        with self._open() as (cat, _rs, _ts):
            held_id = cat.find_document_id(cand) if cand else None
            if held_id is None:
                # Maybe the agent passed a stable_id straight through.  Do not require
                # slash/colon punctuation: dated consolidated CELEX ids are canonical
                # stable ids too (for example ``02005L0029-20220528``).  Check even
                # when a permissive citation grammar proposed another candidate: an
                # exact held stable id is stronger evidence.
                if cat.get_document(raw) is not None:
                    held_id, cand = raw, raw
            if held_id is None:
                # No candidate id doesn't mean no document. A classic law report
                # ("[1932] AC 562"), a case cited by name ("Donoghue v Stevenson") and a
                # retired surrogate id all yield candidate=None from the grammars — and
                # they are precisely the forms every importer mints ALIASES for. Without
                # this hop the whole alias table was unreachable from lookup: Donoghue
                # came back "not held" while the corpus held it twice over.
                probe = fold_citation(raw)
                held_id = cat.find_document_id(probe) if probe else None
        # 1b. the caller named a jurisdiction. Honour it: an Act title is not a global
        # key, and resolving "Data Protection Act 2018 (Ireland)" to the Westminster Act
        # is not a near miss, it is the wrong country's law. A held instrument of that
        # title in the wanted jurisdiction beats whatever the grammar proposed.
        if want_bucket:
            in_jurisdiction = self._held_instrument_titled(raw, want_bucket)
            if in_jurisdiction:
                if in_jurisdiction != held_id:
                    held_id, cand = in_jurisdiction, in_jurisdiction
            elif held_id and self._bucket_of_id(held_id) != want_bucket:
                held_id = None          # → the not-held answer, with its external links
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
        # 2b. an ID THAT NAMES A DATE ("…@2024-01-01") that we don't hold yet: the same
        # on-demand contract as any other authority new to the corpus.
        pit: dict = {}
        dated_ask = re.fullmatch(r"(.+)@(\d{4}-\d{2}-\d{2})", raw.strip())
        if held_id is None and dated_ask and autofetch:
            held_id, pit = self.point_in_time_target(
                dated_ask.group(1), dated_ask.group(2), autofetch=True)
            if not pit.get("as_at"):
                held_id = None                     # fetch failed — fall through to 3b
        # 3a. held → the rich answer
        if held_id:
            requested_held_id = held_id
            # A DATED ASK pins the read: never redirect it to today's consolidation,
            # which is the very text the caller said they did not want.
            if as_at and not pit:
                held_id, pit = self.point_in_time_target(
                    held_id, as_at, autofetch=autofetch)
            with self._open() as (cat, _rs, _ts):
                if (not original and not pit.get("as_at")
                        and not cat.consolidation_base_for(held_id)):
                    current = cat.applicable_consolidation(held_id)
                    if current:
                        held_id = current[0]
            answer = self._lookup_held(
                held_id, raw=raw, pincite=pincite, context=context,
                cited_by=cited_by, similar=similar, fetched=fetched,
                full=full, pincite_inferred=pincite_inferred,
                outline_kind=outline_kind,
            )
            if pit:
                answer["point_in_time"] = pit
                if pit.get("as_at"):
                    answer["note"] = (
                        f"Reading the text as it stood on {pit['as_at']}, not today's. "
                        "Amendments made after that date are NOT in this text.")
                elif pit.get("unavailable"):
                    # Loudly: a caller who asked for a date and silently got today's
                    # text would quote the wrong law with no way of knowing.
                    answer["note"] = (
                        f"THIS IS THE CURRENT TEXT, not the text as at "
                        f"{pit.get('requested')}: {pit['unavailable']}")
            if held_id != requested_held_id and not pit:
                answer["requested_stable_id"] = requested_held_id
                answer["canonical_read_redirected"] = True
                answer["note"] = (
                    f"Opened the latest consolidation applicable today ({held_id}) "
                    f"instead of the base act ({requested_held_id}). "
                    "Pass original=true to inspect the original/base text instead."
                )
            return answer
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
    @staticmethod
    def _struck_out_text(text: str | None) -> float:
        """How much of this text has been GUTTED, 0.0–1.0.

        legislation.gov.uk publishes a repealed provision as rows of dots, so the live
        consolidation of a wholly-repealed Act is a full-length document made almost
        entirely of ``. . . . . . .``. Everything about it reads healthy — held, 319
        segments, an outline, 1,914 citers — and the text says nothing at all. Measuring
        it is the only way to tell that apart from a document that simply is dot-heavy.
        """
        body = re.sub(r"\s+", "", text or "")
        if len(body) < 200:
            return 0.0
        return sum(ch in ".·…" for ch in body) / len(body)

    _STRUCK_OUT_RATIO = 0.4
    _LOOKUP_PREVIEW_CHARS = 2500
    _LOOKUP_FULL_CHARS = 48_000
    _LOOKUP_OUTLINE_LABELS = 120
    _LOOKUP_OUTLINE_ONE_KIND = 400

    @classmethod
    def _outline(cls, segments: list[dict], *, kind: str | None = None) -> dict:
        """The structural spine, with every KIND of provision represented.

        A flat head-of-list truncation is worthless on an EU act: taking the first 60
        labels of a 190-segment regulation returns Recitals 1–60 and never reaches an
        article, which is the one thing the outline exists to help you pincite. So the
        budget is shared between kinds in document order, the true per-kind totals are
        always reported, and one kind can be asked for in full.
        """
        spine = [
            (str(segment.get("label")), str(segment.get("kind") or "section"))
            for segment in segments
            if segment.get("label") and segment.get("kind") not in ("paragraph",)
        ]
        counts: dict[str, int] = {}
        for _label, segment_kind in spine:
            counts[segment_kind] = counts.get(segment_kind, 0) + 1
        wanted = (kind or "").strip().casefold() or None
        if wanted:
            labels = [label for label, k in spine if k.casefold() == wanted]
            return {
                "outline": labels[:cls._LOOKUP_OUTLINE_ONE_KIND],
                "outline_kind": wanted,
                "outline_counts": counts,
                "outline_truncated": len(labels) > cls._LOOKUP_OUTLINE_ONE_KIND,
            }
        budget = cls._LOOKUP_OUTLINE_LABELS
        if len(spine) <= budget:
            return {"outline": [label for label, _k in spine],
                    "outline_counts": counts, "outline_truncated": False}
        # An equal share per kind, with any unused share handed back to the rest — so a
        # document of two recitals and 190 articles still lists the articles.
        share = {k: 0 for k in counts}
        remaining, kinds = budget, [k for k in counts]
        while remaining > 0 and kinds:
            slice_size = max(1, remaining // len(kinds))
            for segment_kind in list(kinds):
                take = min(slice_size, counts[segment_kind] - share[segment_kind], remaining)
                share[segment_kind] += take
                remaining -= take
                if share[segment_kind] >= counts[segment_kind]:
                    kinds.remove(segment_kind)
                if remaining <= 0:
                    break
        taken: dict[str, int] = {k: 0 for k in counts}
        labels: list[str] = []
        for label, segment_kind in spine:
            if taken[segment_kind] < share[segment_kind]:
                taken[segment_kind] += 1
                labels.append(label)
        return {
            "outline": labels,
            "outline_counts": counts,
            "outline_truncated": True,
            "outline_note": (
                "sampled across kinds so every kind is represented; pass "
                "outline_kind='article' (or 'recital', …) for one kind in full"),
        }

    def _lookup_held(self, held_id: str, *, raw: str, pincite: str | None, context: int,
                     cited_by: bool, similar: bool, fetched: bool, full: bool = False,
                     pincite_inferred: bool = False,
                     outline_kind: str | None = None) -> dict:
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
            "original_act": doc.get("original_act"),
            "inherited_recitals": doc.get("inherited_recitals"),
        }
        # text: the pincited passage (+ context scale), a capped full read, or — by
        # default — a short preview plus the structural outline, so the agent decides what
        # to pull rather than paying for the whole document up front.
        if pincite:
            out["pincite"] = pincite
            if pincite_inferred:
                out["pincite_inferred"] = True  # taken from the citation string itself
                out["note"] = (f"Pinpointed to {pincite!r} (read from your citation). For the "
                               "whole instrument instead, pass full=true or omit the "
                               "article/section from the citation.")
            out["passage"] = self.get_provision(held_id, label=pincite, context=context)
        else:
            body = self.document_body(held_id)
            text = body.get("text") or ""
            segs = body.get("segments") or []
            inherited_recitals = body.get("inherited_recitals")
            held_recitals = [s for s in segs
                             if str(s.get("kind") or "").lower() == "recital"]
            if inherited_recitals:
                out["recitals"] = {"status": "held_via_base_act",
                                   "count": inherited_recitals.get("count")}
            elif held_recitals:
                out["recitals"] = {"status": "held", "count": len(held_recitals)}
            elif held_id.startswith("european/"):
                out["recitals"] = {
                    "status": "not_held",
                    "note": ("This assimilated-EU instrument's operative UK text is held, "
                             "but its recitals are absent from the source rendition. Absence "
                             "here is a corpus holding gap, not a statement that the "
                             "instrument has no recitals."),
                }
            elif d.get("doc_type") == "legislation":
                out["recitals"] = {"status": "not_part_of_instrument"}
            out["segment_count"] = len(segs)
            # A repealed Act's live text is struck out at source, so "held: true" with a
            # healthy segment count is not a promise that anything can be READ. Say so,
            # and point at the versions that still carry the words.
            struck = self._struck_out_text(text)
            if struck >= self._STRUCK_OUT_RATIO:
                out["text_available"] = False
                out["struck_out_ratio"] = round(struck, 2)
                out["text_note"] = (
                    "The held text is the CURRENT version of an instrument whose "
                    "provisions have been repealed, so the source publishes them struck "
                    "out — this document is mostly rows of dots, not law. Call "
                    "legislative_status() for what repealed it, and read a point-in-time "
                    "version (list_versions / an @YYYY-MM-DD id) for the words as they "
                    "stood.")
            if inherited_recitals:
                # Keep the original act's immutable preamble separate from the
                # consolidated expression text, but make it fully discoverable to
                # agents and available through get_provision("Recital N").
                out["inherited_recitals"] = {
                    key: inherited_recitals.get(key)
                    for key in (
                        "count", "source_stable_id", "source_title",
                        "source_url", "base_stable_id", "source_is_base_act",
                        "unchanged", "virtual", "note",
                    )
                }
                out["recital_outline"] = [
                    segment.get("label")
                    for segment in inherited_recitals.get("segments", [])
                ]
                if full:
                    out["recitals_text"] = inherited_recitals.get("text")
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
                out.update(self._outline(segs, kind=outline_kind))
                out["how_to_read"] = ("preview only — pass pincite='<label>' for one "
                                      "provision (with context 0/1/2), or full=true for the "
                                      "whole text")
        if cited_by:
            if pincite:
                # PROVISION-SCOPED: who cites exactly this article/section — the thing an
                # agent researching "cases on Article 15" actually wants, and a browse
                # handle so it can page/filter/sort the rest without losing its place.
                cd = self.citing_documents(held_id, anchor=pincite, sort="pagerank",
                                           limit=6, snippets=True)
                out["citing"] = {
                    "provision": pincite,
                    "total": cd.get("total", 0),
                    "is_floor": True,
                    "count_note": cd.get("count_note"),
                    "facets": cd.get("facets"),
                    "top": cd.get("results", []),
                    "browse": {"tool": "citing_documents",
                               "args": {"target": held_id, "anchor": pincite}},
                    "hint": (f"{cd.get('total', 0)} document(s) in the corpus cite {pincite}. "
                             f"Browse / filter / sort them with "
                             f"citing_documents(target='{held_id}', anchor='{pincite}', "
                             f"sort='newest'|'cited'|…, jurisdiction='fr'|…) — re-call it "
                             f"anytime to return to this list."),
                }
            else:
                cit = self.citator(held_id)
                out["cited_by"] = {"stats": cit.get("cited_by"),
                                   "significant": cit.get("most_significant_citors", [])[:8]}
                out["citing"] = {
                    "browse": {"tool": "citing_documents", "args": {"target": held_id}},
                    "hint": ("For who cites a SPECIFIC provision, pincite it — e.g. "
                             f"lookup(citation='{raw}', pincite='Article 15') — or call "
                             f"citing_documents(target='{held_id}', anchor='Article 15'). "
                             "Browse all citers with citing_documents(target='"
                             f"{held_id}')."),
                }
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
        # Whether search_text() can SEE each jurisdiction, on the same row as how much of
        # it is held. Holding 2.9M French documents and indexing none of them are two
        # different facts, and an agent that read only the first ran a full-text search,
        # got nothing, and concluded the corpus lacked the material — a cycle a single
        # field ends. Measured against the INDEX, not the gate setting: an empty gate
        # means "no explicit narrowing", not "nothing indexed". Cached, because the
        # aggregate behind it takes ~2s and this is an orientation call.
        # sync_wait, and "unknown" rather than a placeholder, because a cold cache
        # answering "indexed: no" for every jurisdiction is not a missing field — it is
        # the false claim this exists to prevent.
        coverage = self._cached("fts-jurisdiction-coverage", 600,
                                self.freetext_index_summary,
                                placeholder={"jurisdictions": [], "_unknown": True},
                                sync_wait=4.0)
        coverage_known = not coverage.get("_unknown")
        indexed_docs: dict[str, int] = {
            row["jurisdiction"]: row["documents"]
            for row in coverage.get("jurisdictions", [])}
        rows = []
        for j in shape.get("jurisdictions", []):
            total = j.get("total", 0)
            if total < 1:
                continue
            n_indexed = indexed_docs.get(j["jurisdiction"], 0)
            rows.append({
                "jurisdiction": j["jurisdiction"],
                "held": {"cases": j.get("cases", 0), "legislation": j.get("legislation", 0),
                         "guidance": (j.get("guidance", 0) or 0) + (j.get("administrative", 0) or 0)},
                "total": total,
                # yes | partial | no — what search_text() will find here. "no" is the one
                # that matters: held, and invisible to full-text search.
                "full_text_indexed": ("unknown" if not coverage_known
                                      else "no" if not n_indexed
                                      else "yes" if n_indexed >= total * 0.9
                                      else "partial"),
                "full_text_documents": n_indexed if coverage_known else None,
                "fetch_on_demand": sorted(fetch.get(j["jurisdiction"], [])),
            })
        rows.sort(key=lambda r: -r["total"])
        # Head of the distribution only: an agent orienting itself wants the jurisdictions
        # the corpus is actually deep in, not a long tail of one-offs. Keep those carrying a
        # meaningful share (≥1% of the corpus, or ≥250 docs), but never fewer than the top 6
        # nor more than 15; fold the rest into one summary line so nothing is hidden.
        total_docs = shape.get("total", 0) or sum(r["total"] for r in rows)
        cutoff = max(250, int(total_docs * 0.01))
        head = [r for r in rows if r["total"] >= cutoff]
        head = rows[:6] if len(head) < 6 else head[:15]
        tail = rows[len(head):]
        out: dict = {"jurisdictions": head, "total_documents": total_docs,
                     "warming": bool(shape.get("_warming")),
                     "note": "Main jurisdictional coverage, deepest first, with held density "
                             "(cases / legislation / guidance). full_text_indexed says whether "
                             "search_text() can see a jurisdiction at all — 'no' means held but "
                             "invisible to full-text search, reachable only by citation/title "
                             "(search_coverage() has the per-source detail). fetch_on_demand "
                             "lists adapters that can pull MORE on demand; lookup() fetches "
                             "silently where it can. Search is by citation/title "
                             "(find/lookup), not concept."}
        if tail:
            out["other_jurisdictions"] = {
                "count": len(tail), "documents": sum(r["total"] for r in tail),
                "names": [r["jurisdiction"] for r in tail],
                "note": "smaller holdings — query them directly by name/citation via find()/lookup()",
            }
        return out

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
            from .core.segmentation import recover_numbered_segments
            segs, _synthesised = recover_numbered_segments(
                text, ts.get_segments(doc["payload_hash"]))
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
            if idx < 0 and label and re.match(r"^\s*recitals?\b", label, re.I):
                inherited = self._inherited_recitals(
                    cat, ts, stable_id, include_citations=False)
                if inherited:
                    from types import SimpleNamespace

                    virtual_segments = [
                        SimpleNamespace(**{
                            key: segment[key]
                            for key in ("label", "kind", "level",
                                        "char_start", "char_end")
                        })
                        for segment in inherited["segments"]
                    ]
                    virtual_idx = _match_segment(virtual_segments, label)
                    if virtual_idx >= 0:
                        lo = max(0, virtual_idx - context)
                        hi = min(
                            len(virtual_segments), virtual_idx + context + 1)
                        virtual_text = inherited["text"]
                        return {
                            "stable_id": stable_id,
                            "title": doc["title"],
                            "segments": [
                                {
                                    "label": segment.label,
                                    "kind": segment.kind,
                                    "level": segment.level,
                                    "char_start": segment.char_start,
                                    "char_end": segment.char_end,
                                    "focus": i == virtual_idx,
                                    "text": virtual_text[
                                        segment.char_start:segment.char_end
                                    ].strip(),
                                    "inherited": True,
                                }
                                for i, segment in enumerate(
                                    virtual_segments[lo:hi], start=lo)
                            ],
                            "path": ["Inherited unchanged recitals"],
                            "inherited_recitals": {
                                key: inherited[key]
                                for key in (
                                    "source_stable_id", "source_title",
                                    "source_url", "base_stable_id",
                                    "source_is_base_act", "unchanged", "virtual",
                                    "note",
                                )
                            },
                        }
            if idx < 0 and segs:
                missing = {"error": "no matching segment", "stable_id": stable_id,
                           "requested": label, **_label_help(segs)}
                if label and re.match(r"^\s*recitals?\b", label, re.I):
                    if stable_id.startswith("european/"):
                        missing["recitals"] = {
                            "status": "not_held",
                            "note": ("The assimilated-EU instrument's recitals are absent "
                                     "from this source rendition; this is a corpus holding "
                                     "gap, not a statement that the instrument has none."),
                        }
                    elif doc["doc_type"] == "legislation":
                        missing["recitals"] = {"status": "not_part_of_instrument"}
                return missing
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
            out = {"stable_id": stable_id, "title": doc["title"],
                   "oscola": _oscola_cite(doc, _row_meta(doc)),
                   "segments": out_segs, "path": list(reversed(path))}
            note = _subdivision_note(label, segs[idx], text)
            if note:
                out["anchor_exact"] = False
                out["anchor_note"] = note
            return out

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
        (("it-",), "Italy"),
        (("ie-", "eisb"), "Ireland"),
        (("au-",), "Australia"),
        (("ca-",), "Canada"),
        (("nz-",), "New Zealand"),
        (("sg-",), "Singapore"),
        (("hk-",), "Hong Kong"),
        (("in-",), "India"),
        (("us-",), "United States"),
        # Imported corpora that have no adapter and so no SourceInfo to read a
        # jurisdiction from. Without these two they are the only real material still
        # filed under "Other", which is a worse answer than either name.
        (("ci-",), "Channel Islands"),
        (("offshore-",), "Offshore & int'l commercial"),
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
        "uk-ico-enforcement": "ICO enforcement", "uk-ico-audits": "ICO audits",
        "uk-ico-consultations": "ICO consultations", "uk-ico-guidance": "ICO guidance",
        "eu-cellar": "EUR-Lex (CJEU)", "eu-legislation": "EUR-Lex",
        "eu-preparatory": "EUR-Lex (EU preparatory & Commission policy documents)",
        "eu-consumer-guidance": "European Commission consumer guidance",
        "edpb": "EDPB", "edpb-oss": "EDPB one-stop-shop", "a29wp": "Article 29 WP",
        "dma-cases": "DMA case register", "echr": "HUDOC (ECtHR)",
        "nl-rechtspraak": "Rechtspraak.nl", "nl-legislation": "wetten.overheid.nl",
        "nl-acm-guidance": "ACM", "it-agcm": "AGCM",
        "uk-cma": "Competition and Markets Authority",
        "uk-cma-guidance": "Competition and Markets Authority",
        "uk-govuk-policy": "GOV.UK",
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
        # Harvestable-source names have one canonical home. This keeps document/search
        # payloads aligned with /sources/catalog, MCP, Backfill, and Keep Current instead
        # of letting this legacy short-label table drift from the adapter registry.
        try:
            from .adapters.registry import SOURCE_INFO
            info = SOURCE_INFO.get(source)
            if info is not None:
                return info.label
        except Exception:  # noqa: BLE001 — display fallback must never break a reader
            pass
        if source in self._SOURCE_LABELS:
            return self._SOURCE_LABELS[source]
        # A manual import declares its jurisdiction in its source key (uk-user-import),
        # so name it that way rather than letting the prettifier say "UK User Import".
        from .imports.service import jurisdiction_of_source

        code = jurisdiction_of_source(source)
        if code:
            named = dict(_IMPORT_JURISDICTION_LABELS).get(code, code.upper())
            return f"Manual imports ({named})"
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
        # Regulators and apex courts a source stores by ACRONYM rather than a citation
        # court code. They never reach the citation registry (it keys on neutral-citation
        # codes), so without this the UI shows the bare acronym — a live audit of all 846
        # stored court values found exactly these eight.
        if up_name := self._BODY_NAMES.get((code or "").strip().upper()):
            return up_name
        # A French court name arrives in whatever case the register typed it — "COUR
        # ADMINISTRATIVE D'APPEL DE LYON" beside "Cour administrative d'appel de Lyon".
        # Shouting is a data artefact, not a name, so it is cased for display.
        if len(code or "") > 6 and code.isupper():
            return _sentence_case_court(code)
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
        # German courts are stored under their full German name, which the prettifier at
        # the bottom of this method mangles: it splits on hyphens, so every court of a
        # hyphenated Land came out as "Oberverwaltungsgericht Nordrhein Westfalen". The
        # table also canonicalises an ECLI court token and the register's own slug, so a
        # decision stored under either shows the court's name (see citations/de_courts).
        if (source or "").lower().startswith("de-") or _re.search(r"[gG]ericht|[gG]erichtshof", code or ""):
            from .citations.de_courts import court_name as _de_court_name

            de_name = _de_court_name(code)
            if de_name:
                return de_name
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
        """The bucket a source's documents are filed under on Explore and in search.

        Read from the registry first, because ``SourceInfo`` is where a source states its
        jurisdiction and ``adapter-authoring.md`` says that is the only place it may be
        stated. The prefix table below is the fallback for the legacy keys that predate
        the registry (``bailii``, ``westlaw``, ``hol``, ``ico``…) and is no longer the
        thing a new source has to be added to.

        It used to be the only lookup, and every source whose key did not begin with one
        of fifteen hardcoded prefixes fell through to "Other" — which on 2026-08-14 meant
        358,000 documents, including the whole of Finland, Estonia, Slovakia, Austria and
        Sweden, sitting in a bucket named after not knowing what they were. Each of those
        had declared its jurisdiction correctly in its ``SourceInfo`` all along.
        """
        s = (source or "").lower()
        registered = _registered_jurisdictions().get(s)
        if registered:
            return registered
        for prefixes, label in self._JURISDICTIONS:
            if any(s.startswith(p) or s == p.rstrip("-") for p in prefixes):
                return label
        return "Other"

    # Bodies stored by acronym rather than a neutral-citation court code (see
    # court_label). Extend as adapters introduce new ones — the convention is that every
    # court/body value a document can carry must have a name here or in the citation
    # registry, because the UI never shows a raw code.
    _BODY_NAMES = {
        "CNIL": "Commission nationale de l’informatique et des libertés (CNIL)",
        "CCPC": "Competition and Consumer Protection Commission",
        "CMA": "Competition and Markets Authority",
        "EDPS": "European Data Protection Supervisor",
        "BGH": "Bundesgerichtshof (Federal Court of Justice)",
        "BFH": "Bundesfinanzhof (Federal Fiscal Court)",
        "BAG": "Bundesarbeitsgericht (Federal Labour Court)",
        "BVerwG": "Bundesverwaltungsgericht (Federal Administrative Court)",
        "BSG": "Bundessozialgericht (Federal Social Court)",
        "BVerfG": "Bundesverfassungsgericht (Federal Constitutional Court)",
        "BE-MARKET-COURT": "Marktenhof / Cour des marchés",
    }

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
    # Regulators and EU bodies whose output is ADMINISTRATIVE DECISIONS — a kind of its
    # own, not case law and not guidance. Extend as bodies join (Scottish Information
    # Commissioner, state privacy commissioners…).
    #
    # A regulator's decision carries doc_type "decision", which the case-type check would
    # otherwise read as case law: before this list grew, a live audit found 3,750 documents
    # filed as case law that are nothing of the kind — ESMA sanctions, Commission antitrust
    # and DMA decisions, CCPC merger determinations, CMA cases, FCA final notices, EDPB
    # binding decisions and Art 64 opinions, and three EU appeal bodies. They now answer the
    # "administrative decisions" filter, where a lawyer looking for them actually looks.
    _ADMIN_SOURCES = {
        "edpb-oss", "ofcom-enforcement", "ico", "uk-ico-enforcement", "ie-dpc",
        # EU bodies: the Board itself (binding decisions + Art 64 opinions), the
        # Commission's competition/DMA registers, the sectoral appeal panels, the Ombudsman
        "edpb", "eu-dgcomp-antitrust", "dma-cases", "eu-esma-sanctions",
        "eu-esas-boa", "eu-srb-appeals", "eu-ombudsman",
        # national competition / financial regulators
        "ie-ccpc-mergers", "uk-cma", "uk-fca-notices", "it-agcm",
    }
    # Mixed bulk sources need a body-level discriminator: ``fr-dila`` carries courts,
    # legislation, constitutional decisions *and* CNIL deliberations under one source
    # key.  CNIL is a regulator, not a court, so its decisions belong beside other DPA
    # administrative decisions even though DILA stores them with doc_type ``decision``.
    _ADMIN_COURTS = {"cnil"}
    # The only source whose PREPARATORY documents are legislative travaux (Commission
    # proposals, impact assessments) rather than reports. Everything else that files as
    # preparatory — a Law Commission report, a Scottish Law Commission paper — is a REPORT,
    # and belongs with guidance under "Guidance/Reports" rather than in a category a reader
    # would never think to open.
    _TRAVAUX_SOURCES = {"eu-preparatory"}
    # Whole-register policy/advisory collections whose source vocabulary happens to use
    # legal-sounding raw types such as ``opinion`` and ``decision``. BEREC opinions and
    # common positions are regulatory publications, not judgments; source identity must
    # win over the generic opinion→case-law fallback used for Advocate General opinions.
    _GUIDANCE_SOURCES = {"eu-berec"}
    # What a regulator DECIDES (as opposed to writes about): the doc types that make a
    # document from an admin body an administrative decision.
    _ADMIN_DOC_TYPES = {"decision", "opinion", "notice"}

    # A DPA the corpus knows by its proper name — shown instead of the generic
    # "Data protection authority · <country>". The `iedpc` court code (BAILII's
    # Irish DPC case studies) is canonicalised to `dpa-ie` at write time
    # (Catalogue._COURT_CANON), so this one label covers both intake paths.
    _DPA_PROPER_NAME = {"ie": "Data Protection Commission (Ireland)"}

    # How a citation names the country whose law it means. Ireland and the United
    # Kingdom re-enacted each other's statute book under the same short titles — both
    # have a Data Protection Act 2018, commenced within a fortnight of each other, both
    # implementing the GDPR — so "Data Protection Act 2018" alone cannot be resolved
    # correctly by any grammar, and a caller who writes the country has answered the
    # only question that matters.
    # Tried in order: a trailing bracket wins over a leading adjective, or "Data" in
    # "Data Protection Act 2018 (Ireland)" is read as the qualifier and Ireland is lost.
    _JURISDICTION_QUALIFIERS = (
        re.compile(r"(?i)\s*[(\[]\s*(?P<w>[A-Za-z. ]{2,30}?)\s*[)\]]\s*$"),  # "… (Ireland)"
        re.compile(r"(?i)^\s*(?P<w>[A-Za-z.]{2,20})\s+(?=\S)"),        # "Irish … Act 2018"
    )
    #: Adjectives and codes a citation uses for a jurisdiction, → the display bucket.
    _JURISDICTION_WORDS = {
        "ireland": "Ireland", "irish": "Ireland", "ie": "Ireland", "irl": "Ireland",
        "roi": "Ireland", "eire": "Ireland", "éire": "Ireland",
        "uk": "United Kingdom", "u.k.": "United Kingdom", "gb": "United Kingdom",
        "british": "United Kingdom", "england": "United Kingdom",
        "english": "United Kingdom", "scotland": "United Kingdom",
        "scottish": "United Kingdom", "westminster": "United Kingdom",
        "eu": "European Union", "european": "European Union",
        "australia": "Australia", "australian": "Australia", "au": "Australia",
        "cth": "Australia", "canada": "Canada", "canadian": "Canada",
        "ca": "Canada", "nz": "New Zealand", "singapore": "Singapore",
        "sg": "Singapore", "india": "India", "indian": "India",
    }

    @classmethod
    def _jurisdiction_qualifier(cls, raw: str) -> tuple[str, str | None]:
        """``(citation without the country, the bucket it named)``.

        Only strips a word it RECOGNISES as a jurisdiction, so "(Amendment)" in a short
        title and "Roads Act 1993" keep their own words.
        """
        for pattern in cls._JURISDICTION_QUALIFIERS:
            m = pattern.search(raw)
            bucket = cls._JURISDICTION_WORDS.get(
                m.group("w").strip().lower()) if m else None
            if bucket:
                return (raw[:m.start()] + raw[m.end():]).strip(" ,;"), bucket
        return raw, None

    def _bucket_of_id(self, stable_id: str) -> str | None:
        doc = self.get_document(stable_id).get("document") or {}
        if not doc:
            return None
        return self._doc_bucket(doc.get("source", ""), doc.get("court"))

    def _held_instrument_titled(self, raw: str, bucket: str) -> str | None:
        """The held legislation of THIS jurisdiction whose short title the caller wrote.

        Matches the normalised short title exactly — a citation is a name, not a search
        — after discarding any provision prefix ("section 117 of the …") and the
        consolidation suffix an administrative revision carries ("(revised to …)").
        Where several renditions share the title the newest wins, which is the text the
        law currently is.
        """
        from .citations.stage import _statutory_short_title
        from .citations.statute_gazetteer import normalise_title, reference_key

        want = reference_key(raw)
        if not want:
            return None
        best: str | None = None
        for row in self.list_documents(query=raw, doc_type="legislation", limit=60):
            title = _statutory_short_title(str(row.get("title") or ""))
            if normalise_title(title) != want:
                continue
            if self._doc_bucket(row.get("source", ""), row.get("court")) != bucket:
                continue
            sid = str(row["stable_id"])
            if best is None or best < sid:
                best = sid
        return best

    def _doc_bucket(self, source: str, court: str | None) -> str:
        c = (court or "").lower()
        if c.startswith(("dpa-", "court-")):
            return self._DPA_COUNTRY.get(c.split("-", 1)[1], "European Union")
        return self._jurisdiction_of(source)

    @staticmethod
    def _pending_flags(meta: dict | None) -> dict:
        """``{"pending": …, "preliminary": …}`` for ``_doc_kind`` from a document's own
        metadata bag — the row-level form of the flags ``citing_breakdown`` computes in
        SQL. ``pending`` false once the notice is retired, so a resolved reference goes
        back to being ordinary EU material."""
        meta = meta or {}
        pending = bool(meta.get("pending"))
        return {"pending": pending,
                "preliminary": pending and Facade._is_preliminary(
                    meta.get("pending_procedure"))}

    @staticmethod
    def _pending_meta(row, meta: dict | None) -> dict:
        """The display facts about a live notice (proceeding, case number, referring
        court) — empty for everything else, so callers can splat it unconditionally."""
        meta = meta or {}
        if not meta.get("pending"):
            return {}
        from .adapters.eu_cellar import celex_case_number
        courts = meta.get("referring_courts") or []
        return {
            "pending": True,
            "pending_proceeding": meta.get("pending_proceeding"),
            "pending_procedure": meta.get("pending_procedure"),
            "case_number": celex_case_number(meta.get("celex") or row["stable_id"]),
            "referring_court": courts[0] if courts else None,
            "origin_country": meta.get("origin_country"),
        }

    def _doc_kind(self, source: str, doc_type: str, court: str | None, *,
                  pending: bool = False, preliminary: bool = False) -> str:
        # A LIVE CJEU application notice first. It is not a decision of anything — it is
        # a question put to the Court, and reading a statute the difference matters more
        # than any other distinction on the page: "12 references pending on Article 22"
        # is tomorrow's law, whereas the same notices filed under "other EU material"
        # (where doc_type "note" landed them) said nothing at all. A genuine Article 267
        # reference is kept apart from the other pending proceedings — an annulment
        # action is also pending, but it is not a question about interpretation.
        if pending:
            return "preliminary_references" if preliminary else "pending_cases"
        # GUIDANCE wins first: a regulator's guidance is guidance, not an
        # "administrative decision", even though it comes from an admin source (ICO,
        # EDPB) — otherwise guidance never appears as its own filter category.
        if doc_type == "guidance":
            return "guidance"
        if doc_type == "preparatory":
            return "preparatory" if source in self._TRAVAUX_SOURCES else "guidance"
        if doc_type == "note" and source == "uk-legislation-materials":
            return "explanatory"
        if source in self._GUIDANCE_SOURCES:
            return "guidance"
        # then an administrative body's DECISIONS (a DPA decision, an enforcement
        # notice) — before the case-type check, since those carry doc_type "decision".
        # A data-protection authority's whole output is administrative whatever it is
        # filed as (BAILII's Irish DPC case studies arrive as "judgment"); for the other
        # regulators only the DECIDING documents count, so an EDPB commentary stays
        # commentary rather than being announced as a decision.
        court_key = (court or "").lower()
        if court_key.startswith("dpa-") or court_key in self._ADMIN_COURTS:
            return "administrative"
        if source in self._ADMIN_SOURCES and doc_type in self._ADMIN_DOC_TYPES:
            return "administrative"
        if doc_type in self._CASE_TYPES:
            return "cases"
        if doc_type == "legislation":
            return "legislation"
        return "other"

    # ISO 3166 alpha-2 (and a few common aliases) → the natural-language jurisdiction
    # bucket _doc_bucket() emits, so an agent can filter by "fr"/"gb"/"eu" instead of
    # having to know the exact display string. The DPA countries come for free from
    # _DPA_COUNTRY (already code→name); the majors + Council-of-Europe alias are added.
    @classmethod
    def _iso_jurisdiction(cls) -> dict[str, str]:
        m = {
            "gb": "United Kingdom", "uk": "United Kingdom",
            "eu": "European Union", "eec": "European Union",
            "coe": "Council of Europe", "echr": "Council of Europe",
            "fr": "France", "de": "Germany", "nl": "Netherlands", "ie": "Ireland",
            "au": "Australia", "ca": "Canada", "nz": "New Zealand", "sg": "Singapore",
            "hk": "Hong Kong", "in": "India", "us": "United States", "usa": "United States",
        }
        m.update({code: name for code, name in cls._DPA_COUNTRY.items()})
        return m

    def _norm_jurisdiction(self, arg: str | None) -> str | None:
        """Accept an ISO country code ("fr", "gb", "eu"), a known alias, or the natural-
        language jurisdiction name itself (any case) and return the canonical bucket name
        used across the citing/facet machinery. Returns None when it can't be mapped — the
        caller then reports the available facet names rather than silently filtering to
        nothing."""
        if not arg:
            return None
        a = arg.strip()
        if not a:
            return None
        iso = self._iso_jurisdiction()
        if a.lower() in iso:
            return iso[a.lower()]
        # exact (case-insensitive) match against a known display name
        names = {name.lower(): name for name in {*iso.values(),
                 *(lb for _pref, lb in self._JURISDICTIONS)}}
        return names.get(a.lower(), a)  # unknown → pass the raw string through for a later match

    def corpus_shape(self) -> dict:
        """The Explore homepage's data: the whole corpus's shape in one payload —
        per JURISDICTION (bucketed from sources): document counts split by kind,
        the year distribution (a sparkline per row), text/embedding coverage,
        citation density, top courts, and the most authoritative documents
        (PageRank). Drill-down targets are ids, not prefilled searches — the UI
        expands in place. Heavy aggregates → stale-while-revalidate cached."""
        return self._cached("corpus-shape", 600, self._corpus_shape_uncached,
                            placeholder={"jurisdictions": [], "total": 0,
                                         "stats_refreshed_at": None})

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
            # When the roll-ups these figures come from were last rebuilt — the front
            # page shows "updated X ago" + a manual Refresh button (they now refresh
            # weekly, not hourly, so the timestamp is meaningful and the button matters).
            refreshed = cat.conn.execute(
                "SELECT MAX(rebuilt_at) AS t FROM citation_counts").fetchone()
            return {"jurisdictions": out, "total": sum(b["total"] for b in out),
                    "stats_refreshed_at": refreshed["t"] if refreshed else None}

    _DRILL_SORTS = {
        "authority": "pagerank DESC, cited_by DESC, d.decision_date DESC",
        "cited": "cited_by DESC, pagerank DESC, d.decision_date DESC",
        "newest": "d.decision_date DESC, pagerank DESC",
        "oldest": "d.decision_date ASC, pagerank DESC",
    }

    # administrative decisions = regulator output: OSS register rows (court dpa-xx)
    # or a registered admin source. Must be excluded from "cases" so DPA decisions
    # never masquerade as case law.
    def _kind_clause(self, kind: str) -> tuple[str, list]:
        """SQL for a display KIND, derived from the same constants ``_doc_kind`` uses.

        These two had drifted, and silently: the bucket a document displays under is
        computed in Python by ``_doc_kind``, while the filter behind "show me this
        slice" was a hand-written SQL string naming three admin sources when
        ``_ADMIN_SOURCES`` had grown to eleven — and it compared ``doc_type`` directly
        against the kind, which is simply not what a kind is. The Law Commission's 722
        reports are stored ``doc_type='preparatory'`` and display as guidance, so
        narrowing the guidance slice to them returned "nothing in this slice".

        Deriving the clause from the constants is the fix that stays fixed: adding a
        source to ``_ADMIN_SOURCES`` now moves it in the filter as well as the label."""
        admin_sources = sorted(self._ADMIN_SOURCES)
        admin_courts = sorted(self._ADMIN_COURTS)
        admin_types = sorted(self._ADMIN_DOC_TYPES)
        travaux = sorted(self._TRAVAUX_SOURCES)
        guidance_sources = sorted(self._GUIDANCE_SOURCES)
        # mirrors _doc_kind's order: a DPA's whole output, then an admin body's
        # DECIDING documents
        # COALESCE is load-bearing: court is nullable, and `NULL LIKE 'dpa-%'` is
        # NULL, so `NOT (…)` is NULL too and the row is silently dropped. Every
        # judgment with no recorded court would have vanished from the cases slice.
        # _doc_kind gives GUIDANCE (and the reports filed as 'preparatory') precedence
        # over the administrative bucket, and the clause has to say so too. Without this
        # a DPA's guidance satisfies both slices at once, because the court test below
        # matches a data-protection authority's whole output regardless of doc type. It
        # only became reachable when a DPA guidance library was first harvested — until
        # then every dpa-* document was a decision.
        guidance_source_sql = (f"d.source IN ({','.join('?' * len(guidance_sources))})"
                               if guidance_sources else "1 = 0")
        admin_sql = ("(d.doc_type NOT IN ('guidance', 'preparatory')"
                     f" AND NOT ({guidance_source_sql})"
                     " AND (COALESCE(lower(d.court), '') LIKE 'dpa-%'"
                     + (f" OR COALESCE(lower(d.court), '') IN "
                        f"({','.join('?' * len(admin_courts))})"
                        if admin_courts else "")
                     + (f" OR (d.source IN ({','.join('?' * len(admin_sources))})"
                        f" AND d.doc_type IN ({','.join('?' * len(admin_types))}))"
                        if admin_sources and admin_types else "")
                     + "))")
        admin_params = list(guidance_sources) + list(admin_courts)
        if admin_sources and admin_types:
            admin_params += admin_sources + admin_types

        if kind == "administrative":
            return admin_sql, list(admin_params)
        if kind == "guidance":
            # doc_type 'guidance', plus 'preparatory' from any source that is NOT a
            # travaux collection — which is where the Law Commission lives
            sql = f"(({guidance_source_sql}) OR d.doc_type = 'guidance' OR (d.doc_type = 'preparatory'"
            params: list = list(guidance_sources)
            if travaux:
                sql += f" AND d.source NOT IN ({','.join('?' * len(travaux))})"
                params += travaux
            sql += "))"
            return sql, params
        if kind == "preparatory":
            if not travaux:
                return "1 = 0", []
            return (f"(d.doc_type = 'preparatory' AND d.source IN "
                    f"({','.join('?' * len(travaux))}))"), list(travaux)
        if kind == "cases":
            types = sorted(self._CASE_TYPES)
            return (f"(d.doc_type IN ({','.join('?' * len(types))})"
                    f" AND NOT ({guidance_source_sql}) AND NOT {admin_sql})",
                    list(types) + list(guidance_sources) + list(admin_params))
        if kind == "legislation":
            return "(d.doc_type = 'legislation')", []
        if kind == "other":
            known = sorted({"guidance", "preparatory", "legislation"} | set(self._CASE_TYPES))
            return (f"(d.doc_type NOT IN ({','.join('?' * len(known))})"
                    f" AND NOT ({guidance_source_sql}) AND NOT {admin_sql})",
                    list(known) + list(guidance_sources) + list(admin_params))
        # an explicit doc_type ("judgment", "notice") rather than a display kind
        return "(d.doc_type = ?)", [kind]

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
            if kind:
                ksql, kparams = self._kind_clause(kind)
                clauses.append(ksql)
                params.extend(kparams)
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
                if cat.backend == "postgres":
                    # `SELECT DISTINCT source` scans all ~5M rows (~100s, and it stampedes
                    # on a cold cache — the explore-page "hangs forever" pool-killer). A
                    # loose index skip-scan over documents_source_idx instead jumps to each
                    # next distinct value: ~one index seek per source (~dozens), milliseconds.
                    rows = cat.conn.execute("""
                        WITH RECURSIVE s AS (
                            (SELECT source FROM documents WHERE source IS NOT NULL
                             ORDER BY source LIMIT 1)
                            UNION ALL
                            SELECT (SELECT source FROM documents
                                    WHERE source > s.source AND source IS NOT NULL
                                    ORDER BY source LIMIT 1)
                            FROM s WHERE s.source IS NOT NULL
                        )
                        SELECT source AS k FROM s WHERE source IS NOT NULL
                    """).fetchall()
                else:
                    rows = cat.conn.execute(
                        "SELECT DISTINCT source AS k FROM documents").fetchall()
                return {"sources": [r["k"] for r in rows]}
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
        from datetime import datetime, timedelta, timezone

        from .adapters.eu_legislation import CELEX_BASE, EULegislationAdapter
        from .core.models import Stub
        from .pipeline import Pipeline
        from .pipeline.runner import RunStats

        # Skip stubs we already tried and found still-absent upstream within this window, so
        # successive runs make FORWARD progress into never-checked stubs instead of
        # re-hammering the same permanently-absent (but most-cited, so first-in-order)
        # instruments every pass — the harvest-miss poisoning failure mode, here draining
        # 18k EU stubs 500 at a time. A miss expires (default 30d) so a transient upstream
        # gap is retried later, never written off forever.
        try:
            miss_ttl = max(0.0, float(os.environ.get("RAGLEX_EU_STUB_MISS_DAYS") or 30.0))
        except (TypeError, ValueError):
            miss_ttl = 30.0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=miss_ttl)).isoformat()

        checked = upgraded = 0
        still_absent: list[str] = []
        with self._open() as (cat, rs, ts):
            # Select on has_text = 0, NOT on the meta_json marker: an older
            # generation of stubs (bare CELEX title, meta_json NULL — 31970L0156
            # among them) carried no marker at all, so the marker-LIKE selection
            # could never see the very rows most in need of repair. Textless IS the
            # condition being repaired; most-cited first so the pass spends itself
            # on the instruments the corpus actually leans on. Recently-checked-absent
            # stubs are excluded IN SQL so a bounded run always advances into new ones.
            rows = cat.conn.execute(
                "SELECT d.stable_id, d.landing_url FROM documents d "
                "LEFT JOIN citation_counts cc ON cc.candidate_id = d.stable_id "
                "WHERE d.source = 'eu-legislation' AND d.has_text = 0 "
                "  AND NOT EXISTS (SELECT 1 FROM enrichment_misses m "
                "      WHERE m.kind = 'eu-stub-miss' AND m.key = d.stable_id "
                "        AND m.attempted_at >= ?) "
                "ORDER BY COALESCE(cc.occurrences, 0) DESC, d.stable_id LIMIT ?",
                (cutoff, limit)).fetchall()
            if not rows:
                return {"checked": 0, "upgraded": 0, "still_absent": 0}
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
                    still_absent.append(celex)
                    continue
                # still a stub upstream: leave the existing record untouched, but remember we
                # tried it so the next run moves on to stubs we haven't checked yet.
                if rec is None or not rec.text or (rec.extra or {}).get("metadata_only"):
                    still_absent.append(celex)
                    continue
                if pipe._ingest(rec, RunStats(source=adapter.source)):
                    upgraded += 1
            if still_absent:
                cat.record_enrichment_misses("eu-stub-miss", still_absent)
        self._invalidate_caches()
        return {"checked": checked, "upgraded": upgraded, "still_absent": len(still_absent)}

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
        # Stale-while-revalidate + placeholder so the endpoint NEVER blocks the UI. The
        # doc-count breakdowns are roll-up-derived (cheap), but resolution_stats/tag_counts
        # still GROUP BY the (10M-row, post fr-dila) relations/tags tables — under write load
        # (a reparse) that can run tens of seconds, and the old no-placeholder 30s cache
        # re-blocked on every cold/stale hit, hanging the homepage. Now a cold call returns a
        # {_warming} placeholder and computes in the background; a longer TTL stops it churning.
        def _compute():
            with self._open() as (cat, _rs, _ts):
                return corpus_stats(cat).to_dict()
        return self._cached("stats", 600, _compute, sync_wait=2.5, placeholder={
            "total": None, "by_doc_type": {}, "by_source": {}, "by_upstream_status": {},
            "by_tag": {}, "resolution": {}})

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

    def _citation_frontier(self, *, limit: int = 50, only_unharvestable: bool = False) -> list[dict]:
        """The citation frontier (§5a): forms the corpus cites but doesn't yet hold,
        grouped by (form, jurisdiction, adapter) and ranked by frequency. Feeds the
        coverage dashboard. ``only_unharvestable=True`` narrows to forms with no adapter."""
        from .citations import citation_frontier

        def _compute():
            with self._open() as (cat, _rs, _ts):
                return {"rows": citation_frontier(cat, limit=limit,
                                                  only_unharvestable=only_unharvestable)}
        # A corpus-wide roll-up; it doesn't move between page loads.
        return self._cached(f"frontier:{limit}:{only_unharvestable}", 300, _compute)["rows"]

    def refresh_statute_gazetteer(self, *, years: list[int] | None = None) -> dict:
        """Top up the statute gazetteer from the legislation.gov.uk feeds into the
        data-dir extra list — so acts confirm by name without a package release. Run
        weekly by the scheduler; cheap and no-op when nothing new has been enacted.

        ``years`` backfills a specific span. The weekly run covers only the current and
        previous year, which leaves the SEAM between the vendored lists and the
        self-updating top-up permanently unfilled: the Investigatory Powers Act 2016
        (Royal Assent 29 November) fell in exactly that gap and was unreachable by its
        own name, while the 2000 and 2024 Acts either side of it resolved fine.
        """
        from .citations.statute_gazetteer import refresh_from_feeds

        clean = tuple(sorted({int(y) for y in years})) if years else None
        n = refresh_from_feeds(self.config.data_dir / "statutes_extra.lst", years=clean)
        return {"added": n, "years": list(clean) if clean else "current+previous"}

    def rescan_contested_shorthands(self, *, limit: int = 200000, workers: int | None = None,
                                    run_id: str | None = None,
                                    on_progress=None, cancel_check=None) -> dict:
        """Re-extract the documents carrying an edge from a CONTESTED learned shorthand.

        The store applies a shorthand corpus-wide, and it used to apply a contested one
        — a name it holds against several different candidates — whenever the citing
        document happened to cite exactly one of them. That is a coincidence test, and
        the store is contested precisely where it has mislearned: "PACE" was held
        against six acts, none of them the Police and Criminal Evidence Act 1984, so
        "s. 8(1) of PACE" in a judgment that never spells PACE out was recorded as a
        citation of RIPA s.8(1). ``_stored_shorthands_for`` no longer applies those, but
        the edges they already wrote stay in the graph until the document is read again.

        The scope is computed from the edges themselves rather than from the text,
        because that is exactly what the defect is recorded in: 590,315 rows over 64,080
        documents at the time of writing. Everything else is the ordinary pooled rescan,
        so the run is checkpointed, cancellable and idempotent."""
        from .citations import extract_documents_parallel
        from .citations.extractor import shorthand_name_from_use

        with self._open() as (cat, _rs, ts):
            owners: dict[str, set[str]] = {}
            for cid, rows in cat.learned_shorthand_map().items():
                for name, _kind, _abbrev in rows:
                    owners.setdefault(name, set()).add(cid)
            contested = {n for n, o in owners.items() if len(o) > 1}
            _progress(on_progress, stage="finding documents with contested shorthands",
                      done=0, total=0, item=f"{len(contested)} contested names")
            ids: dict[str, None] = {}
            scanned = 0
            for row in cat.conn.execute(
                    "SELECT src_id, raw FROM citations WHERE method = ?",
                    ("shorthand_global",)):
                scanned += 1
                if scanned % 250000 == 0:
                    _progress(on_progress,
                              stage="finding documents with contested shorthands",
                              done=len(ids), total=0, item=f"{scanned} edges read")
                    if cancel_check and cancel_check():
                        return {"cancelled": True, "documents": len(ids)}
                if shorthand_name_from_use(row["raw"]) in contested:
                    ids.setdefault(row["src_id"], None)
                    if len(ids) >= limit:
                        break
            targets = list(ids)
            # Resume: the scope is re-derived from the edges each run (a document this
            # run already fixed no longer HAS a contested edge, but only once its rows
            # are rewritten), so skip anything this root run has already stamped.
            done_already = 0
            if run_id:
                stamped = {
                    r["stable_id"] for r in cat.conn.execute(
                        "SELECT stable_id FROM documents WHERE last_extraction_run_id = ?",
                        (run_id,))}
                if stamped:
                    before = len(targets)
                    targets = [t for t in targets if t not in stamped]
                    done_already = before - len(targets)
            if not targets:
                return {"contested_names": len(contested), "documents": 0,
                        "already_done": done_already}
            aliases = cat.named_alias_map()
            stats = extract_documents_parallel(
                cat, ts, targets, aliases=aliases, run_id=run_id, workers=workers,
                on_progress=on_progress, cancel_check=cancel_check,
                stage="re-extracting documents with contested shorthands")
            out = {"contested_names": len(contested), "documents": len(targets),
                   "already_done": done_already,
                   "extracted": stats.processed, "citations": stats.citations,
                   "cancelled": stats.cancelled}
        if not (cancel_check and cancel_check()):
            with self._open() as (cat, _rs, _ts):
                Resolver(cat).run()
        self._invalidate_caches()
        return out

    def rescan_matching(self, *, query: str, exact: bool = True, limit: int = 20000,
                        citers_of: list[str] | None = None,
                        on_progress=None, cancel_check=None) -> dict:
        """Re-extract every document whose TEXT matches a free-text query.

        ``citers_of`` adds a GRAPH-derived scope on top: every document holding a
        resolved edge into any of those targets. A text query cannot express "everything
        that cites the static-export set" — the set is 57 instruments with dozens of
        names between them, and the documents that matter are exactly the ones the graph
        already knows about. Either scope may be given alone; together they are unioned
        and each document is still re-read once.

        The scope a citation fix actually needs. When a grammar, alias or shorthand
        changes, the documents to re-read are the ones that MENTION the thing — which is
        precisely what the edges do not yet record, so they cannot be found by walking
        the graph. Searching the text finds them; ``citing_documents`` would only return
        the ones already resolved, i.e. the ones that least need re-reading.

        Supports the full query syntax (quoted phrases, OR, -exclusion, NEAR/n), and
        several queries separated by ``|||`` are unioned — one pass over the union
        rather than one pass per phrase, so a document naming three of the Acts is
        re-extracted once.

        Extraction runs on the same pooled path as every other bulk re-scan. The
        serial loop this replaced cost ~2.5s/document for three avoidable reasons:
        it rebuilt the whole named-alias map from the DB *per document*, ran the
        (CPU-bound, pure-Python) grammar pass on one core, and committed per
        document. None of those scale with the scope, so a "small" rescan was no
        faster per document than a corpus-wide one.
        """
        from .citations import extract_documents_parallel

        queries = [q.strip() for q in str(query or "").split("|||") if q.strip()]
        targets = [t.strip() for t in (citers_of or []) if str(t or "").strip()]
        if not queries and not targets:
            return {"error": "a query or citers_of is required"}
        ids: dict[str, None] = {}
        per_query: dict[str, int] = {}
        from_graph = 0
        if targets:
            with self._open() as (cat, _rs, _ts):
                # The version FAMILY of each target, not the bare id: a citer of the
                # GDPR's enacted text and a citer of a dated consolidation of it are the
                # same citer of the same instrument, and re-reading only one of them
                # leaves the other's edges standing.
                family: dict[str, None] = {}
                for target in targets:
                    family[target] = None
                    for row in cat.conn.execute(
                            "SELECT src_id, dst_id FROM relations WHERE "
                            "(src_id = ? OR dst_id = ?) AND relationship_type IN "
                            "('consolidates', 'point_in_time_of')", (target, target)):
                        for side in (row["src_id"], row["dst_id"]):
                            if side:
                                family[side] = None
                members = list(family)
                for start in range(0, len(members), 200):
                    chunk = members[start:start + 200]
                    marks = ",".join("?" * len(chunk))
                    for row in cat.conn.execute(
                            f"SELECT DISTINCT src_id FROM relations WHERE dst_id IN ({marks}) "
                            "AND resolution_status = 'resolved'", tuple(chunk)):
                        ids[row["src_id"]] = None
                ids.pop(None, None)  # defensive: a null src_id is not a document
                for target in members:
                    ids.pop(target, None)  # an instrument citing itself is not a citer
                from_graph = len(ids)
        for one in queries:
            # ``items`` is the row list; ``total`` is the match count before the limit.
            # Reading a key the search does not return is a silent empty scope — the
            # job reported success over zero documents — so a query that matches
            # nothing is called out below rather than shrugged at.
            found = self.freetext_search(one, exact=exact, limit=limit)
            rows = found.get("items") or []
            per_query[one] = found.get("total", len(rows))
            for row in rows:
                sid = row.get("stable_id")
                if sid:
                    ids[sid] = None
        total = len(ids)
        if not total:
            # Every query matched nothing. Nearly always a mistyped phrase or a
            # jurisdiction that isn't in the free-text index — never a reason to
            # report a successful pass over an empty corpus.
            return {"error": "no documents matched", "queries": per_query,
                    "citers_of": targets,
                    "hint": "check the phrase and that its jurisdiction is indexed "
                            "(freetext_scope lists what is)"}
        with self._open() as (cat, _rs, ts):
            # Confirm held documents that actually have text, once, instead of
            # discovering per document that there is nothing to extract.
            scope = cat.held_text_document_ids(list(ids))
            aliases = cat.named_alias_map()   # hoisted: constant for the whole run
            ex = extract_documents_parallel(
                cat, ts, scope, aliases=aliases,
                stage="re-extracting",
                # The bulk default (every 200 documents over a 2,000+ scope) leaves the
                # panel sitting on "1 of 10,773" for several minutes, which reads as a
                # frozen job — the failure mode this whole pass exists to stop showing.
                # A rescan is watched, so report often enough to look alive.
                report_every=25,
                on_progress=on_progress, cancel_check=cancel_check)
            base = {"queries": per_query, "citers_of": len(targets),
                    "from_graph": from_graph, "documents": total,
                    "extractable": len(scope), "re_extracted": ex.processed,
                    "citations": ex.citations}
            if ex.cancelled:
                # A cancel used to be invisible: the resolve stage overwrote the
                # progress row's done/total, so a run stopped at document 52 of
                # 10,767 displayed as 10,767/10,767 and read as finished. Stop here
                # and say so, both in the result and in the progress panel.
                _progress(on_progress, stage="cancelled", done=ex.processed, total=len(scope),
                          item=f"cancelled after {ex.processed:,} of {len(scope):,} documents")
                self._invalidate_caches()
                return {**base, "cancelled": True, "resolved": 0}
            # The BATCHED resolver, not the one-shot: it is the same set-based SQL in
            # bounded relation-id ranges, but it reports a cursor and honours cancel.
            # The one-shot version is silent for as long as it takes, which is exactly
            # what made a finished rescan look frozen in its final phase.
            resolved = Resolver(cat).run_batched(
                on_progress=on_progress, cancel_check=cancel_check)
        self._invalidate_caches()
        return {**base,
                "resolved": getattr(resolved, "resolved", None),
                "still_pending": getattr(resolved, "still_pending", None)}

    def rebuild_citation_counts(self, *, on_progress=None) -> dict:
        """Refresh the snowball's frequency roll-up + the Explore-homepage roll-ups it shares a
        cadence with. Five serial full-table aggregates (counts, source stats, corpus shape,
        legislation-type rail, the ~96s hanging-reference worklist), so it emits a stage line
        before each — otherwise the progress heartbeat never ticks and the Jobs panel wrongly
        flags a working job as 'frozen' after 150s of silence."""
        # 5 phases; a coarse (phase-level) progress so the heartbeat ticks and the operator
        # can see WHICH roll-up is running rather than a silent multi-minute block.
        def _step(i: int, label: str) -> None:
            if on_progress:
                try:
                    on_progress(stage=label, done=i, total=5)
                except Exception:  # noqa: BLE001 — progress must never break the work
                    pass
        with self._open() as (cat, _rs, _ts):
            _step(0, "citation counts")
            n = cat.rebuild_citation_counts()
            # same cadence, same shape of work: every roll-up the Explore homepage
            # reads instead of live full-table scans and aggregates
            _step(1, "source stats")
            srcs = cat.refresh_source_stats()
            _step(2, "corpus shape")
            shape = cat.refresh_corpus_shape_stats()
            _step(3, "legislation types")
            leg = self._refresh_leg_type_stats(cat)
            # the hanging-reference worklist (Unresolved page + auto-drain) — a ~96s live
            # aggregate, rolled up here so those reads are instant.
            _step(4, "pending references")
            pend = cat.rebuild_pending_reference_stats()
            _step(5, "done")
        self._invalidate_caches()
        return {"candidates": n, "sources": srcs, "shape_rows": shape, "leg_types": leg,
                "pending_refs": pend}

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

    def db_health(self) -> dict:
        """Read-only DB diagnostic for "the whole thing is sluggish": planner-stat freshness,
        bloat, seq-scan-heavy tables, unused indexes, cache hit ratio, connection pressure,
        long-running queries, plus actionable hints. Postgres-substantive, SQLite stub."""
        with self._open() as (cat, _rs, _ts):
            return cat.db_health()

    def db_maintenance(self, *, analyze: bool = True, vacuum: bool = False) -> dict:
        """Run ANALYZE (refresh planner stats — the cheap big lever after a corpus grows) and
        optionally VACUUM ANALYZE (reclaim dead-tuple bloat; online, no exclusive lock)."""
        with self._open() as (cat, _rs, _ts):
            out = cat.db_analyze(vacuum=vacuum) if (analyze or vacuum) else {"skipped": True}
        return out

    # -- scheduled-task toggles (per-task on/off + cadence) ----------------
    def list_scheduled_tasks(self) -> dict:
        """Every recurring scheduler task with its effective enabled/cadence — the on/off UI
        payload. Also reports the global pause (which overrides all)."""
        from .schedule import list_tasks
        from .jobs import scheduler_paused
        return {"tasks": list_tasks(), "scheduler_paused": scheduler_paused()}

    def set_scheduled_task(self, name: str, *, enabled: bool | None = None,
                           every_minutes: int | None = None, remove: bool = False,
                           at_hour: int | str | None = None) -> dict:
        """Enable/disable a scheduler task, set its cadence, or pin it to one UTC hour
        (persisted); ``remove`` reverts it to its default, ``at_hour='any'`` unpins it.
        The scheduler picks the change up on its next tick."""
        from .schedule import set_task
        return set_task(self.settings, name, enabled=enabled,
                        every_minutes=every_minutes, remove=remove, at_hour=at_hour)

    def maintenance_plan(self, **params) -> dict:
        """Preview the serial maintenance queue without running it (what a maintenance-run
        job would do, in order)."""
        from .maintenance import build_plan
        return {"steps": build_plan(self, params)}

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
        frontier = self._citation_frontier(limit=10)
        # uncapped: count EVERY distinct hanging reference (the grouping is built in full
        # regardless of limit, so this is no extra cost) — the headline number must not
        # plateau at an arbitrary cap.
        hanging = self.unresolved_references(limit=None)
        low_conf = [h for h in hanging if h["confidence"] == "low"]
        # The TRUE count of one-click-harvestable references (distinct docs we could
        # fetch), as opposed to the frontier's *occurrence* counts (one instrument can
        # be cited hundreds of times) — so the "Harvest all routable (N)" button can
        # show the real total instead of only what a page happens to have loaded.
        routable = [h for h in hanging if h.get("fetchable")
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
        from .citations.snowball import (ECHR_APPNO_RE, _classify, is_fetchable,
                                         uk_leg_category as _uk_leg_category)
        from .citations.taxonomy import classify_candidate
        from .adapters.bailii import bailii_url as _bailii_url

        with self._open() as (cat, _rs, _ts):
            import os as _os
            miss_ttl = float(_os.environ.get("RAGLEX_MISS_TTL_DAYS") or 90)
            retry_ttl = float(_os.environ.get("RAGLEX_RETRY_TTL_HOURS") or 6) / 24.0
            absent = cat.enrichment_misses("harvest-miss", max_age_days=miss_ttl)
            retry = cat.enrichment_misses("harvest-retry", max_age_days=retry_ttl)
            rows = []
            # Read the pre-aggregated worklist (ms); fall back to the ~96s live aggregate
            # only when the roll-up hasn't been built yet (fresh DB / test). The roll-up is
            # ranked by citing_count, so a bounded read (limit × slack for the junk/cooled
            # rows dropped below) still returns the most-cited references — and classifying
            # only those, not all ~930k groups, is what keeps the page + drain responsive.
            scan = None if limit is None else max(limit * 3, 200)
            groups = cat.pending_reference_groups_rollup(limit=scan)
            if not groups:
                groups = cat.pending_reference_groups(limit=scan)
            for g in groups:
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
                # Naming the source that holds a reference is not the same as being able
                # to FETCH it: gesetze-im-internet holds the German statute book and
                # NeuRIS the federal judgments, but neither adapter turns a citation into
                # a one-item fetch. Such a reference is reported with its source (that is
                # the honest answer to "where does this live?") but must never be offered
                # to the drain, which would spend an attempt slot to fail with
                # "no targeted adapter" and then file it as absent.
                fetchable = is_fetchable(adapter)
                # low confidence: no candidate, an LLM-surfaced reference, a form we can't
                # fetch, OR a fuzzy name-based ECHR match (keep these out of auto-harvest —
                # a HUDOC docname guess wants a human's eye).
                low = (needs_identifier or "llm" in methods or not fetchable
                       or misrouted_appno
                       or (cand or "").lower().startswith("echr:"))
                cooling_reason = ("source reported absent" if cand in absent else
                                  "temporary retrieval failure" if cand in retry else None)
                tax = classify_candidate(cand or "", "" if cand else "case")
                rows.append({
                    "ref": ref, "candidate": cand, "raw": g["raw"],
                    "pinpoint": g["anchor"], "form": form, "jurisdiction": juris,
                    "suggested_adapter": adapter, "needs_identifier": needs_identifier,
                    # the source that would hold it, vs whether we can ask that source
                    # for this one item — the "build an id-fetch" worklist is the gap
                    "fetchable": fetchable,
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

    def unresolved_references_cached(self, *, limit: int = 5000) -> dict:
        """Stale-while-revalidate wrapper for the hanging-reference queue (the Unresolved
        panel). That panel asks for up to 5000 rows WITH each one's citing-document list —
        tens of seconds cold — so caching it (1h TTL) with a placeholder means the panel
        never blocks: a cold load returns ``{rows: [], _warming: true}`` and computes in the
        background, and the nightly 1am refresh + startup warm keep it hot. Dropped on any
        harvest/resolve (it's in the volatile set) so a freshly-resolved reference leaves the
        list on the next view. Returns ``{rows, _warming?}``."""
        return self._cached(f"unresolved:{limit}", 3600,
                            lambda: {"rows": self.unresolved_references(limit=limit, with_citing=True)},
                            placeholder={"rows": []}, sync_wait=2.5)

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
                              scan_limit: int | None = None, enrich: bool = True,
                              offset: int = 0) -> dict:
        from .citations.frontier import classify as _frontier_classify
        from .citations.snowball import _classify, is_fetchable as _is_fetchable
        from .adapters.bailii import external_link
        from .citations.reporters import report_series, series_jurisdiction

        floor = self._UNFETCHABLE_MIN_CITING if min_citing is None else min_citing
        rows = []
        with self._open() as (cat, _rs, _ts):
            # Read the pre-aggregated worklist (ms); the live GROUP BY over the pending
            # slice is ~96s even bounded, so fall back to it only for a fresh/un-rolled DB.
            # ``offset`` pages deeper (the export scans page-by-page under a jurisdiction
            # filter so foreign refs at the top don't cap the batch).
            groups = cat.pending_reference_groups_rollup(min_citing=floor, limit=scan_limit,
                                                         offset=offset)
            if not groups:
                groups = cat.pending_reference_groups(min_citing=floor, limit=scan_limit,
                                                      need_echr=False)
            scanned = len(groups)
            for g in groups:
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
                    if _is_fetchable(adapter):
                        continue  # routable — belongs in the harvest worklist, not here
                    # Otherwise it lands here even though a source is named for it: the
                    # source holds it but has no id-fetch, which is exactly the
                    # "cannot be retrieved without a human" the dead list is for.
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
            # Drop references that are ALREADY satisfiable before returning them. The
            # pending-reference rollup keys off each citing EDGE's resolution_status, which
            # stays 'pending' until the resolver re-runs over it — but the authority may
            # already be HELD with an alias pointing the citation at it (a prior Westlaw/
            # BAILII import, CanLII enrichment, a parallel-cite merge). Listing those anyway
            # is why an imported case kept reappearing on the Westlaw retrieval export for
            # days. Checked only over the ranked head we actually return (a bounded number of
            # indexed point lookups), not the whole ~100k-row pending scan.
            out = []
            skipped_resolvable = 0
            for r in rows:
                if len(out) >= limit:
                    break
                if self._resolved_target(cat, r["candidate"], r["raw"]):
                    skipped_resolvable += 1
                    continue
                out.append(r)
            # The Westlaw/Lexis export only needs the citation string + rank, and asks for a
            # huge `limit` (the long tail). Skip the per-item citing-documents / suggestions /
            # cited-from enrichment for it — those are three more queries over up-to-20k refs
            # that the export discards, and were part of why it never returned.
            if not enrich:
                for r in out:
                    r["citing_documents"] = []
                    r["suggestions"] = []
                    r["cited_from"] = []
                return {"total": len(rows), "references": out, "min_citing": floor,
                        "already_held_skipped": skipped_resolvable, "scanned": scanned}
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
                "min_citing": floor, "already_held_skipped": skipped_resolvable,
                "scanned": scanned}

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
        floor = max(1, min_citing)

        def _take(r) -> None:
            """Classify one frontier row and, if it's a pasteable target matching the
            series/jurisdiction filters, add it to ``items`` (deduped)."""
            if r["citing_count"] < min_citing:
                return
            # Collapse internal whitespace: a citation extracted across a PDF line break
            # ("[1991] ATPR\n   41") is stored with the newline, and pasted verbatim it
            # spans two lines and won't retrieve. One space between tokens is the paste form.
            raw = " ".join((r["raw"] or r["ref"] or "").split())
            series = r.get("series")          # computed once, on the frontier row
            if series and series.upper() in ("ECR", "EHRR"):
                return  # own sources (CELLAR / HUDOC), not a Westlaw/Lexis target
            is_cite = bool(r["is_report"]) or is_report_citation(raw) or bool(cite_shape.search(raw))
            if not is_cite and not (include_names and r["form"] == "case (by name)"):
                return
            if want_series and (not series or series.upper() not in want_series):
                return
            # Jurisdiction: a recognised report series maps directly; otherwise the
            # reference is a neutral citation ("[2019] IESC 4") or bare name, whose
            # jurisdiction is read from the candidate's court token — so Irish (IESC/
            # IECA/IEHC) and Commonwealth neutral citations don't default to "uk" and
            # leak into a UK-only Westlaw batch.
            jur = r.get("jurisdiction")
            if want_jur and jur not in want_jur:
                return
            key = _re.sub(r"[\s.'’\[\]()]+", "", raw).upper()  # fold for dedup
            if not key or key in seen:
                return
            seen.add(key)
            items.append({"citation": raw, "citing_count": r["citing_count"],
                          "series": series, "jurisdiction": jur, "form": r["form"]})

        # PAGE the citation-ranked pending frontier rather than taking a single top slice.
        # A jurisdiction/series-filtered export (a UK Westlaw subscription can't fetch
        # foreign reports) would otherwise be capped by whatever ranks in the global top
        # ``scan_limit`` — dominated by heavily-cited foreign reports — so the user's own
        # backlog never surfaced. When a filter is active we keep scanning DEEPER pages
        # (skipping the crowd of other jurisdictions) until we've collected enough of the
        # selected one or the pool is exhausted; an unfiltered export still stops at one
        # page (the top is what it wants). Each page cached so re-exports/formats are instant.
        page = scan_limit
        filtered = bool(want_jur or want_series)
        target = batch_size * 100                    # plenty of batches; stop once satisfied
        max_scan = (400_000 if filtered else scan_limit)
        offset = 0
        while offset < max_scan:
            frontier = self._cached(
                f"unfetchable:export:{page}:{floor}:{offset}", 300,
                lambda off=offset: self._unfetchable_uncached(
                    page, scan_limit=page, min_citing=floor, enrich=False, offset=off))
            for r in frontier["references"]:
                _take(r)
            offset += page
            if frontier.get("scanned", 0) < page:
                break                                 # reached the end of the pending pool
            if not filtered or len(items) >= target:
                break                                 # unfiltered: top page is enough

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

    def backfill_intituling(self, *, source: str | None = "uk-caselaw", limit: int = 500000,
                            on_progress=None, cancel_check=None) -> dict:
        """Record WHO decided each held judgment, and who argued it, from its own first page.

        A Find Case Law document's metadata carries only provenance keys — no bench, no
        counsel — but the judgment prints both above the word JUDGMENT. Parsed here
        (:mod:`citations.intituling`) and stored as ``coram`` / ``representation``, which
        the reader shows under the title. Names are standardised the way a lawyer writes
        them: "LORD JUSTICE CHADWICK" → "Chadwick LJ". Idempotent — a document that already
        has a bench is skipped."""
        from .citations.intituling import parse_intituling

        st = {"scanned": 0, "named": 0, "already": 0, "no_block": 0, "no_text": 0}
        with self._open() as (cat, _rs, ts):
            rows = cat.conn.execute(
                "SELECT stable_id, payload_hash, meta_json FROM documents "
                "WHERE has_text = 1 AND is_latest = 1 AND doc_type IN ('judgment','decision','opinion') "
                + ("AND source = ? " if source else "")
                + "ORDER BY stable_id LIMIT ?",
                ((source, limit) if source else (limit,))).fetchall()
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                if n % 500 == 0:
                    cat.commit()
                    _progress(on_progress, stage="reading judgment headers", done=n,
                              total=len(rows), item=r["stable_id"])
                st["scanned"] += 1
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                if meta.get("coram"):
                    st["already"] += 1
                    continue
                try:
                    text = ts.get(r["payload_hash"]) if r["payload_hash"] else None
                except OSError:
                    text = None
                if not text:
                    st["no_text"] += 1
                    continue
                found = parse_intituling(text)
                if not found:
                    st["no_block"] += 1
                    continue
                cat.set_document_meta(r["stable_id"], {**meta, **found}, commit=False)
                st["named"] += 1
            cat.commit()
        self._invalidate_caches()
        return st

    def repair_mojibake(self, *, source: str | None = None, limit: int = 200000,
                        on_progress=None, cancel_check=None) -> dict:
        """Repair Windows-1252 punctuation mis-decoded into the C1 control block, in text
        that is ALREADY stored.

        A judgment reading "Home Park House ▯ a fortiori" is not a rendering bug: the
        source bytes were cp1252, something decoded them as ISO-8859-1, and every en dash,
        curly quote and ellipsis landed in the control range where a browser draws an empty
        rectangle (74 of them in one Court of Appeal judgment). New text is fixed as it is
        written; this walks what is held.

        The substitution is **1:1**, so nothing moves: no re-anchoring, no re-extraction,
        and the payload_hash still describes the same document. Bounded by ``limit`` and
        scoped by ``source``; re-running it finds nothing to do."""
        from .core.text import fix_cp1252_c1

        st = {"scanned": 0, "repaired": 0, "chars_fixed": 0, "unreadable": 0}
        seen_hashes: set[str] = set()
        with self._open() as (cat, _rs, ts):
            rows = cat.conn.execute(
                "SELECT stable_id, payload_hash FROM documents WHERE has_text = 1 "
                + ("AND source = ? " if source else "")
                + "ORDER BY stable_id LIMIT ?",
                ((source, limit) if source else (limit,))).fetchall()
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                # Progress is reported on documents SEEN, not documents changed, and
                # before the skips. Keyed on work done, a resumed pass that correctly
                # skips everything reports nothing, and the cross-process reaper —
                # which decides a job has died from an idle heartbeat — eventually
                # kills a job that is working perfectly well.
                if n % 2000 == 0:
                    _progress(on_progress, stage="repairing mis-decoded text", done=n,
                              total=len(rows), item=r["stable_id"])
                ph = r["payload_hash"]
                if not ph or ph in seen_hashes:
                    continue
                seen_hashes.add(ph)
                st["scanned"] += 1
                try:
                    text = ts.get(ph)
                except OSError:
                    st["unreadable"] += 1
                    continue
                fixed = fix_cp1252_c1(text)
                if fixed != text:
                    st["chars_fixed"] += sum(1 for a, b in zip(text, fixed) if a != b)
                    ts.put(ph, fixed)
                    st["repaired"] += 1
        return st

    def recase_shouty_titles(self, *, source: str = "echr", dry_run: bool = False,
                             on_progress=None, cancel_check=None) -> dict:
        """Re-case the upper-case case names a register publishes — HUDOC ships every
        ``docname`` shouting, which is 37,635 of the corpus's 38,191 Strasbourg titles.

        Only the SHOUTY tokens move (see ``core.case_title``); a token that already
        carries a lower-case letter is left byte-for-byte, so the human-written half of a
        HUDOC docname — "(Judgment : Violation of Article 6 …)", "[Armenian Translation]
        by the COE …" — survives untouched.

        Reversible by construction: the register's own spelling is already kept in the
        document's metadata (``docname`` for HUDOC), and where it is not, this records it
        as ``title_original`` before writing. ``curate=False`` — a system re-casing is not
        a human correction and must not claim to be one.

        Idempotent, so a re-run is a no-op and it is safe behind the import path that now
        cases new documents on the way in."""
        from .core.case_title import titlecase_case_name

        st = {"scanned": 0, "recased": 0, "unchanged": 0}
        samples: list[dict] = []
        with self._open() as (cat, _rs, _ts):
            rows = cat.conn.execute(
                "SELECT stable_id, title, meta_json FROM documents "
                "WHERE source = ? AND title IS NOT NULL AND title <> ''",
                (source,)).fetchall()
            total = len(rows)
            for row in rows:
                if cancel_check and cancel_check():
                    break
                st["scanned"] += 1
                original = row["title"]
                recased = titlecase_case_name(original)
                if not recased or recased == original:
                    st["unchanged"] += 1
                    continue
                st["recased"] += 1
                if len(samples) < 25:
                    samples.append({"id": row["stable_id"], "from": original,
                                    "to": recased})
                if dry_run:
                    continue
                try:
                    meta = json.loads(row["meta_json"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                # Keep the register's own spelling if the adapter didn't already.
                if not meta.get("docname") and not meta.get("title_original"):
                    meta["title_original"] = original
                    cat.set_document_meta(row["stable_id"], meta, commit=False)
                cat.update_document_fields(row["stable_id"], {"title": recased},
                                           curate=False)
                if st["recased"] % 500 == 0:
                    _progress(on_progress, stage=f"re-casing {source} titles",
                              done=st["scanned"], total=total)
        self._invalidate_caches()
        return {**st, "source": source, "dry_run": dry_run, "sample": samples}

    def repair_de_duplicate_renditions(self, *, source: str = "de-neuris",
                                       dry_run: bool = False,
                                       on_progress=None, cancel_check=None) -> dict:
        """Fold a second register's copies of judgments the corpus already holds back into
        the originals.

        NeuRIS and Rechtsprechung im Internet publish the SAME federal decisions — 83,515
        and 83,465 of them — but NeuRIS answers ``ecli: null``, so its copies were stored
        as ``de/<documentNumber>`` while the held ones are keyed by ECLI. Nothing linked
        the two, and because the adapter declares the docket alias, each copy also
        RE-POINTED that docket key away from the ECLI-keyed judgment: a citation to
        "BGH AnwZ (Brfg) 40/25" resolved to a rendition with no ECLI and none of the edges
        the original had.

        For every document of ``source`` whose declared docket alias names a decision held
        from another register, this: re-points the alias to the original, moves any edges
        resolved to the copy onto the original, records the copy as a *rendition* in the
        original's metadata, and deletes the copy. What it never does is merge two
        documents that only look alike — the match is the docket key the adapters
        themselves mint, not a heuristic."""
        from .citations.german import case_alias as _case_alias

        st = {"scanned": 0, "duplicates": 0, "aliases_repointed": 0, "edges_moved": 0,
              "documents_deleted": 0}
        samples: list[dict] = []

        def _dockets(meta: dict, court: str | None) -> list[str]:
            """Every docket key a German document answers to. Adapters state them in
            ``extra["aliases"]``; the bulk register states court + Aktenzeichen instead."""
            keys = [str(a) for a in (meta.get("aliases") or []) if a]
            raw = meta.get("file_numbers") or meta.get("aktenzeichen") or []
            if isinstance(raw, str):
                raw = [raw]
            for docket in raw:
                if docket and court:
                    keys.append(_case_alias(str(court), str(docket)))
            return keys

        with self._open() as (cat, _rs, _ts):
            # The originals, keyed by docket: every German document from another
            # register — OR from this one, once it has an ECLI. The alias TABLE can't
            # answer this — it holds one row per key, and the copy has already taken it;
            # the question is which document also *claims* the key.
            #
            # The same-source rung matters because the reason NeuRIS forked the corpus
            # has since gone away: the beta answered `ecli: null` and now populates it,
            # so a decision held as de/<documentNumber> comes back ECLI-keyed and is
            # stored beside its own older copy. Both carry `de-neuris`, and a rule that
            # only ever looked at other registers could not see it.
            origins: dict[str, str] = {}
            for o in cat.conn.execute(
                    "SELECT stable_id, source, court, meta_json, ecli FROM documents "
                    "WHERE source LIKE 'de-%' AND (source <> ? OR ecli IS NOT NULL)",
                    (source,)).fetchall():
                try:
                    ometa = json.loads(o["meta_json"] or "{}")
                except (ValueError, TypeError):
                    ometa = {}
                for key in _dockets(ometa, o["court"]):
                    k = key.casefold()
                    # an ECLI-keyed original wins any tie — it is the better identity
                    if k not in origins or (o["ecli"] and not origins[k].startswith("de/")):
                        origins.setdefault(k, o["stable_id"])
                        if o["ecli"]:
                            origins[k] = o["stable_id"]

            # A copy is by definition the rendition with NO identifier of its own; a
            # document that carries an ECLI is never folded away, whichever register
            # supplied it.
            rows = cat.conn.execute(
                "SELECT stable_id, court, meta_json FROM documents "
                "WHERE source = ? AND ecli IS NULL", (source,)).fetchall()
            for r in rows:
                if cancel_check and cancel_check():
                    break
                st["scanned"] += 1
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                held = None
                for key in _dockets(meta, r["court"]):
                    cand = origins.get(key.casefold())
                    if cand and cand != r["stable_id"]:
                        held = cand
                        break
                if held is None:
                    continue
                st["duplicates"] += 1
                if len(samples) < 20:
                    samples.append({"copy": r["stable_id"], "original": held})
                if dry_run:
                    continue
                with cat._atomic():
                    for key in _dockets(meta, r["court"]):
                        cat.put_alias(key.casefold(), held, source="de-rendition",
                                      commit=False)
                        st["aliases_repointed"] += 1
                    st["edges_moved"] += cat.conn.execute(
                        "UPDATE relations SET dst_id = ? WHERE dst_id = ?",
                        (held, r["stable_id"])).rowcount
                    cat.record_rendition(held, source, r["stable_id"], commit=False)
                    cat.conn.execute("DELETE FROM citations WHERE src_id = ?", (r["stable_id"],))
                    cat.conn.execute("DELETE FROM relations WHERE src_id = ?", (r["stable_id"],))
                    cat.conn.execute("DELETE FROM citation_aliases WHERE dst_id = ?",
                                     (r["stable_id"],))
                    cat.conn.execute("DELETE FROM documents WHERE stable_id = ?",
                                     (r["stable_id"],))
                    st["documents_deleted"] += 1
                if st["duplicates"] % 200 == 0:
                    _progress(on_progress, stage="folding duplicate renditions",
                              done=st["duplicates"], total=len(rows))
        self._invalidate_caches()
        return {**st, "sample": samples}

    def repair_de_citations(self, *, dry_run: bool = False,
                            on_progress=None, cancel_check=None) -> dict:
        """Re-validate every German citation against the CURRENT German grammar and drop
        the ones it would no longer mint.

        The German grammars are deliberately open-ended — bundesrecht accepts any
        abbreviation-shaped tail, and a docket is recognised near a court name — and at
        corpus scale that let three families of phantom through:

        - an ordinary German word where a law abbreviation belongs, because the pattern
          must END on a law and German capitalises its nouns ("§ 100 Absatz 1 Satz 1" →
          de/gesetz/satz1, "§ 100a Rn" → de/gesetz/rn, cited from 7,676 documents);
        - the next word swallowed as a book numeral ("MarkenG i.V.m." → de/gesetz/markeng1,
          "BGB v Smith" → de/gesetz/bgb5) — phantom siblings of laws already held;
        - a docket read off a report series ("BSG SozR 4-1500" → de:case:BSG:ZR4-1500) or
          off the *next* court in a judgment's header ("BGH … vorgehend KG Berlin … 10 U
          54/19" → de:case:BGH:10U54/19).

        The test is the grammar itself, not a list: each distinct (candidate, raw string)
        pair is re-extracted, and a candidate survives if ANY of the strings that minted
        it still mints it. That makes this the standing migration for a grammar fix — it
        will keep working after the next one.

        Only PENDING edges are deleted: a German citation that resolved to a held
        judgment or statute is a real link, and stays even if the grammar has since
        narrowed (the count of those is reported, never acted on). ``dry_run`` counts
        without deleting. Re-running finds nothing."""
        from .citations.german import german_citations

        st = {"pairs_checked": 0, "candidates": 0, "phantom_candidates": 0,
              "kept_resolved": 0, "citations_deleted": 0, "edges_deleted": 0}
        with self._open() as (cat, _rs, _ts):
            valid: set[str] = set()
            seen: set[str] = set()
            # One aggregate over the German citation rows: the same raw string repeats
            # thousands of times, so the distinct (candidate, raw) pairs are a fraction of
            # the rows (~416k of 2.4M on the live corpus) and each costs one bounded regex.
            cur = cat.conn.execute(
                "SELECT candidate_id, raw FROM citations "
                "WHERE method IN ('de_law_reference', 'de_case_reference') "
                "AND candidate_id IS NOT NULL GROUP BY candidate_id, raw")
            for r in cur:
                if cancel_check and cancel_check():
                    return {**st, "cancelled": True}
                st["pairs_checked"] += 1
                cand = r["candidate_id"]
                seen.add(cand)
                if cand in valid:
                    continue
                if any(c.candidate_id == cand for c in german_citations(r["raw"] or "")):
                    valid.add(cand)
                if st["pairs_checked"] % 5000 == 0:
                    _progress(on_progress, stage="re-extracting German citations",
                              done=st["pairs_checked"], total=None, item=cand)
            st["candidates"] = len(seen)
            phantom = sorted(seen - valid)
            # A phantom that nevertheless RESOLVED points at a real document — leave it
            # alone and say so, rather than cutting a live edge on a grammar change.
            resolved: set[str] = set()
            for i in range(0, len(phantom), 500):
                chunk = phantom[i:i + 500]
                qs = ",".join("?" for _ in chunk)
                resolved.update(row["candidate_id"] for row in cat.conn.execute(
                    f"SELECT DISTINCT candidate_id FROM relations WHERE candidate_id IN ({qs}) "
                    "AND resolution_status <> 'pending'", chunk).fetchall())
            drop = [c for c in phantom if c not in resolved]
            st["phantom_candidates"] = len(drop)
            st["kept_resolved"] = len(resolved)
            if dry_run or not drop:
                return {**st, "sample": drop[:20]}
            for i in range(0, len(drop), 500):
                if cancel_check and cancel_check():
                    break
                chunk = drop[i:i + 500]
                qs = ",".join("?" for _ in chunk)
                with cat._atomic():
                    st["citations_deleted"] += cat.conn.execute(
                        f"DELETE FROM citations WHERE candidate_id IN ({qs}) "
                        "AND method IN ('de_law_reference', 'de_case_reference')",
                        chunk).rowcount
                    st["edges_deleted"] += cat.conn.execute(
                        f"DELETE FROM relations WHERE candidate_id IN ({qs}) "
                        "AND resolution_status = 'pending'", chunk).rowcount
                _progress(on_progress, stage="dropping phantom German references",
                          done=min(i + 500, len(drop)), total=len(drop))
        self._invalidate_caches()  # worklist/frontier/dashboard all counted the phantoms
        return {**st, "sample": drop[:20]}

    def resegment_judgments(self, *, source: str | None = None, limit: int = 400000,
                            on_progress=None, cancel_check=None) -> dict:
        """Recompute paragraph structure from text that is ALREADY stored.

        The text is not touched — only the segment index beside it — so no character
        offset moves and every citation stays anchored exactly where it was. That is
        what makes this safe to run over a live corpus, unlike a reparse.

        Its purpose is to pick up segmentation improvements retroactively: judgments
        imported before ``_split_author_labels`` existed have their first judge's
        byline ("LORD JUSTICE BURNETT:") stranded in the preamble, in no segment at
        all and so never rendered, while the concurrences below appear as trailing
        text of the paragraph above them.

        Where a document already HAS segments they are improved in place rather than
        replaced. A third of uk-caselaw has a stored segmentation that differs from
        what flat text yields, and it differs by having MORE paragraphs — it came from
        the import's own HTML structure, which sees paragraphs the sequential-number
        guard has to reject. Throwing that away to gain a byline would be a bad trade,
        so the byline split is applied to the stored segments; only a document with no
        segmentation at all is derived from scratch."""
        from .core.segmentation import _split_author_labels, recover_numbered_segments

        st = {"scanned": 0, "improved": 0, "derived": 0, "headings_added": 0,
              "unchanged": 0, "unreadable": 0}
        seen: set[str] = set()
        with self._open() as (cat, _rs, ts):
            rows = cat.conn.execute(
                "SELECT stable_id, payload_hash FROM documents WHERE has_text = 1 "
                + ("AND source = ? " if source else "")
                + "ORDER BY stable_id LIMIT ?",
                ((source, limit) if source else (limit,))).fetchall()
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                if n % 2000 == 0:
                    _progress(on_progress, stage="recomputing paragraph structure",
                              done=n, total=len(rows), item=r["stable_id"])
                ph = r["payload_hash"]
                if not ph or ph in seen:
                    continue
                seen.add(ph)
                st["scanned"] += 1
                try:
                    text = ts.get(ph)
                except OSError:
                    st["unreadable"] += 1
                    continue
                old = list(ts.get_segments(ph) or [])
                recovered, derived = recover_numbered_segments(text, old)
                fresh = recovered if derived else _split_author_labels(text, old)
                if not fresh or len(fresh) == len(old):
                    st["unchanged"] += 1
                    continue
                ts.put_segments(ph, fresh)
                st["derived" if derived else "improved"] += 1
                st["headings_added"] += (len([s for s in fresh if s.kind == "heading"])
                                         - len([s for s in old if s.kind == "heading"]))
        return st

    def backfill_ag_names(self, *, limit: int = 20000, on_progress=None,
                          cancel_check=None) -> dict:
        """Fill in WHO wrote each held AG Opinion, from the Opinion's own first page.

        CELLAR's metadata doesn't carry the Advocate General, and these documents arrive
        with an empty title, so their OSCOLA citation rendered as "Case C-526/24
        EU:C:2025:723, Opinion of AG" — a citation with a hole in it. The name is printed
        on the face of every Opinion ("OPINION OF ADVOCATE GENERAL EMILIOU delivered on
        15 May 2025"), so this reads the stored text (no network) and writes
        ``advocate_general`` into the document's metadata, which the OSCOLA formatter
        already knows how to use. Idempotent — skips opinions that already have a name."""
        from .adapters.eu_cellar import EUCellarAdapter, parse_ag_opinion_head

        st = {"scanned": 0, "from_cellar": 0, "from_document": 0, "already": 0,
              "unnamed": 0, "no_text": 0}
        cellar = EUCellarAdapter()
        BATCH = 200
        with self._open() as (cat, _rs, ts):
            rows = [dict(r) for r in cat.conn.execute(
                "SELECT stable_id, payload_hash, meta_json FROM documents "
                "WHERE source = 'eu-cellar' AND doc_type = 'opinion' AND is_latest = 1 "
                "ORDER BY stable_id LIMIT ?", (limit,)).fetchall()]
            todo: list[tuple[dict, dict]] = []          # (row, meta) still needing a name
            for r in rows:
                st["scanned"] += 1
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                if meta.get("advocate_general"):
                    st["already"] += 1
                    continue
                todo.append((r, meta))

            for i in range(0, len(todo), BATCH):
                if cancel_check and cancel_check():
                    break
                chunk = todo[i: i + BATCH]
                celex_of = {r["stable_id"]: (m.get("celex") or r["stable_id"]) for r, m in chunk}
                # one SPARQL per 200 opinions — CELLAR models the AG as a relation, so this
                # is the authoritative answer; the printed heading only fills its gaps.
                names = cellar.advocate_generals(list(celex_of.values()))
                for r, meta in chunk:
                    sid = r["stable_id"]
                    name = names.get(celex_of[sid])
                    source = "cellar"
                    # Read the document either way: it carries the delivery date, which
                    # the metadata does not, and the name whenever CELLAR has none.
                    try:
                        text = ts.get(r["payload_hash"]) if r["payload_hash"] else None
                    except OSError:
                        text = None
                    if text is None and not name:
                        st["no_text"] += 1
                        continue
                    printed = parse_ag_opinion_head(text) if text else {}
                    extra = {k: v for k, v in printed.items() if k != "advocate_general"}
                    if not name:
                        name, source = printed.get("advocate_general"), "document"
                    if not name:
                        st["unnamed"] += 1   # an Opinion of the COURT, or an unparsed scan
                        continue
                    cat.set_document_meta(
                        sid, {**meta, **extra, "advocate_general": name,
                              "advocate_general_source": source}, commit=False)
                    st["from_cellar" if source == "cellar" else "from_document"] += 1
                cat.commit()
                _progress(on_progress, stage="naming AG opinions",
                          done=min(i + BATCH, len(todo)), total=len(todo))
            cat.commit()
        self._invalidate_caches()
        return st

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
            elif doc["source"] == "uk-caselaw":
                # Find Case Law stores LegalDocML judgments.  Byte sniffing sees the
                # Akoma Ntoso envelope and would otherwise send these through the
                # legislation parser, losing judgment headings and native paragraph
                # boundaries.  Keep the same source-selected parser used at harvest.
                from .adapters.uk_caselaw import parse_judgment
                text, _relations, _ncn, segments = parse_judgment(raw)
                fmt = "uk-caselaw-judgment"
            else:
                # Older harvests can pre-date (or omit) a byte signature that the
                # current sniffer knows about.  The importer records the parser format
                # in document metadata, so prefer that durable projection hint and use
                # byte sniffing only as a fallback.  This is especially important for
                # LawMaker pages whose surrounding site template changes over time.
                meta = cat.document_meta(stable_id)
                hinted = str(meta.get("format") or "").strip().lower()
                # A GOV.UK publication is HTML the sniffer cannot tell from any other
                # page, but its structure is entirely knowable — so the SOURCE selects
                # the parser. Without this a reparse would fall back to the generic
                # extractor and put the cookie banner back.
                if doc["source"] == "uk-ipa-codes":
                    hinted = "govuk-govspeak"
                fmt = hinted if hinted in {
                    "akn", "bwb", "formex-legislation", "lawmaker-html",
                    "govuk-govspeak",
                    # An EUR-Lex HTML page is HTML the byte sniffer cannot tell from any
                    # other page, so without the durable hint a reparse fell through to
                    # "no structural format" and did nothing. That is 22,637 instruments —
                    # every EU act with no Formex rendition — permanently unable to pick
                    # up a parser fix, including the one that gives their annexes their
                    # own segments.
                    "eurlex-html",
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
            if _would_flatten(ts, doc["payload_hash"], segments):
                return {"stable_id": stable_id, "reparsed": False,
                        "reason": "would flatten held structure", "segments": len(segments)}
            ts.put(doc["payload_hash"], text)            # overwrite (same hash → same path)
            ts.put_segments(doc["payload_hash"], segments)
            # Currency facts the raw carries but an earlier harvest did not read. The raw
            # is canonical (§1.2), so these are re-derivable exactly like text and
            # segments — and re-deriving them here is the difference between one local
            # pass and re-downloading 100,027 acts to learn something already on disk.
            currency = _currency_from_raw(doc["source"], stable_id, raw)
            updated = []
            if currency:
                meta = dict(cat.document_meta(stable_id) or {})
                block = dict(meta.get("currency") or {})
                changed = {k: v for k, v in currency.items() if block.get(k) != v}
                if changed:
                    block.update(changed)
                    meta["currency"] = block
                    cat.set_document_meta(stable_id, meta)
                    updated = sorted(changed)
            return {"stable_id": stable_id, "reparsed": True, "format": fmt,
                    "segments": len(segments), "currency_updated": updated}

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

    def reparse_pending_eu_notices(self, *, limit: int = 20000, on_progress=None,
                                   cancel_check=None) -> dict:
        """Re-derive already-held CN/TN notices from their immutable raw: the structured
        form (named sections, one segment per numbered question, footnotes lifted out of
        the sentences they were spliced into), the docketed title, and the citations —
        which are re-mined because the pinpoints were being read out of the mangled text
        the old flat parse produced.

        A projection refresh, not a re-fetch (§1.2): the OJ notice itself never changes.
        """
        from pathlib import Path

        from .adapters.eu_cellar import (
            celex_case_number, extract_formex, pending_formex_title,
        )
        from .citations import extract_document

        out = {"candidates": 0, "reparsed": 0, "retitled": 0, "recited": 0}
        with self._open() as (cat, _rs, ts):
            rows = [r for r in cat.list_documents(source="eu-cellar", limit=200000)
                    if str(r["doc_type"]) == "note"][:limit]
            out["candidates"] = len(rows)
            for i, row in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                _progress(on_progress, stage="reparsing pending notices", done=i,
                          total=len(rows), item=row["stable_id"])
                doc = cat.get_document(row["stable_id"])
                if doc is None or not doc["raw_path"] or not doc["payload_hash"]:
                    continue
                try:
                    raw = Path(doc["raw_path"]).read_bytes()
                except OSError:
                    continue
                text, segments = extract_formex(raw)
                if text:
                    ts.put(doc["payload_hash"], text)
                    ts.put_segments(doc["payload_hash"], segments)
                    out["reparsed"] += 1
                    extract_document(cat, ts, row["stable_id"])
                    out["recited"] += 1
                meta = _row_meta(doc)
                if not meta.get("pending"):
                    continue        # retired: its title is history, leave it alone
                celex = str(meta.get("celex") or row["stable_id"])
                name = pending_formex_title(raw)
                case_no = celex_case_number(celex)
                if not name:
                    continue
                title = name if not case_no or case_no in name else f"{name} ({case_no})"
                title = title if title.startswith("Pending:") else f"Pending: {title}"
                if title != (doc["title"] or ""):
                    cat.update_document_fields(row["stable_id"], {"title": title},
                                               curate=False)
                    out["retitled"] += 1
            Resolver(cat).run()
        self._invalidate_caches()
        return out

    def repair_oj_wrapper_notices(self, *, limit: int = 2000, batch: int = 40,
                                  on_progress=None, cancel_check=None) -> dict:
        """Re-fetch notices whose stored raw is the OJ issue's masthead, not the notice.

        The Formex archive ships TWO wrappers beside the item — the issue's masthead and
        a bibliographic manifest — and the unzip took first one and then the other, so
        these notices were stored as an OJ front page or as a run of manifest fields: no
        parties to read a case name from ("Pending: Case T-8/24") and no questions. The
        wrapper is what we kept, so reparsing cannot recover them; only the source can.
        Identified from the stored bytes rather than from the symptom — which is what
        lets one pass clean up after both mistakes — and re-fetched in batches through
        the normal pipeline.
        """
        from pathlib import Path

        from .adapters.eu_cellar import _is_wrapper
        from .adapters.registry import get_adapter
        from .citations import extract_corpus
        from .pipeline import Pipeline

        damaged: list[str] = []
        with self._open() as (cat, _rs, _ts):
            # The scan reads a file per note over ~64k rows and used to emit nothing
            # until the first re-fetch, so a run that was working looked identical to a
            # run that was hung — and a job whose heartbeat is quiet for long enough is
            # reaped as stalled. Report while scanning.
            seen = 0
            for row in cat.list_documents(source="eu-cellar", limit=200000):
                seen += 1
                if seen % 5000 == 0:
                    _progress(on_progress, stage="scanning stored notices for wrappers",
                              done=len(damaged), total=0,
                              item=f"{seen} documents read")
                    if cancel_check and cancel_check():
                        return {"cancelled": True, "damaged": len(damaged)}
                if str(row["doc_type"]) != "note" or len(damaged) >= limit:
                    continue
                doc = cat.get_document(row["stable_id"])
                if doc is None or not doc["raw_path"]:
                    continue
                try:
                    # 1200 bytes, not 400: the manifest is recognised by <BIB.DOC>
                    # following its <DOC> root, which sits past the XML declaration and
                    # the schema URL. A 400-byte read sees the root and misses the proof.
                    head = Path(doc["raw_path"]).read_bytes()[:1200]
                except OSError:
                    continue
                if _is_wrapper(head):
                    celex = str((_row_meta(doc) or {}).get("celex") or row["stable_id"])
                    damaged.append(celex)
        out: dict = {"damaged": len(damaged), "refetched": 0, "failed_batches": 0,
                     "failed": []}
        for start in range(0, len(damaged), batch):
            if cancel_check and cancel_check():
                out["cancelled"] = True
                break
            chunk = damaged[start:start + batch]
            _progress(on_progress, stage="re-fetching OJ-wrapper notices",
                      done=start, total=len(damaged), item=chunk[0])
            # One batch is ~40 CELEXes behind a SPARQL query and 40 archive fetches
            # against a service that times out routinely. Letting that propagate ended
            # the whole 1,161-notice repair on its FIRST batch, having fixed nothing.
            # A failed batch is skipped and named; the pass is re-runnable, and the next
            # run re-derives its scope from the stored bytes, so anything still damaged
            # is simply picked up again.
            try:
                with self._open() as (cat, rs, ts):
                    adapter = get_adapter("eu-cellar", celexes=",".join(chunk))
                    # backfill ignores the watermark, refetch_held re-pulls what we hold
                    # — the point is precisely to replace the stored bytes.
                    Pipeline(cat, rs, textstore=ts).run(
                        adapter, backfill=True, refetch_held=True)
                    for celex in chunk:
                        extract_corpus(cat, ts, stable_id=celex)
                    Resolver(cat).run()
            except Exception as exc:  # noqa: BLE001 — a flaky source must not end the pass
                out["failed_batches"] += 1
                out["failed"].extend(chunk)
                log.warning("[oj-repair] batch %d-%d failed, continuing: %s",
                            start, start + len(chunk), exc)
                continue
            out["refetched"] += len(chunk)
        self._invalidate_caches()
        return out

    def retitle_preparatory_documents(self, *, limit: int = 5000,
                                      on_progress=None, cancel_check=None) -> dict:
        """Give held preparatory documents their own titles, read from what we already
        store — no re-fetch.

        A proposal's title is on its face ("Proposal for a REGULATION … laying down
        additional procedural rules relating to the enforcement of Regulation (EU)
        2016/679"), above the enacting terms the HTML parser keeps; 332 of 963 were
        therefore titled with their own CELEX. Reports what it could NOT fix, because
        most of those are metadata-only records with no English rendition at all — a
        gap upstream, not one this pass can close.
        """
        from pathlib import Path

        from .adapters.eu_preparatory import title_from_html, title_from_text

        out = {"untitled": 0, "retitled": 0, "no_content": 0, "unreadable": 0}
        with self._open() as (cat, _rs, ts):
            rows = cat.list_documents(source="eu-preparatory", limit=limit)
            for i, row in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                doc = cat.get_document(row["stable_id"])
                if doc is None or (doc["title"] or "") != row["stable_id"]:
                    continue
                out["untitled"] += 1
                _progress(on_progress, stage="retitling preparatory documents",
                          done=i, total=len(rows), item=row["stable_id"])
                if not doc["has_text"] and not doc["raw_path"]:
                    out["no_content"] += 1
                    continue
                title = None
                if doc["has_text"] and doc["payload_hash"]:
                    try:
                        title = title_from_text(ts.get(doc["payload_hash"]))
                    except OSError:
                        title = None
                if not title and doc["raw_path"]:
                    try:
                        title = title_from_html(Path(doc["raw_path"]).read_bytes())
                    except OSError:
                        title = None
                if title and title != row["stable_id"]:
                    cat.update_document_fields(row["stable_id"], {"title": title},
                                               curate=False)
                    out["retitled"] += 1
                else:
                    out["unreadable" if doc["has_text"] else "no_content"] += 1
        self._invalidate_caches()
        return out

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

    def repair_eu_split_annexes(self, *, after_stable_id: str = "",
                                limit: int = 100000, on_progress=None,
                                cancel_check=None) -> dict:
        """Repair held EU acts whose Formex annexes were previously omitted.

        This is a local, resumable projection repair: immutable raw ZIPs are inspected
        without touching the network. It covers both annexes split into sibling XML
        members and sector-0 ``CONS.ANNEX`` elements. Qualifying acts are reparsed and
        their citations re-extracted so offsets and annex pinpoints match the regenerated
        text. The checkpoint advances only after both operations complete.
        """
        from pathlib import Path

        from .citations import extract_document
        from .formats.formex import unzip_formex_contents

        cursor = after_stable_id or ""
        checked = eligible = reparsed = reextracted = failed = 0
        complete = True
        cap = max(1, int(limit or 100000))

        with self._open() as (cat, _rs, _ts):
            total = int(cat.conn.execute(
                "SELECT count(*) AS n FROM documents "
                "WHERE source = 'eu-legislation' AND doc_type = 'legislation' "
                "AND raw_path LIKE '%.zip' AND stable_id > ?",
                (cursor,),
            ).fetchone()["n"])

        while checked < min(total, cap):
            with self._open() as (cat, _rs, _ts):
                rows = [dict(r) for r in cat.conn.execute(
                    "SELECT stable_id, raw_path FROM documents "
                    "WHERE source = 'eu-legislation' AND doc_type = 'legislation' "
                    "AND raw_path LIKE '%.zip' AND stable_id > ? "
                    "ORDER BY stable_id LIMIT 250",
                    (cursor,),
                ).fetchall()]
            if not rows:
                break
            for row in rows:
                if checked >= cap or (cancel_check and cancel_check()):
                    complete = False
                    break
                sid = row["stable_id"]
                cursor = sid
                checked += 1
                try:
                    raw = Path(row["raw_path"]).read_bytes()
                    members = unzip_formex_contents(raw)
                    qualifies = any(
                        b"<ANNEX" in member.upper()
                        or b"<CONS.ANNEX" in member.upper()
                        for member in members
                    )
                except OSError:
                    failed += 1
                    qualifies = False
                if qualifies:
                    eligible += 1
                    result = self.reparse_document(stable_id=sid)
                    if result.get("reparsed"):
                        reparsed += 1
                        # Reparse can move text offsets and introduces citations from
                        # annexes. Re-mine this document before advancing the checkpoint.
                        with self._open() as (cat, _rs, ts):
                            extract_document(cat, ts, sid)
                        reextracted += 1
                    else:
                        failed += 1
                _progress(
                    on_progress,
                    stage="repairing EU Formex annexes",
                    done=checked,
                    total=min(total, cap),
                    item=sid,
                    eligible=eligible,
                    reparsed=reparsed,
                    _checkpoint={
                        "phase": "repair",
                        "source": "eu-legislation",
                        "after_stable_id": sid,
                    },
                )
            if not complete:
                break

        if checked < total and checked >= cap:
            complete = False
        if reextracted:
            with self._open() as (cat, _rs, _ts):
                Resolver(cat).run()
            self._invalidate_caches()
        return {
            "checked": checked,
            "eligible": eligible,
            "reparsed": reparsed,
            "citations_reextracted": reextracted,
            "failed": failed,
            "after_stable_id": cursor,
            "remaining": max(0, total - checked),
            "complete": complete,
        }

    def sync_eu_consolidations(self, *, stable_id: str, on_progress=None,
                               cancel_check=None) -> dict:
        """Import every dated Cellar expression for one held sector-3 act.

        Called automatically when a reader opens an EU base act whose consolidation
        lineage is absent. The completion timestamp prevents a genuinely
        unconsolidated act causing another external lookup on every page view.
        """
        from .eu_law import consolidation_base

        base = consolidation_base(stable_id) or stable_id
        if not re.fullmatch(r"3\d{4}[RLD]\d{4}", base or "", re.I):
            return {"error": "stable_id must be a sector-3 Regulation, Directive or Decision CELEX"}
        result = self.harvest(
            "eu-legislation",
            backfill=True,
            max_pages=None,
            options={"celex": base, "include_consolidations": "true"},
            force_full=True,
            # The adapter rediscovers this act's small, complete version set on every
            # retry, and Pipeline carries any of THOSE held-but-unextracted records into
            # extraction.  A source-wide unfinished scan here turns a three-version
            # reader-triggered lookup into a ~20k-document EU citation backfill, making
            # the targeted job look frozen and competing with the dedicated sector-0
            # sweep.  Source-wide cursor recovery belongs to that sweep, not this sync.
            resume_unfinished=False,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )
        if not (cancel_check and cancel_check()) and not result.get("error"):
            with self._open() as (cat, _rs, _ts):
                meta = cat.document_meta(base)
                meta["consolidations_checked_at"] = _now_iso()
                cat.set_document_meta(base, meta)
        return {"base_id": base, **result}

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

        from .formats import available as available_formats
        from .formats import parse as parse_format
        # Trusted stored ``meta.format`` values → used directly, instead of re-sniffing.
        #
        # Every name the registry knows, rather than a hand-kept list. The list was kept
        # by hand twice and was wrong both times: "eurlex-html" had to be added after a
        # whole-source reparse silently declined to touch a third of the source (5,178
        # skipped in one 12,000-document sample), and it was still missing eisb-xml,
        # eisb-html, nz-pco-xml, lims-xml, hklm-xml, hudoc-html, lawmaker-html,
        # ep-ta-xml, formex-resolution, legislation-en-xml and frl-epub — about 45,000
        # documents across Ireland, New Zealand, Canada, Hong Kong, Strasbourg and
        # Australia that no parser fix has ever reached, because a sniffer cannot tell
        # one XML or HTML dialect from another and answers None → "skip".
        #
        # The stored value came from the adapter that fetched the bytes, so it is better
        # evidence than sniffing them; the only question is whether we still have a
        # parser under that name, which is what the registry answers.
        hints = set(available_formats())

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

            # What went wrong, and where. A bad file must never stop the sweep — but
            # swallowing the exception entirely made "44 failed" out of 61,340 a number
            # with no way to act on it: no id, no reason, nowhere to look.
            failures: list[dict] = []

            # Currency the raw states but an earlier harvest never read, collected by the
            # worker threads and written by the main thread with the rest of the batch —
            # only the textstore is safe to write from here.
            pending_currency: dict[str, dict] = {}

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
                    # Never trade real structure for one undifferentiated block — over a
                    # whole source this flattens thousands of documents unseen.
                    if _would_flatten(ts, r["payload_hash"], pd.segments):
                        return "skip"
                    ts.put(r["payload_hash"], pd.text)
                    ts.put_segments(r["payload_hash"], pd.segments)
                    # Re-derivable from the same bytes, so take it while the file is open:
                    # the alternative is re-downloading the whole source to learn something
                    # already on disk.
                    fresh = _currency_from_raw(source, r["stable_id"], raw)
                    if fresh:
                        block = dict(meta.get("currency") or {})
                        if any(block.get(k) != v for k, v in fresh.items()):
                            block.update(fresh)
                            pending_currency[r["stable_id"]] = {**meta, "currency": block}
                    return "ok"
                except Exception as exc:  # noqa: BLE001 — reported, never raised
                    log.warning("[reparse] %s (%s): %s: %s", r["stable_id"],
                                r.get("raw_path"), exc.__class__.__name__, exc)
                    if len(failures) < 200:      # bounded: a systemic fault would flood
                        failures.append({"stable_id": r["stable_id"],
                                         "error": f"{exc.__class__.__name__}: {exc}"[:200]})
                    return "fail"

            done = 0
            reanchored = 0
            currency_updated = 0
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
                    for sid, meta in pending_currency.items():
                        cat.set_document_meta(sid, meta, commit=False)
                    currency_updated += len(pending_currency)
                    pending_currency.clear()
                    cat.commit()
                    cursor = chunk[-1]["stable_id"]
                    _progress(on_progress, stage=f"reparsing {source}", done=done, total=total,
                              item=cursor, _checkpoint={"phase": "reparse", "source": source,
                                                        "after_stable_id": cursor})
        return {"source": source, "total": total, "reparsed": ok, "skipped": skip,
                "failed": fail, "offsets_reanchored": reanchored,
                "currency_updated": currency_updated,
                # the ids and reasons, so a failure count is something to act on
                **({"failures": failures[:50]} if failures else {})}

    def _reanchor_chunk(self, cat, ts, id_to_hash: dict) -> tuple[int, int, int]:
        """Re-anchor the citation offsets of a batch of documents to their current text.
        ``id_to_hash`` maps stable_id → payload_hash (both callers already hold it, so no
        extra document lookup). Reads each doc's citations in one query, re-locates each
        ``raw`` in the current text, and batches the offset updates (uncommitted — the
        caller commits). Returns ``(offsets_fixed, docs_changed, unlocatable)``."""
        from .citations.reanchor import reanchor

        if not id_to_hash:
            return 0, 0, 0
        # A citation's position is stored TWICE — citations.char_start/end, which the
        # reader highlights from, and relations.context_start/end, which the "all
        # mentions" previews mark from. Re-anchoring only the first left the two 52
        # characters apart on reparsed eu-cellar judgments: the judgment page highlighted
        # the citation, and the preview of that same citation highlighted the words before
        # it. Both tables are walked here, by the same in-order sweep.
        by_src: dict[str, list] = {}
        for r in cat.citations_for_many(list(id_to_hash)):
            by_src.setdefault(r["src_id"], []).append(r)
        rel_by_src: dict[str, list] = {}
        for r in cat.relation_spans_for_many(list(id_to_hash)):
            rel_by_src.setdefault(r["src_id"], []).append(r)

        updates: list[tuple[int, int, int]] = []
        rel_updates: list[tuple[int, int, int]] = []
        changed_docs: set[str] = set()
        unlocatable = 0
        texts: dict[str, str] = {}
        for sid, ph in id_to_hash.items():
            try:
                texts[sid] = ts.get(ph) or ""
            except OSError:
                continue
        for sid, rows in by_src.items():
            text = texts.get(sid)
            if text is None:
                continue
            ups, miss = reanchor(text, rows)
            unlocatable += miss
            if ups:
                updates.extend(ups)
                changed_docs.add(sid)
        for sid, rows in rel_by_src.items():
            text = texts.get(sid)
            if text is None:
                continue
            ups, miss = reanchor(text, rows)
            unlocatable += miss
            if ups:
                changed_docs.add(sid)
            rel_updates.extend(ups)
        cat.reanchor_citation_offsets(updates, commit=False)
        cat.reanchor_relation_offsets(rel_updates, commit=False)
        return len(updates) + len(rel_updates), len(changed_docs), unlocatable

    def reanchor_source(self, *, source: str, court: str | None = None,
                        after_stable_id: str = "",
                        on_progress=None, cancel_check=None) -> dict:
        """Re-anchor a whole source's stored citation offsets to its CURRENT text — the
        cheap, reliable repair for a corpus that was reparsed (text regenerated) without
        re-extraction, so its ``citations`` char spans drifted (the fr-dila/de-rii
        paragraphing pass). Unlike :meth:`rescan`, this re-runs no grammar, re-resolves
        nothing, and rewrites no edges — the raw strings, candidates, pinpoints and
        resolved targets are all still correct; only ``char_start``/``char_end`` move. One
        citations SELECT + one batched UPDATE per chunk; keyset-paginated and resumable
        from the ``after_stable_id`` checkpoint."""
        court_sql = " AND lower(COALESCE(court, '')) = lower(?)" if court else ""
        court_params = [court] if court else []
        with self._open() as (cat, _rs, ts):
            total = cat.conn.execute(
                "SELECT count(*) AS n FROM documents WHERE source=? AND payload_hash IS NOT NULL "
                f"AND stable_id > ?{court_sql}",
                (source, after_stable_id or "", *court_params)).fetchone()["n"]
            done = fixed = docs_changed = unlocatable = 0
            cursor = after_stable_id or ""
            batch = 2000
            while True:
                if cancel_check and cancel_check():
                    break
                chunk = [dict(r) for r in cat.conn.execute(
                    "SELECT stable_id, payload_hash FROM documents "
                    "WHERE source=? AND payload_hash IS NOT NULL AND stable_id > ? "
                    f"{court_sql} ORDER BY stable_id LIMIT ?",
                    (source, cursor, *court_params, batch)).fetchall()]
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
        return {"source": source, "court": court, "total": total,
                "docs_reanchored": docs_changed,
                "offsets_fixed": fixed, "unlocatable": unlocatable}

    def _enrich_cited(self, cat, rs, ts, doc_ids, *, limit: int = 100,
                      on_progress=None, cancel_check=None) -> dict:
        """One hop only: fetch the routable authorities that ``doc_ids`` cite but the corpus
        doesn't hold, then extract + resolve the new documents. This is the enrichment a
        watch runs over each newly harvested case — pull the cases and instruments it relies
        on, once — with no further outward crawl."""
        cands: list[str] = []
        seen: set[str] = set()
        for sid in doc_ids:
            for c in cat.citations_for(sid):
                cand = c["candidate_id"]
                if cand and cand not in seen and cat.find_document_id(cand) is None:
                    seen.add(cand)
                    cands.append(cand)
        newly: list[str] = []
        attempts = 0
        target = min(limit, len(cands))
        for cand in cands:
            if len(newly) >= limit or attempts >= limit * 3:
                break
            if cancel_check and cancel_check():
                break
            attempts += 1
            _progress(on_progress, stage="fetching cited authorities", done=len(newly),
                      total=target, item=cand)
            res = self._fetch_reference(cat, rs, ts, ref=cand, candidate=cand)
            if res.get("stored") or res.get("present"):
                newly.append(res["candidate"])
        self._extract_ids(cat, ts, newly)
        Resolver(cat).run_for_documents(newly)
        return {"cited_candidates": len(cands), "fetched": len(newly)}

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
        """Fetch the current official English ECHR text and store article-level structure.

        Paragraphs and lettered points are separate citable segments (``Article 5(1)(a)``),
        while their canonical article family remains ``Article 5`` for the citator.
        """
        from .core.http import build_client
        from .formats.echr_pdf import (
            OFFICIAL_ECHR_CONVENTION_URL,
            parse_echr_convention_pdf,
        )

        client = build_client(timeout=45)
        resp = client.get(OFFICIAL_ECHR_CONVENTION_URL)
        resp.raise_for_status()
        parsed = parse_echr_convention_pdf(resp.content)
        with self._open() as (cat, _rs, ts):
            self._store_echr_convention(cat, ts, parsed)
        return {
            "stable_id": "echr/convention", "created": True, "source": "ECHR official PDF",
            "articles": len({s.label.split("(", 1)[0] for s in parsed[1]
                             if s.label.startswith("Article ")}),
        }

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
                   "https://rm.coe.int/1680a2353d", "is_authoritative": True,
                   "as_amended": "Protocol No. 15, in force 2021-08-01"},
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
        # Read a bounded top-slice of the (roll-up-backed, citing_count-ranked) worklist
        # rather than the whole thing: classifying all ~930k groups on every drain tick is
        # what made auto-drain "never start". A generous multiple of the batch survives the
        # routable/cooled filtering below; the long tail is reached over successive ticks
        # (and by the nightly harvest-all). `limit=None` (nightly "everything") still scans
        # all — that's its job, and it runs when the box is idle.
        scan = None if limit is None else max(limit * 60, 2000)
        candidates = [r for r in self.unresolved_references(limit=scan)
                      if r.get("fetchable") and r["confidence"] != "low"
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

        # Group by source and drain the sources CONCURRENTLY. Each reference's source is a
        # different server (CourtListener, legislation.gov.uk, CELLAR, HUDOC, CanLII…) that
        # doesn't compete with the others, so serialising across them wasted almost all the
        # wall-clock. Crucially it also fixes the old failure mode: one rate-limited source
        # (US case law routinely caps out) used to `break` the WHOLE batch, starving every
        # other source — now rate-limiting stops only ITS worker. Within a source the fetch
        # stays serial (one connection, one worker) so per-source rate limits/budgets are
        # never raced.
        import threading
        from collections import defaultdict
        from concurrent.futures import ThreadPoolExecutor

        by_source: dict[str, list] = defaultdict(list)
        for r in rows:
            by_source[r["suggested_adapter"]].append(r)
        # Run every distinct API concurrently — they're different servers, so the ceiling is
        # simply "how many distinct sources are in the queue" (rarely more than ~15-20), not a
        # small fixed number. Network calls are cheap to parallelise; the cap only guards the
        # DB connection pool (each worker holds one while its source drains) — set
        # RAGLEX_HARVEST_SOURCE_WORKERS alongside RAGLEX_PG_POOL_MAX to go wider.
        try:
            cap = max(1, int(_os.environ.get("RAGLEX_HARVEST_SOURCE_WORKERS") or 16))
        except (TypeError, ValueError):
            cap = 16
        n_workers = min(len(by_source), cap) or 1

        fetched, fetched_ids, failed = [], [], []
        absent, transient, rate_limited_sources = [], [], []
        lock = threading.Lock()
        done_ctr = {"n": 0}
        total = len(rows)

        def _drain_source(src: str, src_rows: list) -> None:
            with self._open() as (cat, rs, ts):
                for r in src_rows:
                    if cancel_check and cancel_check():
                        return
                    res = self._fetch_reference(cat, rs, ts, ref=r["ref"], candidate=r["candidate"])
                    outcome = res.get("outcome")
                    ok = outcome in ("stored", "present")
                    with lock:
                        done_ctr["n"] += 1
                        if ok:
                            fetched.append({"ref": r["ref"]})
                            fetched_ids.append(res["candidate"])
                        else:
                            failed.append({"ref": r["ref"], "outcome": outcome,
                                           **({} if "error" not in res else {"error": res["error"]})})
                            # "no_adapter" is OUR gap, not the source's answer — the
                            # worklist shouldn't have offered it (see TARGETED_ADAPTERS).
                            # Cooling it as a miss would mark a live document absent for
                            # three months and keep it away from the adapter that lands
                            # next week, so it goes on neither list.
                            if outcome == "absent":
                                absent.append(r["candidate"])
                            elif outcome != "no_adapter":
                                transient.append(r["candidate"])
                        error = res.get("error")
                        if not ok and outcome == "transient":
                            error = (f"temporary; eligible again after the "
                                     f"{retry_ttl_days * 24:g}h retry cooldown"
                                     + (f" — {error}" if error else ""))
                        elif not ok and outcome == "rate_limited":
                            error = ("source paused; remaining items stay queued"
                                     + (f" — {error}" if error else ""))
                        _progress(on_progress, stage="harvesting", done=done_ctr["n"], total=total,
                                  item=res.get("candidate") or r["ref"], ok=ok,
                                  outcome=outcome, msg=error if not ok else None)
                    if outcome == "rate_limited":
                        # This source is throttling — stop draining IT (its remaining refs
                        # would all "fail" for reasons that say nothing about them), but let
                        # every other source keep going.
                        with lock:
                            rate_limited_sources.append(src)
                        return

        if by_source:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                list(pool.map(lambda kv: _drain_source(*kv), by_source.items()))

        with self._open() as (cat, rs, ts):
            if absent:
                cat.record_enrichment_misses("harvest-miss", absent)
            if transient:
                cat.record_enrichment_misses("harvest-retry", transient)
            # extract just the newly-fetched docs, then resolve once — both AFTER the
            # fetch loop, so report them as their own stages (this is the phase that
            # looked "stuck" because the progress bar had finished the harvest loop).
            # NB: extraction stays SERIAL on purpose. Fetching is I/O-bound (parallel is
            # free), but citation extraction is CPU/GIL-bound — threading it wouldn't use
            # more cores (the GIL serialises it anyway) and a burst of concurrent regex
            # passes is exactly what has frozen the whole API before. One doc at a time,
            # yielding the GIL between them (the job worker's yield_s), keeps the box
            # responsive and within CPU.
            self._extract_ids(cat, ts, fetched_ids, on_progress=on_progress)
            _progress(on_progress, stage="resolving citations",
                      done=0, total=len(fetched_ids))
            # bounded: only the fetched docs' own edges + edges pointing at them can
            # newly resolve — the whole-graph pass here cost minutes per drain batch
            resolved = Resolver(cat).run_for_documents(fetched_ids)
        rate_limited = bool(rate_limited_sources)
        self._invalidate_caches()  # refresh the worklist's per-source "remaining" counts
        remaining = len(candidates) - skipped - len(fetched)
        return {"attempted": len(rows), "harvested": len(fetched),
                "resolved_edges": resolved.resolved, "failed": failed,
                "absent": len(absent), "retry_later": len(transient),
                "rate_limited": rate_limited,
                # which sources threw in the towel (throttling) — the rest still drained
                "rate_limited_sources": sorted(set(rate_limited_sources)),
                "sources_drained": len(by_source), "source_workers": n_workers,
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

    def point_in_time_target(self, base_id: str, date: str, *,
                             autofetch: bool = True) -> tuple[str, dict]:
        """The held id for ``base_id`` as it stood on ``date``, fetching it if need be.

        "Which text applied then?" is a question about a date, not about what happens to
        be harvested — and legislation.gov.uk serves a dated version of everything it
        publishes. So a specific date is answerable on demand: this returns the dated id
        if we hold it, fetches it if we can, and otherwise says why not and leaves the
        caller reading the current text KNOWING that is what it got."""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            return base_id, {"error": "as_at must be a date, YYYY-MM-DD"}
        base = base_id.split("@")[0]
        dated = f"{base}@{date}"
        with self._open() as (cat, _rs, _ts):
            if cat.get_document(dated) is not None:
                return dated, {"as_at": date, "base_id": base, "fetched": False}
            doc = cat.get_document(base)
            source = doc["source"] if doc is not None else None
        if source != "uk-legislation":
            return base_id, {
                "as_at": None, "requested": date, "base_id": base,
                "unavailable": ("point-in-time text is served by legislation.gov.uk; "
                                f"this instrument is held from {source!r}. The text "
                                "returned is the current one."),
            }
        if not autofetch:
            return base_id, {"as_at": None, "requested": date, "base_id": base,
                             "unavailable": "not held, and autofetch is off"}
        try:
            res = self.harvest_legislation_at(stable_id=base, date=date)
        except Exception as exc:  # noqa: BLE001 — a fetch failure must not lose the answer
            return base_id, {"as_at": None, "requested": date, "base_id": base,
                             "unavailable": f"could not fetch: {exc}"}
        if res.get("present"):
            return dated, {"as_at": date, "base_id": base, "fetched": True}
        return base_id, {
            "as_at": None, "requested": date, "base_id": base,
            "unavailable": (res.get("error")
                            or f"legislation.gov.uk served no version at {date}"),
        }

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
        """Held dated expressions of legislation: UK point-in-time records and EU
        consolidation snapshots, all linked back to the undated/base instrument."""
        from .eu_law import consolidation_base, is_consolidation

        base = consolidation_base(stable_id) or _act_level(stable_id.split("@")[0])
        with self._open() as (cat, _rs, _ts):
            requested = cat.get_document(stable_id)
            requested_meta = _row_meta(requested) if requested is not None else {}
            if is_consolidation(stable_id) and requested_meta.get("consolidation_of"):
                base = str(requested_meta["consolidation_of"])
            base_doc = cat.get_document(base)
            versions = []
            for sid, version_date in reversed(cat.legislative_versions(base)):
                row = cat.get_document(sid)
                versions.append({
                    "stable_id": sid,
                    "date": version_date,
                    "title": row["title"] if row is not None else None,
                    "kind": "consolidation" if is_consolidation(sid) else "point_in_time",
                })
            return {
                "base_id": base,
                "versions": versions,
                "can_fetch_point_in_time": bool(
                    base_doc is not None and base_doc["source"] == "uk-legislation"
                ),
            }

    def _materialize_fr_legislation_parents_open(self, cat, ts, parent_ids: list[str]) -> list[str]:
        """Aggregate DILA LEGI article rows into searchable, citable statute nodes."""
        from .formats.dila_xml import parse_dila_article
        from .citations.french import text_alias
        from .core.models import Record, Segment, DocType, ExtractedVia
        from xml.etree import ElementTree as _ET

        parents = list(dict.fromkeys(p for p in parent_ids if p and p.startswith("LEGITEXT")))
        made: list[str] = []
        meta_expr = ("meta_json::jsonb ->> 'code_cid'" if cat.backend == "postgres"
                     else "json_extract(meta_json, '$.code_cid')")
        for parent in parents:
            existing = cat.get_document(parent)
            if existing is not None and existing["has_text"] and existing["source"] != "fr-dila":
                continue
            rows = cat.conn.execute(
                f"SELECT * FROM documents WHERE source='fr-dila' AND doc_type='legislation' "
                f"AND {meta_expr} = ? ORDER BY stable_id", (parent,)).fetchall()
            parsed = []
            for row in rows:
                raw_path = row["raw_path"]
                if not raw_path:
                    continue
                try:
                    art = parse_dila_article(_ET.fromstring(Path(raw_path).read_bytes()))
                except (OSError, _ET.ParseError):
                    continue
                parsed.append((row, art))
            if not parsed:
                continue

            # One current/best version per article number. DILA's 2999 date is an
            # open-ended sentinel, not a publication date; substantive text wins before
            # recency so an empty historical shell cannot erase the article.
            by_num: dict[str, tuple] = {}
            for row, art in parsed:
                num = art.num or row["stable_id"]
                key = (bool((art.text or "").strip()), art.etat == "VIGUEUR",
                       art.date_debut.isoformat() if art.date_debut and art.date_debut.year < 2999 else "")
                old = by_num.get(num)
                if old is None or key > old[0]:
                    by_num[num] = (key, row, art)

            def number_key(value: str):
                return [int(x) if x.isdigit() else x.casefold()
                        for x in re.split(r"(\d+)", value)]

            chunks: list[str] = []
            segments: list[Segment] = []
            relations = []
            seen_rel: set[tuple[str, str]] = set()
            for num in sorted(by_num, key=number_key):
                _key, row, art = by_num[num]
                body = (art.text or "").strip()
                if body:
                    value = f"Article {num}\n{body}"
                    if chunks:
                        chunks.append("\n\n")
                    start = sum(map(len, chunks))
                    chunks.append(value)
                    segments.append(Segment(f"Article {num}", start, start + len(value),
                                            kind="article"))
                for rel in art.relations:
                    target = rel.dst_id or ""
                    if not target.startswith(("JORFTEXT", "LEGITEXT")):
                        continue
                    rel_key = (str(rel.relationship_type), target)
                    if rel_key not in seen_rel and target != parent:
                        seen_rel.add(rel_key)
                        relations.append(rel)

            exemplar = next((art for _row, art in parsed if art.full_title), parsed[0][1])
            title = re.sub(r"\s*\(\d+\)\.?\s*$", "", exemplar.full_title
                           or exemplar.code_title or parent).strip()
            subject_title = re.sub(
                r"(?i)^loi\s+n(?:o|°)\s*\d{2,4}-\d+\s+du\s+"
                r"\d{1,2}\s+\S+\s+\d{4}\s+", "Loi ", title).strip()
            aliases = [x for x in (exemplar.jorf_cid,
                                    text_alias(exemplar.text_number)
                                    if exemplar.text_number else None) if x]
            text = "".join(chunks) or None
            record = Record(
                source="fr-dila", stable_id=parent, doc_type=DocType.LEGISLATION,
                title=title, decision_date=exemplar.signature_date,
                language="fr", source_language="fr",
                landing_url=(f"https://www.legifrance.gouv.fr/loda/id/{exemplar.jorf_cid}"
                             if exemplar.jorf_cid else
                             f"https://www.legifrance.gouv.fr/codes/texte_lc/{parent}"),
                text=text, segments=segments, relations=relations,
                extracted_via=ExtractedVia.STRUCTURED,
                extra={"fond": "LEGI", "materialized_from_articles": len(parsed),
                       "jorf_cid": exemplar.jorf_cid,
                       "text_number": exemplar.text_number,
                       "nature": exemplar.nature, "aliases": aliases},
            )
            record.ensure_payload_hash()
            text_path = None
            if text and record.payload_hash:
                text_path = str(ts.put(record.payload_hash, text, source=record.source))
                ts.put_segments(record.payload_hash, segments)
            cat.upsert_document(record, text_path=text_path)
            for alias in aliases:
                cat.put_alias(str(alias).casefold(), parent, source="fr-legislation-parent")
            # Conseil constitutionnel decisions are titled with the law they review.
            # Preserve both documents, but connect the decision to the statute instead
            # of letting an exact-title search make the decision look like the law.
            from .core.models import RelationshipType, ResolutionStatus, TypedRelation
            decisions = cat.conn.execute(
                "SELECT stable_id FROM documents WHERE source='fr-dila' "
                "AND doc_type='decision' AND (lower(title)=lower(?) OR lower(title)=lower(?) "
                "OR lower(title) LIKE '%' || lower(?) || '%')",
                (title, subject_title, subject_title)).fetchall()
            for decision in decisions:
                already = cat.conn.execute(
                    "SELECT 1 FROM relations WHERE src_id=? AND candidate_id=? LIMIT 1",
                    (decision["stable_id"], parent)).fetchone()
                if not already:
                    cat.add_relation(decision["stable_id"], TypedRelation(
                        relationship_type=RelationshipType.CONSIDERS,
                        raw_citation_string=title, dst_id=parent,
                        extracted_via=ExtractedVia.STRUCTURED,
                        resolution_status=ResolutionStatus.PENDING,
                    ))
            made.append(parent)
        return made

    def materialize_fr_legislation(self, *, parent_ids: list[str]) -> dict:
        """Public targeted repair/backfill for full French statute nodes."""
        with self._open() as (cat, _rs, ts):
            made = self._materialize_fr_legislation_parents_open(cat, ts, parent_ids)
            from .citations import extract_corpus
            for stable_id in made:
                extract_corpus(cat, ts, stable_id=stable_id)
            Resolver(cat).run_for_documents(made)
        self._invalidate_caches()
        return {"requested": len(parent_ids), "materialized": len(made), "ids": made}

    def refresh_uk_legislation(self, *, stable_id: str) -> dict:
        """Re-fetch one held legislation.gov.uk instrument on explicit user request."""
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline
        from .citations import extract_corpus

        base = _act_level((stable_id or "").split("@")[0])
        with self._open() as (cat, rs, ts):
            existing = cat.get_document(base)
            if existing is None:
                return {"error": "document is not held", "stable_id": base}
            if existing["source"] != "uk-legislation":
                return {"error": "refresh is available only for legislation.gov.uk documents",
                        "stable_id": base}
            before = existing["payload_hash"]
            adapter = get_adapter("uk-legislation", ids=base, patient=True)
            stats = Pipeline(cat, rs, textstore=ts).run(
                adapter, backfill=True, refetch_held=True, max_pages=1)
            current = cat.get_document(base)
            if current is None:
                return {"error": "publisher returned no document", "stable_id": base}
            changed = current["payload_hash"] != before
            # Re-extract even when only structured metadata/effects changed: a refresh
            # is an explicit request for all projections to agree with the new source.
            extract_corpus(cat, ts, stable_id=base)
            Resolver(cat).run_for_documents([base])
            fetched_at = current["fetched_at"]
        self._invalidate_caches()
        return {"stable_id": base, "refreshed": True, "changed": changed,
                "stored": stats.stored, "fetched_at": fetched_at}

    def ensure_uk_legislation_original(self, *, stable_id: str) -> dict:
        """Hold the official ``/enacted`` rendition used by ``original=true`` reads."""
        from .adapters.registry import get_adapter
        from .pipeline import Pipeline
        from .citations import extract_corpus

        base = _act_level((stable_id or "").split("@", 1)[0])
        enacted_id = f"{base}@enacted"
        with self._open() as (cat, rs, ts):
            base_row = cat.get_document(base)
            if base_row is None:
                return {"error": "document is not held", "stable_id": base}
            if base_row["source"] != "uk-legislation":
                return {"error": "original retrieval is available only for legislation.gov.uk documents",
                        "stable_id": base}
            held = cat.get_document(enacted_id)
            if held is not None and held["has_text"]:
                return {"stable_id": enacted_id, "base_id": base, "as_enacted": True,
                        "fetched": False}
            adapter = get_adapter(
                "uk-legislation", ids=base, version_date="enacted", patient=True)
            stats = Pipeline(cat, rs, textstore=ts).run(
                adapter, backfill=True, refetch_held=True, max_pages=1)
            original = cat.get_document(enacted_id)
            if original is None or not original["has_text"]:
                return {"error": "publisher returned no enacted text", "stable_id": base}
            extract_corpus(cat, ts, stable_id=enacted_id)
            Resolver(cat).run_for_documents([enacted_id])
        self._invalidate_caches()
        return {"stable_id": enacted_id, "base_id": base, "as_enacted": True,
                "fetched": True, "stored": stats.stored}

    def ensure_uk_legislation_current(self, *, stable_id: str) -> dict:
        """Cheaply verify one UK rendition, downloading it only when it changed.

        The scheduled static export calls this for every UK law it publishes. A HEAD
        comparison makes the steady state one small request per law; missing provenance,
        a moved Last-Modified value, or an undated base escalates to the full refresh.
        """
        from .adapters.registry import get_adapter
        from .adapters.uk_legislation import _last_modified

        base = _act_level((stable_id or "").split("@")[0])
        with self._open() as (cat, _rs, _ts):
            row = cat.get_document(base)
            if row is None:
                return {"error": "document is not held", "stable_id": base}
            if row["source"] != "uk-legislation":
                return {"error": "currency check is available only for legislation.gov.uk documents",
                        "stable_id": base}
            meta = _row_meta(row)
            held_stamp = meta.get("source_last_modified")
            as_at = ((meta.get("currency") or {}).get("as_at")
                     if isinstance(meta.get("currency"), dict) else None)
        adapter = get_adapter("uk-legislation", ids=base, patient=True)
        try:
            stub = next(adapter.discover(None, max_pages=1))
            response = adapter._client.request("HEAD", stub.raw_url, raise_for_4xx=False)
            served_stamp = _last_modified(response)
        except Exception:  # noqa: BLE001 — a full GET gives the authoritative outcome
            served_stamp = None
        if held_stamp and served_stamp == held_stamp and as_at:
            return {"stable_id": base, "checked": True, "refreshed": False,
                    "changed": False, "source_last_modified": served_stamp,
                    "as_at": as_at}
        result = self.refresh_uk_legislation(stable_id=base)
        return {"checked": True, **result}

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

    def check_uk_currency(self, *, limit: int = 200, max_age_days: float = 30) -> dict:
        """Ask legislation.gov.uk whether the text we hold is still the text it serves.

        The effects queue answers a narrower question — "have the amendments this act
        already knows about been applied yet?" — and an act drops out of it the moment
        its backlog reaches zero. Nothing then re-checks that act unless some *other*
        act's changes feed names it, so an act quietly revised in place can stay stale
        indefinitely while every field on the page says it is current.

        This is the direct check, and it is cheap because it never downloads the act: a
        HEAD against the rendition we stored, comparing the publisher's ``Last-Modified``
        with the one recorded at harvest. Only when they differ is the act flagged for a
        real re-pull, through the same queue the effects machinery drains. Acts stored
        before that header was recorded have nothing to compare against and are skipped
        rather than guessed at — they acquire a marker on their next harvest.
        """
        from .adapters.uk_legislation import BASE_URL, _last_modified
        from .adapters.registry import get_adapter

        with self._open() as (cat, _rs, _ts):
            done = cat.enrichment_misses("currency-head", max_age_days=max_age_days)
            rows = cat.list_documents(source="uk-legislation", doc_type="legislation",
                                      limit=max(limit * 5, 1000))
            todo = []
            for r in rows:
                sid = r["stable_id"]
                if "@" in sid or sid in done:
                    continue          # dated snapshots are immutable; skip recent checks
                stamp = (_row_meta(r) or {}).get("source_last_modified")
                if stamp:
                    todo.append((sid, str(stamp)))
                if len(todo) >= limit:
                    break
        if not todo:
            return {"checked": 0, "stale": 0, "unchanged": 0, "errors": 0, "ids": []}
        adapter = get_adapter("uk-legislation")
        stale, unchanged, errors, ids = 0, 0, 0, []
        for sid, held in todo:
            try:
                resp = adapter._client.request(
                    "HEAD", f"{BASE_URL}/{sid}/data.akn", raise_for_4xx=False)
                current = _last_modified(resp)
            except Exception:  # noqa: BLE001 — one unreachable act mustn't stop the sweep
                errors += 1
                continue
            if not current:
                errors += 1
            elif current == held:
                unchanged += 1
            else:
                stale += 1
                ids.append(sid)
        if ids:
            with self._open() as (cat, _rs, _ts):
                for sid in ids:
                    # due NOW, through the queue refresh_effects already drains
                    cat.mark_effects_due(sid, [])
        with self._open() as (cat, _rs, _ts):
            cat.record_enrichment_misses("currency-head", [sid for sid, _ in todo])
        return {"checked": len(todo), "stale": stale, "unchanged": unchanged,
                "errors": errors, "ids": ids[:50]}

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
            from .resolve.matchers import assimilated_canonical_path
            # group by affected instrument; track distinct effects + any unapplied ones
            by_affected: dict[str, dict] = {}
            for e in effects:
                affected_id = e.affected_id
                canonical = assimilated_canonical_path(affected_id)
                if canonical and cat.get_document(canonical) is not None:
                    affected_id = canonical
                if not affected_id or affected_id == base:
                    continue
                g = by_affected.setdefault(affected_id, {"effects": [], "unapplied": 0})
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
        from .citations.snowball import UK_LEG_TYPES

        with self._open() as (cat, _rs, _ts):
            done = cat.enrichment_misses("changes-feed", max_age_days=max_age_days)
            rows = cat.list_documents(source="uk-legislation", doc_type="legislation", limit=2000)
            todo = [r["stable_id"] for r in rows
                    if r["stable_id"] not in done
                    and "@" not in r["stable_id"]
                    # legislation.gov.uk also serves assimilated EU instruments under
                    # eur/eudr/eudn/european ids, but its Changes-to-Legislation service
                    # has no affecting feed for them.  Letting those into this bounded
                    # queue spent every tick on guaranteed 404s.
                    and r["stable_id"].split("/", 1)[0].lower() in UK_LEG_TYPES][:limit]
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
        from .citations.courts import ni_division_alias as _ni_division_alias
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
            ni = _ni_division_alias(stable_id)
            if ni and ni != stable_id.lower():
                cat.put_alias(ni, stable_id, source="ni-division-alias", commit=False)
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
        from .citations.courts import ni_division_alias as _ni_division_alias
        from .resolve.matchers import first_candidate
        from .core.text import fold

        names = load_name_index(names_csv) if names_csv else {}
        st = {"total": 0, "imported": 0, "secondary": 0, "no_slug": 0, "named": 0,
              "aliases": 0, "citation_mismatch": 0, "merged_surrogate": 0, "extracted": 0}
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
                        ni = _ni_division_alias(slug)
                        if ni and ni != slug:
                            alias_pairs.append((ni, "ni-division-alias"))

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

                        if slug not in existing and self._adopt_surrogate_duplicate(
                                cat, slug, secondary):
                            existing.add(slug)
                            st["merged_surrogate"] = st.get("merged_surrogate", 0) + 1

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

    # An id minted only because no citation-derived identity was available at import time
    # (a Westlaw report slug, WL number or content hash). It names a real case, but by the
    # weakest key in the ladder — so a copy that arrives later under its neutral citation
    # should absorb it rather than sit beside it.
    _SURROGATE_ID_RE = re.compile(r"^westlaw:", re.I)

    def _adopt_surrogate_duplicate(self, cat, target: str, citations) -> str | None:
        """Fold a held SURROGATE copy of this case into ``target`` before importing it.

        The Westlaw importer already checks whether a case it is about to key by a
        surrogate is *already* held under a real citation — but only forwards. A Westlaw
        RTF imported BEFORE the same case's BAILII/FCL copy therefore stayed a permanent
        duplicate: Donoghue v Stevenson was held twice, as ``westlaw:1932-a-c-562`` (08:28)
        and ``ukhl/1932/100`` (18:11 the same day), with nothing to collapse them. Close
        the loop from the other side — if a parallel report citation of the incoming case
        resolves to a held surrogate, re-key that document onto ``target``, carrying its
        text, edges, aliases, tags and versions with it (:meth:`Catalogue.rekey_document`).

        Precise identifiers only: a report citation names one case, a party name does not
        ("Harris v Harris"). Returns the absorbed id, or None if there was nothing to fold.
        """
        from .core.text import fold
        from .resolve.matchers import first_candidate

        for c in citations:
            cand = first_candidate(c)
            key = fold(cand.value) if cand else fold(c or "")
            if not key or key == target:
                continue
            held = cat.get_alias(key)
            if (not held or held == target
                    or not self._SURROGATE_ID_RE.match(held)
                    or cat.get_document(held) is None):
                continue
            cat.rekey_document(held, target, commit=False)
            # the retired id stays resolvable: old links and any edge minted against it
            # still land on the surviving document.
            cat.put_alias(fold(held), target, source="merged-surrogate", commit=False)
            return held
        return None

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
        from .citations import extract_citations
        from .citations.courts import IRISH_COURTS
        from .citations.name_variants import name_variants
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .pipeline.runner import _chamberless_alias
        from .citations.courts import ni_division_alias as _ni_division_alias
        from .resolve.matchers import first_candidate
        from .core.text import fold

        st = {"total": 0, "imported": 0, "superseded": 0, "secondary": 0,
              "unparseable": 0, "aliases": 0, "merged_surrogate": 0, "extracted": 0}
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
                ni = _ni_division_alias(slug)
                if ni and ni != slug:
                    alias_pairs.append((ni, "ni-division-alias"))

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
                if existing is None and self._adopt_surrogate_duplicate(
                        cat, slug, parsed.citations):
                    existing = cat.get_document(slug)
                    st["merged_surrogate"] = st.get("merged_surrogate", 0) + 1
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
            # The pooled extractor, not a serial loop. A zip of BAILII pages or a
            # Westlaw export is exactly the scale where the serial shape costs most:
            # one core of N, the named-alias map rebuilt from the DB per document,
            # and a progress callback per document. The pool batches its own commits.
            from .citations import extract_documents_parallel
            ex = extract_documents_parallel(
                cat, ts, to_extract, aliases=cat.named_alias_map(),
                on_progress=on_progress, cancel_check=cancel_check)
            st["extracted"] = ex.processed
            cat.commit()
            resolved_edges = 0
            if ex.cancelled:
                # Don't grind through the (long, un-interruptible) resolve after a
                # cancel — the rows are committed, and the bulk post-process job
                # resolves them later. Say plainly that the pass stopped early.
                st["cancelled"] = True
            else:
                _progress(on_progress, stage="resolving citations", done=0, total=0)
                resolved_edges = Resolver(cat).run().resolved
        st["resolved_edges"] = resolved_edges
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
              "unparseable": 0, "aliases": 0, "merged_surrogate": 0, "extracted": 0}
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
        from .citations.courts import ni_division_alias as _ni_division_alias
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
        # …and a pre-neutral case may already be held under a Westlaw surrogate imported
        # before this copy existed — absorb it instead of standing up a duplicate.
        if existing is None and self._adopt_surrogate_duplicate(
                cat, target, parsed.self_citations):
            existing = cat.get_document(target)
            st["merged_surrogate"] = st.get("merged_surrogate", 0) + 1

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
        ni = _ni_division_alias(slug)
        if ni and ni != slug and ni != target:
            alias_pairs.append((ni, "ni-division-alias"))

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
        from .citations.courts import IRISH_COURTS
        from .citations.name_variants import name_variants
        from .core.models import AddedBy, DocType, ExtractedVia, Record, sha256_bytes
        from .pipeline.runner import _chamberless_alias
        from .citations.courts import ni_division_alias as _ni_division_alias
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
                    ni = _ni_division_alias(stable_id)
                    if ni and ni != stable_id:
                        alias_pairs.append((ni, "ni-division-alias"))

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
            # The pooled extractor, not a serial loop. A zip of BAILII pages or a
            # Westlaw export is exactly the scale where the serial shape costs most:
            # one core of N, the named-alias map rebuilt from the DB per document,
            # and a progress callback per document. The pool batches its own commits.
            from .citations import extract_documents_parallel
            ex = extract_documents_parallel(
                cat, ts, to_extract, aliases=cat.named_alias_map(),
                on_progress=on_progress, cancel_check=cancel_check)
            st["extracted"] = ex.processed
            cat.commit()
            resolved_edges = 0
            if ex.cancelled:
                # Don't grind through the (long, un-interruptible) resolve after a
                # cancel — the rows are committed, and the bulk post-process job
                # resolves them later. Say plainly that the pass stopped early.
                st["cancelled"] = True
            else:
                _progress(on_progress, stage="resolving citations", done=0, total=0)
                resolved_edges = Resolver(cat).run().resolved
        st["resolved_edges"] = resolved_edges
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

    # The GOV.UK feeds that minted ids under their own source key before every GOV.UK
    # feed moved to one namespace. Their stable_ids are "<source>/<gov.uk base path>",
    # so the new id is the same path under "govuk/" — no re-fetch, no re-parse.
    _LEGACY_GOVUK_PREFIXES = ("uk-cma-guidance", "uk-cma", "uk-ofgem", "uk-ofwat")
    _GOVUK_NAMESPACE = "govuk"

    #: IP completion day. EU material decided on or before this governs the assimilated
    #: text; what Luxembourg said afterwards does not, and presenting it as authority on
    #: the UK provision would be a legal error rather than an untidy result.
    ASSIMILATION_CUTOFF = "2020-12-31"

    _ARTICLE_ANCHOR = re.compile(r"^(Article\s+\d+[A-Z]*)\b", re.IGNORECASE)

    def _article_anchors(self, cat, ts, stable_id: str) -> dict[str, str]:
        """``{"article 15": "Article 15"}`` for a held instrument, from its own segments.

        Keyed on the folded form so the two sides compare, and valued with the label as
        the document actually writes it, because that is what a pinpoint has to match.

        Falls back to the instrument's latest readable version when the base act itself
        carries no article structure — which is the ordinary case for assimilated law,
        whose base node is fetched from a path that serves a landing page rather than the
        text. That version is what the reader opens anyway.
        """
        anchors = self._anchors_of(cat, ts, stable_id)
        if not anchors:
            current = cat.latest_readable_version(stable_id)
            if current:
                anchors = self._anchors_of(cat, ts, current)
        return anchors

    def _anchors_of(self, cat, ts, stable_id: str) -> dict[str, str]:
        doc = cat.get_document(stable_id)
        if doc is None or not doc["payload_hash"]:
            return {}
        try:
            segments = ts.get_segments(doc["payload_hash"]) or []
        except OSError:
            return {}
        out: dict[str, str] = {}
        for seg in segments:
            m = self._ARTICLE_ANCHOR.match(str(seg.label or "").strip())
            if m:
                label = " ".join(m.group(1).split())
                out.setdefault(label.lower(), label)
        return out

    def map_assimilated_provisions(self, *, stable_id: str | None = None,
                                   apply: bool = False, limit: int | None = None,
                                   include_predecessors: bool = True,
                                   on_progress=None, cancel_check=None) -> dict:
        """Link every assimilated UK regulation's articles to their EU originals.

        An assimilated instrument is the EU text, adopted word for word, so Luxembourg's
        reading of Article 15 before exit day is a reading of the very words the UK
        provision contains. Those are the citers a UK lawyer needs and cannot otherwise
        see: the corpus holds them against the CELEX node, and nothing joins the two.

        Three constraints, all of them load-bearing:

        * **Only where the provision survived.** The article set is INTERSECTED, taken
          from each document's own segments. An article the UK dropped simply has no
          counterpart, and a UK insertion (Article 22A, 8ZA) has no EU one — so neither
          is mapped, without needing a list of them.
        * **Exit day.** ``inherit_before`` stops at IP completion day.
        * **Jurisdiction-locked to the EU.** Every member state cites the GDPR; without
          the lock the UK instrument would be buried under the rest of Europe instead of
          informed by it. Only EU-source material travels.

        ``include_predecessors`` carries the chain one step further: where the EU
        instrument itself maps back to a predecessor (the Data Protection Directive
        behind the GDPR), the same correspondence is written from the UK article, under
        the same cutoff and lock.

        DRY RUN unless ``apply=True``.
        """
        from .resolve.matchers import assimilated_celex

        st = {"instruments": 0, "mappings": 0, "predecessor_mappings": 0,
              "skipped_no_segments": 0, "applied": apply}
        plan: list[dict] = []
        with self._open() as (cat, _rs, ts):
            if stable_id:
                targets = [str(stable_id)]
            else:
                targets = [r["stable_id"] for r in cat.conn.execute(
                    "SELECT stable_id FROM documents WHERE stable_id LIKE ? "
                    "AND stable_id NOT LIKE ? ORDER BY stable_id",
                    ("european/%", "%@%"))]
            if limit:
                targets = targets[:limit]
            for n, uk_id in enumerate(targets, 1):
                if cancel_check and cancel_check():
                    break
                celex = assimilated_celex(uk_id)
                if not celex or cat.get_document(celex) is None:
                    continue
                uk_anchors = self._article_anchors(cat, ts, uk_id)
                eu_anchors = self._article_anchors(cat, ts, celex)
                if not uk_anchors or not eu_anchors:
                    st["skipped_no_segments"] += 1
                    continue
                shared = sorted(set(uk_anchors) & set(eu_anchors),
                                key=lambda k: (len(k), k))
                if not shared:
                    continue
                st["instruments"] += 1
                rows = [{"current_anchor": uk_anchors[k],
                         "previous_anchor": eu_anchors[k],
                         "mapping_type": "equivalent",
                         "inherit_before": self.ASSIMILATION_CUTOFF,
                         "source_jurisdiction": "European Union",
                         "note": "Assimilated text: the EU original's own words."}
                        for k in shared]
                st["mappings"] += len(rows)
                entry = {"uk": uk_id, "eu": celex, "articles": len(rows),
                         "dropped_in_uk": len(set(eu_anchors) - set(uk_anchors)),
                         "uk_only": len(set(uk_anchors) - set(eu_anchors))}
                # …and the EU instrument's own predecessors, one step back
                inherited: list[dict] = []
                if include_predecessors:
                    for pm in cat.conn.execute(
                            "SELECT previous_doc_id, previous_anchor, current_anchor "
                            "FROM provision_mappings WHERE current_doc_id = ?",
                            (celex,)):
                        key = str(pm["current_anchor"] or "").lower()
                        if key in uk_anchors:
                            inherited.append({
                                "previous_doc_id": str(pm["previous_doc_id"]),
                                "current_anchor": uk_anchors[key],
                                "previous_anchor": str(pm["previous_anchor"]),
                            })
                    entry["predecessor_mappings"] = len(inherited)
                    st["predecessor_mappings"] += len(inherited)
                plan.append(entry)
                if apply:
                    cat.upsert_provision_mappings(uk_id, celex, rows,
                                                  created_by="structured", replace=False)
                    by_prev: dict[str, list[dict]] = {}
                    for row in inherited:
                        by_prev.setdefault(row["previous_doc_id"], []).append({
                            "current_anchor": row["current_anchor"],
                            "previous_anchor": row["previous_anchor"],
                            "mapping_type": "functional_predecessor",
                            "inherit_before": self.ASSIMILATION_CUTOFF,
                            "source_jurisdiction": "European Union",
                            "note": "Carried through the assimilated text's EU original.",
                        })
                    for prev_id, prev_rows in by_prev.items():
                        cat.upsert_provision_mappings(uk_id, prev_id, prev_rows,
                                                      created_by="structured",
                                                      replace=False)
                _progress(on_progress, stage="mapping assimilated provisions", done=n,
                          total=len(targets), item=uk_id)
        if apply:
            self._invalidate_caches()
        st["plan"] = plan[:500]
        return st

    def merge_assimilated_duplicates(self, *, apply: bool = False,
                                     limit: int | None = None,
                                     on_progress=None, cancel_check=None) -> dict:
        """Fold assimilated EU law held twice onto one node.

        legislation.gov.uk serves an assimilated instrument on two paths, and the corpus
        took both as identities: the type-code form the Atom feeds emit
        (``eur/2016/679``) and the canonical form the reader, the citation grammars and
        every stored edge use (``european/regulation/2016/0679``). 4,171 instruments were
        therefore stored twice — the UK GDPR among them — and 40,042 citations landed on
        the copy nothing else pointed at, which is why a heavily-cited instrument could
        show almost no citations on the page a reader actually opens.

        The canonical node wins. :meth:`Catalogue.rekey_document` merges the serving-form
        copy into it, carrying its text, edges, aliases and tags; where only the
        serving-form node exists it is a plain rename, so nothing is lost either way. The
        retired id stays resolvable as an alias.

        Idempotent. DRY RUN unless ``apply=True``.
        """
        from .core.text import fold
        from .resolve.matchers import assimilated_canonical_path

        st = {"scanned": 0, "merged": 0, "renamed": 0, "unchanged": 0, "applied": apply}
        changes: list[dict] = []
        with self._open() as (cat, _rs, _ts):
            rows = cat.conn.execute(
                "SELECT stable_id FROM documents WHERE stable_id LIKE ? OR "
                "stable_id LIKE ? OR stable_id LIKE ? OR stable_id LIKE ? "
                "ORDER BY stable_id",
                ("eur/%", "eudr/%", "eudn/%", "eudc/%")).fetchall()
            if limit:
                rows = rows[:limit]
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                st["scanned"] += 1
                cur = str(r["stable_id"])
                # A dated expression keeps its suffix: eur/2016/679@2024-01-01 belongs on
                # european/regulation/2016/0679@2024-01-01, beside its own base.
                stem, version = cat.version_base_and_date(cur)
                canonical = assimilated_canonical_path(stem)
                target = (f"{canonical}@{version}" if canonical and version
                          else canonical)
                if not target or target == cur:
                    st["unchanged"] += 1
                    continue
                merging = cat.get_document(target) is not None
                changes.append({"old": cur, "new": target,
                                "kind": "merge" if merging else "rename"})
                if apply:
                    action = cat.rekey_document(cur, target, commit=False)
                    cat.put_alias(fold(cur), target, source="assimilated-merge",
                                  commit=False)
                    st["merged" if action == "merge" else "renamed"] += 1
                    if n % 200 == 0:
                        cat.commit()
                _progress(on_progress, stage="merging assimilated duplicates", done=n,
                          total=len(rows), item=cur)
            if apply:
                cat.commit()
        if apply:
            self._invalidate_caches()
        st["changes"] = changes[:5000]
        return st

    def rekey_govuk_ids(self, *, apply: bool = False, limit: int | None = None,
                        on_progress=None, cancel_check=None) -> dict:
        """Move GOV.UK documents onto the shared ``govuk/<base_path>`` namespace.

        Every GOV.UK feed now mints ids in one namespace, because the feeds genuinely
        overlap — 268 CMA publications are also in the cross-government
        ``policy_and_engagement`` corpus, and the CMA's own guidance was harvested by
        both ``uk-cma`` and ``uk-cma-guidance``. Keyed by source, the same page is stored
        two or three times; keyed by base path it is one document whichever feed found
        it. Documents harvested before that change still carry the old keys and would
        not dedupe against the new ones, so the next harvest would store twins.

        This is a RE-KEY, not a re-harvest. :meth:`Catalogue.rekey_document` cascades
        every reference — citations, relations, aliases, embeddings, tags, assets,
        version history — so nothing is lost and nothing is downloaded again; where the
        target id already exists (the ``uk-cma`` / ``uk-cma-guidance`` twins) the two
        rows MERGE and the duplicate's edges fold into the survivor. Deleting the rows
        instead would discard all of that and force a full re-fetch of ~6,400 documents
        for no gain, and the corpus is append-only by design (§1.4a).

        The retired id is kept as an alias of the new one, so a link, an export or an
        edge minted against ``uk-cma/…`` still lands on the document.

        Idempotent — an id already in the namespace is a no-op, so it is safe to re-run.
        DRY RUN unless ``apply=True``; the dry run lists every planned change.
        """
        from collections import Counter

        from .core.text import fold

        st = {"scanned": 0, "rekeyed": 0, "merged": 0, "unchanged": 0, "applied": apply}
        changes: list[dict] = []
        with self._open() as (cat, _rs, _ts):
            rows: list = []
            for prefix in self._LEGACY_GOVUK_PREFIXES:
                rows.extend(cat.conn.execute(
                    "SELECT stable_id, source FROM documents WHERE stable_id LIKE ?",
                    (f"{prefix}/%",)).fetchall())
            # A document may be listed once per matching prefix ("uk-cma/" also matches
            # nothing of "uk-cma-guidance/", but keep this honest against future prefixes).
            seen_ids: set[str] = set()
            rows = [r for r in rows
                    if not (r["stable_id"] in seen_ids or seen_ids.add(r["stable_id"]))]
            # Deterministic order, so a dry run and the apply that follows it plan the
            # same survivor for each merged pair rather than whichever the DB listed first.
            rows.sort(key=lambda r: r["stable_id"])
            if limit:
                rows = rows[:limit]
            claimed: set[str] = set()
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                st["scanned"] += 1
                cur = r["stable_id"]
                prefix, _, path = cur.partition("/")
                # Only ever re-key a genuine "<legacy source>/<path>" id. Anything whose
                # remainder is empty is malformed and is left exactly where it is.
                if prefix not in self._LEGACY_GOVUK_PREFIXES or not path.strip("/"):
                    st["unchanged"] += 1
                    continue
                target = f"{self._GOVUK_NAMESPACE}/{path.strip('/')}"
                if target == cur:
                    st["unchanged"] += 1
                    continue
                # A collision is usually created BY this run: the uk-cma and
                # uk-cma-guidance copies of one page both want the same target, and only
                # the second is a merge. A dry run that asked the database alone would
                # call both a rename and under-report the merges the apply will do — the
                # very number the reader is deciding on — so claimed targets are tracked.
                merging = target in claimed or cat.get_document(target) is not None
                claimed.add(target)
                changes.append({"old": cur, "new": target, "source": r["source"],
                                "kind": "merge" if merging else "rename"})
                _progress(on_progress, stage="planning GOV.UK re-key", done=n,
                          total=len(rows), item=cur)

            if not apply:
                st["changes"] = changes[:5000]
                return st

            # -- apply, in two passes -------------------------------------------
            # EVERY document whose target is contested goes through the per-document
            # path, not just the second one: it drops the loser's row and folds its
            # references in, honouring the uniqueness keys. Routing only the "merge"
            # member there and leaving its partner to the batch was a real bug — the
            # batch then renamed the partner onto an id the merge had just created and
            # hit a UNIQUE violation.
            contested = {t for t, k in Counter(c["new"] for c in changes).items() if k > 1}
            contested |= {c["new"] for c in changes
                          if cat.get_document(c["new"]) is not None}
            for c in (c for c in changes if c["new"] in contested):
                action = cat.rekey_document(c["old"], c["new"], commit=False)
                cat.put_alias(fold(c["old"]), c["new"], source="govuk-rekey", commit=False)
                st["merged" if action == "merge" else "rekeyed"] += 1
            cat.commit()

            # Everything else is a pure RENAME, and pure renames go set-based: one
            # statement per referencing column instead of eleven per document. The
            # per-document path was measured at ~4 hours for 822 documents on the live
            # corpus, because `citations` holds 41M rows with no plain index on
            # candidate_id, so every move sequentially scanned it. By now the contested
            # ids no longer carry a legacy prefix, so the sweep only sees clean renames.
            plain = [c for c in changes if c["new"] not in contested]
            if plain:
                _progress(on_progress, stage="re-keying GOV.UK ids (batched)",
                          done=0, total=len(plain))
                counts = cat.reprefix_documents(
                    self._LEGACY_GOVUK_PREFIXES, self._GOVUK_NAMESPACE, commit=False)
                for c in plain:
                    cat.put_alias(fold(c["old"]), c["new"], source="govuk-rekey",
                                  commit=False)
                st["rekeyed"] += counts.get("documents.stable_id", len(plain))
                st["reference_updates"] = counts
                cat.commit()
                _progress(on_progress, stage="re-keying GOV.UK ids (batched)",
                          done=len(plain), total=len(plain))
        if apply:
            self._invalidate_caches()
        st["changes"] = changes[:5000]
        return st

    def refix_westlaw_imports(self, *, apply: bool = False, limit: int | None = None,
                              on_progress=None, cancel_check=None) -> dict:
        """Repair already-imported Westlaw documents whose id predates the current identity
        rules — chiefly the opaque ``westlaw:<hash>`` keys minted before WL-less law reports
        keyed by their report citation. Recompute each doc's identity from its stored
        ``meta_json`` (no re-parse of the raw RTF needed) and, where it differs, re-key the
        document in place (:meth:`Catalogue.rekey_document`, cascading every reference). Also
        folds a doc into a held record that shares a **precise** alias (report citation or
        WL/ECLI/CJEU id) — never a bare party name; that second half applies to EVERY
        ``westlaw:`` id, because a report slug is still a surrogate and the case may have
        arrived under its neutral citation later (import order alone decided which of the
        two exists). ``apply=False`` is a dry run that just reports the planned changes."""
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
            # Only the opaque content-hash surrogates get their identity RECOMPUTED — a doc
            # already keyed by an ECLI, a neutral slug, a WL id or a report slug is as good
            # as its metadata allows and must be left alone (its meta_json may be incomplete
            # after a merge, so recomputing from meta could wrongly demote a good id).
            hash_id = re.compile(r"^westlaw:[0-9a-f]{16}$")
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                st["scanned"] += 1
                cur = r["stable_id"]
                if not cur.lower().startswith("westlaw:"):
                    st["unchanged"] += 1  # already keyed by a real citation
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
                is_hash = bool(hash_id.match(cur))
                if is_hash:
                    # only ever re-key TO a citation-derived identity — never to a fresh hash
                    if not (p.neutral_citation or p.ecli or p.wl_number or p.report_citations):
                        st["unchanged"] += 1
                        continue
                    target, kind = westlaw_identity(p)
                else:
                    # keep the id; a report/WL slug is still only a SURROGATE, so it stays
                    # eligible for the merge check below — the Donoghue case, held as both
                    # westlaw:1932-a-c-562 and ukhl/1932/100 because the Westlaw RTF was
                    # imported first and nothing ever revisited it.
                    target, kind = cur, "surrogate"
                # fold into a held record sharing a precise alias (report cite / id)
                if kind in ("wl", "report", "hash", "surrogate"):
                    precise = list(p.report_citations) + [
                        x for x in (p.wl_number, p.ecli, p.case_number) if x]
                    for c in precise:
                        cand = first_candidate(c)
                        key = fold(cand.value) if cand else fold(c)
                        held = cat.get_alias(key)
                        if held is None and cat.get_document(key) is not None:
                            held = key
                        # A hash id may fold into any held twin (including a better Westlaw
                        # surrogate); an id that is already a report/WL slug only ever yields
                        # to a real citation-derived identity, never to another surrogate.
                        if held and not is_hash and self._SURROGATE_ID_RE.match(held):
                            continue
                        if held and held != cur and cat.get_document(held) is not None:
                            target, kind = held, "merged"
                            break
                if target == cur or not target:
                    st["unchanged"] += 1
                    continue
                changes.append({"old": cur, "new": target, "kind": kind})
                if apply:
                    action = cat.rekey_document(cur, target, commit=False)
                    # the retired id stays resolvable — a link or an edge minted against it
                    # still lands on the surviving document.
                    cat.put_alias(fold(cur), target, source="merged-surrogate", commit=False)
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
        same directory and picks up only its own extension.

        Any ``.zip`` staged in the folder is unpacked first (its ``.html``/``.rtf``/``.doc``
        entries extracted into a sibling subfolder), so uploading a batch of Westlaw zips
        runs as ONE import job with ONE post-import roll-up, not one per zip."""
        import os
        import uuid
        import zipfile

        # unpack staged zips in place, so the folder walk below sees their case files
        for nm in sorted(os.listdir(dir_path)):
            if not nm.lower().endswith(".zip"):
                continue
            if cancel_check and cancel_check():
                break
            zpath = os.path.join(dir_path, nm)
            dest = os.path.join(dir_path, f"_unzipped_{nm[:-4]}")
            try:
                os.makedirs(dest, exist_ok=True)
                with zipfile.ZipFile(zpath) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        low = info.filename.lower()
                        if not low.endswith((".html", ".htm", ".rtf", ".doc")):
                            continue
                        # flatten to a safe basename (no path traversal / nested dirs)
                        base = os.path.basename(info.filename) or "entry"
                        out = os.path.join(dest, base)
                        if os.path.exists(out):
                            out = os.path.join(dest, f"{uuid.uuid4().hex[:8]}_{base}")
                        with zf.open(info) as src, open(out, "wb") as fh:
                            fh.write(src.read())
            except (zipfile.BadZipFile, OSError) as exc:
                log.warning("skipping unreadable staged zip %s: %s", nm, exc)

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
        jurisdiction: str | None = None, court: str | None = None,
        decision_date: str | None = None, citation: str | None = None,
        language: str | None = None, tags: list[str] | None = None,
        structure: str = "auto",
    ) -> dict:
        # The general upload form used to ingest a legislation.gov.uk AKN file as
        # ``user:commentary:*`` and run the plain-text extractor over the XML. Its own
        # FRBRWork is a stronger identity signal than a stale/default form selection:
        # promote it to the canonical UK legislation importer automatically. This also
        # means harvest-all sees the canonical id as held instead of refetching bytes the
        # user has already supplied.
        if b"akomaNtoso" in data[:8192]:
            from .formats.akoma_ntoso import _frbr_work_id

            akn_id = _frbr_work_id(data)
            if akn_id and _UK_INSTRUMENT_ID_RE.match(akn_id):
                result = self.import_legislation_akn(
                    data=data, stable_id=akn_id, filename=filename)
                if "error" not in result:
                    result["auto_promoted_from_upload"] = True
                return result
        with self._open() as (cat, rs, ts):
            res = import_file(
                cat, rs, ts, data=data, filename=filename,
                doc_type=_doc_type(doc_type, DocType.COMMENTARY), title=title,
                link_to=link_to, relationship=_rel_type(relationship),
                jurisdiction=jurisdiction, court=court,
                decision_date=_as_date(decision_date), citation=citation,
                language=language, tags=tuple(tags or ()), structure=structure,
            )
            return asdict(res)

    def import_many(self, items: list[dict]) -> dict:
        """Import a batch of files, each with its own metadata row.

        One failure is one row's failure: a corrupt PDF in the middle of a drop must not
        cost the operator the other nine imports, so every item is reported individually
        and the batch always returns 200.
        """
        results: list[dict] = []
        for index, item in enumerate(items or []):
            data = item.get("data")
            if not isinstance(data, (bytes, bytearray)):
                results.append({"index": index, "error": "no file content",
                                "filename": item.get("filename")})
                continue
            try:
                res = self.import_bytes(
                    data=bytes(data), filename=item.get("filename") or "upload.bin",
                    doc_type=item.get("doc_type") or "commentary",
                    title=item.get("title"), link_to=item.get("link_to"),
                    relationship=item.get("relationship"),
                    jurisdiction=item.get("jurisdiction"), court=item.get("court"),
                    decision_date=item.get("decision_date"),
                    citation=item.get("citation"), language=item.get("language"),
                    tags=item.get("tags"), structure=item.get("structure") or "auto",
                )
                results.append({"index": index, **res})
            except Exception as exc:  # noqa: BLE001 — one bad file, not a bad batch
                log.exception("import_many: %s failed", item.get("filename"))
                results.append({"index": index, "filename": item.get("filename"),
                                "title": item.get("title"), "error": str(exc)[:300]})
        ok = [r for r in results if not r.get("error")]
        # Read what they cite, and link it. Without this an import is a document the graph
        # cannot see: three EU codes of practice arrived with 34 "AI Act" references
        # between them and no edges at all, because nothing had ever read their text.
        # "Resolve citations" does NOT do this — it resolves edges already extracted — so
        # there was no button the operator could have pressed either.
        #
        # A drop is tens of documents, not the corpus, so it is affordable inline; the
        # grammar pass runs under the usual wall-clock guard, so one pathological PDF
        # costs its own extraction and not the request.
        extracted = self._extract_imported([r["stable_id"] for r in ok if r.get("stable_id")])
        return {
            "imported": len(ok),
            "failed": len(results) - len(ok),
            "documents": results,
            **extracted,
        }

    def _extract_imported(self, ids: list[str]) -> dict:
        """Citations out of freshly-imported documents, then resolve just those."""
        if not ids:
            return {}
        try:
            with self._open() as (cat, _rs, ts):
                self._extract_ids(cat, ts, ids)
                resolved = Resolver(cat).run_for_documents(ids)
            self._invalidate_caches()
            return {"citations_resolved": getattr(resolved, "resolved", 0),
                    "next": "these documents are in the citation graph; run Embed / index "
                            "(Operations) to make their full text searchable"}
        except Exception as exc:  # noqa: BLE001 — the documents ARE imported; say so
            log.exception("import: citation extraction failed")
            return {"extraction_error": str(exc)[:300],
                    "next": "imported, but reading their citations failed — re-run a "
                            "rescan scoped to this source"}

    def import_options(self) -> dict:
        """The vocabularies the import form's dropdowns offer.

        Read from the live corpus wherever the corpus is the authority — the courts and
        languages actually held, the tags actually in use — so the form never offers a
        value the rest of the app would not recognise.
        """
        from .imports.service import JURISDICTIONS, STRUCTURE_CHOICES, import_source_key

        held = {j["jurisdiction"]: j["documents"] for j in self.jurisdictions()}
        # Courts the corpus actually holds, per jurisdiction bucket — so picking
        # "United Kingdom" then offers UKSC, EWCA (Civ)… under the names the reader
        # already sees everywhere else. This is the Explore page's own facet, so the
        # labels are already disambiguated (a Canadian "FCA" is not the Australian one)
        # and report series are already excluded; it is the leading courts by volume,
        # not an exhaustive registry, which is why the field also takes free text.
        courts: dict[str, list[dict]] = {}
        for row in self._shape_ready().get("jurisdictions", []) or []:
            entries = [
                {"court": c["court"], "label": c.get("label") or c["court"],
                 "documents": int(c.get("n") or 0)}
                for c in (row.get("courts") or []) if c.get("court")
            ]
            if entries:
                courts[str(row.get("jurisdiction"))] = entries
        return {
            "jurisdictions": [
                {"code": code, "label": label, "source": import_source_key(code),
                 "documents": held.get(label, 0)}
                for code, label in JURISDICTIONS
            ],
            "doc_types": [t.value for t in DocType],
            "relationships": [r.value for r in RelationshipType],
            "structures": [{"value": v, "label": label} for v, label in STRUCTURE_CHOICES],
            "courts_by_jurisdiction": courts,
            "languages": self._held_languages(),
            "tags": self._held_tags(),
        }

    def _held_languages(self) -> list[str]:
        """Language codes already in the corpus. The dropdown is a picker over what the
        corpus knows, but an import may legitimately be the first of its tongue — so the
        UI keeps a free-text escape beside it."""
        try:
            with self._open() as (cat, _rs, _ts):
                rows = cat.conn.execute(
                    "SELECT DISTINCT language FROM documents "
                    "WHERE language IS NOT NULL AND language <> '' "
                    "ORDER BY language LIMIT 60").fetchall()
            return [str(r[0]) for r in rows]
        except Exception:  # noqa: BLE001 — a dropdown must never break the form
            return []

    def _held_tags(self) -> list[str]:
        try:
            with self._open() as (cat, _rs, _ts):
                return sorted(cat.tag_counts().keys())
        except Exception:  # noqa: BLE001
            return []

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

    def link_at_selection(self, *, doc_id: str, target_id: str, selected_text: str,
                          context: str | None = None, pinpoint: str | None = None,
                          relationship: str = "mentions") -> dict:
        """Create a user-authored anchored link at a HIGHLIGHTED span (highlight-to-link).

        The reader renders inline links from ``citations`` rows keyed by char offset, so a
        manual link must land AS one of those rows — the old path only created a corpus-wide
        alias (which shows nothing until the next re-extraction) plus, iff a pinpoint was
        typed, a doc→doc relation (no char anchor). Here we locate the selection in the
        stored text, write a ``method='manual'`` citation at that exact span (so it renders
        immediately, resolves via candidate_id, and survives every rescan — clear_citations
        spares manual rows), and also mint the manual graph edge."""
        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(doc_id)
            if doc is None or not doc["payload_hash"]:
                return {"error": f"no text held for {doc_id}"}
            try:
                text = ts.get(doc["payload_hash"])
            except OSError:
                text = None
            if not text:
                return {"error": f"no text held for {doc_id}"}
            span = _locate_span(text, selected_text, context)
            if span is None:
                return {"error": "couldn't locate the highlighted text in the stored "
                                 "document — the reader text may differ from the selection"}
            cs, ce = span
            present = cat.find_document_id(target_id) is not None
            cat.add_manual_citation(
                doc_id, candidate_id=target_id, raw=(selected_text or "")[:200],
                char_start=cs, char_end=ce, pinpoint=pinpoint)
            # The graph edge too (extracted_via=manual → survives clear_relations), so the
            # link is a first-class citation, not only an inline decoration.
            rel = _rel_type(relationship, RelationshipType.MENTIONS)
            resolved = link_documents(cat, src_id=doc_id, dst_id=target_id,
                                      relationship=rel, dst_anchor=pinpoint)
        return {"doc_id": doc_id, "target_id": target_id, "char_start": cs, "char_end": ce,
                "pinpoint": pinpoint, "target_present": present, "resolved": resolved}

    def link(self, *, src_id: str, dst_id: str, relationship: str,
             src_anchor: str | None = None, dst_anchor: str | None = None,
             note: str | None = None, dry_run: bool = False) -> dict:
        """Assert ONE hand-written edge between two held documents.

        Three properties this call did not previously have, each of which turned a
        mistake into a durable false statement about the law:

        * an unrecognised ``relationship`` is REFUSED, with the accepted vocabulary in the
          error. It used to fall back to ``analyses`` and report ``resolved: true``, so a
          typo became a plausible-looking edge the response could not be distinguished
          from a success;
        * writing the same edge twice UPDATES it rather than minting a second row;
        * ``dry_run`` reports exactly what would be written — including whether each
          anchor resolves to a real segment — without writing it.
        """
        rel = _rel_type(relationship)
        if rel is None:
            return {"error": f"unknown relationship {relationship!r}",
                    "known": sorted(r.value for r in RelationshipType)}
        with self._open() as (cat, _rs, ts):
            if cat.get_document(src_id) is None:
                return {"error": f"source document not held: {src_id}"}
            target = cat.get_document(dst_id)
            anchors = {
                "src_anchor_resolved": self._anchor_resolves(cat, ts, src_id, src_anchor),
                "dst_anchor_resolved": self._anchor_resolves(cat, ts, dst_id, dst_anchor),
            }
            plan = {"src_id": src_id, "dst_id": dst_id, "relationship": rel.value,
                    "src_anchor": src_anchor, "dst_anchor": dst_anchor,
                    "note": note, "resolved": target is not None, **anchors}
            if dry_run:
                return {**plan, "dry_run": True, "written": False}
            written = cat.upsert_manual_relation(
                src_id,
                TypedRelation(
                    relationship_type=rel,
                    raw_citation_string=dst_id,
                    dst_id=dst_id,
                    extracted_via=ExtractedVia.MANUAL,
                    resolution_status=(ResolutionStatus.RESOLVED if target is not None
                                       else ResolutionStatus.PENDING),
                    src_anchor=src_anchor,
                    dst_anchor=dst_anchor,
                ),
                note=note,
            )
        self._invalidate_caches()
        return {**plan, **written, "written": True}

    @staticmethod
    def _anchor_resolves(cat, textstore, stable_id: str, anchor: str | None) -> bool | None:
        """Does ``anchor`` name a real segment of ``stable_id``?

        ``None`` means the question doesn't arise (no anchor given, or no text held to
        check against) — deliberately distinct from ``False``, which means the caller
        named a provision that does not exist and is almost certainly a mistake.
        """
        if not anchor:
            return None
        row = cat.get_document(stable_id)
        if row is None or not row["payload_hash"]:
            return None
        try:
            segments = textstore.get_segments(row["payload_hash"])
        except OSError:
            return None
        if not segments:
            return None
        wanted = _anchor_key(anchor)
        if not wanted:
            return None
        return any(_anchor_key(segment.label or "") == wanted for segment in segments)

    @staticmethod
    def _anchor_heading(cat, textstore, stable_id: str, anchor: str | None) -> str | None:
        """The FULL LABEL of the segment ``anchor`` names — "Article 52 Transparency
        obligations…", not just "yes, it exists".

        Existence is not identity. A mapping to "Article 52" resolves happily against any
        instrument that has one, so a caller who assumed the wrong numbering wrote a
        silently wrong correspondence and had no way to see it short of a separate
        get_provision probe per anchor. Echoing the heading back turns that into a read."""
        if not anchor:
            return None
        row = cat.get_document(stable_id)
        if row is None or not row["payload_hash"]:
            return None
        try:
            segments = textstore.get_segments(row["payload_hash"])
        except OSError:
            return None
        wanted = _anchor_key(anchor)
        if not wanted:
            return None
        for segment in segments or []:
            if _anchor_key(segment.label or "") == wanted:
                return " ".join((segment.label or "").split())[:160]
        return None

    def upsert_provision_mappings(
        self, *, current_id: str, previous_id: str, mappings: list[dict],
        created_by: str = "manual", replace: bool = False,
        mapping_type: str = "functional_predecessor", dry_run: bool = False,
        return_all: bool = False, quiet: bool = False,
    ) -> dict:
        """Bulk-create article/section correspondences between two laws.

        Direction is current provision → the other law's provision. This never rewrites
        the literal citation and therefore must not be implemented with citation aliases.

        ``mapping_type`` says what the correspondence CLAIMS (see
        :data:`PROVISION_MAPPING_TYPES`) and may be set per item. It is not cosmetic: a
        repealed predecessor's citations are the current provision's history, whereas a
        companion instrument's are a parallel provision in force alongside it, and the
        reader labels them differently. An unknown value is an error rather than a silent
        downgrade — asserting descent between companion instruments would be wrong.

        Every anchor is resolved against its document's segments at write time, and any
        that names nothing is reported back per row. ``dry_run`` runs that check and
        returns the plan without writing.

        The reply carries only the rows this call wrote, plus ``total_for_pair``. Batch
        sizes above ~55 rows have been seen to exceed the four-minute tool ceiling; the
        write is atomic under that timeout (nothing is stored), so the safe recovery is
        simply to re-send in smaller batches.

        ``quiet`` returns ids and anchors only — writing 32 mappings otherwise echoes
        back considerably more text than was sent, most of it the ~280-character title of
        the previous law repeated on every row.
        """
        created_by = (created_by or "manual").strip().lower()
        if created_by not in {"manual", "llm", "structured"}:
            return {"error": "created_by must be manual, llm, or structured"}
        if not current_id or not previous_id or current_id == previous_id:
            return {"error": "distinct current_id and previous_id are required"}
        default_type = str(mapping_type or "functional_predecessor").strip().lower()
        if default_type not in PROVISION_MAPPING_TYPES:
            return {"error": f"unknown mapping_type {mapping_type!r}",
                    "known": sorted(PROVISION_MAPPING_TYPES)}
        clean: list[dict] = []
        for item in mappings or []:
            current_anchor = str(item.get("current_anchor") or "").strip()
            previous_anchor = str(item.get("previous_anchor") or "").strip()
            if not current_anchor or not previous_anchor:
                return {"error": "every mapping needs current_anchor and previous_anchor"}
            confidence = item.get("confidence")
            if confidence is not None:
                try:
                    confidence = max(0.0, min(1.0, float(confidence)))
                except (TypeError, ValueError):
                    return {"error": "confidence must be between 0 and 1"}
            item_type = str(item.get("mapping_type") or default_type).strip().lower()
            if item_type not in PROVISION_MAPPING_TYPES:
                return {"error": f"unknown mapping_type {item.get('mapping_type')!r} for "
                                 f"{current_anchor}",
                        "known": sorted(PROVISION_MAPPING_TYPES)}
            # An explicit cutoff wins; otherwise it is derived from the jurisdiction of
            # the transposing law once we have opened the catalogue (below).
            inherit_before = item.get("inherit_before")
            explicit_cutoff = inherit_before is not None
            if explicit_cutoff:
                if str(inherit_before).strip().lower() in ("", "none", "never"):
                    inherit_before = None
                else:
                    inherit_before = str(inherit_before).strip()[:10]
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", inherit_before):
                        return {"error": f"inherit_before must be YYYY-MM-DD, got "
                                         f"{item.get('inherit_before')!r}"}
            clean.append({
                "current_anchor": current_anchor,
                "previous_anchor": previous_anchor,
                "mapping_type": item_type,
                "inherit_before": inherit_before,
                "_explicit_cutoff": explicit_cutoff,
                "note": str(item.get("note") or "").strip() or None,
                "confidence": confidence,
            })
        if not clean or len(clean) > 1000:
            return {"error": "supply between 1 and 1000 mappings"}
        with self._open() as (cat, _rs, ts):
            current_doc = cat.get_document(current_id)
            if current_doc is None:
                return {"error": f"current law not held: {current_id}"}
            if cat.get_document(previous_id) is None:
                return {"error": f"previous law not held: {previous_id}"}
            # A transposition gates itself on the jurisdiction of the law doing the
            # transposing: a UK provision inherits retained EU case law only. Derived
            # here rather than asked of the caller, so the guarantee does not depend on
            # anyone remembering it.
            jurisdiction = self._doc_bucket(current_doc["source"], current_doc["court"])
            derived_cutoff = _TRANSPOSITION_CUTOFF_BY_JURISDICTION.get(jurisdiction)
            if derived_cutoff is None and _UK_INSTRUMENT_ID_RE.match(current_id):
                derived_cutoff = RETAINED_EU_CASELAW_CUTOFF
            for item in clean:
                if (item.pop("_explicit_cutoff") is False
                        and item["mapping_type"] == "transposition"):
                    item["inherit_before"] = derived_cutoff
            # Resolve every anchor against the two documents' own segments BEFORE
            # writing. The whole value of this table is that its anchors point at real
            # provisions; an unresolvable one (a "regulation 6" against an instrument
            # whose segments are all "s. (1)") was previously stored in silence and
            # simply never matched anything.
            checked = []
            unresolved = []
            for item in clean:
                # The HEADING each anchor landed on, not merely whether it landed.
                # Existence is not identity: "Article 52" exists in most instruments,
                # so a caller working from the wrong numbering wrote a wrong mapping
                # that resolved perfectly. With the headings echoed back, a dry run
                # reads as a correlation table you can check.
                current_head = self._anchor_heading(
                    cat, ts, current_id, item["current_anchor"])
                previous_head = self._anchor_heading(
                    cat, ts, previous_id, item["previous_anchor"])
                current_ok = self._anchor_resolves(
                    cat, ts, current_id, item["current_anchor"])
                previous_ok = self._anchor_resolves(
                    cat, ts, previous_id, item["previous_anchor"])
                checked.append({**item, "current_anchor_resolved": current_ok,
                                "previous_anchor_resolved": previous_ok,
                                "current_heading": current_head,
                                "previous_heading": previous_head})
                if current_ok is False or previous_ok is False:
                    unresolved.append({
                        "current_anchor": item["current_anchor"],
                        "previous_anchor": item["previous_anchor"],
                        "current_anchor_resolved": current_ok,
                        "previous_anchor_resolved": previous_ok,
                    })
            if dry_run:
                return {"current_id": current_id, "previous_id": previous_id,
                        "dry_run": True, "written": 0, "would_write": len(clean),
                        "replace": bool(replace),
                        "mappings": [
                            {k: v for k, v in m.items() if k != "note"} if quiet else m
                            for m in checked],
                        "unresolved_anchors": unresolved}
            written = cat.upsert_provision_mappings(
                current_id, previous_id, clean, created_by=created_by,
                replace=replace)
            # Only the rows THIS call wrote. Echoing every mapping for the pair made a
            # build quadratic in its own output: 260 rows in 6 batches re-read ~900 rows
            # of already-known data, the notes being the bulk of it, and the last batches
            # blew the response ceiling. `list_provision_mappings` returns the whole set
            # for anyone who wants it; `return_all=True` restores the old behaviour.
            everything = [dict(r) for r in cat.provision_mappings(current_id)]
            for_pair = [r for r in everything if r["previous_doc_id"] == previous_id]
            anchors = {(m["current_anchor"].strip().lower(),
                        m["previous_anchor"].strip().lower()) for m in clean}
            rows = for_pair if return_all else [
                r for r in for_pair
                if (str(r["current_anchor"] or "").strip().lower(),
                    str(r["previous_anchor"] or "").strip().lower()) in anchors
            ]
        self._invalidate_caches()
        if quiet:
            # ids + anchors, nothing repeated. The caller sent the notes; it does not
            # need them read back, and the previous law's title is one fact, not one
            # fact per row.
            rows = [{"mapping_id": r["mapping_id"],
                     "current_anchor": r["current_anchor"],
                     "previous_anchor": r["previous_anchor"],
                     "mapping_type": r["mapping_type"]} for r in rows]
        result = {"current_id": current_id, "previous_id": previous_id,
                  "written": written, "mappings": rows,
                  # The pair's full size, so a caller can see the running total without
                  # being handed it row by row on every batch.
                  "total_for_pair": len(for_pair),
                  "sent": len(clean),
                  "returned": "all" if return_all else "written"}
        # ROWS THIS PAIR HOLDS THAT THIS CALL DID NOT SEND. A tool call whose response is
        # lost (the four-minute ceiling) still ran: the write landed, the caller saw
        # nothing, retried with a slightly different list, and the pair silently kept
        # both. That was only diagnosable by inferring orphans from gaps in the
        # mapping_id sequence. Name them instead — with their ids, so they can be
        # deleted or corrected in one step.
        surplus = [r for r in for_pair
                   if (str(r["current_anchor"] or "").strip().lower(),
                       str(r["previous_anchor"] or "").strip().lower()) not in anchors]
        if surplus:
            result["not_sent_in_this_call"] = [
                {"mapping_id": r["mapping_id"], "current_anchor": r["current_anchor"],
                 "previous_anchor": r["previous_anchor"],
                 "mapping_type": r["mapping_type"], "created_at": r["created_at"]}
                for r in surplus[:200]]
        warnings = [
            f"{len(surplus)} mapping(s) exist for this pair that this call did not "
            "send — an earlier batch, or a call whose response was lost. Check "
            "not_sent_in_this_call and delete_provision_mapping any that are wrong."
        ] if surplus else []
        if unresolved:
            # Reported, not refused: a mapping can legitimately name a provision of a
            # document held only as a metadata stub. The caller must be able to SEE it.
            result["unresolved_anchors"] = unresolved
            warnings.append(
                f"{len(unresolved)} of {len(clean)} mappings name an anchor that does "
                "not match any segment of the document it belongs to")
        if warnings:
            result["warning"] = " ".join(warnings)
        return result

    def provision_mappings(self, *, stable_id: str, previous_id: str | None = None,
                           limit: int | None = None, offset: int = 0) -> dict:
        """Every mapping written against this law, or — with ``previous_id`` — just the
        ones to that other law.

        The pair filter is what makes a batch auditable. Without it the only way to see
        what a write actually left behind was to list all 441 mappings across the law and
        infer the surplus from gaps in the id sequence."""
        with self._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.provision_mappings(stable_id)]
            inherited = [dict(r) for r in cat.inherited_mentions_for(
                stable_id, limit=5000)]
        counts: dict[int, set[str]] = {}
        for row in inherited:
            counts.setdefault(int(row["mapping_id"]), set()).add(row["src_id"])
        for row in rows:
            row["mentioned_by_count"] = len(
                counts.get(int(row["mapping_id"]), set()))
        total = len(rows)
        if previous_id:
            rows = [r for r in rows if r["previous_doc_id"] == previous_id]
        matched = len(rows)
        if limit is not None:
            rows = rows[offset:offset + max(1, int(limit))]
        return {"stable_id": stable_id, "previous_id": previous_id,
                "mappings": rows, "total": total, "matched": matched,
                "offset": offset,
                "inherited_documents": len({r["src_id"] for r in inherited})}

    #: how many inherited citers are assembled before filtering/sorting. The filters
    #: and the count have to describe the whole set, not a prefix of it.
    _INHERITED_POOL = 3000

    def inherited_provision_mentions(
        self, *, stable_id: str, current_anchor: str | None = None,
        limit: int = 600, offset: int = 0, sort: str = "pagerank",
        kind: str | None = None, jurisdiction: str | None = None,
    ) -> dict:
        """Documents that cited a MAPPED provision of another instrument, projected onto
        this one — with the same browse surface as :meth:`citing_documents`.

        It had none, and that largely defeated it: rows came back in relation_id order
        with ``src_authority`` 0.0 on every one, so the first page of AI Act Article 40
        was UK assimilated copies making routine cross-references, while the CJEU
        harmonised-standards line (James Elliott, Anstar, Germany v Commission) sat
        somewhere in the tail with no way to reach it but paging. The data was right; the
        order was arbitrary.
        """
        with self._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.inherited_mentions_for(
                stable_id, current_anchor=current_anchor, limit=self._INHERITED_POOL)]
            incoming = self._assemble_cited_by(cat, rows, cap=self._INHERITED_POOL)
            # PageRank for the citers — the ranking signal ``_assemble_cited_by`` cannot
            # supply here, because these edge rows come from the mapping join rather than
            # from the cited-by query that carries src_pagerank.
            ids: list[str] = []
            for row in incoming:
                ids.append(row["src_id"])
            pr = cat.authority_for(ids)
            for row in incoming:
                row["src_authority"] = float(
                    (pr.get(row["src_id"]) or {}).get("pagerank", 0.0) or 0.0)
        from collections import Counter

        facets = {
            "kind": dict(Counter(
                r.get("src_kind") for r in incoming if r.get("src_kind"))),
            "jurisdiction": dict(Counter(
                r.get("src_jurisdiction") for r in incoming
                if r.get("src_jurisdiction"))),
        }
        want_j = self._norm_jurisdiction(jurisdiction)
        if kind:
            wanted = {k.strip().casefold() for k in str(kind).split(",") if k.strip()}
            incoming = [r for r in incoming
                        if (r.get("src_kind") or "").casefold() in wanted]
        if want_j:
            incoming = [r for r in incoming if r.get("src_jurisdiction") == want_j]

        def _year(row) -> int:
            d = str(row.get("src_date") or "")[:4]
            return int(d) if d.isdigit() else 0

        tie = lambda r: (-(r.get("src_cited_by") or 0), r["src_id"])  # noqa: E731
        keys = {
            "pagerank": lambda r: (-(r.get("src_authority") or 0.0), *tie(r)),
            "cited": tie,
            "newest": lambda r: (-_year(r), *tie(r)),
            "oldest": lambda r: (_year(r) or 9999, *tie(r)),
        }
        sort = sort if sort in keys else "pagerank"
        incoming.sort(key=keys[sort])
        total = len(incoming)
        page = incoming[offset:offset + max(1, int(limit))]
        return {"stable_id": stable_id, "current_anchor": current_anchor,
                "documents": total, "total": total,
                "offset": offset, "limit": limit, "sort": sort,
                "sorts": ["pagerank", "cited", "newest", "oldest"],
                "kind": kind, "jurisdiction": want_j or jurisdiction,
                "facets": facets, "incoming": page}

    def relationship_types(self) -> dict:
        """The closed vocabulary ``link`` accepts, so it can be read rather than probed.

        Guessing at it was how two wrong edges were written: the only way to learn which
        terms existed was to send one, read the corpus back and see what had landed.
        """
        families = {
            "treatment": ["follows", "distinguishes", "overrules", "applies",
                          "considers", "cites_for_fact", "mentions", "interprets"],
            "commentary": ["analyses", "summarises", "criticises", "annotates",
                           "cited_by_commentary"],
            "legislative": ["implements", "transposes", "supersedes", "amends",
                            "amended_by", "repeals", "repealed_by", "corrects",
                            "corrected_by", "consolidates", "point_in_time_of",
                            "assimilated_version_of", "legal_basis"],
        }
        known = sorted(r.value for r in RelationshipType)
        placed = {value for values in families.values() for value in values}
        return {
            "relationship_types": known,
            "families": {name: [v for v in values if v in known]
                         for name, values in families.items()},
            "other": sorted(v for v in known if v not in placed),
            "note": "link() refuses anything not on this list; it does not fall back.",
        }

    def manual_links(self, *, stable_id: str, limit: int = 500) -> dict:
        """Every hand-written edge into or out of a document, each with its relation_id.

        Without this there was no addressable handle on a manual assertion that had been
        folded into an existing relation's passages, and therefore no way to retract one.
        """
        with self._open() as (cat, _rs, _ts):
            outgoing = [dict(r) for r in cat.manual_relations(
                src_id=stable_id, limit=limit)]
            incoming = [dict(r) for r in cat.manual_relations(
                dst_id=stable_id, limit=limit)]
        return {"stable_id": stable_id, "outgoing": outgoing, "incoming": incoming,
                "total": len(outgoing) + len(incoming)}

    def delete_manual_link(self, *, relation_id: int) -> dict:
        """Retract one hand-written edge by its relation_id, leaving extracted citations
        between the same two documents untouched."""
        with self._open() as (cat, _rs, _ts):
            result = cat.delete_manual_relation(relation_id)
        if result.get("deleted"):
            self._invalidate_caches()
        return result

    # -- UK division/chamber identity (see ops/uk_identity.py) -----------------
    def backfill_effective_dates(self, *, dry_run: bool = True) -> dict:
        """Fill the interface's date column for rows written before it existed."""
        with self._open() as (cat, _rs, _ts):
            with cat._maintenance_timeout():
                out = cat.backfill_effective_dates(dry_run=dry_run)
        if not dry_run:
            self._invalidate_caches()
        return out

    def uk_identity_audit(self) -> dict:
        """How many chamber-less UK aliases name more than one judgment."""
        from .ops import uk_identity

        with self._open() as (cat, _rs, _ts):
            return uk_identity.audit_chamber_aliases(cat)

    def uk_identity_repair(self, *, dry_run: bool = True) -> dict:
        """Drop the ambiguous chamber-less aliases and demote what followed them."""
        from .ops import uk_identity

        with self._open() as (cat, _rs, _ts):
            out = uk_identity.repair_chamber_aliases(cat, dry_run=dry_run)
        if not dry_run:
            self._invalidate_caches()
        return out

    def uk_identity_tiebreak(self, *, dry_run: bool = True, limit: int = 5000) -> dict:
        """Settle chamber-less references against the name written beside them."""
        from .ops import uk_identity

        with self._open() as (cat, _rs, ts):
            out = uk_identity.tiebreak_ambiguous_divisions(
                cat, ts, dry_run=dry_run, limit=limit)
        if not dry_run:
            self._invalidate_caches()
        return out

    def uk_identity_unify(self, *, dry_run: bool = True) -> dict:
        """Fold same-court duplicate slugs (ewhc/pat + ewhc/patents) into one node."""
        from .ops import uk_identity

        with self._open() as (cat, _rs, _ts):
            out = uk_identity.unify_synonym_slugs(cat, dry_run=dry_run)
        if not dry_run:
            self._invalidate_caches()
        return out

    def delete_provision_mapping(self, *, mapping_id: int) -> dict:
        with self._open() as (cat, _rs, _ts):
            deleted = cat.delete_provision_mapping(mapping_id)
        self._invalidate_caches()
        return {"mapping_id": mapping_id, "deleted": deleted}

    def retype_provision_mappings(
        self, *, current_id: str, to_type: str, previous_id: str | None = None,
        from_type: str | None = None, mapping_ids: list[int] | None = None,
        current_anchor: str | None = None, previous_anchor: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Re-label existing provision mappings in place: what they CLAIM, not what they
        connect.

        The correspondences are the work — every anchor pair resolved against both laws.
        A build that got the type wrong throughout (companion provisions written as
        'functional_predecessor') needs one relabelling, not a re-derivation of the
        table. Scope it with ``mapping_ids`` or either anchor for a mixed pair of laws;
        ``previous_id`` and ``from_type`` provide broader filters.
        """
        to_type = str(to_type or "").strip().lower()
        if to_type not in PROVISION_MAPPING_TYPES:
            return {"error": f"unknown mapping_type {to_type!r}",
                    "known": sorted(PROVISION_MAPPING_TYPES)}
        if from_type:
            from_type = str(from_type).strip().lower()
            if from_type not in PROVISION_MAPPING_TYPES:
                return {"error": f"unknown mapping_type {from_type!r}",
                        "known": sorted(PROVISION_MAPPING_TYPES)}
        with self._open() as (cat, _rs, _ts):
            n = cat.retype_provision_mappings(
                current_id, to_type=to_type, previous_doc_id=previous_id,
                from_type=from_type, mapping_ids=mapping_ids,
                current_anchor=current_anchor, previous_anchor=previous_anchor,
                dry_run=dry_run)
        if not dry_run:
            self._invalidate_caches()
        return {"current_id": current_id, "previous_id": previous_id,
                "from_type": from_type, "to_type": to_type,
                "mapping_ids": mapping_ids, "current_anchor": current_anchor,
                "previous_anchor": previous_anchor,
                "updated": 0 if dry_run else n, "would_update": n if dry_run else None,
                "dry_run": bool(dry_run)}

    def tag(self, *, doc_id: str, tag: str) -> dict:
        with self._open() as (cat, _rs, _ts):
            written = tag_document(cat, doc_id, tag)
        self._invalidate_caches()  # a cached document view holds this doc's tag list
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
        if apply:
            self._invalidate_caches()  # edges changed corpus-wide
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

    def apply_rules(self, *, source: str | None = None,
                    sources: list[str] | None = None,
                    source_prefix: str | None = None,
                    target_ids: list[str] | None = None,
                    document_ids: list[str] | None = None,
                    run_id: str | None = None,
                    on_progress=None, cancel_check=None) -> dict:
        """Re-extract document text with the current grammars + user rules — the "re-scan the
        corpus for new potential citations" action. Run this after a new adapter/grammar
        lands (e.g. the law-report grammars, ECHR app numbers) so already-stored docs pick
        them up. ``source`` scopes it (e.g. just ``uk-caselaw``) — reports are cited by case
        law, so a scoped re-scan is far faster than the whole corpus. ``target_ids`` narrows
        further to documents already observed citing one of those targets; this is how a
        French EU-article upgrade revisits the digital-acquis worklist without scanning
        millions of unrelated DILA records. ``document_ids`` is the exact, bounded repair
        path used by the reader refinement queue. Heavy → run as a job."""
        from .citations import extract_documents_parallel

        with self._open() as (cat, _rs, ts):
            aliases = cat.named_alias_map()
            if document_ids is not None:
                requested = list(dict.fromkeys(str(i) for i in document_ids if i))
                # Confirm extractable held documents rather than trusting arbitrary ids.
                ids = cat.held_text_document_ids(requested)
            elif target_ids:
                ids = cat.text_document_ids_citing(
                    target_ids,
                    sources=sources or ([source] if source else None),
                    source_prefix=source_prefix,
                    exclude_extraction_run_id=run_id,
                )
            else:
                # Preserve the old single-source/full-corpus contract. Multiple sources
                # only have meaning with a bounded target worklist.
                ids = cat.text_document_ids(
                    source=source, exclude_extraction_run_id=run_id)
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
            stats = {"documents": docs, "citations": cites, "resolved_edges": resolved.resolved}
        self._invalidate_caches()  # re-scan changed edges → dashboards + doc views are stale
        return stats

    def untag(self, *, doc_id: str, tag: str) -> dict:
        """Remove a manual tag (a mis-tag correction). Rule tags are re-derived, so
        they're corrected by editing the rule, not here."""
        with self._open() as (cat, _rs, _ts):
            removed = cat.remove_document_tag(doc_id, tag, method="manual")
        self._invalidate_caches()  # a cached document view holds this doc's tag list
        return {"doc_id": doc_id, "tag": tag, "removed": removed}

    def tag_many(self, *, doc_ids: list[str], tag: str) -> dict:
        """Bulk-tag a selection — the academic's "drop these into a collection" gesture
        (a collection is just a shared manual tag)."""
        with self._open() as (cat, _rs, _ts):
            n = sum(1 for d in doc_ids if tag_document(cat, d, tag))
        self._invalidate_caches()  # cached document views hold these docs' tag lists
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
        # The document view is cached for 120s and this is the ONE mutation that never
        # dropped it — so a corrected title was written, the editor re-read the document,
        # and the pre-edit copy came back. It read exactly like the save had been ignored.
        # Every sibling correction (untag, tag_many, correct_citation) already did this.
        if ok:
            self._invalidate_caches()
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
                out = {"relation_id": relation_id, "action": "suppressed"}
            elif dst_id is not None:
                if cat.get_document(dst_id) is None:
                    return {"error": f"no document {dst_id!r} in corpus", "relation_id": relation_id}
                cat.resolve_relation(relation_id, dst_id)
                cat.set_relationship_type(relation_id, rel["relationship_type"], extracted_via="manual")
                out = {"relation_id": relation_id, "action": "repointed", "dst_id": dst_id}
            elif treatment is not None:
                rel_type = _rel_type(treatment, RelationshipType.MENTIONS)
                cat.set_relationship_type(relation_id, rel_type.value, extracted_via="manual")
                out = {"relation_id": relation_id, "action": "reclassified",
                       "relationship_type": rel_type.value}
            else:
                return {"error": "nothing to do — pass treatment, dst_id, or suppress"}
        # The edge changed — drop the cached document views (and dashboard aggregates) so the
        # citator panel reflects the correction immediately instead of serving a pre-edit copy.
        self._invalidate_caches()
        return out

    def embed_source_scope(self) -> list[str] | None:
        """Resolve the RAGLEX_EMBED_JURISDICTIONS setting to a source-key list, or ``None``
        for "embed everything". The setting is a comma-separated list of jurisdiction names
        (as shown by jurisdictions(), e.g. "United Kingdom, European Union") — indexing the
        whole ~5M-doc corpus is infeasible on a small box, so this lets an operator embed
        only the jurisdictions that matter. An unrecognised/empty entry contributes no
        sources; if the setting is set but resolves to nothing, embedding is scoped to
        nothing (rather than silently indexing everything)."""
        raw = self.settings.resolve("RAGLEX_EMBED_JURISDICTIONS")
        if not raw or not raw.strip():
            return None
        names = [n.strip() for n in raw.replace(";", ",").split(",") if n.strip()]
        sources: list[str] = []
        for name in names:
            sources.extend(self.sources_for_jurisdiction(name))
        # dedupe, preserve order; empty list ≠ None → scope to nothing
        return list(dict.fromkeys(sources))

    def embed(self, *, limit: int | None = None, on_progress=None, cancel_check=None) -> dict:
        """Embed/index documents that have text but no vectors in the current embedding
        family — the lexical (FTS) + semantic (vector) index both search reads. Resumable
        and cancellable; run as the ``embed`` background job so it shows progress and can be
        stopped. Scoped by RAGLEX_EMBED_JURISDICTIONS (see embed_source_scope). Returns
        per-run stats (documents, chunks, skipped)."""
        scope = self.embed_source_scope()
        with self._open() as (cat, _rs, ts):
            stats = asdict(EmbedStage(cat, self._provider(), textstore=ts, sources=scope).run(
                limit=limit, on_progress=on_progress, cancel_check=cancel_check))
        self._invalidate_caches()  # has_embedding changed → coverage/search availability
        return stats

    def embedding_backlog(self) -> dict:
        """How much of the corpus is indexed in the current embedding family — the number
        a UI shows next to the 'Embed' button so it's clear how much work remains. Reflects
        the RAGLEX_EMBED_JURISDICTIONS scope so the count matches what will actually embed."""
        p = self._provider()
        scope = self.embed_source_scope()
        with self._open() as (cat, _rs, _ts):
            pending = len(cat.pending_embedding(p.name, p.model, p.model_version, sources=scope))
            total = cat.count_documents()
        return {"provider": p.name, "model": p.model, "scope": scope,
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

    @staticmethod
    def _watch_summary(w: dict, now) -> dict:
        """One watch, flattened for the keep-current panel: its plan (keywords / discover /
        enrich / tag), cadence, last run, and next-due."""
        import datetime as _dt
        spec = w.get("spec") or {}
        cadence = w.get("cadence_minutes") or 1440
        last = w.get("last_run_at")
        next_due = None
        if w.get("enabled") and last:
            try:
                prev = _dt.datetime.fromisoformat(last)
                if prev.tzinfo is None:
                    prev = prev.replace(tzinfo=_dt.timezone.utc)
                next_due = (prev + _dt.timedelta(minutes=cadence)).isoformat()
            except (ValueError, TypeError):
                next_due = None
        return {
            "watch_id": w["watch_id"], "name": w.get("name"),
            "enabled": bool(w.get("enabled")), "cadence_minutes": cadence,
            "last_run_at": last, "next_due": next_due,
            "is_due": watch_is_due(w["watch_id"], cadence, last, now) if w.get("enabled") else False,
            "keywords": spec.get("keywords") or [],
            "discover": (spec.get("discover") or {}).get("citing"),
            "enrich": spec.get("enrich", True),
            "tag": spec.get("tag"),
            "backfill": bool(spec.get("backfill")),
            "overlap_days": spec.get("overlap_days"),
            "max_pages": spec.get("max_pages"),
            "last_result": w.get("last_result"),
        }

    def keep_current_overview(self) -> dict:
        """The Maintain "keep-current diagnosis" payload: every harvestable source with its
        incremental mode, whether a watch is wired (+ cadence / last-run / next-due), its
        held-doc count and failure state, and the last few runs' pulled/new/deduped/errors
        counts. Grouped by jurisdiction on the client."""
        import datetime as _dt
        from .adapters.registry import source_catalog

        now = _dt.datetime.now(_dt.timezone.utc)
        cat_rows = {r["key"]: r for r in source_catalog()}
        watches = self.list_watches()
        # source_key → its watches (a source may have several; keep the enabled/most-recent)
        watches_by_source: dict[str, list[dict]] = {}
        for w in watches:
            src = (w.get("spec") or {}).get("source")
            if src:
                watches_by_source.setdefault(src, []).append(w)

        with self._open() as (cat, _rs, _ts):
            states = {r["key"]: dict(r) for r in cat.all_sources()}
            runs = cat.source_run_summaries(per_source=8)
            doc_counts = {k: cat.source_doc_count(k) for k in cat_rows}

        overlap_default = 2
        raw = (os.environ.get("RAGLEX_INCREMENTAL_OVERLAP_DAYS") or "").strip()
        if raw.isdigit():
            overlap_default = int(raw)

        rows = []
        for key, info in cat_rows.items():
            st = states.get(key, {})
            src_watches = watches_by_source.get(key, [])
            # pick the enabled watch if any, else the most-recently-run
            watch = next((w for w in src_watches if w.get("enabled")), None) \
                or (max(src_watches, key=lambda w: w.get("last_run_at") or "") if src_watches else None)
            next_due = None
            if watch and watch.get("enabled"):
                cadence = watch.get("cadence_minutes") or 1440
                last = watch.get("last_run_at")
                if last:
                    try:
                        prev = _dt.datetime.fromisoformat(last)
                        if prev.tzinfo is None:
                            prev = prev.replace(tzinfo=_dt.timezone.utc)
                        next_due = (prev + _dt.timedelta(minutes=cadence)).isoformat()
                    except (ValueError, TypeError):
                        next_due = None
                is_due = watch_is_due(watch["watch_id"], cadence, last, now)
            else:
                is_due = False
            rows.append({
                "key": key,
                "label": info.get("label") or key,
                "jurisdiction": info.get("jurisdiction") or "",
                "kind": info.get("kind") or "",
                "group_key": info.get("group_key") or "other",
                "group_label": info.get("group_label") or "Other",
                "kind_label": info.get("kind_label") or info.get("kind") or "",
                "incremental_mode": info.get("incremental_mode"),
                "can_incremental": info.get("can_incremental"),
                "doc_count": doc_counts.get(key, 0),
                "watermark": st.get("watermark"),
                "last_run": st.get("last_run"),
                "last_yield_at": st.get("last_yield_at"),
                "consecutive_failures": st.get("consecutive_failures") or 0,
                "watch": None if not watch else {
                    "watch_id": watch["watch_id"], "name": watch.get("name"),
                    "enabled": bool(watch.get("enabled")),
                    "cadence_minutes": watch.get("cadence_minutes"),
                    "last_run_at": watch.get("last_run_at"),
                    "next_due": next_due, "is_due": is_due,
                    "overlap_days": (watch.get("spec") or {}).get("overlap_days"),
                    "backfill": bool((watch.get("spec") or {}).get("backfill")),
                },
                # every watch on this source (the unified panel expands a source to these),
                # not just the representative one above
                "watches": [self._watch_summary(w, now) for w in
                            sorted(src_watches, key=lambda w: (not w.get("enabled"),
                                                               w.get("name") or ""))],
                "watch_count": len(src_watches),
                "recent_runs": runs.get(key, []),
            })
        rows.sort(key=lambda r: (r["jurisdiction"], r["kind"], r["label"]))
        return {"overlap_default_days": overlap_default, "sources": rows}

    def create_watch(self, *, name: str, spec: dict, cadence_minutes: int = 1440,
                     enabled: bool = True) -> dict:
        """Save a harvest plan run on a cadence. ``spec`` keys: ``source``
        (+ ``source_options`` and ``keywords`` — searched at the source API where
        supported, else post-filtered), ``discover`` ({"citing": id} — find NEW cases
        citing a target), ``enrich`` (default true: fetch what each new case cites, one
        hop), ``max_pages``, ``tag`` (label everything brought in), ``backfill`` (first
        run walks deep)."""
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

    def backfill_titles(self, *, limit: int = 500, reset_misses: bool = False,
                        on_progress=None, cancel_check=None) -> dict:
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

        _progress(on_progress, stage="reading held CJEU cases", done=0, total=0)
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
            _progress(on_progress, stage="shortening stored titles", done=shortened,
                      total=len(rows))
            # And give EU-legislation docs a real name where the source gave a generic
            # one ("EUR-Lex - 12008E267 - EN", "ANNEX", an OJ filename) — derived from
            # the CELEX (e.g. "Article 267 TFEU"). Local, no webservice.
            for r in cat.list_documents(source="eu-legislation", limit=100000):
                if cancel_check and cancel_check():
                    break
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

        if cancel_check and cancel_check():
            return {"candidates": len(targets), "updated": 0, "shortened": shortened,
                    "cancelled": True}
        _progress(on_progress, stage="mapping ECLI → CELEX (CELLAR)", done=0,
                  total=len(targets))
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
        _progress(on_progress, stage="EUR-Lex webservice", done=0, total=len(want))
        meta = eurlex_metadata(list(want))  # batched: ⌈N/50⌉ credentialed calls
        titled = tagged = 0
        _progress(on_progress, stage="writing names + subject tags", done=0, total=len(want))
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
            # The final resolve is a corpus-wide set-based pass; on a large single-source
            # corpus (the 117k-doc German de-rii relink) it outgrows the pool's 3-minute
            # request timeout and died with 'canceling statement due to statement timeout'.
            # Give it the same raised timeout the roll-ups get, and a heartbeat so the stall
            # detector doesn't flag the (legitimately long) resolve as frozen.
            _progress(on_progress, stage="resolving matched legislation", done=len(refs),
                      total=len(refs))
            with cat._maintenance_timeout():
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

    # -- free-text search (§6c) -----------------------------------------------
    def freetext_scope(self) -> dict:
        """What the free-text index covers, and what it could cover.

        The gate is stored as a list of sources rather than jurisdictions because a
        source is what the index is built over and what ``_apply_filters`` already
        filters on; the UI groups them by jurisdiction for the reader."""
        from .settings import SettingsStore

        store = SettingsStore(self.config.settings_path)
        raw = (store.resolve("RAGLEX_FTS_SOURCES") or "").strip()
        chosen = [s for s in re.split(r"[,\s]+", raw) if s]
        with self._open() as (cat, _rs, _ts):
            cov = cat.fts_coverage()
        by_source: dict[str, dict] = {}
        for row in cov:
            s = row["source"]
            e = by_source.setdefault(s, {"source": s, "with_text": 0, "indexed": 0,
                                         "courts": {}, "doc_types": {}})
            e["with_text"] += row["with_text"]
            e["indexed"] += row["indexed"]
            if row.get("court"):
                e["courts"][row["court"]] = e["courts"].get(row["court"], 0) + row["with_text"]
            if row.get("doc_type"):
                e["doc_types"][row["doc_type"]] = (
                    e["doc_types"].get(row["doc_type"], 0) + row["with_text"])
        return {
            "sources": sorted(by_source.values(), key=lambda r: -r["with_text"]),
            "selected": chosen,
            "note": store.resolve("RAGLEX_FTS_NOTE") or _DEFAULT_FTS_NOTE,
            "indexed_total": sum(e["indexed"] for e in by_source.values()),
        }

    def _freetext_selected(self) -> list[str]:
        """The gated sources, read from the setting alone — no coverage query."""
        from .settings import SettingsStore

        raw = (SettingsStore(self.config.settings_path).resolve("RAGLEX_FTS_SOURCES")
               or "").strip()
        return [s for s in re.split(r"[,\s]+", raw) if s]

    def freetext_index_summary(self) -> dict:
        """What the free-text box actually searches, by jurisdiction.

        The front page's job here is to manage expectations — a search box that does
        not say what it covers invites the reader to conclude the corpus lacks
        something it simply has not indexed — so this is the *jurisdictions in the
        index*, not the configuration behind them. Configuring belongs in
        Admin > Search."""
        with self._open() as (cat, _rs, _ts):
            rows = cat.fts_indexed_by_source()
        by_jur: dict[str, int] = {}
        for r in rows:
            if not r["n"]:
                continue
            jurisdiction = self._doc_bucket(r["source"], r.get("court"))
            by_jur[jurisdiction] = by_jur.get(jurisdiction, 0) + r["n"]
        out = [{"jurisdiction": j, "documents": n} for j, n in by_jur.items()]
        out.sort(key=lambda x: -x["documents"])
        return {"jurisdictions": out,
                "documents": sum(x["documents"] for x in out),
                "sources": len([r for r in rows if r["n"]])}

    def set_freetext_scope(self, *, sources: list[str] | None = None,
                           note: str | None = None) -> dict:
        """Set the gate. Narrowing it does NOT delete the index — a source dropped
        from the gate simply stops being searched, so re-adding it costs nothing."""
        from .settings import SettingsStore

        store = SettingsStore(self.config.settings_path)
        payload = {}
        if sources is not None:
            payload["RAGLEX_FTS_SOURCES"] = ",".join(sorted(set(sources)))
        if note is not None:
            payload["RAGLEX_FTS_NOTE"] = note
        if payload:
            store.update(payload)
        return self.freetext_scope()

    def search_status(self) -> dict:
        """One picture of both retrieval paths, for the admin Search page.

        Free text and semantics are independent — a source can be fully indexed for
        one and untouched by the other — and until this existed nothing said so,
        which is how the corpus ended up with a free-text feature that silently
        depended on an embedding pass that had never run."""
        from .settings import SettingsStore

        store = SettingsStore(self.config.settings_path)
        scope = self.freetext_scope()
        with self._open() as (cat, _rs, _ts):
            emb = {r["source"]: r for r in cat.embedding_coverage()}
        rows = []
        for s in scope["sources"]:
            e = emb.get(s["source"], {})
            rows.append({
                "source": s["source"],
                "jurisdiction": self._jurisdiction_of(s["source"]),
                "with_text": s["with_text"],
                "fts_indexed": s["indexed"],
                "embedded": e.get("embedded", 0),
                "in_fts_scope": s["source"] in scope["selected"],
                "courts": sorted(s["courts"].items(), key=lambda kv: -kv[1])[:12],
                "doc_types": sorted(s["doc_types"].items(), key=lambda kv: -kv[1]),
            })
        rows.sort(key=lambda r: (r["jurisdiction"], -r["with_text"]))
        embed_scope = (store.resolve("RAGLEX_EMBED_JURISDICTIONS") or "").strip()
        return {
            "sources": rows,
            "note": scope["note"],
            "fts_selected": scope["selected"],
            "totals": {
                "with_text": sum(r["with_text"] for r in rows),
                "fts_indexed": sum(r["fts_indexed"] for r in rows),
                "embedded": sum(r["embedded"] for r in rows),
            },
            "embedding": {
                "provider": store.resolve("RAGLEX_EMBED_PROVIDER") or "local-hashing",
                "model": store.resolve("RAGLEX_EMBED_MODEL"),
                "dimensions": store.resolve("RAGLEX_EMBED_DIMENSIONS"),
                "jurisdictions": embed_scope,
                "paused": embed_scope == "__none__",
            },
            "hpc": {
                "host": store.resolve("RAGLEX_HPC_HOST"),
                "model": store.resolve("RAGLEX_HPC_MODEL"),
                "tasks": store.resolve("RAGLEX_HPC_NTASKS"),
                "configured": bool(store.resolve("RAGLEX_HPC_HOST")),
            },
        }

    # Every stored doc_type, and the display KINDS the sibling tools take. The
    # free-text filter accepts both vocabularies because a reader who has just used
    # search(kind='cases') has no way to know this one wanted 'judgment' — and an
    # unrecognised value used to become `doc_type IN ('case')`, matching nothing.
    _FTS_DOC_TYPES = ("judgment", "legislation", "decision", "opinion", "guidance",
                      "preparatory", "note", "commentary")
    _FTS_KIND_DOC_TYPES = {
        "cases": ("judgment", "decision", "opinion"),
        "case": ("judgment", "decision", "opinion"),
        "caselaw": ("judgment", "decision", "opinion"),
        "case-law": ("judgment", "decision", "opinion"),
        "judgments": ("judgment",),
        "legislation": ("legislation",),
        "statute": ("legislation",),
        "law": ("legislation",),
        "act": ("legislation",),
        "guidance": ("guidance",),
        # not expressible as a doc_type set — an administrative decision is one made
        # by a regulator, which is a fact about the SOURCE — so it narrows to the
        # deciding types and says so rather than silently meaning something else
        "administrative": ("decision", "opinion", "notice"),
        "preparatory": ("preparatory",),
        "notes": ("note",),
        "commentary": ("commentary",),
    }

    def _resolve_doc_types(self, values: list[str]) -> tuple[list[str], list[str]]:
        """Map a doc_type filter onto stored doc types, accepting display kinds too.

        Returns (resolved, unrecognised). A value belonging to NEITHER vocabulary is
        reported rather than passed through: a filter nothing can satisfy returns
        total=0, and a silent zero in legal research reads as "the corpus holds no
        such authority" rather than "you passed a bad enum"."""
        resolved: list[str] = []
        unknown: list[str] = []
        for raw in values:
            v = (raw or "").strip().lower()
            if not v:
                continue
            if v in self._FTS_KIND_DOC_TYPES:
                resolved.extend(self._FTS_KIND_DOC_TYPES[v])
            elif v in self._FTS_DOC_TYPES:
                resolved.append(v)
            else:
                unknown.append(raw)
        # order-preserving dedup: 'cases,judgment' must not repeat the type
        return list(dict.fromkeys(resolved)), unknown

    def _doc_type_vocabulary(self) -> list[str]:
        return sorted(set(self._FTS_DOC_TYPES) | set(self._FTS_KIND_DOC_TYPES))

    def search_within_document(self, stable_id: str, query: str, *, limit: int = 20,
                               offset: int = 0) -> dict:
        """Literal search over the complete served body, bypassing the FTS index.

        This is deliberately grep-like rather than a second query language: pass one
        phrase or term, optionally quoted.  It is the authoritative escape hatch when
        a corpus-level index is incomplete or a researcher already knows the document.
        """
        from .core.segmentation import recover_numbered_segments
        from .fulltext.index import _literal_re, highlight_spans, snippet
        from .fulltext.query import Phrase, parse

        written = (query or "").strip()
        if ((written.startswith('"') and written.endswith('"'))
                or (written.startswith("“") and written.endswith("”"))):
            written = written[1:-1].strip()
        if not written:
            return {"stable_id": stable_id, "error": "empty query"}
        if re.search(r"(?i)\b(?:AND|OR|NOT|NEAR\s*/\s*\d+)\b|[()|&]", written):
            return {"stable_id": stable_id,
                    "error": "search_within_document is literal; pass one phrase or term"}
        words = re.findall(r"\d+(?:[./]\d+)+|[^\W_]+(?:['’][^\W_]+)*",
                           written, flags=re.UNICODE)
        if not words:
            return {"stable_id": stable_id, "error": "query contains no searchable words"}
        matcher = _literal_re(Phrase(words))
        with self._open() as (cat, _rs, ts):
            doc = cat.get_document(stable_id)
            if not doc or not doc["payload_hash"]:
                return {"stable_id": stable_id, "error": "not found or no text"}
            try:
                text = ts.get(doc["payload_hash"])
            except OSError:
                return {"stable_id": stable_id, "error": "text unavailable"}
            segs, synthesised = recover_numbered_segments(
                text, ts.get_segments(doc["payload_hash"]))
            offsets = [m.start() for m in matcher.finditer(text)]
            page = offsets[max(0, offset):max(0, offset) + max(1, min(limit, 100))]

            def anchor_at(at: int) -> str | None:
                return next((s.label for s in segs
                             if s.char_start <= at < s.char_end and s.label), None)

            parsed = parse(f'"{written}"', exact=True)
            fts_rows = cat.conn.execute(
                "SELECT count(*) AS parts, min(char_start) AS first_char, "
                "max(char_end) AS last_char, max(words) AS max_part_words "
                "FROM doc_fts WHERE doc_id = ?", (stable_id,)).fetchone()
            last = int(fts_rows["last_char"] or 0)
            safe = int(fts_rows["max_part_words"] or 0) <= cat.FTS_PART_WORDS
            complete = bool(fts_rows["parts"] and last >= len(text) and safe)
            return {
                "stable_id": stable_id,
                "title": doc["title"],
                "query": written,
                "matched": bool(offsets),
                "total": len(offsets),
                "offset": max(0, offset),
                "matches": [{
                    "char_start": at,
                    "anchor": anchor_at(at),
                    "snippet": (frag := snippet(text, at)),
                    "highlights": [list(span) for span in highlight_spans(frag, parsed)],
                } for at in page],
                "text_chars": len(text),
                "segmentation": ("synthesised" if synthesised
                                 else "structural" if segs else "none"),
                "segments_total": len(segs),
                "index_coverage": {
                    "indexed_chars": min(last, len(text)),
                    "text_chars": len(text),
                    "parts": int(fts_rows["parts"] or 0),
                    "max_part_words": int(fts_rows["max_part_words"] or 0),
                    "complete": complete,
                    "note": ("whole text is safely position-indexed" if complete else
                             "the stored index is incomplete or has a legacy oversized part; "
                             "these direct-body matches remain authoritative"),
                },
                "search_route": "complete served body (index bypassed)",
            }

    def freetext_search(self, query: str, *, exact: bool = True, limit: int = 25,
                        offset: int = 0, sources: list[str] | None = None,
                        doc_type: list[str] | None = None,
                        court: list[str] | None = None,
                        jurisdictions: list[str] | None = None,
                        year_from: int | None = None,
                        with_network: bool = True) -> dict:
        """Free-text search over the gated scope, with literal quotation support."""
        import difflib

        from .fulltext import index as fts

        # NOT freetext_scope(): that computes per-source coverage, which is a GROUP BY
        # over all 4.97M documents joined against a DISTINCT over the index — a
        # reporting query, run here on every keystroke-speed search. The search only
        # needs the list of selected sources, which is a setting.
        allowed = sources or self._freetext_selected() or None
        jurisdiction_scope: dict | None = None
        notes: list[str] = []
        if jurisdictions:
            # narrowing THIS search, not the index — the front page's flags.
            #
            # Through the SAME resolver the rest of the citing/facet machinery uses.
            # This compared the raw argument against the display name, so the ISO code
            # the tool documents ("eu") matched no source at all — and an EMPTY source
            # list is falsy, so the filter was then dropped and the search silently ran
            # over the whole corpus. jurisdiction="eu" came back half full of UK
            # assimilated instruments, correctly labelled United Kingdom in their own
            # facets: the filter had not been applied, not misapplied.
            pool = allowed or self._all_sources()
            available = {self._jurisdiction_of(s) for s in pool}
            # GDPRhub is one multilingual source whose records carry their country in
            # court-nl/court-de (or dpa-nl), not in the source key. Include those country
            # buckets in both validation and the SQL scope without admitting GDPRhub
            # records from every other jurisdiction.
            if "gdprhub" in pool or any(s.startswith("edpb") for s in pool):
                available.update(self._DPA_COUNTRY.values())
            want = {self._norm_jurisdiction(j) for j in jurisdictions} - {None}
            unknown = sorted(str(w) for w in want if w not in available)
            jurisdiction_sources = [s for s in pool if self._jurisdiction_of(s) in want]
            country_codes = sorted(c for c, name in self._DPA_COUNTRY.items() if name in want)
            jurisdiction_courts = [f"{prefix}-{code}"
                                   for code in country_codes for prefix in ("dpa", "court")]
            jurisdiction_scope = {"sources": jurisdiction_sources,
                                  "courts": jurisdiction_courts}
            if unknown:
                notes.append(
                    f"no indexed source lies in {', '.join(unknown)} — "
                    "call jurisdictions() for the names this filter accepts")
            if not jurisdiction_sources and not jurisdiction_courts:
                # No sources left: the honest answer is nothing, not everything. This
                # is the branch that used to widen.
                return {"items": [], "total": 0, "verified": 0, "candidates": 0,
                        "truncated": False, "took_ms": 0, "exact": exact,
                        "tsquery": None, "scope": [], "facets": {}, "network": {},
                        "matched": [], "notes": notes or ["nothing in scope"]}
        filters: dict = {"source": allowed} if allowed else {}
        if jurisdiction_scope is not None:
            filters["source_or_court"] = jurisdiction_scope
        if doc_type:
            wanted, unknown = self._resolve_doc_types(doc_type)
            if unknown:
                vocab = self._doc_type_vocabulary()
                suggestions = {
                    u: (difflib.get_close_matches(u.lower(), vocab, n=2, cutoff=0.5)
                        or None)
                    for u in unknown}
                # Loudly, not as an empty result set: the filter cannot be satisfied,
                # and "0 documents say this" is a different claim from "that is not a
                # document type".
                return {"items": [], "total": 0, "verified": 0, "candidates": 0,
                        "truncated": False, "took_ms": 0, "exact": exact,
                        "tsquery": None, "scope": [], "facets": {}, "network": {},
                        "matched": [],
                        "error": ("unknown doc_type: " + ", ".join(unknown)),
                        "did_you_mean": {k: v for k, v in suggestions.items() if v},
                        "accepts": vocab,
                        "notes": [*notes, "no search was run — the doc_type filter "
                                          "names no document type in this corpus"]}
            filters["doc_type"] = wanted
        if year_from:
            filters["year_from"] = year_from
        with self._open() as (cat, _rs, ts):
            positional_risk = cat.fts_positional_risk_count()
            if positional_risk:
                notes.append(
                    f"{positional_risk:,} indexed documents still have legacy oversized "
                    "parts while the positional-index repair runs; phrase/proximity misses "
                    "in those documents are not conclusive. Use search_within_document() "
                    "for a known authority."
                )
            res = fts.search(cat, ts, query, filters=filters, exact=exact,
                             limit=limit, offset=offset)
            # Facets over the WHOLE result set, never the page. One metadata read
            # for every match, which also lets the client narrow a facet instantly
            # instead of asking the server again.
            meta = cat.documents_meta(res.matched)
            facets = self._freetext_facets(meta)
            # Without the citing-id lists: fetching those cost 10.6 seconds of a
            # 12-second search — 60 queries over `relations` with 900-element IN
            # lists — to pre-compute an answer for a facet click that usually never
            # comes. The lists are fetched on demand instead (freetext_cites_filter).
            network = ({"cites": cat.cited_by_documents(res.matched, limit=20)}
                       if with_network and res.matched else {})
            items = []
            for h in res.hits:
                doc = cat.get_document(h.doc_id)
                if not doc:
                    continue
                # which paragraph the match falls in, so the result links to the
                # passage rather than the top of a 400-paragraph judgment
                seg_label = _segment_at(ts, doc, h.char_start)
                # a court sub-gate is applied here rather than in SQL: the tick-list
                # is per-source and sparse, and the candidate set is already small
                if court and doc["court"] not in court:
                    continue
                items.append({
                    "stable_id": h.doc_id,
                    "title": doc["title"],
                    "court": doc["court"],
                    "court_label": (self.court_label(doc["court"], doc["source"])
                                    if doc["court"] else None),
                    "doc_type": doc["doc_type"],
                    "source": doc["source"],
                    "decision_date": doc["decision_date"],
                    "jurisdiction": self._doc_bucket(doc["source"], doc["court"]),
                    "oscola": _oscola_cite(doc, _row_meta(doc)),
                    "snippet": h.snippet,
                    "highlights": [list(sp) for sp in h.highlights],
                    "char_start": h.char_start,
                    "anchor": seg_label,
                    "rank": h.rank,
                })
        return {
            "items": items, "total": res.total, "verified": res.verified,
            **({"error": res.error} if res.error else {}),
            "candidates": res.candidates, "truncated": res.truncated,
            "notes": [*notes, *res.notes], "took_ms": res.took_ms, "exact": exact,
            "tsquery": res.tsquery, "scope": allowed,
            "facets": facets, "network": network,
            # Compact metadata for EVERY match, not just the page. The client narrows
            # over this and pages through what survives — without it the facet counts
            # describe 79 documents while the filter applies to the 20 that happened
            # to be hydrated, which is the same defect as "912 citing · showing 40".
            "matched": [
                {"id": m["stable_id"], "s": m.get("source"), "c": m.get("court"),
                 "t": m.get("doc_type"),
                 "j": self._doc_bucket(m.get("source") or "", m.get("court")),
                 "y": (m.get("decision_date") or m.get("effective_date") or "")[:4] or None,
                 # how many documents cite it, and PageRank — so the whole result set
                 # can be re-sorted in the browser rather than re-queried
                 "n": m.get("cited_by") or 0, "p": m.get("pagerank") or 0}
                for m in meta],
        }

    def _freetext_facets(self, meta: list[dict]) -> dict:
        """Count the result set along every dimension the metadata supports.

        Years are returned in full rather than bucketed so the client can draw a
        histogram AND offer a range brush over the same data; decades are a
        convenience for the collapsed view."""
        from collections import Counter

        src: Counter[str] = Counter()
        jur: Counter[str] = Counter()
        kind: Counter[str] = Counter()
        court: Counter[str] = Counter()
        years: Counter[str] = Counter()
        undated = 0
        court_labels: dict[str, str] = {}
        for m in meta:
            source = m.get("source") or ""
            src[source] += 1
            jur[self._doc_bucket(source, m.get("court"))] += 1
            if m.get("doc_type"):
                kind[m["doc_type"]] += 1
            if m.get("court"):
                key = f"{source}\u241f{m['court']}"
                court[key] += 1
                court_labels.setdefault(
                    key, self.court_label(m["court"], source) or m["court"])
            d = (m.get("decision_date") or m.get("effective_date") or "")[:4]
            if len(d) == 4 and d.isdigit():
                years[d] += 1
            else:
                undated += 1

        def rows(counter: Counter, labeller=None) -> list[dict]:
            return [{"value": k.split("\u241f")[-1], "key": k,
                     "label": (labeller or {}).get(k, k.split("\u241f")[-1]),
                     "n": n}
                    for k, n in counter.most_common()]

        return {
            "source": rows(src),
            "jurisdiction": rows(jur),
            "doc_type": rows(kind),
            "court": rows(court, court_labels)[:40],
            "years": [{"year": y, "n": n} for y, n in sorted(years.items())],
            "undated": undated,
        }

    def localise_text(self, *, sources: list[str] | None = None,
                      limit: int = 2_000_000, on_progress=None,
                      cancel_check=None) -> dict:
        """Copy the text of the given sources onto the PRIMARY (fast) store.

        The query path reads the document text twice — once to verify a literal
        quotation, once to build the snippet — so free-text search is only usable
        where the text is local: 0.046 ms a document against 22.57 ms over the mount,
        measured. This brings a jurisdiction across.

        Deliberately explicit. ``TextStore.put`` writes in place precisely so that a
        corpus-wide repair cannot migrate documents onto the small disk by accident;
        this is the one caller that says "yes, move it", and it is scoped by source.
        Idempotent — a payload already local is skipped — and safe to interrupt."""
        st = {"scanned": 0, "copied": 0, "already": 0, "missing": 0, "bytes": 0}
        with self._open() as (cat, _rs, ts):
            if ts.fallback is None:
                return {"error": "no fallback store configured — nothing to copy from",
                        **st}
            where = "WHERE has_text = 1 AND payload_hash IS NOT NULL"
            params: list[object] = []
            if sources:
                where += f" AND source IN ({','.join('?' * len(sources))})"
                params.extend(sources)
            rows = cat.conn.execute(
                # ORDERED, so an interrupted run resumes through the same sequence
                # instead of re-walking an arbitrary permutation. Four interruptions
                # each cost a fresh walk of everything already done.
                f"SELECT DISTINCT payload_hash FROM documents {where} "
                "ORDER BY payload_hash LIMIT ?",
                params + [limit]).fetchall()
            total = len(rows)
            for n, r in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                if n % 500 == 0:
                    _progress(on_progress, stage="copying text to local storage",
                              done=n, total=total,
                              item=f"{st['bytes'] / 1e9:.2f} GB copied")
                ph = r["payload_hash"]
                st["scanned"] += 1
                if ts.locate(ph) == "local":
                    st["already"] += 1
                    # a re-run repairs a missing sidecar without re-copying the text
                    if not ts.get_segments(ph):
                        remote = ts.fallback / ts._rel(ph)
                        alt = remote.with_suffix(".seg.json")
                        if alt.exists():
                            (ts.root / ts._rel(ph)).with_suffix(".seg.json").write_text(
                                alt.read_text(encoding="utf-8"), encoding="utf-8")
                            st["segments_repaired"] = st.get("segments_repaired", 0) + 1
                    continue
                try:
                    text = ts.get(ph)
                except OSError:
                    st["missing"] += 1
                    continue
                # READ the sidecar first: put_local makes path_for resolve to the
                # new local copy, and get_segments would then look for a sidecar that
                # is not there yet and return nothing. That ordering silently copied
                # 483,000 documents without their paragraph structure, so every
                # free-text hit came back with no paragraph to link to.
                segs = ts.get_segments(ph)
                ts.put_local(ph, text)
                if segs:
                    ts.put_segments(ph, segs)
                st["copied"] += 1
                st["bytes"] += len(text.encode("utf-8"))
        return st

    def text_storage(self) -> dict:
        """Where the corpus's text actually lives, per source — so a split store is
        legible rather than something to infer from latency."""
        with self._open() as (cat, _rs, ts):
            if ts.fallback is None:
                return {"split": False, "root": str(ts.root)}
            rows = cat.conn.execute(
                "SELECT source, payload_hash FROM documents "
                "WHERE has_text = 1 AND payload_hash IS NOT NULL").fetchall()
            per: dict[str, dict] = {}
            seen: set[str] = set()
            for r in rows:
                ph = r["payload_hash"]
                if ph in seen:
                    continue
                seen.add(ph)
                e = per.setdefault(r["source"], {"source": r["source"], "local": 0,
                                                 "remote": 0, "missing": 0})
                where = ts.locate(ph)
                e["local" if where == "local"
                  else "remote" if where == "fallback" else "missing"] += 1
            return {"split": True, "root": str(ts.root), "fallback": str(ts.fallback),
                    "sources": sorted(per.values(), key=lambda x: -(x["local"] + x["remote"]))}

    def freetext_cites_filter(self, *, ids: list[str], target: str) -> dict:
        """Which of these results cite ``target``.

        Called when a reader clicks the "cites" facet, rather than pre-computed for
        every possible target on every search — see the comment in freetext_search."""
        with self._open() as (cat, _rs, _ts):
            return {"ids": sorted(cat.documents_citing(ids[:20000], target)),
                    "target": target}

    def freetext_hydrate(self, *, ids: list[str], query: str,
                         exact: bool = True) -> dict:
        """Full result rows — snippet, highlights, paragraph anchor — for a specific
        page of ids the client has already narrowed to.

        Searching returns metadata for every match but a snippet for none of them:
        building a snippet means reading the document, and reading four thousand to
        show twenty is the cost this split avoids."""
        from .fulltext import index as fts
        from .fulltext.query import parse

        parsed = parse(query or "", exact=exact)
        items: list[dict] = []
        with self._open() as (cat, _rs, ts):
            # The LIVE count off the resolved graph, which is what lookup() and
            # citator() report. documents_meta reads doc_authority.in_degree — a
            # PageRank-layer roll-up refreshed on its own schedule — so between
            # refreshes one session could see "cited_by 7" in a search row and
            # "cited_by_count 8" on the same document a call later. Cheap here: this
            # page is at most 100 ids and it is one grouped aggregate.
            cited = cat.cited_by_counts(list(ids[:100]))
            for doc_id in ids[:100]:
                doc = cat.get_document(doc_id)
                if not doc or not doc["payload_hash"]:
                    continue
                try:
                    text = ts.get(doc["payload_hash"])
                except OSError:
                    continue
                at = fts.verify(text, parsed) if (parsed.literals or parsed.excluded) \
                    else fts._first_term_at(text, parsed)
                frag = fts.snippet(text, at)
                # every passage that matches, so a document using the phrase eight
                # times reads differently from one using it once. Segments are read
                # once per document and shared across its passages.
                try:
                    from .core.segmentation import recover_numbered_segments
                    segs, _recovered = recover_numbered_segments(
                        text, ts.get_segments(doc["payload_hash"]) or [])
                except OSError:
                    segs = []

                def label_at(off: int) -> str | None:
                    for sg in segs:
                        if sg.char_start <= off < sg.char_end and sg.label:
                            return sg.label
                    return None

                passages = []
                for off in fts.match_offsets(text, parsed):
                    pfrag = fts.snippet(text, off)
                    passages.append({
                        "char_start": off, "snippet": pfrag,
                        "highlights": [list(sp) for sp
                                       in fts.highlight_spans(pfrag, parsed)],
                        "anchor": label_at(off),
                    })
                items.append({
                    "stable_id": doc_id,
                    "title": doc["title"],
                    "court": doc["court"],
                    "court_label": (self.court_label(doc["court"], doc["source"])
                                    if doc["court"] else None),
                    "doc_type": doc["doc_type"],
                    "source": doc["source"],
                    "decision_date": doc["decision_date"],
                    "jurisdiction": self._doc_bucket(doc["source"], doc["court"]),
                    "oscola": _oscola_cite(doc, _row_meta(doc)),
                    "snippet": frag,
                    "highlights": [list(sp) for sp in fts.highlight_spans(frag, parsed)],
                    "char_start": at,
                    "anchor": (passages[0]["anchor"] if passages
                               else _segment_at(ts, doc, at)),
                    "cited_by": cited.get(doc_id, 0),
                    "passages": passages,
                    "passage_count": len(passages),
                })
        return {"items": items}

    def freetext_for_agent(self, query: str, *, limit: int = 10, exact: bool = True,
                           jurisdictions: list[str] | None = None,
                           sources: list[str] | None = None,
                           doc_type: list[str] | None = None,
                           court: list[str] | None = None,
                           year_from: int | None = None,
                           passages: int = 3) -> dict:
        """Free-text search shaped for an agent rather than a browser.

        The web response carries compact metadata for every match — up to four
        thousand rows — because the page narrows and sorts locally. An agent pays for
        that in tokens and cannot use it, so this returns the page, the counts, the
        top of each facet, and the authorities the results have in common: the things
        that change what an agent does next.

        Passages are the part worth spending tokens on. A judgment that uses the
        phrase eight times is a different answer from one that mentions it once, and
        an agent cannot see that from a single snippet."""
        res = self.freetext_search(
            query, exact=exact, limit=limit, sources=sources, doc_type=doc_type,
            court=court, jurisdictions=jurisdictions, year_from=year_from)
        if res.get("error"):
            # never as an empty result: a rejected filter, or a query the index
            # refused, must not be readable as an answer about the corpus
            hint = next((n for n in reversed(res.get("notes") or [])
                         if n != res["error"]), None)
            return {k: v for k, v in {
                "query": query, "error": res["error"],
                "did_you_mean": res.get("did_you_mean") or None,
                "accepts": res.get("accepts"),
                "hint": hint,
            }.items() if v}
        ids = [it["stable_id"] for it in res.get("items", [])]
        hydrated = {h["stable_id"]: h
                    for h in self.freetext_hydrate(ids=ids, query=query,
                                                   exact=exact).get("items", [])}
        items = []
        for it in res.get("items", []):
            h = hydrated.get(it["stable_id"], {})
            ps = (h.get("passages") or [])[:max(0, passages)]
            items.append({k: v for k, v in {
                "id": it["stable_id"],
                "title": it.get("title"),
                "citation": (it.get("oscola") or {}).get("plain") if isinstance(
                    it.get("oscola"), dict) else None,
                "court": it.get("court_label") or it.get("court"),
                "jurisdiction": it.get("jurisdiction"),
                "date": it.get("decision_date"),
                "cited_by": h.get("cited_by") or None,
                "passage_count": h.get("passage_count"),
                "passages": [{"at": p["anchor"], "text": p["snippet"]} for p in ps]
                            or None,
            }.items() if v not in (None, [], "")})

        def top(rows, n=8):
            return {r["label"] or r["value"]: r["n"] for r in (rows or [])[:n]}

        fac = res.get("facets") or {}
        out = {
            "query": query,
            "exact": exact,
            "total": res.get("verified") if res.get("verified") is not None
                     else res.get("total"),
            "shown": len(items),
            "items": items,
            "facets": {k: v for k, v in {
                # keyed by the PARAMETER name — a facet a reader cannot feed back into
                # the filter that produced it is a third vocabulary to guess at
                "jurisdiction": top(fac.get("jurisdiction")),
                "doc_type": top(fac.get("doc_type")),
                "court": top(fac.get("court")),
                "decade": _decades(fac.get("years")),
            }.items() if v},
            # what the matching documents cite between them — the doctrinal anchors of
            # a result set, which no per-document view can show
            "commonly_cited": [
                {"id": c["stable_id"], "title": c.get("title"), "by": c["citing"]}
                for c in ((res.get("network") or {}).get("cites") or [])[:8]],
        }
        if res.get("truncated"):
            out["note"] = ("more than the candidate budget matched — the total is a "
                           "lower bound; narrow the query for an exact count")
        if res.get("notes"):
            out["warnings"] = res["notes"]
        return out

    def build_freetext_index(self, *, sources: list[str] | None = None,
                             reindex: bool = False, limit: int = 1_000_000,
                             on_progress=None, cancel_check=None) -> dict:
        """Build (or extend) the free-text index over the gated scope."""
        from .fulltext import index as fts

        scope = self.freetext_scope()
        targets = sources or scope["selected"]
        if not targets:
            return {"error": "no sources selected — set the free-text scope first"}
        with self._open() as (cat, _rs, ts):
            return fts.build(cat, ts, sources=targets, reindex=reindex, limit=limit,
                             on_progress=on_progress, cancel_check=cancel_check)

    @staticmethod
    def _index_freetext_ids_open(cat, ts, document_ids: list[str], *,
                                 on_progress=None, cancel_check=None) -> dict:
        """Index a known harvested delta using the caller's open stores.

        ``fulltext.index.build`` discovers a whole source and is the right repair/backfill
        operation. A harvest already knows its exact changed ids, so rescanning the source
        after every weekly tick is unnecessary. Refreshed ids are intentionally included:
        ``put_doc_fts`` replaces their old parts with vectors for the current text.
        """
        ids = list(dict.fromkeys(document_ids))
        out = {"indexed": 0, "parts": 0, "unreadable": 0}
        for n, sid in enumerate(ids, 1):
            if cancel_check and cancel_check():
                break
            if n == 1 or n % 200 == 0 or n == len(ids):
                _progress(on_progress, stage="indexing harvested full text",
                          done=n, total=len(ids), item=sid)
            doc = cat.get_document(sid)
            if doc is None or not doc["payload_hash"] or doc["search_excluded"]:
                continue
            try:
                text = ts.get(doc["payload_hash"])
                segments = ts.get_segments(doc["payload_hash"])
            except (OSError, TypeError):
                out["unreadable"] += 1
                continue
            if not text.strip():
                continue
            labels = [(str(doc["title"] or sid), 0)]
            labels.extend((segment.label, segment.char_start) for segment in segments
                          if segment.label and segment.label != doc["title"])
            out["parts"] += cat.put_doc_fts(
                sid, text, headings=labels, commit=False,
            )
            out["indexed"] += 1
            if out["indexed"] % 200 == 0:
                cat.commit()
        cat.commit()
        return out

    def repair_freetext_positions(self, *, limit: int = 1_000_000,
                                  on_progress=None, cancel_check=None) -> dict:
        """Repartition only legacy FTS rows that exceed PostgreSQL's position budget."""
        st = {"risky": 0, "reindexed": 0, "parts": 0, "unreadable": 0}
        with self._open() as (cat, _rs, ts):
            rows = cat.conn.execute(
                "SELECT d.stable_id, d.payload_hash FROM documents d "
                "WHERE EXISTS (SELECT 1 FROM doc_fts f WHERE f.doc_id = d.stable_id "
                "AND f.words > ?) ORDER BY d.stable_id LIMIT ?",
                (cat.FTS_PART_WORDS, limit),
            ).fetchall()
            st["risky"] = len(rows)
            for n, row in enumerate(rows, 1):
                if cancel_check and cancel_check():
                    break
                if on_progress and (n == 1 or n % 200 == 0):
                    _progress(on_progress, stage="repairing full-text phrase positions",
                              done=n, total=len(rows), item=row["stable_id"])
                try:
                    text = ts.get(row["payload_hash"])
                except (OSError, TypeError):
                    st["unreadable"] += 1
                    continue
                st["parts"] += cat.put_doc_fts(row["stable_id"], text, commit=False)
                st["reindexed"] += 1
                if st["reindexed"] % 200 == 0:
                    cat.conn.commit()
            cat.conn.commit()
            st["remaining"] = cat.fts_positional_risk_count()
        return st

    # -- learned shorthands (the corpus-wide store, curated by hand) -----------
    def browse_shorthands(self, *, query: str | None = None,
                          candidate_id: str | None = None, state: str = "all",
                          limit: int = 100, offset: int = 0) -> dict:
        """A page of the learned-shorthand store, with the titles of what each points
        at so a reviewer can judge it without looking anything up."""
        with self._open() as (cat, _rs, _ts):
            rows, total = cat.browse_learned_shorthands(
                query=query, candidate_id=candidate_id, state=state,
                limit=limit, offset=offset)
            out = [dict(r) for r in rows]
            titles = self._titles_for({r["candidate_id"] for r in out})
            for r in out:
                r["blocked"] = bool(r.get("blocked"))
                r["is_abbrev"] = bool(r.get("is_abbrev"))
                r["target_title"] = titles.get(r["candidate_id"])
                r["valid"] = _valid_shorthand(r["shorthand"])
                r["doc_count"] = int(r.get("doc_count") or 0)
                # what the reviewer needs to know at a glance: does this one travel?
                r["applies_corpus_wide"] = (
                    r["valid"] and not r["blocked"]
                    and r["doc_count"] >= _SHORTHAND_MIN_DOCS)
            counts = {
                "total": cat.count_learned_shorthands(),
                "blocked": cat.browse_learned_shorthands(state="blocked", limit=1)[1],
                "corpus_wide": cat.browse_learned_shorthands(state="active", limit=1)[1],
                "threshold": _SHORTHAND_MIN_DOCS,
            }
            return {"rows": out, "total": total, "counts": counts}

    def _titles_for(self, ids: set[str]) -> dict[str, str]:
        """Best-effort titles for candidate ids — a candidate may name an authority the
        corpus doesn't hold, which is not an error (that is what the worklist is for)."""
        if not ids:
            return {}
        found: dict[str, str] = {}
        with self._open() as (cat, _rs, _ts):
            for cid in ids:
                try:
                    doc = cat.get_document(cid)
                except Exception:  # noqa: BLE001 — a malformed stored id must not 500
                    doc = None
                if doc and doc["title"]:
                    found[cid] = doc["title"]
        return found

    def set_shorthand(self, *, shorthand: str, candidate_id: str, **fields) -> dict:
        with self._open() as (cat, _rs, _ts):
            return {"updated": cat.set_learned_shorthand(shorthand, candidate_id, **fields)}

    def delete_shorthand(self, *, shorthand: str, candidate_id: str) -> dict:
        with self._open() as (cat, _rs, _ts):
            return {"deleted": cat.delete_learned_shorthand(shorthand, candidate_id)}

    def purge_shorthands(self, *, dry_run: bool = True,
                         include_local: bool = False) -> dict:
        """Delete stored shorthands that would not be learned today; with
        ``include_local``, also those below the ≥3-document threshold (the bulk of the
        store, including the report boilerplate that reads as a plausible name)."""
        with self._open() as (cat, _rs, _ts):
            return cat.purge_invalid_learned_shorthands(
                dry_run=dry_run, include_local=include_local)

    def backfill_shorthand_doc_counts(self, *, dry_run: bool = True,
                                      on_progress=None) -> dict:
        """Recover ``doc_count`` for the store's existing rows from the citations table.

        Until this runs, every row written before the store counted anything reads as
        document-local and nothing travels — including the abbreviations that should."""
        with self._open() as (cat, _rs, _ts):
            with cat._maintenance_timeout():
                return cat.backfill_learned_shorthand_doc_counts(
                    dry_run=dry_run, on_progress=on_progress)

    # -- feedback (Bugs / Feature requests from the app's feedback box) --------
    def submit_feedback(self, *, kind: str, message: str, page: str | None = None,
                        url: str | None = None, metadata: dict | None = None) -> dict:
        """Record a Bug / Feature-request / Improvement into the review queue, with whatever
        page context the client captured (route, doc id, query, role, user-agent) as JSON
        metadata.

        ``improvement`` is the engineering-observation kind: an agent working through MCP
        sees the corpus from an angle no one using the UI does — which anchor forms fail,
        which idioms a grammar misses, where a tool's contract fights the task — and that
        belongs in the same queue a human's bug report does, not in a chat log.
        """
        import json as _json
        k = (kind or "bug").strip().lower()
        if k not in ("bug", "feature", "improvement"):
            k = "bug"
        msg = (message or "").strip()
        if not msg:
            return {"error": "empty message"}
        meta = _json.dumps(metadata) if metadata else None
        with self._open() as (cat, _rs, _ts):
            fid = cat.add_feedback(kind=k, message=msg[:8000], page=page, url=url, metadata=meta)
        return {"submitted": True, "feedback_id": fid, "kind": k}

    def report_issue(self, *, message: str, fingerprint: str, page: str | None = None,
                     metadata: dict | None = None, kind: str = "error") -> dict:
        """File a SYSTEM-reported problem into the same review queue as user feedback and
        refinement flags, deduplicated on ``fingerprint`` (see ops/errorlog.py).

        This is what turns "a warning went into a container log nobody reads" into a work
        item an agent can be told to fix, alongside the reports users file by hand."""
        import json as _json
        msg = (message or "").strip()
        if not msg:
            return {"error": "empty message"}
        meta = None
        try:
            meta = _json.dumps(metadata) if metadata else None
        except (TypeError, ValueError):
            meta = None
        with self._open() as (cat, _rs, _ts):
            fid = cat.record_issue(fingerprint=fingerprint, message=msg[:8000], page=page,
                                   metadata=meta, kind=kind)
        return {"recorded": True, "feedback_id": fid, "kind": kind}

    def list_feedback(self, *, status: str | None = "open", limit: int = 500,
                      kind: str | None = None) -> list[dict]:
        """The review queue: user Bugs / Feature requests AND the system's own errors
        (``kind='error'``). Filter with ``kind``."""
        import json as _json
        with self._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.feedback(status=status, limit=limit, kind=kind)]
        for r in rows:
            if r.get("metadata"):
                try:
                    r["metadata"] = _json.loads(r["metadata"])
                except (ValueError, TypeError):
                    pass
        return rows

    def resolve_feedback(self, *, feedback_id: int, status: str = "resolved") -> dict:
        with self._open() as (cat, _rs, _ts):
            return {"updated": cat.set_feedback_status(feedback_id, status)}

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
                    # Structured native/report aliases are stronger than a cluster
                    # inferred from prose and must never be overwritten by it.
                    cat.put_alias(m, canonical, source=source, commit=False,
                                  overwrite=False)
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
        """Execute one watch: harvest its source's delta (keywords searched at the API
        where supported), and/or discover NEW cases citing a target; fetch what each new
        case cites, one hop (unless ``enrich`` is false); tag everything brought in.
        Records the result + last-run time.

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
                             use_llm=spec.get("use_llm"), overlap_days=spec.get("overlap_days"),
                             return_ids=True, on_progress=on_progress)
            result["harvest"] = {k: v for k, v in h.items() if k != "new_ids"}
            seed_ids = list({*seed_ids, *h.get("new_ids", [])})

        # Forward-citation discovery: NEW cases citing a target (the renewing seed).
        disc = spec.get("discover")
        if disc and disc.get("citing"):
            _emit(f"discovering cases citing {disc['citing']}")
            d = self.discover_citing(target=disc["citing"], via=disc.get("via", "auto"),
                                     query=disc.get("query"), max_pages=spec.get("max_pages", 1))
            result["discover"] = {k: d.get(k) for k in ("via", "query", "count")}
            seed_ids = list({*seed_ids, *d.get("discovered", [])})

        # One-hop enrichment (opt-out with enrich:false): pull the routable authorities each
        # newly harvested case cites, once. No further crawl — the old degree-N radiate is
        # gone. The register delta plus this single hop is the whole of a watch's work.
        if spec.get("enrich", True) and seed_ids and not (cancel_check and cancel_check()):
            _emit("fetching cited authorities")
            with self._open() as (cat, rs, ts):
                result["enrich"] = self._enrich_cited(
                    cat, rs, ts, seed_ids, limit=int(spec.get("enrich_limit", 100)),
                    on_progress=on_progress, cancel_check=cancel_check)

        if spec.get("tag") and seed_ids:
            self.tag_many(doc_ids=seed_ids, tag=spec["tag"])
            result["tagged"] = len(seed_ids)

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
        overlap_days: int | None = None, force_full: bool = False,
        resume_unfinished: bool = False,
        postprocess_after_relation_id: int = 0, return_ids: bool = False,
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

        started_at = _now_iso()
        # A watch harvest carries a ``watch:<id>:<source>`` cursor key — parse it so the
        # per-run log records what triggered the run and against which watch.
        run_watch_id: int | None = None
        run_trigger = "manual"
        if watermark_key and watermark_key.startswith("watch:"):
            run_trigger = "watch"
            try:
                run_watch_id = int(watermark_key.split(":")[1])
            except (IndexError, ValueError):
                run_watch_id = None

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
                             overlap_days=overlap_days, force_full=force_full,
                             on_progress=_phase_progress, cancel_check=cancel_check)
            # Log the run for the Maintain keep-current diagnosis view (bounded history).
            # Only feed-style harvests are worth logging; a targeted single-item fetch sets
            # record_health=False upstream and never reaches here with a real crawl.
            try:
                cat.record_source_run(
                    adapter.source, started_at=started_at, finished_at=_now_iso(),
                    discovered=stats.discovered, stored=stats.stored, deduped=stats.deduped,
                    refreshed=len(stats.refreshed_ids), errors=stats.errors,
                    not_found=stats.not_found, rate_limited=stats.rate_limited,
                    backfill=backfill, watermark=stats.watermark,
                    trigger=run_trigger, watch_id=run_watch_id)
            except Exception:  # noqa: BLE001 — logging a run must never fail the harvest
                log.exception("record_source_run failed for %s", adapter.source)
            # Extract + classify ONLY the newly-fetched documents — NOT the whole corpus.
            # (Re-extracting all ~20k docs on every harvest was O(minutes) of pure-CPU
            # grammar work; resolution already links existing pending edges to the new
            # nodes without re-mining their text.) Upstream-REVISED documents the crawl
            # re-fetched (contenthash changed) aren't "new" but their text changed, so
            # they get the same re-extract/classify pass.
            new_ids = list(dict.fromkeys([*stats.stored_ids, *stats.refreshed_ids]))
            # DILA's bulk LEGI fund is article-granular. Keep those precise nodes, but
            # also materialise their LEGITEXT parent so a search/citation for "loi
            # n° 2004-801" reaches one statute rather than a constitutional decision or
            # twenty-two disconnected article rows. Only parents touched by this run are
            # rebuilt, so daily deltas remain proportional to the delta.
            if adapter.source == "fr-dila" and new_ids:
                code_expr = ("meta_json::jsonb ->> 'code_cid'"
                             if cat.backend == "postgres"
                             else "json_extract(meta_json, '$.code_cid')")
                parent_ids: list[str] = []
                for start in range(0, len(new_ids), 20_000):
                    chunk = new_ids[start:start + 20_000]
                    qs = ",".join("?" for _ in chunk)
                    parent_ids.extend(
                        str(r["parent_id"]) for r in cat.conn.execute(
                            f"SELECT DISTINCT {code_expr} AS parent_id FROM documents "
                            f"WHERE stable_id IN ({qs}) AND {code_expr} LIKE 'LEGITEXT%'",
                            tuple(chunk)).fetchall() if r["parent_id"])
                parent_docs = self._materialize_fr_legislation_parents_open(
                    cat, ts, parent_ids)
                new_ids = list(dict.fromkeys([*new_ids, *parent_docs]))
            # Bulk primary-law/caselaw sources: no guidance can occur in them (skip the
            # per-document classification PK lookups) and their post-processing must be
            # rebuilt from durable state on restart (see below).
            primary_bulk_sources = {
                "fr-dila", "fr-dila-legi", "de-rii", "de-gii", "de-gesetze",
                "de-gesetze-im-internet", "de-openlegaldata",
                "nl-rechtspraak", "nl-legislation",
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
            if (resume_unfinished or (options or {}).get("start_offset")
                    or adapter.source in primary_bulk_sources):
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
            # Free-text search is an explicitly gated derived layer. Once a source is
            # selected, every later harvest must extend it immediately; otherwise a
            # healthy keep-current watch grows the documents table while search silently
            # remains frozen at the last manual full-index build. That was the apparent
            # Irish recency gap: hundreds of 2026 judgments were held and extracted, but
            # zero ie-caselaw rows existed in doc_fts.
            if adapter.source in self._freetext_selected() and new_ids:
                self._index_freetext_ids_open(
                    cat, ts, new_ids, on_progress=_phase_progress,
                    cancel_check=cancel_check,
                )
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
                    "new_documents": len(new_ids),
                    # a watch asks for the ids so it can enrich just the new docs (capped —
                    # an incremental watch delta is small; a backfill is not, hence the cap)
                    **({"new_ids": new_ids[:1000]} if return_ids else {})}

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
