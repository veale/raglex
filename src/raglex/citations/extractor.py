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
from functools import lru_cache
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


def _disambiguate_online_safety_act(cites: list[Citation]) -> list[Citation]:
    """Use an explicit Australian title in the document to disambiguate later shorthand.

    ``the Online Safety Act`` is curated as the UK 2023 Act in UK guidance, while an
    Australian judgment naturally uses the same short form after first naming the Online
    Safety Act 2021 (Cth).  Once that explicit Australian citation is present, only
    year-less UK matches in the same document are redirected; an explicit 2023 citation
    remains UK law.
    """
    au_id = "au/cth/act/2021/76"
    uk_id = "ukpga/2023/50"
    if not any(c.candidate_id == au_id for c in cites):
        return cites
    return [
        replace(c, candidate_id=au_id)
        if c.candidate_id == uk_id
        and re.search(r"\bOnline\s+Safety\s+Act\b", c.raw, re.IGNORECASE)
        and not re.search(r"\b2023\b", c.raw)
        else c
        for c in cites
    ]


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
    # Both curly single quotes are listed. Without ‘…’ the Home Office's own house
    # style was invisible: the communications-data and bulk-acquisition IPA codes write
    # ("Part 3 … of the Investigatory Powers Act 2016 (‘the Act’)") and so defined
    # nothing, while their six sibling codes — identical but for the quote character —
    # defined "the Act" and resolved their provisions.
    r"[\[({]\s*(?:(?:herein)?after\s+|hereafter\s+|henceforth\s+|collectively\s+|or\s+)?"
    r"(?:the\s+)?[\"“‘']\s*(?P<q>[A-Za-z][\w'’&.\- ]{1,45}?)\s*[\"”’']\s*[\])}]"
    r"|[\[({]\s*(?:(?:herein)?after|hereafter|henceforth)\s+(?:the\s+)?"
    r"(?P<cue>[A-Z][\w'’&.\- ]{1,45}?)\s*[\])}]"
    # bare name, square brackets only (OSCOLA short-title convention)
    r"|\[\s*(?:the\s+)?(?P<br>[A-Z][A-Za-z'’&.\- ]{1,40}?)\s*\]"
    # legacy "hereinafter Name" with no brackets
    r"|(?:hereina?fter|hereafter|henceforth)\s+[\"“']?(?P<hf>[A-Z][A-Za-z'’&.\- ]{1,40})[\"”']?"
)

# Law Commission reports commonly put a short glossary definition at the start of
# its own line, in the reverse order from the bracket form above:
#
#   “the 1967 Act”: Leasehold Reform Act 1967.
#   LRA: Land Registration Act 2002.
#
# The line boundary, colon, ≤15-character label and immediately following resolvable
# statute are all required.  This is deliberately much narrower than treating any
# prose before a colon as an alias.
_COLON_STATUTE_SHORTHAND = re.compile(
    r"(?:^|\n)[ \t]*"
    r"(?:[\"“](?P<quoted>[^\"”:\n]{2,15})[\"”]"
    r"|(?P<bare>(?:(?i:the\s+)?(?:18|19|20)\d{2}\s+Act|"
    r"[A-Za-z][A-Za-z0-9.\- ]{1,14})))"
    r"[ \t]*:[ \t]*$"
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
    #
    # A COLON or a spaced dash ends the lookback, because Australian and Canadian
    # reports are field-labelled documents and the label sits immediately before the
    # party name:
    #
    #   "Medium Neutral Citation:  Ratewave Pty Limited v BJ Illingby [2017] NSWCA 103"
    #   "Library Sheet - R. v. Paul"
    #   "Cases cited: Foo v Bar"
    #
    # Without that boundary the first party reads as "Medium Neutral Citation:
    # Ratewave Pty Limited", whose short form is "Medium Neutral" — and the corpus-wide
    # shorthand store then carried that phrase into 18,692 documents. "Cases cited:",
    # "Library Sheet" and "Citation: R" arrived by the identical route. No case name
    # has ever contained a colon, so this costs nothing.
    r"(?P<p1>[A-ZÀ-ÖØ-Þ](?:(?!\s[-–—]\s)[^,;:\n]){1,100}?)\s+v\.?\s+"
    r"(?P<p2>[A-ZÀ-ÖØ-Þ](?:(?!\s[-–—]\s)[^,;:\n]){1,100}?)\s*,?\s*$")
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
    # The TRUNCATION half of the same problem (see _is_generic_party): the reduced
    # form of a specific office is a generic one. "Commissioners for Her Majesty's
    # Revenue and Customs" reduces to "Commissioners", "Supreme Court of Canada" to
    # "Supreme Court" — names that stand for a hundred different parties apiece.
    "secretary", "the secretary", "secretaries", "commissioners",
    "the commissioners", "supreme court", "the supreme court", "high court",
    "the high court", "federal court", "the federal court", "court of appeal",
    "chief constable", "the chief constable", "home office", "the home office",
    "home department", "the home department", "home secretary", "the home secretary",
}
_STOP_WORDS = {"and", "others", "ors", "anor", "another", "et", "al", "no", "inc",
               "ltd", "llc", "plc", "co", "corp", "the", "of", "for"}
# Words that introduce a citation but aren't part of the case name; the plaintiff
# capture reaches back over them ("See Dunsmuir v …"), so strip them off the front.
_LEADING_SIGNAL = {"see", "in", "cf", "cf.", "also", "accord", "compare", "citing",
                   "following", "applying", "per", "and", "but", "e.g", "e.g.",
                   "i.e", "i.e.", "namely", "viz", "eg", "ie", "from", "at", "as",
                   "held", "decision", "judgment", "the"}


def _is_generic_party(name: str | None) -> bool:
    """Whether ``name`` IS, or merely EXTENDS, a party too generic to name an authority.

    The prefix half is the fix for an ordering bug: ``_party_short_form`` tested the FULL
    party string against ``_GENERIC_PARTY`` and only then truncated it to its leading
    words, so the truncation manufactured exactly the generic party the set exists to
    stop —

        "Secretary of State"                          -> blocked (exact match)
        "Secretary of State for the Home Department"  -> not blocked -> 'Secretary'

    Only MULTI-WORD generic prefixes count. A one-word prefix would take real names down
    with it ("State Street Bank", "Crown Holdings"), and a one-word party is already
    caught by the whole-string test."""
    words = [w for w in re.split(r"\s+", (name or "").casefold().strip(" ,.")) if w]
    if not words:
        return False
    if " ".join(words) in _GENERIC_PARTY:
        return True
    return any(" ".join(words[:k]) in _GENERIC_PARTY
               for k in range(2, len(words)))


def _party_short_form(party: str | None) -> str | None:
    """The distinctive short form of a party name — "Dunsmuir v. New Brunswick" is
    referred to as "Dunsmuir" — or None for a generic government/Crown party (whose
    surname would mislink a bare later mention)."""
    p = " ".join((party or "").split()).strip(" ,.")
    p = re.sub(r"\s*\([^)]*\)\s*$", "", p).strip()
    if not p or _is_generic_party(p):
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
    if len(short) < 3 or _is_generic_party(short):
        return None
    # must contain a real alphabetic surname, not just initials/numbers
    return short if re.search(r"[A-Za-z]{3,}", short) else None


_STATUTE_KINDS = ("act", "regulation", "directive", "treaty", "eu_instrument")

# --- the host a document defines for ITSELF ------------------------------------
# A statutory code of practice names its parent Act once and then calls it "the Act"
# for the rest of the document: "…Part 2 (interception) of the Investigatory Powers
# Act 2016 ("the Act")", then 184 further "section 18 of the Act". Those are the
# document's most important references — a code of practice IS guidance ON those
# provisions — and every one of them was dropped, because "the Act" is (rightly)
# refused as a corpus-wide shorthand by ``_GENERIC_SHORTHAND`` and because
# ``_EXPLICIT_HOST_RE`` stops carry-forward wherever the text names its own host.
#
# Both refusals are correct GLOBALLY and wrong LOCALLY. "the Act" is document-
# relative — corpus-wide it would misattribute every "the Act" in 108,390 held UK
# instruments — but inside the document that defined it, it is as explicit as the
# full title. So these bind here and travel nowhere: they are never harvested into
# ``learned_shorthands`` (see ``_def_rows``, which only ever sees ``defs``), and they
# only link a mention that carries a PROVISION ("s. 18 of the Act"), never a bare one,
# so the edge always says which provision it is about.
_HOST_NOUNS = {
    "act", "code", "regulation", "regulations", "order", "rules", "directive",
    "convention", "charter", "treaty", "agreement", "protocol", "scheme", "statute",
}
# "the Act", "the 2016 Act", "the Code", "the 1998 Regulations" — an instrument noun,
# optionally qualified by the year that distinguishes it from its neighbours.
_HOST_NAME_RE = re.compile(
    r"(?i)^(?:the\s+)?(?:(?:18|19|20)\d{2}\s+)?([A-Za-z]+)$")


def _is_host_noun(name: str) -> bool:
    """Whether ``name`` is an instrument noun a document may bind to itself — as
    opposed to a role noun ("the appellant"), which never names an authority."""
    m = _HOST_NAME_RE.match((name or "").strip())
    return bool(m) and m.group(1).casefold() in _HOST_NOUNS


# UNQUOTED house style: "the Equality Act 2010 (the Act) consolidates…".
# ``_SHORTHAND_DEF`` requires quotes inside a round bracket — deliberately, because a
# bare round-bracket name is far too loose to learn as a corpus-wide shorthand — and
# accepts a bare name only in square brackets. So the commonest binding form in UK
# regulator drafting defined nothing at all: the EHRC's employment code writes
# "(the Act)" and its 312 later "section N of the Act" references carried forward onto
# whatever statute a passing sentence last named (233 landed on the Employment Rights
# Act 1996, 147 on the Civil Partnership Act 2004). This pattern is matched ONLY
# against the instrument-noun vocabulary and only feeds ``hosts_out``, so the
# looseness stays out: the binding is document-scoped and never reaches the store.
_BARE_HOST_DEF = re.compile(
    r"\s*[\[(]\s*(?P<name>(?:the\s+)?(?:(?:18|19|20)\d{2}\s+)?[A-Za-z]+)\s*[\])]")

# --- what may become a shorthand ---------------------------------------------
# A shorthand links every later bare mention of a name to one authority, and the
# corpus-wide store then carries that name into OTHER documents. So a name that is
# not distinctive doesn't mislink once — it mislinks everywhere. The store had
# accumulated 382,885 entries including "Article 8", "appellant", "the Court",
# "Analysis" and "Act", all pointing at the ECHR with ``is_abbrev`` set, which is
# why a Scotland Act 1998 section had "may make" and "the grounds" rendered as
# links to the Convention. These guards are the gate on both the in-document pass
# and the store.
#
# 1. A PROVISION REFERENCE is never a name. "Article 8" as a shorthand for the
#    Convention makes every "Article 8" in every document that cites the ECHR into
#    a Convention link, including "Article 8 of the Charter".
_PROVISION_NAME_RE = re.compile(
    r"(?i)^(?:art(?:icle|\.)?|s(?:ec)?(?:tion|\.)?|para(?:graph|\.)?|reg(?:ulation|\.)?|"
    r"recital|sch(?:edule|\.)?|rule|order|part|chapter|annex|appendix|clause)\b")
# 2. GENERIC ROLE AND INSTRUMENT NOUNS name a slot, not an authority. Every
#    judgment has "the appellant"; every statute book has "the Act".
_GENERIC_SHORTHAND = {
    "act", "acts", "code", "codes", "regulation", "regulations", "directive",
    "decision", "convention", "charter", "treaty", "agreement", "protocol",
    "guidance", "guidelines", "rules", "order", "orders", "scheme", "statute",
    "bill", "report", "application", "analysis", "judgment", "decision letter",
    "appellant", "appellants", "respondent", "respondents", "applicant",
    "applicants", "claimant", "claimants", "defendant", "defendants", "plaintiff",
    "plaintiffs", "petitioner", "interested party", "intervener", "appeal",
    "court", "the court", "tribunal", "judge", "the judge", "panel", "board",
    "committee", "commission", "council", "parliament", "authority", "regulator",
    "company", "the company", "bank", "the bank", "trust", "the trust",
    "ground", "grounds", "issue", "issues", "evidence", "witness", "parties",
    "party", "the parties", "person in question", "the person in question",
    "information", "the information", "policy", "the policy", "the state",
    # The same role nouns in the corpus's other languages. The list was English-only,
    # so the Dutch party labels printed in a rechtspraak.nl header — VERZOEKSTER,
    # VERZOEKER, EISER, Klaagster — were learned as names for the statute the judgment
    # cites and passed every popularity threshold, because every judgment prints them.
    "verzoeker", "verzoekster", "verzoekers", "eiser", "eiseres", "eisers",
    "gedaagde", "gedaagden", "klager", "klaagster", "verweerder", "verweerster",
    "appellant", "appellante", "belanghebbende", "betrokkene", "de staat",
    "de minister", "de rechtbank", "het hof", "de inspecteur", "de raad",
    "kläger", "klägerin", "beklagte", "beklagter", "antragsteller",
    "antragstellerin", "antragsgegner", "antragsgegnerin", "beschwerdeführer",
    "beschwerdeführerin", "das gericht", "die kammer",
    "requérant", "requérante", "demandeur", "demanderesse", "défendeur",
    "défenderesse", "intimé", "intimée", "appelant", "appelante", "le tribunal",
    "la cour", "le ministre", "recurrente", "demandante", "demandado",
    "ricorrente", "resistente",
}

# Abbreviations for actors, requests, privileges and numbered issues are ordinary
# litigation vocabulary, not names of legal authorities.  A noisy inline definition
# once taught these to the corpus-wide store (``HMRC -> FOIA``, ``SAR -> DPA 1998``,
# ``LPP -> FOIA``); rejecting them on both write and read retires the historical rows.
_NON_AUTHORITY_SHORTHAND = {
    "hmrc", "sar", "lpp", "information rights", "secretary of state", "human rights",
}
_NUMBERED_ISSUE = re.compile(r"(?i)^(?:the\s+)?issues?\s+\d+[a-z]?(?:\([a-z0-9]+\))?$")
# A JUDGE is not an authority. "Nicklin J" was learned as a name for the judgment he
# wrote, so any later "Nicklin J" — in a document citing that judgment for any reason —
# rendered as a link to it. The judicial suffixes are unambiguous and appear nowhere in
# an instrument's or a party's name.
_JUDICIAL_TITLE = re.compile(
    r"(?i)^(?:the\s+)?[A-ZÀ-Þ][\w'’\-]+(?:\s+[A-ZÀ-Þ][\w'’\-]+)?\s+"
    r"(?:JJ?A?|LJJ?|CJ|MR|P|PSC|JSC|JA|B|VC)\.?$")
# 3. A well-formed name doesn't START or END on a function word. "Code in",
#    "Code by", "Code. As", "ets of our " and "may make" all came from a lookback
#    that swept up sentence fragments; requiring both ends to be substantive
#    discards them without touching "Vienna Convention" or "Suncor Energy".
_FRAGMENT_EDGE = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "by", "to",
    "for", "from", "with", "as", "is", "was", "are", "were", "be", "been", "may",
    "must", "shall", "can", "could", "would", "should", "will", "that", "this",
    "these", "those", "it", "its", "his", "her", "their", "our", "which", "who",
    "when", "where", "if", "so", "such", "any", "all", "no", "not", "make",
    "made", "given", "said", "same", "other", "under", "over", "into", "than",
}


def valid_shorthand(name: str | None) -> bool:
    """Whether ``name`` is distinctive enough to stand for an authority.

    Applied both when a document DEFINES a shorthand and when a stored one is read
    back, so tightening it retires bad entries already in the store without waiting
    for a purge."""
    # Checked BEFORE the edge-strip below, which would otherwise remove the very
    # character that identifies the problem: a colon separates a FIELD LABEL from its
    # value ("Cases cited:", "Citation: R", "Catchwords:"). No authority's name contains
    # one, so a candidate carrying a colon is part of a report's header that the party
    # lookback ran through, not a name. This is the read-side half of the boundary fix
    # in _CASE_NAME_BEFORE: it retires entries already in the store without a purge.
    if ":" in (name or ""):
        return False
    n = " ".join((name or "").strip(" '\"“”’.,;:()[]").split())
    if len(n) < 3 or len(n) > 60:
        return False
    if not re.search(r"[A-Za-z]{3}", n):        # initials/numbers only
        return False
    if re.search(r"[.!?]\s", n) or "\n" in n:   # a sentence fragment, not a name
        return False
    if _PROVISION_NAME_RE.match(n):
        return False
    if _JUDICIAL_TITLE.match(n):
        return False
    low = n.casefold()
    if low.removeprefix("the ") in _NON_AUTHORITY_SHORTHAND or _NUMBERED_ISSUE.match(n):
        return False
    if low in _GENERIC_SHORTHAND or low.removeprefix("the ") in _GENERIC_SHORTHAND:
        return False
    words = [w.strip(".,'’\"").casefold() for w in n.split()]
    words = [w for w in words if w]
    if not words:
        return False
    # "the Vienna Convention" is fine; "the Court" was caught above as generic.
    if words[0] in _FRAGMENT_EDGE and len(words) < 2:
        return False
    if words[-1] in _FRAGMENT_EDGE:
        return False
    if len(words) > 1 and words[0] in _FRAGMENT_EDGE and words[1] in _FRAGMENT_EDGE:
        return False
    # a name has to carry at least one capitalised or all-caps token of its own
    return any(w[:1].isupper() or w.isupper() for w in n.split() if w)


# --- who does a bracketed short name actually define? -------------------------
# ``_SHORTHAND_DEF`` looks for a bracketed name in the 90 characters AFTER a citation,
# and attributes it to that citation. In O'Connor v Bar Standards Board (uksc/2017/78):
#
#   "...claims damages under the Human Rights Act 1998 against the respondent, the Bar
#    Standards Board ("the BSB"), alleging discrimination..."
#
# the bracket sits 55 characters after the Act, no intervening word reaches the 12-letter
# guard, and "the BSB" was filed as a shorthand for the Human Rights Act — which the
# corpus-wide store then applied 54 times in one document. The answer was in the gap:
# "the Bar Standards Board" immediately precedes the bracket and B-S-B are its initials.
# The rule attributed the definition to the nearest CITATION while ignoring the nearest
# ANTECEDENT. "the Government" (respondent State, beside a Convention citation), "The
# Commissioner" (Information Commissioner, beside FOIA) and "the Home" (Home Office,
# beside the Mental Health Act) all arrived by this same route.
_CAP_WORD = re.compile(r"[A-Z][A-Za-z0-9'’\-]+")
# lower-case words that continue a body's name rather than ending it
_NAME_CONNECTIVE = {"of", "for", "and", "the", "de", "der", "van", "und", "et"}
# What tells an APPOSITIVE phrase (part of the citation's own reference — "Case
# C-131/12 Google Spain SL v AEPD") from a NEW ANTECEDENT ("…against the respondent,
# the Bar Standards Board"): citation apparatus carries no prose words and no sentence
# break, so either standing between the citation and the phrase means a new subject.
_SEPARATES = re.compile(r"[.;]|(?<![A-Za-z])[a-z]{3,}")
_INITIAL_SKIP = {"of", "for", "and", "the", "a", "an", "in", "on", "to", "de", "der",
                 "van", "und", "et", "la", "le"}


def _capitalised_phrases(gap: str) -> list[tuple[str, int]]:
    """Every run of two or more capitalised words in ``gap``, with its offset — the
    NAMED BODIES ("the Bar Standards Board", "Digital Rights Ireland") standing between a
    citation and a bracketed short name. Lower-case connectives ("of", "and") continue a
    run; any other lower-case word ends it."""
    out: list[tuple[str, int]] = []
    run: list[str] = []
    start = 0
    for m in re.finditer(r"\S+", gap):
        word = m.group(0).strip("()[]{}\"“”'’,;:.")
        if _CAP_WORD.fullmatch(word):
            if not run:
                start = m.start()
            run.append(word)
            continue
        if run and word.casefold() in _NAME_CONNECTIVE:
            continue                     # "Secretary of State" is still one name
        if len(run) >= 2:
            out.append((" ".join(run), start))
        run = []
    if len(run) >= 2:
        out.append((" ".join(run), start))
    return out


def _initials(phrase: str) -> set[str]:
    """The initialisms a phrase could be abbreviated to — with and without its function
    words, since drafting produces both ("FOIA" from Freedom of Information Act, "HRA"
    from Human Rights Act 1998)."""
    words = [w.strip("()[]{}\"“”'’,;:.") for w in phrase.split()]
    words = [w for w in words if w and w[:1].isalpha()]
    full = "".join(w[0] for w in words).upper()
    lean = "".join(w[0] for w in words if w.casefold() not in _INITIAL_SKIP).upper()
    return {i for i in (full, lean) if len(i) >= 2}


def _derives_from(name: str, phrase: str) -> bool:
    """Whether ``name`` is plainly derived from ``phrase`` — its initials ("BSB" ← Bar
    Standards Board) or a leading part of it ("Digital Rights" ← Digital Rights
    Ireland). This is what settles WHICH of two candidates a short name defines."""
    n = re.sub(r"(?i)^the\s+", "", (name or "").strip()).strip()
    if not n or not phrase:
        return False
    core = re.sub(r"[^A-Za-z0-9]", "", n).upper()
    if " " not in n and core in _initials(phrase):
        return True
    words = [w for w in (w.casefold().strip(".,'’") for w in n.split()) if len(w) >= 3]
    haystack = [w.casefold().strip("()[]{}\"“”'’,;:.") for w in phrase.split()]
    return bool(words) and all(w in haystack for w in words)


def _antecedent_owns_definition(gap: str, name: str, cited_raw: str) -> bool:
    """Whether the bracketed ``name`` defines a NAMED BODY standing in ``gap`` rather
    than the citation the gap follows — in which case there is no id to file it under
    ("the Bar Standards Board" is not an authority) and the definition is dropped.

    Kept deliberately narrow, because the ordinary form has an EMPTY gap and must be
    untouched (``Human Rights Act 1998 ("HRA")``), and an appositive name is part of the
    citation itself (``Case C-131/12 Google Spain SL v AEPD ("Google Spain")``). Two
    things decide it:

    * DERIVABILITY — a name derived from the citation's own words ("HRA" ← Human Rights
      Act, "FOIA" ← Freedom of Information Act) belongs to the citation whatever else
      stands in the gap. Where the definition is dropped, derivability usually confirms
      the other reading ("BSB" ← Bar Standards Board).
    * SEPARATION — see ``_SEPARATES``. Prose or a sentence break before the phrase means
      a new subject has been introduced and the bracket is its abbreviation.
    """
    phrases = _capitalised_phrases(gap)
    if not phrases:
        return False
    if _derives_from(name, cited_raw):
        return False                     # the short name is the authority's own
    return any(_SEPARATES.search(gap[:at]) for _phrase, at in phrases)


# --- how many documents must agree before a shorthand goes corpus-wide --------
# The store's application gate — the citing document must already cite the parent — is
# correct and not enough: for a ubiquitous authority nearly every document satisfies it,
# so it stops filtering. 92,353 documents cite the Convention, and every one of them
# uses the word "Government". What is coincidental there is not the target but the
# NAME: nobody in the citing document ever defined it.
#
# So a shorthand travels only once several documents have independently established it.
# Measured over the 177,068 distinct (shorthand → target) pairs the corpus had ever
# established in-document:
#
#     >=1   177,068   100%       >=5    6,828   3.9%
#     >=3    15,444   8.7%       >=10   2,523   1.4%
#
# and against the known-bad set, >=3 kills "the BSB" -> Human Rights Act (1 document),
# "the UK" -> GDPR (1), "The Commissioner" -> FOIA (1), "the Home" -> Mental Health Act
# (1) while keeping "the CPIA" -> Criminal Procedure and Investigations Act (8). It does
# NOT kill "the Government" -> Convention (128 documents, the same misfire repeated by
# one source) — that is what the antecedent rule above is for. Below the threshold a
# definition still applies in the document that made it; it just doesn't travel.
SHORTHAND_MIN_DOCS = 3


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


def _collect_shorthand_defs(
    text: str, kept: list[Citation],
    hosts_out: dict[str, tuple[Citation, bool]] | None = None,
) -> dict[str, tuple[Citation, bool]]:
    """The shorthand DEFINITIONS this document establishes: name → (host citation,
    is_abbrev). Abbreviations (FMIOA) link on a bare later mention; case short-names
    (Dunsmuir) link only with a pincite. Split out of ``_attach_shorthands`` so the
    stage can harvest the same definitions into the corpus-wide store.

    ``hosts_out``, if given, collects the DOCUMENT-SCOPED host bindings separately —
    "the Act" / "the 2016 Act" / "the Code" bound to a statute this document names in
    full (see ``_HOST_NOUNS``). They are kept apart from ``defs`` precisely because
    they must not reach the corpus-wide store."""
    defs: dict[str, tuple[Citation, bool]] = {}

    def _register(name: str, host: Citation, *, abbrev: bool) -> None:
        name = (name or "").strip(" '\"“”’")
        if valid_shorthand(name) and name not in defs:
            defs[name] = (host, abbrev)
            return
        # Refused as a shorthand — but an instrument noun bound to a statute the
        # document has just named IS a definition, valid inside this document.
        if (hosts_out is not None and name not in hosts_out
                and host.candidate_id and _is_host_noun(name)
                and (host.entity_kind or "") in _STATUTE_KINDS):
            hosts_out[name] = (host, True)

    for c in kept:
        if not c.candidate_id:
            continue
        is_statute = (c.entity_kind or "") in _STATUTE_KINDS
        is_case = (c.entity_kind or "") in ("case", "opinion")
        if is_statute:
            # Reverse glossary form: short label at line start, full Act after the
            # colon. The host must already resolve, so a colon can never invent a
            # destination merely from a year.
            before = text[max(0, c.char_start - 80):c.char_start]
            cm = _COLON_STATUTE_SHORTHAND.search(before)
            if cm:
                _register(
                    cm.group("quoted") or cm.group("bare") or "",
                    c,
                    abbrev=True,
                )
            # Some official reports put an SI acronym between its title and printed
            # series number: ``... Regulations 2008 (BPRs) (SI 2008/1276)``.
            # The SI grammar needs the series number to resolve the instrument and
            # therefore consumes the intervening acronym as part of the same span.
            if c.method == "uk_si_named":
                internal = re.search(
                    r"\(([A-Za-z][A-Za-z0-9.]{1,14})\)", c.raw
                )
                if internal:
                    _register(internal.group(1), c, abbrev=True)
        # Bracketed short-name / abbreviation right after ANY citation — a case
        # ("Suncor"), a statute ("FMIOA"), a treaty ("Vienna Convention"). This is
        # the "(short)" that legal drafting drops after a full first/second mention.
        window = text[c.char_end: c.char_end + 90]
        m = _SHORTHAND_DEF.search(window)
        if m and not re.search(r"[A-Za-z]{12,}", window[:m.start()]):
            name = (m.group("q") or m.group("cue") or m.group("br")
                    or m.group("hf") or "").strip(" '\"“”’")
            # …but only if the citation is what the bracket names. A body standing
            # between the two owns its own abbreviation (see
            # _antecedent_owns_definition), and there is no id to file that under.
            if len(name) >= 3 and not _antecedent_owns_definition(
                    window[:m.start()], name, c.raw):
                _register(name, c, abbrev=is_statute or _is_abbrev(name))
        # Formal chapter citations are commonly introduced by the short title:
        # "Citizenship Act, R.S.C. 1985, c. C-29". Learn that title for later
        # "s. 3(2)(a) of the Citizenship Act" uses in the same judgment.
        if is_statute:
            nm = _STATUTE_NAME_BEFORE.search(text[max(0, c.char_start - 140):c.char_start])
            if nm:
                _register(nm.group("name"), c, abbrev=True)
            # "(the Act)" with no quotes — see _BARE_HOST_DEF. Registered straight into
            # the document-scoped hosts rather than through _register, because it is
            # only ever an instrument noun and must never be offered to the store.
            hm = _BARE_HOST_DEF.match(text[c.char_end: c.char_end + 40])
            if hm and hosts_out is not None:
                name = hm.group("name").strip()
                if _is_host_noun(name) and name not in hosts_out:
                    hosts_out[name] = (c, True)
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


@lru_cache(maxsize=8192)
def _shorthand_use_re(name: str, abbrev: bool, bare: bool = True) -> "re.Pattern[str]":
    """The compiled use-pattern for one shorthand, memoised across documents.

    The same few hundred shorthands recur on every document of a bulk run, but the
    pattern is rebuilt as a fresh string each call, so ``re``'s own 512-entry cache
    thrashes and pays a full parse+compile per use — measured at ~17% of the parent's
    serial half on a heavily-cited judgment, which is the ceiling on the whole
    extraction pool. Keyed on exactly what the pattern depends on: the name, whether it
    links bare, and whether a bare mention is allowed at all.

    ``bare=False`` is the STORED (corpus-wide) case: a pinpoint is then required even
    from an abbreviation. A bare mention there adds nothing — the store only applies at
    all where the document already cites the parent, so the document already links to
    that authority and the extra edge is a duplicate carrying no pincite. It is also
    where the damage was: "the Government", "The Commissioner", "the UK" each fired
    dozens of times against an authority the document did cite, on a word that merely
    occurred."""
    esc = re.escape(name)
    # case / opinion short-name uses always carry a pincite ("Suncor at para 30",
    # "Judgment in Digital Rights, paragraph 57"); an abbreviation links on a
    # bare mention too ("the FMIOA", "under FMIOA", "s. 3 of the FMIOA").
    pat = (rf"\b{esc}(?:,)?\s+at\s+paras?\.?\s*(?P<run>\[?\d{{1,4}}\]?{_PIN_CONT})"
           rf"|judgment\s+in\s+{esc}\s*,?\s*(?:paragraphs?|paras?\.?)\s*"
           rf"(?P<run2>\[?\d{{1,4}}\]?{_PIN_CONT})")
    if abbrev:
        # The compact drafting form puts the shorthand first and its pinpoint
        # afterwards: ``GPSR, reg 5`` / ``BPRs, regs 3, 13(1) and 13(4)``.
        # Keep a multi-provision run intact as one auditable pinpoint string.
        post_list = (
            r"\d+[A-Za-z]?(?:\s*\([^)]*\))*"
            r"(?:\s*(?:,|and|to|&|[-–—])\s*"
            r"\d+[A-Za-z]?(?:\s*\([^)]*\))*)*"
        )
        pat += (
            rf"|(?<![\[(\"“'])\b{esc}\b\s*,?\s*"
            rf"(?P<postprov>(?:(?i:sections?|ss?\.?|regulations?|regs?\.?)"
            rf"\s*{post_list}|(?i:Sched(?:ule)?\.?)\s*[IVXLC\d]+))"
        )
        # a provision OF the named instrument ("s. 3 of the FMIOA") and — unless the
        # caller demands a pinpoint — a bare mention, optionally preceded by "the",
        # but not when it's being (re)defined in brackets, which the def pass owns
        prov_group = (rf"(?P<prov>(?:(?i:sections?|ss?\.?|"
                      rf"regulations?|regs?\.?)\s*"
                      rf"\d+[A-Za-z]?(?:\s*\([^)]*\))*"
                      rf"|Sched(?:ule)?\.?\s*[IVXLC\d]+))\s+(?:of|to)\s+(?:the\s+)?")
        optional = "?" if bare else ""
        pat += (rf"|(?<![\[(\"“'])(?:{prov_group}){optional}"
                rf"\b{esc}\b(?![\"”'\])])")
    year_act = bool(re.fullmatch(r"the\s+(?:18|19|20)\d{2}\s+Act", name, re.I))
    return re.compile(pat, re.IGNORECASE if (not abbrev or year_act) else 0)


def _provision_pinpoint(prov: str | None) -> str | None:
    """A matched provision phrase ("Section 18", "regs 3 and 13(1)", "Schedule 3")
    normalised to the anchor form the corpus stores ("s. 18", "reg. 3 and 13(1)",
    "Sch. 3"). Shared by the shorthand and alias passes so both spell an anchor the
    same way — an anchor that disagrees by a space or a word never matches."""
    if not prov:
        return None
    if re.match(r"(?i)^Sched", prov):
        pin = re.sub(r"(?i)^Sched(?:ule)?\.?\s*", "Sch. ", prov)
    elif re.match(r"(?i)^(?:regulations?|regs?\.?)", prov):
        pin = re.sub(r"(?i)^(?:regulations?|regs?\.?)\s*", "reg. ", prov)
    elif re.match(r"(?i)^(?:articles?|arts?\.?)", prov):
        pin = re.sub(r"(?i)^(?:articles?|arts?\.?)\s*", "Article ", prov)
    elif re.match(r"(?i)^(?:paragraphs?|paras?\.?)", prov):
        pin = re.sub(r"(?i)^(?:paragraphs?|paras?\.?)\s*", "para ", prov)
    else:
        pin = re.sub(r"(?i)^(?:sections?|ss?\.?)\s*", "s. ", prov)
    return re.sub(r"\s+(\([^)]*\))", r"\1", pin)


def _link_shorthand_uses(
    text: str, name: str, *, entity_kind: str | None, candidate_id: str | None,
    abbrev: bool, out: list[Citation], occupied: list[tuple[int, int]],
    after: int = -1, method: str = "shorthand", confidence: float = 0.7,
    bare: bool = True,
) -> None:
    """Append a citation for every later USE of ``name`` in ``text``, skipping spans an
    existing citation already covers. ``after`` is the definition's position — only uses
    beyond it count — and is -1 for a *stored* shorthand, which has no definition here.
    ``bare=False`` requires every use to carry a pinpoint (see ``_shorthand_use_re``)."""
    use_re = _shorthand_use_re(name, abbrev, bare)
    for m in use_re.finditer(text):
        s, e = m.start(), m.end()
        if s <= after:   # only USES after the definition count
            continue
        if any(os < e and s < oe for os, oe in occupied):
            continue
        run = m.groupdict().get("run") or m.groupdict().get("run2")
        prov = m.groupdict().get("prov") or m.groupdict().get("postprov")
        provision_pin = _provision_pinpoint(prov)
        out.append(Citation(
            raw=m.group(0), entity_kind=entity_kind, candidate_id=candidate_id,
            pinpoint=_pin_text(run) if run else provision_pin,
            char_start=s, char_end=e, method=method, confidence=confidence,
        ))
        occupied.append((s, e))


def shorthand_name_from_use(raw: str) -> str:
    """The NAME behind a recorded shorthand use — "Suncor, at para 30" → "Suncor".

    ``citations.raw`` stores the whole matched span, pincite and all, so the stored
    edges cannot be grouped by shorthand without undoing what ``_shorthand_use_re``
    added. That grouping is how ``doc_count`` is backfilled for the million rows the
    store accumulated before it counted anything (see
    ``Catalogue.backfill_learned_shorthand_doc_counts``)."""
    s = " ".join((raw or "").split())
    s = re.sub(r"(?i)^judgment\s+in\s+", "", s)
    s = re.sub(r"(?i)^(?:sections?|ss?\.?|regulations?|regs?\.?|sched(?:ule)?\.?)\s*"
               r"\S+?(?:\s*\([^)]*\))*\s+(?:of|to)\s+(?:the\s+)?", "", s)
    s = re.sub(r"(?i),?\s+at\s+paras?\.?\s*\[?\d.*$", "", s)
    s = re.sub(r"(?i),?\s*(?:paragraphs?|paras?\.?)\s*\[?\d.*$", "", s)
    s = re.sub(r"(?i),?\s*(?:sections?|ss?\.?|regulations?|regs?\.?)\s*\d.*$", "", s)
    s = re.sub(r"(?i),?\s*sched(?:ule)?\.?\s*[IVXLC\d].*$", "", s)
    return s.strip(" ,.")


def _attach_shorthands(text: str, kept: list[Citation],
                       defs: dict[str, tuple[Citation, bool]] | None = None,
                       hosts: dict[str, tuple[Citation, bool]] | None = None,
                       ) -> list[Citation]:
    if defs is None:
        hosts = {} if hosts is None else hosts
        defs = _collect_shorthand_defs(text, kept, hosts)
    if not defs and not hosts:
        return kept
    out = list(kept)
    occupied = [(c.char_start, c.char_end) for c in kept]
    for name in sorted(defs, key=len, reverse=True):
        host, abbrev = defs[name]
        _link_shorthand_uses(
            text, name, entity_kind=host.entity_kind, candidate_id=host.candidate_id,
            abbrev=abbrev, out=out, occupied=occupied, after=host.char_start)
    # Document-scoped hosts last, so a real shorthand always wins the span, and with
    # ``bare=False`` so only a mention carrying a provision links. A bare "the Act"
    # would add an edge the document already has from the full-title mention, and
    # carries nothing a reader could check.
    for name in sorted(hosts or {}, key=len, reverse=True):
        host, _abbrev = hosts[name]
        _link_shorthand_uses(
            text, name, entity_kind=host.entity_kind, candidate_id=host.candidate_id,
            abbrev=True, out=out, occupied=occupied, after=host.char_start,
            method="doc_host", confidence=0.75, bare=False)
    return out


def _def_rows(defs: dict[str, tuple[Citation, bool]]) -> list[dict]:
    """Definitions as plain dicts — the harvest the stage promotes into the corpus-wide
    ``learned_shorthands`` store. Only definitions naming a resolvable candidate are
    kept; an unresolved host would store a link to nothing."""
    rows: list[dict] = []
    for name, (host, abbrev) in defs.items():
        if not host.candidate_id:
            continue
        protected = _protected_shorthand_target(name)
        # Established statutory abbreviations belong to deterministic grammars, not the
        # learned store.  Even a target-correct learned ``AVG -> GDPR`` is unsafe in an
        # Ontario judgment where AVG is a company's name.
        if protected or is_statute_family_name(name):
            continue
        rows.append({
            "shorthand": name, "candidate_id": host.candidate_id,
            "entity_kind": host.entity_kind, "is_abbrev": abbrev,
        })
    return rows


def shorthand_defs(text: str, cites: list[Citation]) -> list[dict]:
    """The shorthand definitions ``text`` establishes, computed from scratch.

    ``extract_citations`` already collects these internally, so the extraction path
    takes them via its ``defs_out`` parameter instead of paying for a second pass —
    on a 700k-document rescan that duplicate harvest measured ~4% of the whole job.
    This standalone form is for callers holding citations from somewhere else."""
    return _def_rows(_collect_shorthand_defs(text, cites))


#: Which generic noun to believe when a document binds several. "the Act" is the one
#: that governs bare provision references; "the Code" usually names the guidance
#: document itself, not the statute it is about.
_HOST_NOUN_PRIORITY = ("act", "regulations", "regulation", "order", "rules",
                       "directive", "convention", "treaty", "charter")


def declared_instrument_host(text: str) -> tuple[str, str] | None:
    """The instrument a document binds its OWN generic noun to, or ``None``.

    "The Equality Act 2010 (the Act) consolidates…" → ``("ukpga/2010/15", "act")``.
    A statutory code names its parent Act once and then says "the Act" for the rest of
    the document; an adapter can use this to declare that Act as the document's
    ``citation_default_instrument``, so a bare "s.9(1)" in a margin note returns to the
    Act the document is ABOUT instead of carrying forward onto whatever statute a
    passing sentence last named.

    This reads what the document itself says rather than what its publisher usually
    means, which is the difference between a fix and a new class of error: pinning the
    Equality Act 2010 across every EHRC page moves its human-rights material's bare
    provisions off the Human Rights Act 1998 and onto the wrong statute, whereas the
    codes of practice — which do declare a host — are corrected.
    """
    if not text:
        return None
    hosts: dict[str, tuple[Citation, bool]] = {}
    _collect_shorthand_defs(text, grammar_citations(text), hosts)
    if not hosts:
        return None
    resolved = {}
    for name, (host, _abbrev) in hosts.items():
        m = _HOST_NAME_RE.match(name.strip())
        if m and host.candidate_id:
            resolved.setdefault(m.group(1).casefold(),
                                (host.candidate_id, host.entity_kind or "act"))
    for noun in _HOST_NOUN_PRIORITY:
        if noun in resolved:
            return resolved[noun]
    return None


# Initialisms too common across the corpus to trust on a bare mention even when their
# parent IS cited: a document citing the Federal Courts Act still uses "CA" for "Court
# of Appeal" a dozen times. These fall back to the case rule — link only with a pincite
# — rather than being dropped, since a pincited "CA, at para 5" is genuinely a reference.
_COMMON_INITIALISMS = {"ca", "sc", "hc", "cj", "dpp", "ec", "eu", "uk", "us", "ecj",
                       "cjeu", "echr", "hl", "fc", "qb", "kb", "sca", "cca"}

# Some legal abbreviations have a single, corpus-wide statutory meaning and must never
# be overwritten by a noisy inline definition learned from another document.  The most
# damaging observed example was ``BDSG -> GDPR``: every German judgment that mentioned
# both acts then acquired a false GDPR citation for each occurrence of BDSG.  This gate
# applies on both WRITE and READ, so deploying it immediately retires conflicting rows
# already in ``learned_shorthands`` and prevents a rescan from learning them again.
_PROTECTED_SHORTHAND_TARGETS = {
    "gdpr": "32016R0679",
    "dsgvo": "32016R0679",
    "avg": "32016R0679",
    "rgpd": "32016R0679",
    # A formal, unambiguous name in EU material.  It is resolved by the deterministic
    # EU grammar (including the Commission form "Article 50 AI Act"), and must never
    # be overwritten by a noisy corpus-learned definition.
    "aiact": "32024R1689",
    "bdsg": "de/gesetz/bdsg",
    "dsa": "32022R2065",
    "dma": "32022R1925",
    "nis2": "32022L2555",
}


def _protected_shorthand_target(name: str | None) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "", (name or "").casefold())
    return _PROTECTED_SHORTHAND_TARGETS.get(key)


# A statute short title MINUS its year is not a corpus-wide name — it is a name whose
# meaning is fixed by the year and the jurisdiction the citing document is writing in.
# "the Data Protection Act" is the 1984, 1998 or 2018 Act in the UK, the 1988 or 2018
# Act in Ireland, and neither anywhere else; "the Data Protection Acts" is Ireland's
# COLLECTIVE citation of all three of its own. The store had learned it four ways over
# — from a French CNIL page (→ the GDPR), a GRC tribunal (→ FOIA 2000), an English
# judgment (→ a case) — and then applied the GDPR reading to "section 117 of the Data
# Protection Act" in 19 Irish judgments, where section 117 is the Oireachtas Act's data
# protection action and the GDPR has no sections at all.
#
# Gated on WRITE and READ, so it retires the rows already in the store. It is
# deliberately NOT applied to the in-document pass: a judgment that writes "the Data
# Protection Act 1998 ('the Data Protection Act')" and then uses the short form for
# forty pages has defined it, for itself, unambiguously. What may not travel is the
# definition's escape into other documents.
#
# Only ACT and MEASURE, the two nouns the statute grammar owns and where the year is
# the identifier. "the Dublin III Regulation" and "the Rome I Regulation" also end in
# an instrument noun and carry no year, but they are nicknames for one instrument
# apiece and the store is right to carry them.
_YEARLESS_STATUTE_NOUN = re.compile(r"(?i)\b(?:acts?|measures?)$")


def is_statute_family_name(name: str | None) -> bool:
    """Is this a statute short title with the identifying YEAR left off?"""
    n = " ".join((name or "").strip(" '\"“”’.,;:()[]").split())
    if not n or re.search(r"\b(?:1[6-9]|20)\d{2}\b", n):
        return False
    return bool(_YEARLESS_STATUTE_NOUN.search(n))


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

    ``stored`` rows are ``(shorthand, candidate_id, entity_kind, is_abbrev)``. EVERY use
    must carry a pinpoint — a paragraph ("Suncor, at para 30") or a provision ("s. 3 of
    the FMIOA", "BPRs, reg 5"). A bare mention is not a weaker version of that, it is
    worth nothing: the store only applies where the document already cites the parent, so
    a bare hit adds a second, pincite-less edge to an authority already linked. That is
    all the "the Government" × 43 and "The Commissioner" × 117 misfires ever produced."""
    if not stored:
        return kept
    out = list(kept)
    # A stored, parent-gated shorthand is stronger evidence than the deliberately
    # low-confidence generic carry-forward pass. In ``section 2 of the DPA``, that
    # pass has already claimed ``section 2`` before the stored shorthand is applied;
    # allow the complete shorthand phrase to supersede it.
    occupied = [
        (c.char_start, c.char_end) for c in kept if c.method != "carry_forward"
    ]
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
        protected = _protected_shorthand_target(name)
        if protected or is_statute_family_name(name):
            continue
        # The store predates ``valid_shorthand`` and holds hundreds of thousands of
        # rows that would never be learned now. Gating on READ retires them the
        # moment this ships, without waiting for the purge and re-scan.
        if not valid_shorthand(name):
            continue
        if name.lower() not in lowered:
            continue
        core = name.replace(".", "").replace(" ", "")
        if abbrev and (len(core) <= 2 or core.lower() in _COMMON_INITIALISMS):
            abbrev = False   # a common initialism keeps only the paragraph-pincite form
        # A learned single ordinary word (``kann``, ``Geltendmachung``) is not an
        # abbreviation even if a noisy bracketed definition once labelled it as one.
        # Keep multi-word formal statute titles and genuine FMIOA/GDPR-style tokens.
        if abbrev and " " not in name.strip() and not _is_abbrev(name):
            abbrev = False
        _link_shorthand_uses(
            text, name, entity_kind=entity_kind, candidate_id=candidate_id,
            abbrev=abbrev, out=out, occupied=occupied, bare=False,
            method="shorthand_global", confidence=0.6)
    strong = [c for c in out if c.method == "shorthand_global"]
    return [
        c for c in out
        if not (
            c.method == "carry_forward"
            and any(
                g.candidate_id == c.candidate_id
                and g.char_start < c.char_end and c.char_start < g.char_end
                for g in strong
            )
        )
    ]


# A list of articles all governed by one instrument — "Articles 7, 8 and 11 and
# Article 52(1) of the Charter", "Articles 4 and 6 of the Charter", "Articles 107
# and 108 TFEU". The single-instrument grammar captures only the article ADJACENT to
# the instrument name and drops the rest of the list, so most of the articles a
# passage turns on went unlinked. This pass finds the instrument that closes such a
# list (an instrument/treaty/regulation citation the grammars already resolved, whose
# span begins at the tail of the list) and mints one pinpointed edge per article to
# it. A single article number is left to the grammar.
_ARTICLE_NUMBER = r"\d{1,3}[a-z]?(?:\([a-z0-9]+\))*"
_ARTICLE_IN_LIST = re.compile(
    r"(?:(?:Art(?:icle|\.)?s?\.?|artikelen?)\s+)?"
    rf"(?P<n>{_ARTICLE_NUMBER})", re.IGNORECASE)
# the whole list construct: two or more article numbers joined by commas / "and"
# (allowing a repeated "and Article 52(1)"), ending just before the instrument
_ARTICLE_LIST = re.compile(
    r"\b(?:Art(?:icle|\.)?s?\.?|artikelen?)\s+"
    rf"(?P<list>{_ARTICLE_NUMBER}"
    # "or" belongs here as much as "and": a pleading argues in the alternative
    # ("the exceptions in Article 4(1) or 4(2) thereof"), and without it the second
    # limb of every such pair went unlinked while the first was linked.
    r"(?:\s*(?:,\s*(?:(?:and|or|en|et|ou|e|ed|&)\s+)?|"
    r"(?:and|or|en|et|ou|oder|e|ed|&|to|through|à|–|—|-)\s+)"
    r"(?:(?:Art(?:icle|\.)?s?\.?|artikelen?)\s+)?"
    rf"{_ARTICLE_NUMBER})+)"
    r"\s+(?:(?:of|du|de\s+la|des|van\s+de)\s+)?(?:the\s+)?",
    re.IGNORECASE)
#: "…of that Regulation", "…thereof" — an instrument named in the preceding sentence
#: and referred back to. Only the demonstrative for a Directive was handled, so a list
#: closing on any other instrument type resolved to nothing.
_ARTICLE_LIST_ANAPHOR = re.compile(
    r"\s*(?:thereof\b|(?:that|the\s+said|the\s+same)\s+"
    r"(?P<kind>Regulation|Directive|Decision|Treaty|Convention|Charter)\b)",
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
        # EU drafting frequently refers back — "Articles 12 to 15 of that Directive",
        # "the exceptions in Article 4(1) or 4(2) thereof" — to the instrument named in
        # the preceding sentence. Resolve the anaphor to the nearest earlier instrument
        # of that type ("thereof": of any type) rather than leaving the list blank.
        anaphor = _ARTICLE_LIST_ANAPHOR.match(text[m.end():]) if not cand else None
        if anaphor:
            want = (anaphor.group("kind") or "").lower()
            prior = [c for c in kept if c.char_end <= m.start() and c.candidate_id
                     and (c.entity_kind == want if want
                          else _is_eu_candidate(c.candidate_id, c.entity_kind))]
            if prior:
                cand, kind = prior[-1].candidate_id, prior[-1].entity_kind
        # A judgment often introduces the instrument in a section heading before a run
        # of quoted provisions: ``The UK GDPR ... provisions`` then Articles 4–12,
        # including ``Articles 15 to 22 and 34``.  The list has no repeated instrument
        # tail, but the closest explicit instrument mention in that short section makes
        # its host clear.  3,000 chars covers a handful of judgment paragraphs, not an
        # unbounded document-wide carry-forward.
        if not cand:
            prior = [c for c in kept if c.char_end <= m.start() and c.candidate_id
                     and (c.entity_kind in {"regulation", "directive", "eu_instrument", "treaty"}
                          or _is_eu_candidate(c.candidate_id, c.entity_kind))
                     and m.start() - c.char_end <= 3000]
            if prior:
                cand, kind = prior[-1].candidate_id, prior[-1].entity_kind
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
    r"artikel|artikelen|articolo|articoli|"
    r"recital|recitals|"
    r"regulation|regulations|reg|regs|paragraph|paragraphs|para|paras|schedule|sch)\.?\s*"
    # Application/pleading paragraph labels can be hierarchical (13.1(a)); stopping
    # at the first integer linked the wrong, much broader pinpoint (feedback 142).
    r"(?P<num>\d+[A-Z]?(?:\.\d+)*(?:\s*\(\s*[A-Z0-9]+\s*\))*)(?!\s*:)(?=\W|$)",
    re.IGNORECASE,
)
# The two-level forms, which the single-cue pattern above cannot express and therefore
# lost: an ANNEX (numbered in roman, so ``num`` never matched it — a bare "Annex I" was
# invisible to carry-forward entirely) and the point/paragraph WITHIN an annex or
# schedule. Both orders occur and mean the same provision, so both fold to one anchor:
#
#   "Annex I, point 29" / "point 29 of Annex I"           → "Annex I, point 29"
#   "Schedule 1, paragraph 27" / "paragraph 27 of Sch. 1" → "Sch. 1, para 27"
#
# Wind Tre (C-54/17) turns entirely on Annex I, point 29 of the UCPD and says so
# fourteen times; every one of those was recorded as a bare reference to the directive,
# because "Annex" was not a cue and "point 29" had nowhere to attach.
_ANNEX_OR_SCHEDULE_NUM = r"[ivxlc]+(?![a-z])|\d{1,3}[A-Z]?"
# Horizontal space only, NEVER a line break. A CJEU judgment numbers its paragraphs at
# the start of a line, so "…by point 29 of that annex.\n\n42 Annex I, point 29 of
# Directive 2005/29…" offered the word "annex" and the paragraph number "42" to a
# newline-crossing gap and minted "Annex 42". Losing a genuine annex whose number was
# split across a line by PDF extraction is the cheaper error by far.
_H = r"[ \t\u00a0]{0,3}"
# "Annex" is NEVER abbreviated with a full stop, unlike "Sch." and "para.". So a dot
# after it always ends a sentence, and what follows is the next sentence's number rather
# than the annex's: "…shall be deemed to comply with paragraph 6.9 of this Annex. 8.6.
# The loss of function…" yielded "Annex 8", and "…point 8 of that annex. 24. The
# referring court considers…" yielded "Annex 24". Allowing the dot only for the schedule
# spellings removes that whole family of false positives at once.
_ANNEX_CUE = r"annexe?"
_SCHEDULE_CUE = r"(?:schedule|sched|sch)\.?"
_CUES = rf"(?:{_ANNEX_CUE}|{_SCHEDULE_CUE})"
_BARE_COMPOUND = re.compile(
    r"\b(?:"
    # reverse order first, so "point 29 of Annex I" is not read as a bare "Annex I"
    rf"(?:points?|paragraphs?|paras?)\.?{_H}\(?(?P<rsub>\d{{1,3}}[a-z]?)\)?{_H}"
    rf"(?:of|to|in){_H}\s(?:the{_H}\s)?(?P<rcue>{_CUES}){_H}"
    rf"(?P<rnum>{_ANNEX_OR_SCHEDULE_NUM})"
    r"|"
    rf"(?P<cue>{_CUES}){_H}(?P<num>{_ANNEX_OR_SCHEDULE_NUM})"
    rf"(?:{_H},?{_H}(?:points?|paragraphs?|paras?)\.?{_H}\(?(?P<sub>\d{{1,3}}[a-z]?)\)?)?"
    r")(?!\s*:)(?=\W|$)",
    re.IGNORECASE,
)
# "this Annex", "that annex", "the present Annex" — an instrument or judgment referring
# to ITS OWN annex. Carry-forward would attach it to whatever instrument was last NAMED,
# which is by definition a different one: not a missed edge but a wrong edge avoided.
_SELF_ANNEX_RE = re.compile(r"(?i)\b(?:this|that|the\s+present|the\s+said|said)\s+$")
# A reference that names its own host through "to"/"in" rather than the "of" that
# _EXPLICIT_HOST_RE already catches: "Annex 9 to the Convention on International Civil
# Aviation", "Annex 2 to the WTO Agreement". That host is not the last-named instrument.
_ANNEX_HOST_RE = re.compile(
    r"(?i)^\s*(?:to|in)\s+(?:the\s+)?\[?(?:[A-Z]|convention\b|agreement\b|treaty\b"
    r"|protocol\b|understanding\b|charter\b)")
# carry-forward only attaches a bare provision to a *legislation* antecedent — a
# bare "section 5" never means a paragraph of a cited case.
_LEG_KINDS = {"act", "regulation", "directive", "decision", "treaty", "eu_instrument", "named"}

# EU instruments are divided into *Articles*, UK Acts/SIs into *sections* and *schedules*.
# So the cue word disambiguates the antecedent: a bare "section 66" can't belong to an EU
# directive, and a bare "Article 6" can't belong to a UK Act. This stops a "section N" from
# carrying forward onto a nearer-but-wrong EU instrument (e.g. Directive 2003/4 in an
# Environmental-Information case where the Communications Act is the real host).
_EU_KINDS = {"directive", "decision", "treaty", "eu_instrument"}
# An EU *regulation* carries entity_kind "regulation", the same label a UK statutory
# instrument gets, so the set above can't separate them — but its candidate_id can: an
# EU instrument is keyed by CELEX (32016R0679), a UK SI by path (uksi/2023/1022).
_CELEX_RE = re.compile(r"^[0-9]{5}[A-Z]{1,2}[0-9]{4}$")


def _is_eu_candidate(candidate_id: str | None, kind: str) -> bool:
    return (kind in _EU_KINDS or bool(_CELEX_RE.match(candidate_id or ""))
            # Assimilated EU instruments use the legislation.gov.uk-derived stable
            # path but retain EU drafting structure (Articles/Recitals). Named aliases
            # such as UK GDPR therefore remain valid antecedents for bare Articles.
            or (candidate_id or "").startswith("european/regulation/"))


def _cue_allows(cue: str, kind: str, candidate_id: str | None = None) -> bool:
    """Whether a bare-provision ``cue`` ("section", "Article", …) can attach to an
    antecedent of this ``entity_kind``."""
    c = cue.lower().rstrip(".")
    eu = _is_eu_candidate(candidate_id, kind)
    if c.startswith(("section", "sub", "ss", "schedule", "sch")) or c == "s":
        # A UK statutory provision never belongs to an EU instrument. Regulations are
        # divided into Articles too, so the CELEX test matters here: without it a
        # "Section 1 of the Code" in a Commission opinion attached itself to the DSA.
        return not eu
    if c.startswith(("article", "art")):
        if c.startswith(("artikel", "articolo", "articoli")):
            return True  # Dutch/Italian statutes and EU instruments both use articles
        return eu                             # Article → EU instrument / treaty, not a UK Act
    if c.startswith("recital"):
        # Recitals belong to EU instruments (regulations included — the GDPR is one) and
        # never to a UK Act, which has no recitals.
        return eu or kind in {"regulation", "named"}
    return True                                # regulation / paragraph — leave to nearest


def _bare_pinpoint(cue: str, num: str) -> str:
    num = re.sub(r"\s+", "", num)
    c = cue.lower().rstrip(".")
    if c.startswith("recital"):
        return f"Recital {num}"
    if c.startswith(("article", "art")):
        if c.startswith("artikel"):
            return f"Artikel {num}"
        if c.startswith(("articolo", "articoli")):
            return f"Articolo {num}"
        return f"Article {num}"
    if c.startswith(("regulation", "reg")):
        return f"reg. {num}"
    if c.startswith(("paragraph", "para")):
        return f"para {num}"
    if c.startswith(("schedule", "sch")):
        return f"Sch. {num}"
    if c.startswith("annex"):
        # Roman numerals upper-cased: the instruments write "Annex I", and an anchor is
        # compared against the segment label the instrument itself carries.
        return f"Annex {num.upper() if num.isalpha() else num}"
    return f"s. {num}"


def _compound_pinpoint(cue: str, num: str, sub: str | None) -> str:
    """"Annex I" + point 29 → "Annex I, point 29"; "Schedule 1" + 27 → "Sch. 1, para 27"."""
    base = _bare_pinpoint(cue, re.sub(r"\s+", "", num))
    if not sub:
        return base
    unit = "point" if cue.lower().startswith("annex") else "para"
    return f"{base}, {unit} {sub}"


# A provision that names its OWN host is not bare, even when that host didn't resolve.
# "section 40A of the Road Traffic Act" and "Section 1 of the Code" both say exactly
# what they belong to; carrying them forward to the last-named instrument produced a
# confident-looking link to the wrong Act (the Automated and Electric Vehicles Act
# 2018 for the Road Traffic Act, the DSA for a Code of Practice). Where the text
# names a host the grammars couldn't resolve, the honest answer is no edge.
_EXPLICIT_HOST_RE = re.compile(
    r"\s*(?:,\s*)?of\s+(?:the\s+|that\s+|this\s+|those\s+|each\s+)?"
    r"(?:[A-Z]|Act\b|Regulations?\b|Code\b|Directive\b|Convention\b|Order\b|Rules\b|"
    r"Schedule\b|Agreement\b|Treaty\b|Charter\b|Protocol\b)")
# A URL is not prose. "…/science/article/pii/S2590198220300245" offered up "article"
# and "para 4"-shaped digits to the bare-provision scan, which then attached them to
# whatever Act the report was discussing.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# The end of the sentence a cross-reference was made in. Deliberately crude — a full
# stop or semicolon followed by space, or a blank line — because the only judgement it
# has to make is "are we still talking about the same instrument".
_SENTENCE_BREAK_RE = re.compile(r"[.;]\s|\n\s*\n")

# A formally named instrument inside a dependency clause is not the subject carried into
# the next provision.  The GDPR's title ends "repealing Directive 95/46/EC"; treating that
# Directive as the active antecedent made the operative ruling's "Article 22(1) of that
# regulation" point to the repealed Directive.  Keep the citation itself, but do not let it
# govern later bare/anaphoric provisions.
_DEPENDENCY_ANTECEDENT_RE = re.compile(
    r"(?i)\b(?:repealing|amending|replacing|recasting|superseding)\s+(?:the\s+)?(?:Council\s+)?$"
)
_ANAPHORIC_HOST_RE = re.compile(
    r"(?i)^\s+of\s+(?:that|this|the\s+said)\s+"
    r"(?P<kind>regulation|directive|decision|treaty|act)\b"
)


def _carry_antecedent(text: str, cite: Citation) -> bool:
    before = text[max(0, cite.char_start - 48):cite.char_start]
    return not _DEPENDENCY_ANTECEDENT_RE.search(before)


def _host_kind_matches(kind: str, candidate_id: str | None, named: str) -> bool:
    named = named.lower()
    if named == "regulation":
        return kind == "regulation" and _is_eu_candidate(candidate_id, kind)
    if named == "act":
        return kind == "act" or (kind == "regulation" and not _is_eu_candidate(candidate_id, kind))
    return kind == named


def _attach_carry_forward(text: str, kept: list[Citation], *,
                          home_id: str | None = None,
                          home_kind: str | None = None) -> list[Citation]:
    """Heuristic (§5): a bare "section 5" / "Article 6" with no statute named in the
    same breath is taken to refer to the **most recently mentioned legislation**, even
    several paragraphs earlier. Emits a low-confidence ``carry_forward`` citation so
    the resulting edge is flagged uncertain (provenance ``inferred``) for human review.
    Skips any bare reference already inside a fuller, literal citation.

    ``home_id`` is the document's OWN instrument, when the document being scanned is
    itself legislation. Inside the GDPR, "the conditions referred to in Article 8"
    means Article 8 *of the GDPR* — it is a self-reference, and the last instrument
    the text happened to cross-refer to (Directive 2009/22, say) is the wrong answer.
    So within legislation the home instrument wins unless another one is named close
    enough to be the obvious subject of the sentence."""
    occupied = sorted((c.char_start, c.char_end) for c in kept)
    urls = [(m.start(), m.end()) for m in _URL_RE.finditer(text)]
    # every citation in document order — used to find what a bare reference FOLLOWS
    all_sorted = sorted(kept, key=lambda c: c.char_start)
    # legislation antecedents in document order, with their candidate + kind
    antecedents = sorted(
        (c for c in kept if c.candidate_id and c.entity_kind in _LEG_KINDS
         and _carry_antecedent(text, c)),
        key=lambda c: c.char_start,
    )
    if not antecedents and not home_id:
        return kept
    out = list(kept)
    # Compound forms are scanned FIRST and their spans marked, so the single-cue pattern
    # cannot come back and record a second, poorer citation for the same words —
    # "Schedule 1, paragraph 27" must not also yield a bare "Sch. 1".
    compound: dict[int, "re.Match[str]"] = {}
    for m in _BARE_COMPOUND.finditer(text):
        compound[m.start()] = m
    matches = sorted(
        [*compound.values(),
         *(m for m in _BARE_PROVISION.finditer(text)
           if not any(cs < m.end() and m.start() < ce
                      for cs, ce in ((c.start(), c.end()) for c in compound.values())))],
        key=lambda m: m.start(),
    )
    for m in matches:
        s, e = m.start(), m.end()
        if any(os < e and s < oe for os, oe in occupied):
            continue  # already part of a literal citation ("s.5 of the FOIA 2000")
        if any(us <= s < ue for us, ue in urls):
            continue  # inside a URL — not a provision reference at all
        groups = m.groupdict()
        is_compound = "rsub" in groups
        if is_compound:
            reverse = bool(groups.get("rcue"))
            cue_raw = (groups.get("rcue") if reverse else groups.get("cue")) or ""
            number = (groups.get("rnum") if reverse else groups.get("num")) or ""
            sub = (groups.get("rsub") if reverse else groups.get("sub")) or None
            if cue_raw.lower().startswith("annex"):
                cue_at = m.start("rcue") if reverse else m.start("cue")
                if _SELF_ANNEX_RE.search(text[max(0, cue_at - 16):cue_at]):
                    continue          # "this Annex" — the instrument's own, not the host's
                if _ANNEX_HOST_RE.match(text[e:e + 48]):
                    continue          # "Annex 9 to the Convention …" names its own host
            pinpoint = _compound_pinpoint(cue_raw, number, sub)
        else:
            cue_raw = m.group("cue")
            pinpoint = _bare_pinpoint(cue_raw, m.group("num"))
        cue = cue_raw.lower().rstrip(".")
        # A possessive followed by a tax/academic year is not the abbreviation for
        # section: ``client's 2011/12 tax return`` previously minted ``s. 2011``.
        if cue in ("s", "ss") and (
            text[max(0, s - 1):s] in {"'", "’"}
            or re.match(r"/\d{2,4}\b", text[e:])
        ):
            continue
        # The text names its own host ("of the Road Traffic Act", "of the Code").
        # Whatever that host is, it is not the last-named instrument.
        if _EXPLICIT_HOST_RE.match(text, e) and not re.match(
                r"\s*(?:,\s*)?of\s+(?:that|this)\s+Act\b", text[e:], re.IGNORECASE):
            continue
        # PDF extraction routinely splits "Articles" into "Article s", leaving a
        # stray "s 50(2)" that reads as a UK section and carried forward to the
        # wrong instrument. The "s" here is the tail of the cue word before it.
        if cue in ("s", "ss") and re.search(r"(?i)\barticles?\s*$", text[max(0, s - 12):s]):
            continue
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
                 and _cue_allows(cue_raw, a.entity_kind, a.candidate_id)]
        # "Article 15 of that regulation" supplies the instrument TYPE even though it
        # does not repeat its name.  Prefer the nearest antecedent of that type, rather
        # than blindly taking a nearer Directive mentioned midway through the sentence.
        anaphor = _ANAPHORIC_HOST_RE.match(text[e:e + 64])
        if anaphor:
            prior = [a for a in prior if _host_kind_matches(
                a.entity_kind, a.candidate_id, anaphor.group("kind"))]
        host_id, host_kind = (prior[-1].candidate_id, prior[-1].entity_kind) if prior \
            else (None, None)
        # Self-reference inside legislation. A cross-reference to another instrument
        # governs its own SENTENCE — "in accordance with Article 33(4) of Regulation
        # (EU) 2022/2065 and Article 35 thereof" is all about that regulation. Once
        # the sentence ends, the instrument is talking about itself again: "Article 8
        # has the effect of…" in the GDPR means the GDPR's Article 8, not the
        # Article 8 of the injunctions directive its recitals last cross-referred to.
        if home_id and _cue_allows(cue_raw, home_kind or "", home_id):
            if not prior or _SENTENCE_BREAK_RE.search(text[prior[-1].char_end:s]):
                host_id, host_kind = home_id, home_kind
        if not host_id:
            continue
        out.append(Citation(
            raw=m.group(0), entity_kind=host_kind, candidate_id=host_id,
            pinpoint=pinpoint,
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


def all_grammar_citations(text: str) -> list[Citation]:
    """Every deterministic grammar, including the language-specific ones.

    ``grammar_citations`` covers only the registered (largely anglophone) grammars, so
    a caller using it to ask "does this document cite any law at all?" answers *no* for
    a document written in Dutch, French, German or Italian — its citations live in the
    per-language modules that ``extract_citations`` adds separately. Continental
    regulator sources are gated on exactly that question, so they need the full set.
    Overlaps are not resolved here: the callers count, they don't link.
    """
    from .danish import danish_citations
    from .dutch import dutch_citations
    from .french import french_citations
    from .german import german_citations
    from .italian import italian_citations
    from .spanish import spanish_citations

    return (grammar_citations(text) + french_citations(text) + dutch_citations(text)
            + italian_citations(text) + spanish_citations(text)
            + danish_citations(text) + german_citations(text))


# The provision an alias mention pins to. "section 16 of RIPA" is a citation OF s.16,
# but the alias pass recorded only the four letters and left the pinpoint to the
# carry-forward heuristic, which files its guesses as ``inferred`` — and
# ``document_mentions`` excludes those, because they aren't citations. So Big Brother
# Watch v UK, which describes RIPA s.16 as doing the "heavy lifting" of the statute's
# safeguards and pins to it eighteen times, was absent from
# ``citing_documents(target='ukpga/2000/23', anchor='s. 16')`` — which then reported,
# in terms, that no citer pins to it. A pinpoint the text states explicitly is not a
# heuristic, so capturing it here turns those into ordinary, checkable edges.
_ALIAS_PROV_UNIT = (r"(?:sections?|ss?\.?|regulations?|regs?\.?|articles?|arts?\.?"
                    r"|paragraphs?|paras?\.?)")
_ALIAS_PROV_NUM = r"\d+[A-Za-z]?(?:\s*\([^)]*\))*"
_ALIAS_PROV_BEFORE = (rf"(?P<prov>{_ALIAS_PROV_UNIT}\s*{_ALIAS_PROV_NUM}"
                      rf"|Sched(?:ule)?s?\.?\s*[IVXLC\d]+)\s+(?:of|to|in)\s+(?:the\s+)?")
# The trailing form ("RIPA, s 17") must not reach forward across the NEXT reference:
# in "section 8(4) of RIPA, s. 15 of RIPA" the ", s. 15" belongs to the second
# mention, so a provision that is itself followed by "of"/"to" is not this one's.
_ALIAS_PROV_AFTER = (rf"\s*,\s*(?P<postprov>{_ALIAS_PROV_UNIT}\s*{_ALIAS_PROV_NUM}"
                     rf"|Sched(?:ule)?s?\.?\s*[IVXLC\d]+)(?!\s+(?:of|to)\b)")


def alias_citations(text: str, aliases: dict[str, str]) -> list[Citation]:
    """Citations from user-defined shorthand *rules* ("UK GDPR" → a document id):
    every occurrence of a phrase becomes a link to its target, so the rule propagates
    across the corpus. Word-boundary, case-insensitive; longer phrases win overlaps.

    A mention that names a PROVISION of the aliased instrument ("section 16 of RIPA",
    "RIPA, s 16") carries that pinpoint onto the edge — see ``_ALIAS_PROV_BEFORE``."""
    found: list[Citation] = []
    for phrase, target in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
        if not phrase or not target:
            continue
        # Protected statutory abbreviations are owned by deterministic grammars.  A
        # user/legacy alias for ``DSA`` must not bypass the grammar's context gate and
        # turn Duty Solicitor Advice, the CDSA or RFDSA into Digital Services Act links.
        # The same rule prevents an old alias row from overriding the canonical target
        # of GDPR/DSGVO/AI Act.  Spelled-out and context-qualified forms remain covered
        # by those grammars (and distinct phrases such as ``UK GDPR`` are not protected).
        if _protected_shorthand_target(phrase):
            continue
        # \b only guards against mid-word matches when the adjacent phrase character
        # is itself a word character. On a non-word edge — e.g. an alias like "(UK)
        # GDPR" — a bare \b demands a boundary that never exists there, so the phrase
        # silently never matches. Apply the boundary per edge only when it helps.
        lb = r"\b" if phrase[0].isalnum() or phrase[0] == "_" else ""
        rb = r"\b" if phrase[-1].isalnum() or phrase[-1] == "_" else ""
        core = rf"{lb}{re.escape(phrase)}{rb}"
        # The pinpointed forms are tried as part of the SAME match, so the span covers
        # the provision words too and the longest-match dedupe keeps this over a bare
        # alias hit at the same place.
        pat = (rf"(?:{_ALIAS_PROV_BEFORE}{core})"
               rf"|(?:{core}{_ALIAS_PROV_AFTER})"
               rf"|{core}")
        for m in re.finditer(pat, text, re.IGNORECASE):
            g = m.groupdict()
            found.append(Citation(
                raw=m.group(0), entity_kind="named", candidate_id=target,
                pinpoint=_provision_pinpoint(g.get("prov") or g.get("postprov")),
                char_start=m.start(), char_end=m.end(),
                method="named_alias"))
    return found


# CPR lists need one edge per printed rule/paragraph, which the one-regex-match /
# one-Citation grammar contract cannot express. Keep this narrow companion beside
# the grammar pass (the same pattern used for German multi-provision references).
_CPR_RULE_TOKEN = r"\d+[A-Z]?\.\d+[A-Z]?(?:\([A-Za-z0-9]+\))*"
_CPR_RULE_LIST_BODY = (
    rf"{_CPR_RULE_TOKEN}(?:\s*(?:,|and|&|to|through|[–—-])\s*"
    rf"(?:r(?:ule)?s?\.?\s*)?{_CPR_RULE_TOKEN})+"
)
_CPR_RULE_LISTS = (
    re.compile(
        rf"\b(?:CPR|Civil\s+Procedure\s+Rules(?:\s+1998)?)\s*,?\s*"
        rf"(?:(?:r(?:ule)?s?)\.?\s*)?(?P<list>{_CPR_RULE_LIST_BODY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:r(?:ule)?s?)\.?\s*(?P<list>{_CPR_RULE_LIST_BODY})"
        rf"\s+(?:of|under)\s+(?:the\s+)?"
        rf"(?:CPR|Civil\s+Procedure\s+Rules(?:\s+1998)?)\b",
        re.IGNORECASE,
    ),
)
_CPR_RULE_ONE = re.compile(_CPR_RULE_TOKEN, re.IGNORECASE)
_CPR_PARA_TOKEN = r"\d+(?:\.\d+)*(?:\([A-Za-z0-9]+\))*"
_CPR_PARA_LIST_BODY = (
    rf"{_CPR_PARA_TOKEN}(?:\s*(?:,|and|&|to|through|[–—-])\s*{_CPR_PARA_TOKEN})+"
)
_CPR_PD_LISTS = (
    re.compile(
        rf"\b(?:CPR\s+)?(?:Practice\s+Direction|PD)\s*"
        rf"(?P<pd>\d+(?:[A-Z]+(?:\d+)?)?)\s*,?\s*"
        rf"(?:paras?|paragraphs?)\.?\s*(?P<list>{_CPR_PARA_LIST_BODY})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:paras?|paragraphs?)\.?\s*(?P<list>{_CPR_PARA_LIST_BODY})"
        rf"\s*(?:(?:of|under)\s+(?:the\s+)?|,\s*)(?:CPR\s+)?"
        rf"(?:Practice\s+Direction|PD)\s*(?P<pd>\d+(?:[A-Z]+(?:\d+)?)?)\b",
        re.IGNORECASE,
    ),
)


def _cpr_code(value: str) -> str:
    m = re.fullmatch(r"0*(\d+)([A-Za-z]*)(\d*)", value)
    return (f"{int(m.group(1))}{m.group(2).lower()}{m.group(3)}"
            if m else value.casefold())


def _cpr_list_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    seen: set[tuple[int, int, str, str]] = set()
    for pattern in _CPR_RULE_LISTS:
        for match in pattern.finditer(text):
            for one in _CPR_RULE_ONE.finditer(match.group("list")):
                printed = one.group(0)
                base = printed.split("(", 1)[0].casefold()
                key = (match.start(), match.end(), base, printed)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Citation(
                    raw=match.group(0), entity_kind="regulation",
                    candidate_id=f"uk/cpr/rule/{base}", pinpoint=f"rule {printed}",
                    char_start=match.start(), char_end=match.end(),
                    method="uk_cpr_rule_list",
                ))
    for pattern in _CPR_PD_LISTS:
        for match in pattern.finditer(text):
            pd = _cpr_code(match.group("pd"))
            for one in re.finditer(_CPR_PARA_TOKEN, match.group("list"), re.IGNORECASE):
                printed = one.group(0)
                key = (match.start(), match.end(), pd, printed)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Citation(
                    raw=match.group(0), entity_kind="guidance",
                    candidate_id=f"uk/cpr/pd/{pd}", pinpoint=f"paragraph {printed}",
                    char_start=match.start(), char_end=match.end(),
                    method="uk_cpr_pd_paragraph_list",
                ))
    return out


def extract_citations(text: str, *, llm: CitationExtractor | None = None,
                      aliases: dict[str, str] | None = None,
                      defs_out: list[dict] | None = None,
                      home_id: str | None = None,
                      home_kind: str | None = None) -> list[Citation]:
    """Recognise citations in ``text``. Grammars run first (deterministic, cheap),
    then user-defined shorthand rules (``aliases``), then an optional ``llm`` pass for
    narrative citations. More specific / earlier matches win an overlap.

    ``defs_out``, if given, is filled with the in-document shorthand definitions found
    along the way (see ``shorthand_defs``). It's an out-parameter rather than a wider
    return type because this function has many callers, none of which want it.

    ``home_id`` names the instrument the text IS, when it is legislation, so that a
    bare "Article 8" inside the GDPR resolves to the GDPR (see
    ``_attach_carry_forward``)."""
    if not text:
        return []
    # User shorthand rules take precedence over the built-in grammars on an overlap: a
    # person who defines "UK GDPR" → X means it, over any generic grammar. They lead the
    # list so the stable longest-match dedupe keeps them on a span tie.
    cites = alias_citations(text, aliases) if aliases else []
    from .french import french_citations
    cites += french_citations(text)
    cites += _cpr_list_citations(text)
    cites += grammar_citations(text)
    # Run the narrow Dutch statute vocabulary before German's deliberately broad law-
    # abbreviation parser: ``art. 18 WAO`` is Dutch and otherwise has the exact same
    # surface span as a German ``Art.`` reference.
    from .dutch import dutch_citations
    cites += dutch_citations(text)
    from .italian import italian_citations
    cites += italian_citations(text)
    # Spanish and Danish are narrow, instrument-named grammars (RGPD/LOPDGDD,
    # databeskyttelsesforordningen/-loven) with no overlap with the broad German
    # abbreviation parser, so their order relative to it does not matter.
    from .spanish import spanish_citations
    cites += spanish_citations(text)
    from .danish import danish_citations
    cites += danish_citations(text)
    # German references are normalised before linking and may expand to several exact
    # targets (ranges, i.V.m., repeated Nr./Abs. clauses), which the one-match/one-edge
    # grammar interface cannot represent.
    from .german import german_citations
    cites += german_citations(text)
    # US reporter citations (self-contained matcher), gated to text that looks American — recognises
    # "135 S. Ct. 2401" so it clusters as a case instead of being misread as statutory
    # material. Added before the dedupe so a genuine overlap resolves by span.
    from .us_cases import us_case_citations
    cites += us_case_citations(text)
    cites = _disambiguate_online_safety_act(cites)
    grammar = _dedupe_overlaps(cites)
    if llm is None:
        base = _attach_case_pinpoints(text, grammar)
    else:
        extra = [c for c in llm.extract(text) if not _overlaps_any(c, grammar)]
        base = _attach_case_pinpoints(text, _dedupe_overlaps(grammar + extra))
    hosts: dict[str, tuple[Citation, bool]] = {}
    defs = _collect_shorthand_defs(text, base, hosts)
    if defs_out is not None:
        # ``hosts`` deliberately excluded: a document-scoped host binding is only true
        # inside the document that made it.
        defs_out.extend(_def_rows(defs))
    return _attach_carry_forward(
        text,
        _attach_section_lists(text, _attach_article_lists(
            text, _attach_shorthands(text, base, defs, hosts))),
        home_id=home_id, home_kind=home_kind)


def _overlaps_any(c: Citation, kept: list[Citation]) -> bool:
    return any(c.char_start < k.char_end and k.char_start < c.char_end for k in kept)


def _dedupe_overlaps(cites: list[Citation]) -> list[Citation]:
    """Keep the longest match at each location; drop spans contained in a kept one
    (so the article-scoped citation wins over the bare instrument number)."""
    ordered = sorted(cites, key=lambda c: (c.char_start, -(c.char_end - c.char_start)))
    kept: list[Citation] = []
    occupied: list[tuple[int, int]] = []
    for c in ordered:
        exact_multi = c.method in ("de_law_reference", "nl_juriconnect",
                                   "fr_code_articles", "fr_echr_articles",
                                   "fr_eu_articles", "fr_statute_articles",
                                   "uk_cpr_rule_list", "uk_cpr_pd_paragraph_list") and any(
            k.char_start == c.char_start and k.char_end == c.char_end
            and k.method == c.method and k.pinpoint != c.pinpoint for k in kept)
        if not exact_multi and any(s <= c.char_start and c.char_end <= e for s, e in occupied):
            continue
        # Two grammars can recognise the SAME instrument in spans that merely overlap,
        # neither containing the other — "…(EC Directive) Regulations 2003" from the
        # short-name grammar and "EC Directive) Regulations 2003, SI 2003/2426" from the
        # series-number one. Containment alone lets both through, and one reference is
        # then counted twice. Same target + overlapping text = one citation; a genuinely
        # different pinpoint still survives, because that IS a second reference.
        if not exact_multi and c.candidate_id and any(
            k.candidate_id == c.candidate_id
            and c.char_start < k.char_end and k.char_start < c.char_end
            and (c.pinpoint is None or c.pinpoint == k.pinpoint)
            for k in kept
        ):
            continue
        kept.append(c)
        occupied.append((c.char_start, c.char_end))
    return kept
