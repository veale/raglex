"""Citation extraction over free text (§5).

Runs every registered grammar over the text, then resolves overlaps so a single
reference yields one citation (the most specific match wins — "Article 17 of
Regulation (EU) 2016/679" beats the bare "2016/679" inside it). Output is a list
of ``Citation`` with char spans; the stage turns each into a hanging typed edge
(§5b) that resolution links later.

An ``llm`` extractor — for narrative citations a grammar can't catch ("the Court's
earlier data-retention ruling") — slots in behind the same ``extract`` signature
and is batched (§5); grammars stay the cheap, deterministic first pass.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

from .grammars import DROP, GRAMMARS
from .models import Citation

# A pinpoint into a *cited case*: the paragraph number(s) trailing the citation —
# "at [57]", "at paras 8, 44", "at paras 30–31", "paragraphs 168 and 177",
# "§§ 35-36". The continuation swallows list/range tails but never a following
# citation's year (4-digit 19xx/20xx excluded there). Years excluded as leads too.
_PIN_CONT = r"(?:\s*(?:,|–|—|-|to|and|&)\s*\[?(?!(?:19|20)\d\d\b)\d{1,4}\]?)*"
_CASE_PINPOINT = re.compile(
    r"^[\s,;]*(?:\(CanLII\)\s*)?\(?\s*(?:per\b[^.;)\n]{0,40}?\s)?(?:"
    rf"(?:at|in)\s+(?:paras?\.?\s*|paragraphs?\s+|§§?\s*)?(?P<a>\[?\d{{1,4}}\]?{_PIN_CONT})"
    rf"|(?:paras?\.?\s*|paragraphs?\s+)(?P<b>\[?\d{{1,4}}\]?{_PIN_CONT})"
    rf"|§§?\s*(?P<d>\d{{1,4}}{_PIN_CONT})"  # ECHR: "Golder v UK, § 35", "§§ 35-36"
    r"|(?:r\.?\s*o\.?|rov\.)\s*(?P<e>\d{1,3}(?:\.\d{1,3}){0,3})"  # Dutch rechtsoverweging
    r"|\[(?P<c>\d{1,3})\]"
    r")",
    re.IGNORECASE,
)


def _pin_text(run: str) -> str:
    """Normalise a matched paragraph run to a stored pinpoint: '[8]' → 'para 8',
    '8, 44' → 'para 8, 44', '30–31' stays a range. First number leads (anchor
    matching jumps to it); the full list is preserved for the network."""
    cleaned = re.sub(r"[\[\]]", "", run)
    cleaned = re.sub(r"\s*(,|–|—|-|to|and|&)\s*", lambda m: {",": ", "}.get(m.group(1), f" {m.group(1)} ")
                     if m.group(1) in (",", "to", "and", "&") else m.group(1), cleaned).strip()
    return f"para {cleaned}"


def _attach_case_pinpoints(text: str, cites: list[Citation]) -> list[Citation]:
    """For case citations with no pinpoint, look just after the citation for a
    paragraph reference ("at [57]", "at paras 8, 44") and attach it — JADE-style
    pinpoint links into the cited judgment, multi-paragraph lists preserved."""
    out: list[Citation] = []
    for c in cites:
        if c.pinpoint or c.entity_kind not in ("case", "opinion"):
            out.append(c)
            continue
        m = _CASE_PINPOINT.match(text[c.char_end: c.char_end + 60])
        run = m and (m.group("a") or m.group("b") or m.group("c") or m.group("d") or m.group("e"))
        first = re.match(r"\[?(\d{1,4})", run or "")
        if run and first and not re.fullmatch(r"(?:19|20)\d{2}", first.group(1)):
            out.append(replace(c, pinpoint=(f"r.o. {run}" if m.group("e") else _pin_text(run))))
        else:
            out.append(c)
    return out


# --- in-document shorthand names (design feedback, Perreault v Canada) --------
# Canadian/UK drafting defines shorthands inline: "Suncor Energy Inc v … 2021 FC
# 138 at para 64 [Suncor]" or "(hereinafter “Dagg”)" — and later cites "Suncor at
# para 30". TWO criteria gate the link (both must hold, so "[Emphasis added]"
# never links): (1) a name defined in citation-adjacent position; (2) a later
# use of that name WITH a paragraph pincite. Each use mints a pinpointed
# citation of the defined case — free extra pincites for the network.
# A short-name DEFINITION beside a citation. Legal drafting introduces one in many
# shapes, and we accept them all (the user's ask): any bracket type — [], (), {} —
# holding a name, in single or double (straight or curly) quotes or bare, optionally
# behind a cue ("hereinafter", "hereafter", "henceforth", "the", "collectively", "or"):
#   [Suncor]  ("Digital Rights")  ('FMIOA')  (hereinafter "the Charter")
#   (the "Vienna Convention")  ("Dagg")  [the Act]
# A BARE (unquoted, no cue) name is only trusted in SQUARE brackets — the OSCOLA
# convention — because a round "(…)" is far more often a year/court-tag/aside; a
# quoted or cued name is trusted in any bracket.
_SHORTHAND_DEF = re.compile(
    # quoted or cued name, any bracket
    r"[\[({]\s*(?:(?:herein)?after\s+|hereafter\s+|henceforth\s+|collectively\s+|or\s+)?"
    r"(?:the\s+)?[\"“']\s*(?P<q>[A-Za-z][\w'’&.\- ]{1,45}?)\s*[\"”']\s*[\])}]"
    r"|[\[({]\s*(?:(?:herein)?after|hereafter|henceforth)\s+(?:the\s+)?"
    r"(?P<cue>[A-Z][\w'’&.\- ]{1,45}?)\s*[\])}]"
    # bare name, square brackets only (OSCOLA short-title convention)
    r"|\[\s*(?:the\s+)?(?P<br>[A-Z][A-Za-z'’&.\- ]{1,40}?)\s*\]"
    # legacy "hereinafter Name" with no brackets
    r"|(?:hereina?fter|hereafter|henceforth)\s+[\"“']?(?P<hf>[A-Z][A-Za-z'’&.\- ]{1,40})[\"”']?"
)


# The CJEU/AG-opinion idiom: a case is introduced in full, then relabelled with a
# short "judgment in <Name>" tag beside the citation, and every later reference is
# "Judgment in <Name>, paragraph N" — never the bracket/hereinafter form above.
# "The judgment of 8 April 2014, Digital Rights Ireland and Others, Cases C-293/12
# and C-594/12, judgment in Digital Rights, EU:C:2014:238 … Judgment in Digital
# Rights, paragraph 57." Without this the later short references dangle, losing the
# pincites the opinion actually turns on. The label can sit either side of the
# citation, so both windows are searched.
_CJEU_LABEL = re.compile(r"judgment\s+in\s+(?P<name>[A-Z][A-Za-z0-9'’&.\- ]{2,40}?)\s*(?=[,.]|$)",
                         re.IGNORECASE)

# A case NAME immediately before a citation — "Dunsmuir v. New Brunswick, " ahead
# of "2008 SCC 9". Party names are short runs of Capitalised words; "v"/"v." is the
# join. Anchored to the end so it's the name that actually introduces the citation.
_CASE_NAME_BEFORE = re.compile(
    # Party names routinely contain lower-case connective words, accents and a
    # parenthesised public-body qualifier: "Mouvement laïque québécois v. Saguenay
    # (City)". Bound each side at citation-list punctuation rather than pretending
    # every token is capitalised.
    r"(?P<p1>[A-ZÀ-ÖØ-Þ][^,;\n]{1,100}?)\s+v\.?\s+"
    r"(?P<p2>[A-ZÀ-ÖØ-Þ][^,;\n]{1,100}?)\s*,?\s*$")
_STATUTE_NAME_BEFORE = re.compile(
    r"(?P<name>[A-Z][A-Za-zÀ-ÿ'’()&.\- ]{2,100}?\s+(?:Act|Regulations?))\s*,?\s*$")
# Parties too generic to be a distinctive short form: a bare "Canada, at para 5"
# or "R, at para 2" must never mint a link. Government/Crown/office parties only —
# a real surname (Dunsmuir, Khosa, Vavilov) always survives.
_GENERIC_PARTY = {
    "r", "the queen", "the king", "regina", "rex", "canada", "quebec", "ontario",
    "the crown", "crown", "her majesty", "his majesty", "the state", "state",
    "united states", "united kingdom", "the united states", "commonwealth",
    "the commonwealth", "director of public prosecutions", "dpp", "attorney general",
    "the attorney general", "minister", "the minister", "secretary of state",
    "the secretary of state", "commissioner", "the commissioner", "government",
}
_STOP_WORDS = {"and", "others", "ors", "anor", "another", "et", "al", "no", "inc",
               "ltd", "llc", "plc", "co", "corp", "the", "of", "for"}
# Words that introduce a citation but aren't part of the case name; the plaintiff
# capture reaches back over them ("See Dunsmuir v …"), so strip them off the front.
_LEADING_SIGNAL = {"see", "in", "cf", "cf.", "also", "accord", "compare", "citing",
                   "following", "applying", "per", "and", "but", "e.g", "e.g.",
                   "i.e", "i.e.", "namely", "viz", "eg", "ie", "from", "at", "as",
                   "held", "decision", "judgment", "the"}


def _party_short_form(party: str | None) -> str | None:
    """The distinctive short form of a party name — "Dunsmuir v. New Brunswick" is
    referred to as "Dunsmuir" — or None for a generic government/Crown party (whose
    surname would mislink a bare later mention)."""
    p = " ".join((party or "").split()).strip(" ,.")
    p = re.sub(r"\s*\([^)]*\)\s*$", "", p).strip()
    if not p or p.lower() in _GENERIC_PARTY:
        return None
    words = [w for w in re.split(r"\s+", p) if w]
    # drop leading citation-signal words the lookback swept in ("See", "In", "held")
    while words and words[0].lower().strip(".") in _LEADING_SIGNAL:
        words.pop(0)
    # a corporate/first-named party: take its leading distinctive word(s), dropping
    # trailing corporate/list tails ("Suncor Energy Inc" → "Suncor")
    lead: list[str] = []
    for w in words:
        if w.lower().strip(".") in _STOP_WORDS and lead:
            break
        lead.append(w)
        if len(lead) >= 2:
            break
    short = " ".join(lead).strip(" ,.")
    if len(short) < 3 or short.lower() in _GENERIC_PARTY:
        return None
    # must contain a real alphabetic surname, not just initials/numbers
    return short if re.search(r"[A-Za-z]{3,}", short) else None


_STATUTE_KINDS = ("act", "regulation", "directive", "treaty", "eu_instrument")


def _is_abbrev(name: str) -> bool:
    """A distinctive short label safe to link on a BARE later mention (no pincite
    needed) — an initialism like FMIOA/GDPR, or a compact CamelCase tag. A single
    ordinary-case word ("Suncor") is NOT, since it could be a common noun; those
    only link with a pincite."""
    core = name.replace(".", "").replace(" ", "")
    if len(core) < 2:
        return False
    letters = [ch for ch in core if ch.isalpha()]
    return bool(letters) and sum(ch.isupper() for ch in letters) >= max(2, len(letters) - 1)


def _collect_shorthand_defs(text: str, kept: list[Citation]) -> dict[str, tuple[Citation, bool]]:
    """The shorthand DEFINITIONS this document establishes: name → (host citation,
    is_abbrev). Abbreviations (FMIOA) link on a bare later mention; case short-names
    (Dunsmuir) link only with a pincite. Split out of ``_attach_shorthands`` so the
    stage can harvest the same definitions into the corpus-wide store."""
    defs: dict[str, tuple[Citation, bool]] = {}

    def _register(name: str, host: Citation, *, abbrev: bool) -> None:
        name = (name or "").strip(" '\"“”’")
        if len(name) >= 2 and name not in defs:
            defs[name] = (host, abbrev)

    for c in kept:
        if not c.candidate_id:
            continue
        is_statute = (c.entity_kind or "") in _STATUTE_KINDS
        is_case = (c.entity_kind or "") in ("case", "opinion")
        # Bracketed short-name / abbreviation right after ANY citation — a case
        # ("Suncor"), a statute ("FMIOA"), a treaty ("Vienna Convention"). This is
        # the "(short)" that legal drafting drops after a full first/second mention.
        window = text[c.char_end: c.char_end + 90]
        m = _SHORTHAND_DEF.search(window)
        if m and not re.search(r"[A-Za-z]{12,}", window[:m.start()]):
            name = (m.group("q") or m.group("cue") or m.group("br")
                    or m.group("hf") or "").strip(" '\"“”’")
            if len(name) >= 3:
                _register(name, c, abbrev=is_statute or _is_abbrev(name))
        # Formal chapter citations are commonly introduced by the short title:
        # "Citizenship Act, R.S.C. 1985, c. C-29". Learn that title for later
        # "s. 3(2)(a) of the Citizenship Act" uses in the same judgment.
        if is_statute:
            nm = _STATUTE_NAME_BEFORE.search(text[max(0, c.char_start - 140):c.char_start])
            if nm:
                _register(nm.group("name"), c, abbrev=True)
        if not is_case:
            continue
        # CJEU "judgment in <Name>" label, immediately either side of the citation
        # Joined-case introductions are longer (``Cases C-203/15 and C-698/15,
        # the judgment in Tele2 Sverige and Watson, EU:C:…``); 60 characters cut
        # the label in half immediately before the ECLI, so its later pincites
        # remained unlinked.  This is still a deliberately tight local window.
        for side in (text[max(0, c.char_start - 140): c.char_start],
                     text[c.char_end: c.char_end + 100]):
            lm = _CJEU_LABEL.search(side)
            if lm and not re.match(r"\s*,?\s*(?:paragraph|para)", side[lm.end():]):
                _register(lm.group("name"), c, abbrev=False)
        # Party-name short forms — the common-law idiom with NO explicit marker
        # ("Dunsmuir v. New Brunswick, 2008 SCC 9" … "Dunsmuir, at para. 61").
        nm2 = _CASE_NAME_BEFORE.search(text[max(0, c.char_start - 220): c.char_start])
        if nm2:
            for party in (nm2.group("p1"), nm2.group("p2")):
                short = _party_short_form(party)
                if short:
                    _register(short, c, abbrev=False)
    return defs


def _link_shorthand_uses(
    text: str, name: str, *, entity_kind: str | None, candidate_id: str | None,
    abbrev: bool, out: list[Citation], occupied: list[tuple[int, int]],
    after: int = -1, method: str = "shorthand", confidence: float = 0.7,
) -> None:
    """Append a citation for every later USE of ``name`` in ``text``, skipping spans an
    existing citation already covers. ``after`` is the definition's position — only uses
    beyond it count — and is -1 for a *stored* shorthand, which has no definition here."""
    esc = re.escape(name)
    # case / opinion short-name uses always carry a pincite ("Suncor at para 30",
    # "Judgment in Digital Rights, paragraph 57"); an abbreviation links on a
    # bare mention too ("the FMIOA", "under FMIOA", "s. 3 of the FMIOA").
    pat = (rf"\b{esc}(?:,)?\s+at\s+paras?\.?\s*(?P<run>\[?\d{{1,4}}\]?{_PIN_CONT})"
           rf"|judgment\s+in\s+{esc}\s*,?\s*(?:paragraphs?|paras?\.?)\s*"
           rf"(?P<run2>\[?\d{{1,4}}\]?{_PIN_CONT})")
    if abbrev:
        # a bare mention, optionally preceded by "the" — but not when it's being
        # (re)defined in brackets, which the def pass already owns
        pat += (rf"|(?<![\[(\"“'])(?:(?P<prov>(?:ss?\.?\s*\d+[A-Za-z]?(?:\s*\([^)]*\))*"
                rf"|Sched(?:ule)?\.?\s*[IVXLC\d]+))\s+(?:of|to)\s+(?:the\s+)?)?"
                rf"\b{esc}\b(?![\"”'\])])")
    use_re = re.compile(pat, re.IGNORECASE if not abbrev else 0)
    for m in use_re.finditer(text):
        s, e = m.start(), m.end()
        if s <= after:   # only USES after the definition count
            continue
        if any(os < e and s < oe for os, oe in occupied):
            continue
        run = m.groupdict().get("run") or m.groupdict().get("run2")
        prov = m.groupdict().get("prov")
        provision_pin = None
        if prov:
            provision_pin = (re.sub(r"(?i)^Sched(?:ule)?\.?\s*", "Sch. ", prov)
                             if re.match(r"(?i)^Sched", prov)
                             else re.sub(r"(?i)^ss?\.?\s*", "s. ", prov))
            provision_pin = re.sub(r"\s*(\([^)]*\))\s*", r"\1", provision_pin)
        out.append(Citation(
            raw=m.group(0), entity_kind=entity_kind, candidate_id=candidate_id,
            pinpoint=_pin_text(run) if run else provision_pin,
            char_start=s, char_end=e, method=method, confidence=confidence,
        ))
        occupied.append((s, e))


def _attach_shorthands(text: str, kept: list[Citation],
                       defs: dict[str, tuple[Citation, bool]] | None = None) -> list[Citation]:
    if defs is None:
        defs = _collect_shorthand_defs(text, kept)
    if not defs:
        return kept
    out = list(kept)
    occupied = [(c.char_start, c.char_end) for c in kept]
    for name in sorted(defs, key=len, reverse=True):
        host, abbrev = defs[name]
        _link_shorthand_uses(
            text, name, entity_kind=host.entity_kind, candidate_id=host.candidate_id,
            abbrev=abbrev, out=out, occupied=occupied, after=host.char_start)
    return out


def _def_rows(defs: dict[str, tuple[Citation, bool]]) -> list[dict]:
    """Definitions as plain dicts — the harvest the stage promotes into the corpus-wide
    ``learned_shorthands`` store. Only definitions naming a resolvable candidate are
    kept; an unresolved host would store a link to nothing."""
    return [
        {"shorthand": name, "candidate_id": host.candidate_id,
         "entity_kind": host.entity_kind, "is_abbrev": abbrev}
        for name, (host, abbrev) in defs.items() if host.candidate_id
    ]


def shorthand_defs(text: str, cites: list[Citation]) -> list[dict]:
    """The shorthand definitions ``text`` establishes, computed from scratch.

    ``extract_citations`` already collects these internally, so the extraction path
    takes them via its ``defs_out`` parameter instead of paying for a second pass —
    on a 700k-document rescan that duplicate harvest measured ~4% of the whole job.
    This standalone form is for callers holding citations from somewhere else."""
    return _def_rows(_collect_shorthand_defs(text, cites))


# Initialisms too common across the corpus to trust on a bare mention even when their
# parent IS cited: a document citing the Federal Courts Act still uses "CA" for "Court
# of Appeal" a dozen times. These fall back to the case rule — link only with a pincite
# — rather than being dropped, since a pincited "CA, at para 5" is genuinely a reference.
_COMMON_INITIALISMS = {"ca", "sc", "hc", "cj", "dpp", "ec", "eu", "uk", "us", "ecj",
                       "cjeu", "echr", "hl", "fc", "qb", "kb", "sca", "cca"}


def attach_stored_shorthands(
    text: str, kept: list[Citation], stored: list[tuple[str, str, str | None, bool]],
    *, exclude: frozenset[str] | set[str] = frozenset(),
) -> list[Citation]:
    """Apply shorthands LEARNED IN OTHER DOCUMENTS — "[Suncor]" defined in one judgment
    linking "Suncor, at para 30" in the next.

    The caller (``citations.stage``) supplies only shorthands whose parent candidate this
    document already cites by some other means; that parent-cited gate is the whole point
    of the feature, because a corpus-wide "FCA" would otherwise link in every unrelated
    judgment that happens to use the letters. ``exclude`` holds the names the document
    defines for ITSELF — an in-document definition always wins over a stored one.

    ``stored`` rows are ``(shorthand, candidate_id, entity_kind, is_abbrev)``. A case
    short-name still requires a pincite; only a statute-hosted initialism links bare, and
    even then not if it is a common legal initialism (CA/SC/DPP…) or ≤2 characters."""
    if not stored:
        return kept
    out = list(kept)
    occupied = [(c.char_start, c.char_end) for c in kept]
    # Presence pre-filter, which is not an optimisation but the thing that makes the
    # feature affordable at all. A heavily-cited case accumulates a short name from
    # every document that ever defined one, and EVERY document citing it would
    # otherwise pay a compiled full-text regex scan per variant — measured at +93% on
    # the rescan hot path before this filter existed.
    #
    # A plain per-name substring test (~1.7µs each), deliberately NOT one combined
    # alternation regex over all the names: the applicable name set is different for
    # every document, so an alternation misses Python's pattern cache and pays a fresh
    # compile of a 100-branch regex per document — measured 12x worse than this loop.
    # The test only asks "does this string occur at all"; the pattern below does the
    # boundary and pincite work.
    lowered = text.lower()
    # longest first, so "Digital Rights Ireland" claims its span before "Digital Rights"
    for name, candidate_id, entity_kind, abbrev in sorted(
            stored, key=lambda r: len(r[0]), reverse=True):
        if not name or not candidate_id or name in exclude:
            continue
        if name.lower() not in lowered:
            continue
        core = name.replace(".", "").replace(" ", "")
        if abbrev and (len(core) <= 2 or core.lower() in _COMMON_INITIALISMS):
            abbrev = False   # demand a pincite rather than trusting a bare mention
        _link_shorthand_uses(
            text, name, entity_kind=entity_kind, candidate_id=candidate_id,
            abbrev=abbrev, out=out, occupied=occupied,
            method="shorthand_global", confidence=0.6)
    return out


# A list of articles all governed by one instrument — "Articles 7, 8 and 11 and
# Article 52(1) of the Charter", "Articles 4 and 6 of the Charter", "Articles 107
# and 108 TFEU". The single-instrument grammar captures only the article ADJACENT to
# the instrument name and drops the rest of the list, so most of the articles a
# passage turns on went unlinked. This pass finds the instrument that closes such a
# list (an instrument/treaty/regulation citation the grammars already resolved, whose
# span begins at the tail of the list) and mints one pinpointed edge per article to
# it. A single article number is left to the grammar.
_ARTICLE_IN_LIST = re.compile(
    r"(?:Art(?:icle|\.)?s?\.?\s+)?(?P<n>\d{1,3}[a-z]?(?:\(\d+[a-z]?\))*)")
# the whole list construct: two or more article numbers joined by commas / "and"
# (allowing a repeated "and Article 52(1)"), ending just before the instrument
_ARTICLE_LIST = re.compile(
    r"\bArt(?:icle|\.)?s?\.?\s+"
    r"(?P<list>\d{1,3}[a-z]?(?:\(\d+[a-z]?\))*"
    r"(?:\s*(?:,|and|&|to|through|–|—|-)\s*(?:Art(?:icle|\.)?s?\.?\s+)?\d{1,3}[a-z]?(?:\(\d+[a-z]?\))*)+)"
    r"\s+(?:of\s+)?(?:the\s+)?",
    re.IGNORECASE)


def _attach_article_lists(text: str, kept: list[Citation]) -> list[Citation]:
    """Split "Articles 7, 8 and 11 … of the Charter" into one edge per article. The
    single-instrument grammar only links the article adjacent to the name; this links
    the rest. The instrument that closes the list is resolved by name here, so it
    works even when NO article reached the grammar ("Articles 4 and 6 of the
    Charter")."""
    from .grammars import instrument_at

    out = list(kept)
    occupied = [(c.char_start, c.char_end) for c in kept]
    for m in _ARTICLE_LIST.finditer(text):
        cand, kind = instrument_at(text[m.end(): m.end() + 120])
        # EU drafting frequently uses "Articles 12 to 15 of that Directive" after
        # naming the directive in the preceding sentence. Resolve the demonstrative
        # to the nearest earlier directive rather than leaving the whole range blank.
        if not cand and re.match(r"that\s+Directive\b", text[m.end():], re.IGNORECASE):
            prior = [c for c in kept if c.char_end <= m.start() and c.candidate_id
                     and c.entity_kind == "directive"]
            if prior:
                cand, kind = prior[-1].candidate_id, "directive"
        if not cand:
            continue
        article_matches = list(_ARTICLE_IN_LIST.finditer(m.group("list")))
        expanded: list[tuple[str, int, int]] = []
        for i, am in enumerate(article_matches):
            expanded.append((am.group("n"), am.start("n"), am.end("n")))
            if i + 1 < len(article_matches) and am.group("n").isdigit():
                gap = m.group("list")[am.end():article_matches[i + 1].start()]
                nxt = article_matches[i + 1].group("n")
                if re.search(r"(?i)\b(?:to|through)\b|[–—-]", gap) and nxt.isdigit() \
                        and 0 < int(nxt) - int(am.group("n")) <= 20:
                    expanded.extend((str(n), am.start("n"), article_matches[i + 1].end("n"))
                                    for n in range(int(am.group("n")) + 1, int(nxt)))
        for num, ns, ne in expanded:
            s = m.start("list") + ns
            e = m.start("list") + ne
            # skip any article the grammar already linked (avoid a duplicate edge)
            if any(os < e and s < oe for os, oe in occupied):
                continue
            out.append(Citation(
                raw=text[s:e], entity_kind=kind or "regulation",
                candidate_id=cand, pinpoint=f"Article {num}",
                char_start=s, char_end=e, method="article_list", confidence=0.75,
            ))
    return out


# A list of sections all governed by one statute — "ss. 27 and 28", "sections 20 to
# 23", "ss 3, 4 and 5 of the Act". The single-section grammar captures only the first
# ("ss. 27" → s. 27) and drops the rest, so "ss 27 and 28" lost s. 28 entirely. The
# statute can sit either side of the list: after it ("ss 27 and 28 of the Act") or
# before it ("R.S.C. 1985, c. F-7, ss. 27 and 28").
_SECTION_LIST = re.compile(
    r"\b(?:ss?\.?|sections?)\s+"
    r"(?P<list>\d{1,4}[A-Z]?(?:\(\d+[A-Za-z]?\))*"
    r"(?:\s*(?:,|and|&|to|through|–|—|-)\s*(?:ss?\.?\s+|sections?\s+)?"
    r"\d{1,4}[A-Z]?(?:\(\d+[A-Za-z]?\))*)+)",
    re.IGNORECASE)
_SECTION_NUM = re.compile(r"\d{1,4}[A-Z]?(?:\(\d+[A-Za-z]?\))*")
_STATUTE_ISH = ("act", "regulation")


def _expand_section_list(list_text: str) -> list[tuple[str, int, int]]:
    """Section tokens in a list, as (label, start, end) offsets into ``list_text``.
    A small "N to M" range is expanded to its members (endpoints carry the offsets;
    interior members get the range's span) so every section in the range links."""
    toks = list(_SECTION_NUM.finditer(list_text))
    out: list[tuple[str, int, int]] = []
    for i, tm in enumerate(toks):
        out.append((tm.group(0), tm.start(), tm.end()))
        # a bare numeric "N to M" range between two simple integers → fill it in
        joiner = list_text[tm.end(): toks[i + 1].start()] if i + 1 < len(toks) else ""
        if re.search(r"(?i)\b(?:to|through)\b|[–—-]", joiner) and tm.group(0).isdigit():
            nxt = toks[i + 1].group(0)
            if nxt.isdigit() and 0 < int(nxt) - int(tm.group(0)) <= 20:
                for k in range(int(tm.group(0)) + 1, int(nxt)):
                    out.append((str(k), tm.start(), toks[i + 1].end()))
    return out


def _attach_section_lists(text: str, kept: list[Citation]) -> list[Citation]:
    """Split a section list into one pinpoint edge per section, borrowing the statute
    identity from an act citation adjacent to the list (before or after)."""
    acts_by_start = {c.char_start: c for c in kept
                     if c.candidate_id and (c.entity_kind or "") in _STATUTE_ISH}
    acts_by_end = {c.char_end: c for c in kept
                   if c.candidate_id and (c.entity_kind or "") in _STATUTE_ISH}
    out = list(kept)
    occupied = [(c.char_start, c.char_end) for c in kept]
    for m in _SECTION_LIST.finditer(text):
        # "…ss 27 and 28 of the Act" — a statute begins just after the list, across a
        # short "of the <Name>," connective (no sentence break); "…c. F-7, ss. 27 and
        # 28" — a statute ends just before it.
        host = None
        for p in range(m.end(), min(len(text), m.end() + 48)):
            if p in acts_by_start:
                gap = text[m.end():p]
                if ". " not in gap and re.fullmatch(r"[\s,]*(?:of\s+)?(?:the\s+)?[\w' .,()\-]*", gap):
                    host = acts_by_start[p]
                break
        if host is None:
            host = next((acts_by_end[p] for p in range(m.start(), max(-1, m.start() - 6), -1)
                         if p in acts_by_end), None)
        if host is None:
            continue
        for lbl, ls, le in _expand_section_list(m.group("list")):
            s = m.start("list") + ls
            e = m.start("list") + le
            if any(os < e and s < oe for os, oe in occupied):
                continue  # the grammar already linked this one (usually the first)
            out.append(Citation(
                raw=text[s:e], entity_kind=host.entity_kind,
                candidate_id=host.candidate_id, pinpoint=f"s. {lbl}",
                char_start=s, char_end=e, method="section_list", confidence=0.75,
            ))
            occupied.append((s, e))
    return out


# A *bare* provision reference with no statute named alongside it — "section 5",
# "Article 6", "regulation 3", "paragraph 12 of Schedule 1". On its own it doesn't
# say which instrument; the carry-forward pass attaches it to the last-named one.
_BARE_PROVISION = re.compile(
    r"\b(?P<cue>section|sections|sub-?section|s|ss|article|articles|art|arts|"
    r"recital|recitals|"
    r"regulation|regulations|reg|regs|paragraph|paragraphs|para|paras|schedule|sch)\.?\s*"
    r"(?P<num>\d+[A-Z]?(?:\s*\(\s*[A-Z0-9]+\s*\))*)(?=\W|$)",
    re.IGNORECASE,
)
# carry-forward only attaches a bare provision to a *legislation* antecedent — a
# bare "section 5" never means a paragraph of a cited case.
_LEG_KINDS = {"act", "regulation", "directive", "decision", "treaty", "eu_instrument", "named"}

# EU instruments are divided into *Articles*, UK Acts/SIs into *sections* and *schedules*.
# So the cue word disambiguates the antecedent: a bare "section 66" can't belong to an EU
# directive, and a bare "Article 6" can't belong to a UK Act. This stops a "section N" from
# carrying forward onto a nearer-but-wrong EU instrument (e.g. Directive 2003/4 in an
# Environmental-Information case where the Communications Act is the real host).
_EU_KINDS = {"directive", "decision", "treaty", "eu_instrument"}


def _cue_allows(cue: str, kind: str) -> bool:
    """Whether a bare-provision ``cue`` ("section", "Article", …) can attach to an
    antecedent of this ``entity_kind``."""
    c = cue.lower().rstrip(".")
    if c.startswith(("section", "sub", "ss", "schedule", "sch")) or c == "s":
        return kind not in _EU_KINDS          # UK statutory provision → not an EU instrument
    if c.startswith(("article", "art")):
        return kind in _EU_KINDS              # Article → EU instrument / treaty, not a UK Act
    if c.startswith("recital"):
        # Recitals belong to EU instruments (regulations included — the GDPR is one) and
        # never to a UK Act, which has no recitals.
        return kind in _EU_KINDS or kind in {"regulation", "named"}
    return True                                # regulation / paragraph — leave to nearest


def _bare_pinpoint(cue: str, num: str) -> str:
    num = re.sub(r"\s+", "", num)
    c = cue.lower().rstrip(".")
    if c.startswith("recital"):
        return f"Recital {num}"
    if c.startswith(("article", "art")):
        return f"Article {num}"
    if c.startswith(("regulation", "reg")):
        return f"reg. {num}"
    if c.startswith(("paragraph", "para")):
        return f"para {num}"
    if c.startswith(("schedule", "sch")):
        return f"Sch. {num}"
    return f"s. {num}"


def _attach_carry_forward(text: str, kept: list[Citation]) -> list[Citation]:
    """Heuristic (§5): a bare "section 5" / "Article 6" with no statute named in the
    same breath is taken to refer to the **most recently mentioned legislation**, even
    several paragraphs earlier. Emits a low-confidence ``carry_forward`` citation so
    the resulting edge is flagged uncertain (provenance ``inferred``) for human review.
    Skips any bare reference already inside a fuller, literal citation."""
    occupied = sorted((c.char_start, c.char_end) for c in kept)
    # every citation in document order — used to find what a bare reference FOLLOWS
    all_sorted = sorted(kept, key=lambda c: c.char_start)
    # legislation antecedents in document order, with their candidate + kind
    antecedents = sorted(
        (c for c in kept if c.candidate_id and c.entity_kind in _LEG_KINDS),
        key=lambda c: c.char_start,
    )
    if not antecedents:
        return kept
    out = list(kept)
    for m in _BARE_PROVISION.finditer(text):
        s, e = m.start(), m.end()
        if any(os <= s and e <= oe for os, oe in occupied):
            continue  # already part of a literal citation ("s.5 of the FOIA 2000")
        cue = m.group("cue").lower().rstrip(".")
        # A "paragraph N" whose nearest preceding citation is a CASE is that
        # judgment's pinpoint, not a provision of whatever instrument was last
        # named — the CJEU's own citation form ends every case reference with
        # ", C-597/19, EU:C:2021:492, paragraph 107". Attaching those to the
        # last-named directive minted a phantom legislation edge per case cite
        # (the 2026-07 C-604/22 bug). Paragraph cues defer to a nearby case.
        if cue.startswith("para"):
            prev = [c for c in all_sorted if c.char_end <= s and s - c.char_end <= 80]
            if prev and prev[-1].entity_kind in ("case", "opinion"):
                continue
        prior = [a for a in antecedents if a.char_end <= s
                 and _cue_allows(m.group("cue"), a.entity_kind)]
        if not prior:
            continue
        host = prior[-1]  # nearest preceding named instrument of a compatible kind
        out.append(Citation(
            raw=m.group(0), entity_kind=host.entity_kind, candidate_id=host.candidate_id,
            pinpoint=_bare_pinpoint(m.group("cue"), m.group("num")),
            char_start=s, char_end=e, method="carry_forward", confidence=0.4,
        ))
    return out


class CitationExtractor(Protocol):
    def extract(self, text: str) -> list[Citation]:
        ...


def grammar_citations(text: str) -> list[Citation]:
    """The deterministic first pass: every registered grammar over the text."""
    found: list[Citation] = []
    for g in GRAMMARS.values():
        for m in g.pattern.finditer(text):
            candidate, pinpoint, kind_override = g.normalize(m)
            if kind_override is DROP:
                continue  # normaliser rejected it as non-citation noise (currency/ISBN/…)
            found.append(
                Citation(
                    raw=m.group(0).strip(),
                    entity_kind=kind_override or g.entity_kind,
                    candidate_id=candidate,
                    pinpoint=pinpoint,
                    char_start=m.start(),
                    char_end=m.end(),
                    method=g.name,
                )
            )
    return found


def alias_citations(text: str, aliases: dict[str, str]) -> list[Citation]:
    """Citations from user-defined shorthand *rules* ("UK GDPR" → a document id):
    every occurrence of a phrase becomes a link to its target, so the rule propagates
    across the corpus. Word-boundary, case-insensitive; longer phrases win overlaps."""
    found: list[Citation] = []
    for phrase, target in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        if not phrase or not target:
            continue
        # \b only guards against mid-word matches when the adjacent phrase character
        # is itself a word character. On a non-word edge — e.g. an alias like "(UK)
        # GDPR" — a bare \b demands a boundary that never exists there, so the phrase
        # silently never matches. Apply the boundary per edge only when it helps.
        lb = r"\b" if phrase[0].isalnum() or phrase[0] == "_" else ""
        rb = r"\b" if phrase[-1].isalnum() or phrase[-1] == "_" else ""
        for m in re.finditer(rf"{lb}{re.escape(phrase)}{rb}", text, re.IGNORECASE):
            found.append(Citation(raw=m.group(0), entity_kind="named", candidate_id=target,
                                  pinpoint=None, char_start=m.start(), char_end=m.end(),
                                  method="named_alias"))
    return found


def extract_citations(text: str, *, llm: CitationExtractor | None = None,
                      aliases: dict[str, str] | None = None,
                      defs_out: list[dict] | None = None) -> list[Citation]:
    """Recognise citations in ``text``. Grammars run first (deterministic, cheap),
    then user-defined shorthand rules (``aliases``), then an optional ``llm`` pass for
    narrative citations. More specific / earlier matches win an overlap.

    ``defs_out``, if given, is filled with the in-document shorthand definitions found
    along the way (see ``shorthand_defs``). It's an out-parameter rather than a wider
    return type because this function has many callers, none of which want it."""
    if not text:
        return []
    # User shorthand rules take precedence over the built-in grammars on an overlap: a
    # person who defines "UK GDPR" → X means it, over any generic grammar. They lead the
    # list so the stable longest-match dedupe keeps them on a span tie.
    cites = alias_citations(text, aliases) if aliases else []
    cites += grammar_citations(text)
    # German references are normalised before linking and may expand to several exact
    # targets (ranges, i.V.m., repeated Nr./Abs. clauses), which the one-match/one-edge
    # grammar interface cannot represent.
    from .german import german_citations
    cites += german_citations(text)
    from .dutch import dutch_citations
    cites += dutch_citations(text)
    # US reporter citations (self-contained matcher), gated to text that looks American — recognises
    # "135 S. Ct. 2401" so it clusters as a case instead of being misread as statutory
    # material. Added before the dedupe so a genuine overlap resolves by span.
    from .us_cases import us_case_citations
    cites += us_case_citations(text)
    grammar = _dedupe_overlaps(cites)
    if llm is None:
        base = _attach_case_pinpoints(text, grammar)
    else:
        extra = [c for c in llm.extract(text) if not _overlaps_any(c, grammar)]
        base = _attach_case_pinpoints(text, _dedupe_overlaps(grammar + extra))
    defs = _collect_shorthand_defs(text, base)
    if defs_out is not None:
        defs_out.extend(_def_rows(defs))
    return _attach_carry_forward(text, _attach_section_lists(text, _attach_article_lists(
        text, _attach_shorthands(text, base, defs))))


def _overlaps_any(c: Citation, kept: list[Citation]) -> bool:
    return any(c.char_start < k.char_end and k.char_start < c.char_end for k in kept)


def _dedupe_overlaps(cites: list[Citation]) -> list[Citation]:
    """Keep the longest match at each location; drop spans contained in a kept one
    (so the article-scoped citation wins over the bare instrument number)."""
    ordered = sorted(cites, key=lambda c: (c.char_start, -(c.char_end - c.char_start)))
    kept: list[Citation] = []
    occupied: list[tuple[int, int]] = []
    for c in ordered:
        exact_multi = c.method in ("de_law_reference", "nl_juriconnect") and any(
            k.char_start == c.char_start and k.char_end == c.char_end
            and k.method == c.method and k.pinpoint != c.pinpoint for k in kept)
        if not exact_multi and any(s <= c.char_start and c.char_end <= e for s, e in occupied):
            continue
        kept.append(c)
        occupied.append((c.char_start, c.char_end))
    return kept
