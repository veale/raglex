from __future__ import annotations

from raglex.core.structure import line_depths


def depths(text: str) -> list[int]:
    return [d for _s, _e, d in line_depths(text)]


def test_senior_courts_act_s24_nests_paragraphs_under_their_subsection():
    # the real shape of ukpga/1981/54 s.24 in the corpus: (1)(2)(a)(b)(c)(3)
    text = (
        "(1) In sections 20 to 23 and this section, unless the context otherwise requires—\n"
        "(2) Nothing in sections 20 to 23 shall—\n"
        "(a) be construed as limiting the jurisdiction of the High Court…\n"
        "(b) affect the provisions of section 226 of the Merchant Shipping Act 1995…\n"
        "(c) authorise proceedings in rem in respect of any claim against the Crown…\n"
        "(3) In this section— “ Her Majesty’s ships ”…"
    )
    # (a)-(c) hang off (2); (3) returns to the subsection tier
    assert depths(text) == [0, 0, 1, 1, 1, 0]


def test_three_tiers_nest_and_pop_back_out():
    text = ("(1) first\n"
            "(a) alpha under one\n"
            "(i) roman under a\n"
            "(ii) still roman\n"
            "(b) back out to alpha\n"
            "(2) back out to the subsection")
    assert depths(text) == [0, 1, 2, 2, 1, 0]


def test_alpha_run_passing_through_i_is_not_read_as_roman():
    # the genuinely ambiguous token: after (h), "(i)" is the LETTER i, not roman 1,
    # so it must stay on the alpha tier instead of opening a nested one
    text = "(g) seven\n(h) eight\n(i) nine\n(j) ten"
    assert depths(text) == [0, 0, 0, 0]


def test_roman_opens_a_new_tier_when_no_alpha_run_expects_it():
    # here (i) cannot continue the alpha run (which sits at (b)), so it nests
    text = "(a) one\n(b) two\n(i) roman one\n(ii) roman two\n(c) three"
    assert depths(text) == [0, 0, 1, 1, 0]


def test_letters_before_numbers_still_level_correctly():
    # the order of tiers is NOT assumed — an Act that runs (a) then (1) nests the
    # numbers under the letters, the reverse of the usual arrangement
    text = "(a) first\n(1) under a\n(2) still under a\n(b) second"
    assert depths(text) == [0, 1, 1, 0]


def test_inserted_provisions_stay_on_their_own_tier():
    # amending Acts insert (4A)/(4B) beside (4) — same tier, not a sub-tier
    text = "(3) three\n(4) four\n(4A) inserted\n(4B) also inserted\n(5) five"
    assert depths(text) == [0, 0, 0, 0, 0]
    # and the multi-character suffix form
    assert depths("(2) two\n(2ZA) inserted\n(3) three") == [0, 0, 0]


def test_continuation_lines_stay_with_their_provision():
    # an unmarked line is part of the provision above it, so it must not snap back
    # to the left margin mid-sentence
    text = "(1) opening words—\n(a) the first limb\ncontinued onto a second line\n(b) the second limb"
    assert depths(text) == [0, 1, 1, 1]


def test_upper_case_tiers_are_distinct_from_lower_case():
    text = "(a) lower\n(A) upper nests\n(B) still upper\n(b) back to lower"
    assert depths(text) == [0, 1, 1, 0]


def test_restarting_a_sub_run_under_the_next_parent():
    text = ("(1) one\n(a) a\n(b) b\n"
            "(2) two\n(a) a again\n(b) b again\n"
            "(3) three")
    assert depths(text) == [0, 1, 1, 0, 1, 1, 0]


def test_dotted_enumerators_are_recognised():
    assert depths("1. first\n2. second\n3. third") == [0, 0, 0]


def test_prose_parentheticals_are_not_mistaken_for_enumerators():
    # a line opening with a parenthetical phrase is prose, not a provision
    text = "(1) the rule\n(see section 5 for the exception) which applies generally\n(2) the next rule"
    assert depths(text) == [0, 0, 0]


def test_offsets_cover_the_text_exactly():
    text = "(1) one\n(a) two\n(b) three"
    spans = line_depths(text)
    assert [text[s:e] for s, e, _d in spans] == ["(1) one", "(a) two", "(b) three"]


def test_depth_is_bounded():
    # pathological input must not indent off the page
    text = "\n".join(f"({'i' * 1}) x" for _ in range(20))
    assert max(depths(text)) < 8


def test_empty_and_single_line_text():
    assert line_depths("") == [(0, 0, 0)]
    assert depths("no markers here at all") == [0]


def test_depth_never_jumps_more_than_one_tier():
    # The invariant that makes the render legible: a provision may open at most one
    # level deeper than the line before it. Verified to hold across a random sample
    # of real UK statutes in the corpus; asserted here so a future tweak to the
    # candidate/continuation rules can't quietly reintroduce a skipped tier.
    samples = [
        "(1) a\n(a) b\n(i) c\n(ii) d\n(b) e\n(2) f",
        "(a) a\n(1) b\n(i) c\n(2) d\n(b) e",
        "(1) a\ncontinuation\n(2) b\n(a) c\n(A) d\n(B) e\n(b) f",
        "1. a\n2. b\n(a) c\n(i) d",
    ]
    for text in samples:
        ds = depths(text)
        for prev, cur in zip(ds, ds[1:]):
            assert cur - prev <= 1, f"skipped a tier in {text!r}: {ds}"


def test_document_body_emits_line_depths_for_legislation_only(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    cfg = Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                 raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                 settings_path=tmp_path / "s.json", embed_provider="local-hashing",
                 embed_model=None)
    f = Facade(cfg)
    body = "<p>(1) The rule.\n(2) Subject to—\n(a) the first case;\n(b) the second case.</p>".encode()
    act = f.import_bytes(data=body, filename="act.html",
                         doc_type="legislation", title="An Act")["stable_id"]
    judgment = f.import_bytes(data=body, filename="j.html",
                              doc_type="judgment", title="A v B")["stable_id"]

    got = f.document_body(act)
    # this import produces no segments, so the depths ride on the top-level fallback
    lines = [ln for s in got["segments"] for ln in s.get("lines", [])] or got["lines"]
    assert lines, "legislation should carry per-line depths"
    assert [ln["depth"] for ln in lines] == [0, 0, 1, 1]
    # offsets are absolute into the document text, so the reader can slice directly
    text = got["text"]
    assert text[lines[2]["start"]:lines[2]["end"]].strip().startswith("(a)")

    # judgments are flat numbered paragraphs — no hierarchy to recover, none emitted
    jb = f.document_body(judgment)
    assert jb["lines"] is None
    assert all("lines" not in s for s in jb["segments"])
