"""BAILII full-text corpus join: path→slug, name cleaning, citation sanity-check."""

from __future__ import annotations

import pytest

from raglex.adapters.bailii import bailii_url
from raglex.adapters.bailii_corpus import (
    bailii_path_to_slug,
    citation_agrees_with_slug,
    clean_case_name,
    slug_to_citation,
)


@pytest.mark.parametrize("slug", [
    "ewhc/comm/2015/3076", "ewca/civ/2006/717", "uksc/2021/12", "ewhc/admin/2007/3039",
])
def test_path_to_slug_inverts_bailii_url(slug):
    # the reverse of bailii_url must land back on the same stable_id
    url_path = bailii_url(slug).replace("https://www.bailii.org", "").replace(".rtf", ".html")
    assert bailii_path_to_slug(url_path) == slug


@pytest.mark.parametrize("path,slug", [
    ("/ew/cases/EWHC/Costs/2015/B17.html", "ewhc/costs/2015/b17"),   # letter-prefixed number
    ("/ew/cases/EWHC/QB/2015/68_2.html", "ewhc/qb/2015/68_2"),        # underscore number
    ("https://www.bailii.org/uk/cases/UKHL/2005/12.html", "ukhl/2005/12"),  # full URL
])
def test_path_to_slug_edge_numbers(path, slug):
    assert bailii_path_to_slug(path) == slug


@pytest.mark.parametrize("path", ["", "/uk/legislation/2000/1", "/ew/cases/EWHC/Comm/2015", None])
def test_path_to_slug_rejects_non_judgments(path):
    assert bailii_path_to_slug(path) is None


def test_clean_strips_leading_junk_date_and_pulls_citation():
    c = clean_case_name(
        "> Bominflot Bunkergesellschaft v Petroplus Marketing AG "
        "[2012] EWHC 3009 (Comm) (30 October 2012)"
    )
    assert c.title == "Bominflot Bunkergesellschaft v Petroplus Marketing AG"
    assert c.citations == ("[2012] EWHC 3009 (Comm)",)


def test_clean_handles_roao_and_v_dot():
    c = clean_case_name(
        "Baxter, R (on the application of) v Lincolnshire County Council "
        "[2015] EWCA Civ 1290 (18 December 2015)"
    )
    assert c.title == "Baxter, R (on the application of) v Lincolnshire County Council"
    assert c.citations == ("[2015] EWCA Civ 1290",)


def test_clean_drops_bailii_catchwords():
    c = clean_case_name(
        "(1) A Ahmed (2) I Ahmed v (1) P Davis (2) T Dolder "
        "(Beneficial interests, trusts and restrictions : Restrictions)"
    )
    assert "Beneficial interests" not in c.title
    assert c.catchwords and "Beneficial interests" in c.catchwords


def test_old_style_technology_citation_keeps_its_number():
    c = clean_case_name("Abb Power Construction Ltd v. Norwest Holst Ltd [2000] EWHC Technology 68")
    assert c.title == "Abb Power Construction Ltd v Norwest Holst Ltd"
    assert c.citations == ("[2000] EWHC Technology 68",)


@pytest.mark.parametrize("slug,citation", [
    ("ewhc/comm/2015/3076", "[2015] EWHC 3076 (Comm)"),
    ("ewca/civ/2006/717", "[2006] EWCA Civ 717"),
    ("uksc/2021/12", "[2021] UKSC 12"),
])
def test_slug_to_citation(slug, citation):
    assert slug_to_citation(slug) == citation


def test_citation_agreement_sanity_check():
    assert citation_agrees_with_slug("ewca/civ/2015/1290", "[2015] EWCA Civ 1290")
    # wrong number → disagreement (an index row mis-joined to the wrong path)
    assert not citation_agrees_with_slug("ewca/civ/2015/1290", "[2015] EWCA Civ 999")
