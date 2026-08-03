"""The tool-contract defects an MCP agent reported from a real research session.

Each of these was a way for the corpus to answer a question wrongly while looking like
it had answered it: a filter that could not be satisfied returning "0 documents" instead
of "that is not a document type"; a judgment served up to paragraph 140 of 213 with the
truncation recorded one level down inside an object named for the windowing mechanism; a
neutral-citation edge minted on a mis-typed year; a pinpoint citator that worked for
statutes and silently found nothing for judgments, which is where practitioners actually
pinpoint. They share a shape — the failure is invisible from the answer — so the fixes
are tested on the thing that made them invisible.
"""

from __future__ import annotations

import pytest

from raglex.facade import (
    _anchor_key,
    _anchor_key_variants,
    _anchor_sql_prefixes,
    _cited_name_conflict,
    _inserted_provisions,
    _label_help,
    _treatment_cues,
    _trim_party_lead_in,
    Facade,
)


class _Seg:
    def __init__(self, label):
        self.label = label


# --- doc_type: a rejected filter is not an answer about the corpus ------------------

@pytest.mark.parametrize("value,expected", [
    ("judgment", ["judgment"]),
    ("cases", ["judgment", "decision", "opinion"]),          # a display kind
    ("CASES", ["judgment", "decision", "opinion"]),          # case-insensitive
    ("legislation", ["legislation"]),
    ("cases,judgment", ["judgment", "decision", "opinion"]),  # deduped, order kept
])
def test_doc_type_accepts_both_vocabularies(value, expected):
    resolved, unknown = Facade._resolve_doc_types(Facade, value.split(","))
    assert resolved == expected
    assert unknown == []


def test_doc_type_unknown_value_is_reported_not_silently_empty():
    """'case' is the singular an agent naturally reaches for after search(kind=…).

    It matched no stored doc_type, so the filter became `doc_type IN ('case')` and the
    search returned total=0 — which reads as "the corpus holds no such authority"."""
    resolved, unknown = Facade._resolve_doc_types(Facade, ["nonsense"])
    assert unknown == ["nonsense"]
    assert resolved == []
    # …and 'case' itself now resolves rather than being rejected at all
    resolved, unknown = Facade._resolve_doc_types(Facade, ["case"])
    assert unknown == [] and "judgment" in resolved


# --- judgment paragraphs are citable units -----------------------------------------

@pytest.mark.parametrize("written", ["[110]", "110", "para 110", "para. 110"])
def test_paragraph_pinpoint_folds_however_it_is_written(written):
    """The citing document's extracted pinpoint is "para 110"; the reader types "[110]".

    These never met, so every judgment pinpoint answered "nothing cites that" — the
    single most valuable anchor in case-law research, silently empty."""
    stored = _anchor_key_variants(_anchor_key("para 110"))
    assert _anchor_key_variants(_anchor_key(written)) & stored


def test_paragraph_range_covers_the_paragraphs_it_spans():
    assert (_anchor_key_variants(_anchor_key("para 70-71"))
            & _anchor_key_variants(_anchor_key("[70]")))


@pytest.mark.parametrize("a,b", [
    ("[110]", "Article 110"),      # a paragraph is not an article
    ("[110]", "s. 110"),           # nor a section
    ("Article 15", "s. 15"),
    ("para 3.19", "para 3"),       # multi-level numbering survives the fold
])
def test_paragraph_fold_does_not_collide_with_other_units(a, b):
    assert not (_anchor_key_variants(_anchor_key(a))
                & _anchor_key_variants(_anchor_key(b)))


def test_bare_paragraph_anchor_guards_on_the_stored_spelling():
    """The SQL prefilter is where the zero came from: a bare number produced the prefix
    "110" while the corpus stores "para 110"."""
    assert set(_anchor_sql_prefixes("[110]")) >= {"110", "para110", "paragraph110"}
    assert "110" in _anchor_sql_prefixes("para 110")


# --- a citation resolved on its number alone ---------------------------------------

def _snippet(text, mark_word):
    at = text.index(mark_word)
    return {"text": text, "mark": [at, at + len(mark_word)]}


def test_name_beside_citation_contradicting_the_target_is_flagged():
    """Bristol CC cites "[2025] EWHC 134 KB" for Valero, which is [2024] EWHC 134 (KB).

    The corpus holds both judgments, so the number alone resolved to a real but
    unrelated case — 20% of the citing set of a judgment with five citers."""
    snip = _snippet(
        "16. In the more recent case of Valero Energy Limited and others v Persons "
        "Unknown and others [2025] EWHC 134 KB, Ritchie J confirmed at [17] that",
        "[2025] EWHC 134 KB")
    conflict = _cited_name_conflict("Michael Ashley v The Commissioners for HMRC", snip)
    assert conflict is not None
    assert conflict["named_beside_citation"] == (
        "Valero Energy Limited and others v Persons Unknown and others")


def test_joined_appeal_is_not_a_contradiction():
    """Only the SECOND name of a joined appeal abuts the citation, and both are right."""
    snip = _snippet(
        "relied on the decision in joined appeals Ittihadieh v 5-11 Cheyne Gardens RTM "
        "Company Limited/ Deer v. University of Oxford [2017] EWCA Civ 121, a decision",
        "[2017] EWCA Civ 121")
    assert _cited_name_conflict(
        "Ittihadieh v 5-11 Cheyne Gardens RTM Company Ltd & Ors", snip) is None


def test_the_same_case_named_differently_is_not_a_contradiction():
    snip = _snippet("see also Durant v FSA [2003] EWCA Civ 1746 at [28]",
                    "[2003] EWCA Civ 1746")
    assert _cited_name_conflict(
        "Durant v Financial Services Authority", snip) is None


def test_a_target_that_is_not_a_case_is_never_flagged():
    snip = _snippet("as required by Article 15 GDPR [2016] OJ L119", "[2016] OJ L119")
    assert _cited_name_conflict("Regulation (EU) 2016/679", snip) is None


@pytest.mark.parametrize("written,expected", [
    ("In the more recent case of Valero Energy Ltd and others v Persons Unknown",
     "Valero Energy Ltd and others v Persons Unknown"),
    ("see also Durant v Financial Services Authority",
     "Durant v Financial Services Authority"),
    ("Michael Ashley v The Commissioners for HMRC",
     "Michael Ashley v The Commissioners for HMRC"),
])
def test_party_name_survives_the_lead_in_trim(written, expected):
    """Trimming used to shorten until the pattern stopped matching, which ate the
    distinctive words and left "Limited and others v Persons Unknown"."""
    assert _trim_party_lead_in(written) == expected


# --- currency metadata against the text actually served ----------------------------

def test_inserted_provisions_are_evidence_of_amendment():
    """A letter-suffixed number is produced by INSERTION and by nothing else."""
    labels = ["Article 12A. Meaning of “applicable time period”",
              "Article 22A Automated processing", "s. 164A Compliance",
              "Article 15 Right of access", "Article 22 Automated decision-making"]
    found = _inserted_provisions(labels)
    assert len(found) == 3
    assert all(x.startswith(("Article 12A", "Article 22A", "s. 164A")) for x in found)


def test_original_numbering_yields_no_false_evidence():
    assert _inserted_provisions(
        ["Article 15 Right of access", "Article 22", "s. 45", "Recital 63"]) == []


# --- knowing the text you were handed is partial ------------------------------------

def test_label_help_reports_the_paragraph_convention_not_the_opening_headers():
    """A long judgment opens with structural headers, so a head-of-document sample
    described the one part that does not use the paragraph convention."""
    segs = [_Seg(x) for x in ["Introduction", "The facts", "The issues"]]
    segs += [_Seg(f"{n}.") for n in range(1, 214)]
    help_ = _label_help(segs)
    assert help_["paragraph_labels"] == {
        "convention": "1.", "first": "1.", "last": "213.", "count": 213}
    # the sample spans the document rather than stopping inside the headers
    assert any(x.rstrip(".").isdigit() and int(x.rstrip(".")) > 100
               for x in help_["labels_sample"])
    assert "BARE INTEGER" in help_["hint"]


def test_label_help_without_numbered_paragraphs_still_samples():
    segs = [_Seg("Article 1"), _Seg("Article 2")]
    help_ = _label_help(segs)
    assert "paragraph_labels" not in help_
    assert help_["labels_sample"] == ["Article 1", "Article 2"]


# --- treatment cues are language, not holdings --------------------------------------

@pytest.mark.parametrize("passage,signal", [
    ("I respectfully decline to follow the reasoning in that case", "negative"),
    ("that decision was, in my view, wrongly decided", "negative"),
    ("it was decided per incuriam and cannot stand", "negative"),
    ("the case is plainly distinguishable on its facts", "distinguished"),
    ("I am bound by the decision of the Court of Appeal", "positive"),
    ("the approach was endorsed by the Supreme Court", "positive"),
])
def test_treatment_cues_are_found_and_quoted(passage, signal):
    cues = _treatment_cues(passage)
    assert cues and cues[0]["signal"] == signal
    # quoted verbatim, so the reader judges the words rather than a label
    assert cues[0]["phrase"].lower() in passage.lower()


@pytest.mark.parametrize("prose", [
    "assisted in identifying personal data by the cases of X following Y",
    "the claimant applied for permission to amend the particulars",
])
def test_ordinary_prose_does_not_mint_a_treatment_signal(prose):
    """"following" and "applied" occur constantly as ordinary words. A cue that fires on
    them fills the roll-up with a majority verdict nobody checked."""
    assert [c for c in _treatment_cues(prose) if c["signal"] == "positive"] == []


@pytest.mark.parametrize("passage,signal", [
    ("IN THE COURT OF APPEAL ON APPEAL FROM THE HIGH COURT (Chancery Division)",
     "appeal"),
    ("permission to appeal was refused by the single judge", "appeal"),
    ("Morgan J upheld the argument that the offer was not compliant", "affirmed"),
    ("the judge's order was set aside", "reversed"),
])
def test_subsequent_history_cues(passage, signal):
    assert any(c["signal"] == signal for c in _treatment_cues(passage))
