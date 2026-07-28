"""Numbered-paragraph synthesis: recovering paragraph structure from flat judgment
text so pinpoints, the minimap, peeks and 'mentioned by para N' work without a
structural re-import."""

from __future__ import annotations

from raglex.core.segmentation import synthesise_numbered_segments as syn


def _labels(text):
    return [s.label for s in syn(text) if s.kind == "paragraph"]


def test_bracket_paragraphs_are_recovered():
    t = "intro\n[1] first\n[2] second\n[3] third"
    assert _labels(t) == ["[1]", "[2]", "[3]"]


def test_quoted_out_of_sequence_numbers_stay_inside_the_paragraph():
    # the Perreault trap: a quoted judgment's own [99] must not split para [3]
    t = "[1] a\n[2] b\n[3] c quoting\n[99] Section 49 directs...\n[4] d"
    assert _labels(t) == ["[1]", "[2]", "[3]", "[4]"]


def test_heading_flattened_onto_paragraph_line_still_found():
    # A2AJ/CanLII flat text merges a section heading onto the following paragraph's line
    # ("II. Analysis A. Standard of Review [9] ...") — the marker isn't at the line start,
    # but the paragraph must still be recovered, or the run dies at the first heading and
    # the rest of the judgment collapses into one segment (Tufail v Canada 2026 FC 914).
    t = ("[1] first\n[2] second, citing Vavilov, 2019 SCC 65, [2019] 4 SCR 653.\n"
         "[3] third\nI. Background to this application [4] fourth\n[5] fifth\n"
         "II. Analysis A. Standard of Review [6] sixth")
    assert _labels(t) == ["[1]", "[2]", "[3]", "[4]", "[5]", "[6]"]
    # the citation year [2019] must NOT be taken as a paragraph
    assert "[2019]" not in _labels(t)


def test_dotted_paragraphs_recovered_when_brackets_absent():
    # High Court of Australia style: "1.", "2." at line start, no brackets
    t = "HIGH COURT OF AUSTRALIA\n1. The first paragraph of the judgment.\n" \
        "2. The second paragraph.\n3. The third paragraph.\n4. The fourth.\n5. Fifth."
    assert _labels(t) == ["1.", "2.", "3.", "4.", "5."]


def test_dotted_fallback_rejects_sparse_stray_numbers():
    # a judgment with NO paragraph numbering, just occasional line-opening "N."
    # (statutory refs, lists) spread thinly — must NOT be mistaken for paragraphs
    filler = "x" * 9000
    t = (f"1. {filler}\n2. {filler}\n3. {filler}\n4. {filler}\n5. {filler}\n6. {filler}")
    # 6 marks, but ~9k chars each → not real paragraph numbering
    assert _labels(t) == []


def test_bracket_wins_over_dotted_when_both_present():
    t = "[1] real para one 2. not a paragraph\n[2] real para two\n[3] three"
    assert _labels(t) == ["[1]", "[2]", "[3]"]


def test_too_few_paragraphs_returns_nothing():
    assert syn("[1] only one") == []
    assert syn("1. only\n2. two") == []          # dotted below the stricter min


def test_sequential_para_marks_drops_quoted_sublists():
    from raglex.core.segmentation import sequential_para_marks

    # real paragraphs 1..6 with a quoted instrument's own "1,2,3" sub-list injected
    # after para 3 (the ECHR-quoting-Bulgarian-law pattern) — the sub-list is dropped
    marks = [(1, 0), (2, 10), (3, 20), (1, 24), (2, 28), (3, 32), (4, 40), (5, 50), (6, 60)]
    assert [n for n, _ in sequential_para_marks(marks)] == [1, 2, 3, 4, 5, 6]
    # a run can only START at 1 or 2 — a scatter of high numbers yields at most the
    # first 1/2 it reaches (the caller's min-length guard then rejects a short run)
    assert sequential_para_marks([(7, 0), (19, 5), (44, 9)]) == []


def test_bailii_para_segments_uses_the_strict_guard():
    from raglex.adapters.bailii_html import _para_segments

    # blocks: real 1,2,3,4 with a quoted "1. … 2. …" sub-list between 2 and 3
    text = ("1. First paragraph.\n\n2. Second paragraph quoting a statute:\n\n"
            "1. Quoted subsection one.\n\n2. Quoted subsection two.\n\n"
            "3. Third paragraph.\n\n4. Fourth paragraph.")
    labels = [s.label for s in _para_segments(text)]
    # the quoted 1./2. must not appear as their own paragraphs
    assert labels == ["para 1", "para 2", "para 3", "para 4"]


def test_bare_number_line_paragraphs_cjeu_layout():
    # CJEU/Formex judgments put the paragraph number ALONE on its own line, then the
    # text — "…cited).\n60\nTherefore, …". The bare-number fallback recovers these.
    text = ("JUDGMENT OF THE COURT\n1\nThis reference concerns data protection.\n"
            "2\nThe questions were referred by the national court.\n"
            "3\nArticle 9 of the GDPR is relevant here.\n"
            "4\nThe Court answers as follows.\n5\nCosts are reserved.")
    assert _labels(text) == ["1", "2", "3", "4", "5"]


def test_bare_number_fallback_still_needs_density_and_sequence():
    # a scatter of bare numbers on lines (a table of amounts) must NOT be read as
    # paragraphs — no from-1 sequence
    text = "Amounts:\n1000\n2500\n3750\ntotal 7250"
    assert _labels(text) == []


# -- judgment author labels ---------------------------------------------------
JUDGMENT = (
    "Neutral Citation Number: [2015] EWCA Civ 81\n\n____________________\n\n"
    "HTML VERSION OF JUDGMENT\n\n____________________\n\nCrown Copyright ©\n\n"
    "LORD JUSTICE BURNETT:\n\n"
    "1. This case concerns funding.\n\n2. By virtue of section 21 of the 1948 Act.\n\n"
    "3. Section 24(1) fixes liability.\n\n4. I would dismiss the claim.\n\n"
    "LORD JUSTICE TOMLINSON\n\n5. I agree.\n\nLORD DYSON MR\n\n6. I also agree.")


def test_every_judgment_author_gets_its_own_heading():
    """A reader spotted that [2015] EWCA Civ 81 rendered "LORD JUSTICE TOMLINSON" and
    "LORD DYSON MR" but not "LORD JUSTICE BURNETT:" — the first author's byline sits in
    the preamble, before paragraph 1, and so belonged to no segment at all, while the
    concurrences were merely trailing text on the paragraph above them."""
    segs = syn(JUDGMENT)
    headings = [JUDGMENT[s.char_start:s.char_end].strip()
                for s in segs if s.kind == "heading"]
    assert headings == ["LORD JUSTICE BURNETT:", "LORD JUSTICE TOMLINSON", "LORD DYSON MR"]
    # the paragraphs are untouched — no offset moves, so citations stay anchored
    assert [s.label for s in segs if s.kind == "paragraph"] == \
        ["1.", "2.", "3.", "4.", "5.", "6."]
    # the boilerplate above the first byline is still the header, not a heading
    header = next(s for s in segs if s.kind == "header")
    assert "Crown Copyright" in JUDGMENT[header.char_start:header.char_end]
    assert "BURNETT" not in JUDGMENT[header.char_start:header.char_end]


def test_a_judge_named_in_prose_is_not_a_byline():
    text = ("1. The appellant relied on the reasoning of LORD JUSTICE BURNETT in the "
            "court below, which the respondent disputes.\n\n"
            "2. LORD JUSTICE TOMLINSON gave the following judgment in that case.\n\n"
            "3. We reject that submission.")
    assert not [s for s in syn(text) if s.kind == "heading"]


def test_bylines_can_be_added_without_disturbing_an_existing_segmentation():
    """A third of uk-caselaw has import-derived segments with MORE paragraphs than flat
    text yields, so the byline split has to be applied to what is stored rather than
    replacing it."""
    from raglex.core.models import Segment
    from raglex.core.segmentation import _split_author_labels

    text = ("1. First point.\n\n2. Second point.\n\nLORD JUSTICE TOMLINSON\n\n"
            "3. I agree.")
    stored = [Segment(label="1.", kind="paragraph", level=1, char_start=0, char_end=17),
              Segment(label="2.", kind="paragraph", level=1, char_start=17, char_end=59),
              Segment(label="3.", kind="paragraph", level=1, char_start=59, char_end=len(text))]
    out = _split_author_labels(text, stored)
    kinds = [(s.kind, text[s.char_start:s.char_end].strip()) for s in out]
    assert ("heading", "LORD JUSTICE TOMLINSON") in kinds
    # the paragraphs either side keep their labels and their starts
    assert [s.label for s in out if s.kind == "paragraph"] == ["1.", "2.", "3."]
    assert [s.char_start for s in out if s.kind == "paragraph"] == [0, 17, 59]


def test_a_party_on_its_own_line_is_not_mistaken_for_a_judge():
    """1,159 of 1,409 sampled uk-caselaw intitulings put a party on its own upper-case
    line — "MR SHAH RAHUL HASAN", "MRS N A K" — which a bare honorific would read as a
    byline. A plain Mr/Mrs/Ms therefore needs an explicit judicial office after it,
    while a peer or an office that stands alone does not."""
    from raglex.core.segmentation import _AUTHOR_LABEL_RE, _NOT_AN_AUTHOR

    def is_byline(s: str) -> bool:
        m = _AUTHOR_LABEL_RE.match(s)
        lab = m.group("label").strip() if m else ""
        return bool(m) and 6 <= len(lab) <= 60 and not _NOT_AN_AUTHOR.search(lab)

    for good in ["LORD JUSTICE BURNETT:", "LORD DYSON MR", "LORD HOFFMANN",
                 "LADY JUSTICE ARDEN", "MR JUSTICE ANDREW SMITH", "MRS JUSTICE STEYN DBE",
                 "HIS HONOUR JUDGE McMULLEN QC", "JUDGE ARMSTRONG-HOLMES",
                 "OPINION OF LORD DOHERTY", "BARONESS HALE OF RICHMOND",
                 "THE LORD CHIEF JUSTICE OF ENGLAND AND WALES"]:
        assert is_byline(good), good
    for bad in ["MR SHAH RAHUL HASAN", "MRS N A K", "MR SMITH", "The Appeal",
                "THE SECRETARY OF STATE FOR THE HOME DEPARTMENT"]:
        assert not is_byline(bad), bad
