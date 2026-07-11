"""Name-variant generation + abbreviation normalisation for the reporter matcher."""

from __future__ import annotations

from raglex.citations.name_variants import name_variants, normalise_abbrev


def _kinds(title):
    return {(v, k) for v, k in name_variants(title)}


def test_normalise_collapses_long_and_short_forms():
    assert normalise_abbrev("A-G v Blake") == normalise_abbrev("Attorney General v Blake")
    assert normalise_abbrev("Lincolnshire CC") == normalise_abbrev("Lincolnshire County Council")
    assert normalise_abbrev("DPP v Smith") == normalise_abbrev(
        "Director of Public Prosecutions v Smith")


def test_roao_reorderings():
    vs = dict(name_variants("Baxter, R (on the application of) v Lincolnshire County Council"))
    assert "R (Baxter) v Lincolnshire County Council" in vs
    assert "R (on the application of Baxter) v Lincolnshire County Council" in vs
    # the bare "R v <defendant>" (claimant dropped) must NOT be produced — it collides
    assert "R v Lincolnshire County Council" not in vs


def test_abbrev_expand_and_contract():
    vs = dict(name_variants("3C Waste Ltd v Mersey Waste Holdings Ltd & Anor"))
    assert any("Limited" in v for v in vs)                       # expanded
    assert any(k == "drop-tail" for v, k in vs.items() if "& Anor" not in v)


def test_single_party_kind_is_generated_but_distinct():
    kinds = {k for _, k in name_variants("Pepper v Hart")}
    assert "single-party" in kinds
    assert "exact" in kinds


def test_empty_title_is_safe():
    assert name_variants("") == []
