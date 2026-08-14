"""Sweden — the publications the paged list does not return, and the ones that are gone.

Two shortfalls, and they need opposite remedies:

* Domstolsverket's paged list returns **one member per publication group**. A case is
  published twice — the court's signed judgment and the edited report that carries the
  NJA citation — and enumeration only ever sees one of them. 391 judgments answer on
  ``/publiceringar/{id}`` and appear on no page. The remedy is in the live adapter:
  expand every group discovery meets.
* 41 further publications answer with HTTP 200 and an **empty body**: withdrawn, mostly
  prövningstillstånd notices taken down once the court answered the question. No route
  reaches those, and the remedy is the archived snapshot.

Telling the two apart is the whole design, so it is what these tests pin.
"""

from __future__ import annotations

import json

import pytest

from raglex.adapters import registry
from raglex.adapters.se_domstol import SwedishCaseLawAdapter
from raglex.adapters.se_domstol_bulk import (
    SwedishCaseLawBulkAdapter,
    _anchor,
    _malnummer,
    _reference_relations,
    _work_id,
)
from raglex.core.errors import FetchError
from raglex.formats.se_dom_pdf import parse_se_judgment_pdf

REFERAT = {
    "id": "f9d5eb58", "gruppKorrelationsnummer": "5f93fd46",
    "publiceringsform": "REFERAT", "typ": "PREJUDIKAT",
    "domstol": {"domstolKod": "HDO", "domstolNamn": "Högsta domstolen"},
    "avgorandedatum": "2024-09-20", "publiceringstid": "2025-04-09T11:52:00",
    "malNummerLista": ["Ö 1737-24"], "referatNummerLista": ["NJA 2024 s. 618"],
    "benamning": "”Det filmade upploppet”", "innehall": "<h1>SKÄL</h1><p>1. Text.</p>",
}
JUDGMENT = {
    "id": "00f222a9", "gruppKorrelationsnummer": "5f93fd46",
    "publiceringsform": "DOM_ELLER_BESLUT", "typ": "PREJUDIKAT",
    "domstol": {"domstolKod": "HDO", "domstolNamn": "Högsta domstolen"},
    "avgorandedatum": "2024-09-20", "publiceringstid": "2025-04-09T11:47:00",
    "malNummerLista": ["Ö 1737-24"], "referatNummerLista": [],
    "benamning": "”Det filmade upploppet”", "innehall": "",
    "bilagaLista": [{"fillagringId": "190/8c/3d/x", "filnamn": "Ö 1737-24.pdf"}],
}


class _Client:
    """The service as it actually behaves: the list hides one member of every group."""

    def __init__(self, pages, groups=None, detail=None):
        self.pages, self.groups, self.detail = pages, groups or {}, detail or {}
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, raise_for_4xx=True):
        self.calls.append(url)
        if "/grupp/" in url:
            body = self.groups.get(url.rsplit("/", 1)[-1], [])
        elif url.endswith("/publiceringar"):
            body = self.pages[min((params or {}).get("page", 0), len(self.pages) - 1)]
        else:
            body = self.detail.get(url.rsplit("/", 1)[-1])
        return _Resp(body)


class _Resp:
    def __init__(self, body):
        self._body = body
        self.status_code = 200
        self.content = b""
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("empty body")
        return self._body


# ── the live adapter: expanding the group ────────────────────────────────────
def test_discovery_yields_the_group_member_the_list_withholds():
    client = _Client(pages=[[REFERAT], []], groups={"5f93fd46": [REFERAT, JUDGMENT]})
    stubs = list(SwedishCaseLawAdapter(page_size=1, client=client).discover(None))
    assert [s.stable_id for s in stubs] == [
        "se/domstol/f9d5eb58", "se/domstol/00f222a9"]
    # The listed member comes first, which is what makes the group alias land on the
    # report rather than on the judgment under first-writer-wins.
    assert stubs[0].hints["row"]["publiceringsform"] == "REFERAT"


def test_a_group_is_expanded_once_however_many_of_its_members_are_listed():
    client = _Client(pages=[[REFERAT, JUDGMENT], []],
                     groups={"5f93fd46": [REFERAT, JUDGMENT]})
    stubs = list(SwedishCaseLawAdapter(page_size=2, client=client).discover(None))
    assert len(stubs) == 2, "a member reached twice must not be fetched twice"
    assert sum(1 for c in client.calls if "/grupp/" in c) == 1


def test_expansion_can_be_turned_off():
    client = _Client(pages=[[REFERAT], []], groups={"5f93fd46": [REFERAT, JUDGMENT]})
    stubs = list(SwedishCaseLawAdapter(page_size=1, expand_groups=False,
                                       client=client).discover(None))
    assert [s.stable_id for s in stubs] == ["se/domstol/f9d5eb58"]
    assert not any("/grupp/" in c for c in client.calls)


def test_expansion_is_on_when_the_option_was_never_touched():
    """A form sends ``None`` for an untouched field and ``bool(None)`` is False, which
    would silently turn the fix off for everybody who left the default alone."""
    assert SwedishCaseLawAdapter(expand_groups=None).expand_groups is True


def test_a_sibling_is_not_re_fetched_for_a_body_the_group_route_already_gave():
    """The group route serves the full DTO, so an empty ``innehall`` on a sibling means
    "this is a PDF", not "the list abridged it"."""
    client = _Client(pages=[[REFERAT], []], groups={"5f93fd46": [REFERAT, JUDGMENT]})
    adapter = SwedishCaseLawAdapter(page_size=1, include_documents=False, client=client)
    sibling = [s for s in adapter.discover(None) if s.hints.get("complete")][0]
    before = len(client.calls)
    adapter.fetch(sibling)
    assert client.calls[before:] == [], "no detail call for a complete group member"


# ── the PDF reader ───────────────────────────────────────────────────────────
HD_PDF = """Dok.Id 364143 Besöksadress
Riddarhustorget 8
Telefon
08-561 666 00
Postadress
Högsta domstolen
Box 2066
103 12 Stockholm
E-post
hogsta.domstolen@dom.se
Webbplats
www.hogstadomstolen.se
Sida 1 (8)
HÖGSTA DOMSTOLENS
BESLUT
meddelat i Stockholm den 14 juli 2026
Mål nr
Ö 4337-25
SAKEN
Avvisning
__________
SKÄL
Frågan i målet
Frågan i Högsta domstolen är om svensk eller kanadensisk rätt ska
tillämpas vid bedömningen av om talan om underhåll kan tas upp till

Sida 2 (8)
HÖGSTA DOMSTOLEN BESLUT Ö 4337-25
Dok.Id 364143
prövning. Det framgår av NJA 1970 s. 274 att utländska regler om
talerättsbegränsning kan tillämpas även här.
"""


def test_the_letterhead_and_the_page_headers_go():
    parsed = parse_se_judgment_pdf(HD_PDF)
    for gone in ("Riddarhustorget", "08-561 666 00", "hogsta.domstolen@dom.se",
                 "Dok.Id", "Sida 1 (8)", "HÖGSTA DOMSTOLEN BESLUT Ö 4337-25"):
        assert gone not in parsed.text, gone
    assert "Avvisning" in parsed.text


def test_a_party_address_survives_because_it_is_not_in_a_page_header():
    """``Box 2066`` under the court's own letterhead is furniture; the identical line
    under PARTER is a party's address and belongs to the document."""
    text = HD_PDF.replace("SAKEN\nAvvisning", "PARTER\nKlagande\nRiksåklagaren\nBox 5553\n"
                                              "114 85 Stockholm\nSAKEN\nAvvisning")
    parsed = parse_se_judgment_pdf(text)
    assert "Box 5553" in parsed.text
    assert "Box 2066" not in parsed.text


def test_a_citation_broken_across_the_page_break_is_rejoined():
    """The sentence carrying ``NJA 1970 s. 274`` is interrupted by a page header. Left
    in, the citation never matches; the wrap itself must also close up."""
    parsed = parse_se_judgment_pdf(HD_PDF)
    assert "NJA 1970 s. 274" in parsed.text
    assert "tas upp till prövning." in parsed.text


def test_the_outline_is_two_levels_and_a_heading_does_not_eat_its_paragraph():
    parsed = parse_se_judgment_pdf(HD_PDF)
    assert parsed.metadata["zones"] == [
        "HÖGSTA DOMSTOLENS", "BESLUT", "SAKEN", "SKÄL"]
    subs = [s.label for s in parsed.segments if s.kind == "heading" and s.level == 1]
    assert subs == ["Frågan i målet"]
    body = [s for s in parsed.segments if s.kind != "heading"]
    assert any(s.label == "Frågan i målet" for s in body), \
        "the paragraph under a subheading must be its own segment, not part of it"


def test_anonymised_initials_are_not_read_as_a_section_heading():
    """Domstolsverket replaces names with initials, so "AA BB" is in capitals for the
    same reason "SAKEN" is."""
    parsed = parse_se_judgment_pdf("SÖKANDE\nAA BB\nCC\nSAKEN\nRättsprövning\n")
    assert parsed.metadata["zones"] == ["SÖKANDE", "SAKEN"]


def test_a_repeated_running_header_is_found_by_its_repetition():
    """An annexed lower-court decision brings its own page headers, split over lines no
    single-line pattern sees. What identifies them is that they repeat under page
    breaks — no sentence of a judgment does."""
    body = "".join(f"Sid {n} (9)\nNACKA TINGSRÄTT\nDOM F 6439-22\nBrödtext {n} här.\n"
                   for n in range(1, 6))
    parsed = parse_se_judgment_pdf("DOMSKÄL\n" + body)
    assert "NACKA TINGSRÄTT" not in parsed.text
    assert "DOM F 6439-22" not in parsed.text
    assert "Brödtext 3 här." in parsed.text


def test_line_end_hyphens_are_closed_up_but_coordinations_are_kept():
    parsed = parse_se_judgment_pdf(
        "SKÄL\nBolaget yrkade ersättning för rätte-\ngångskostnad i mark-\n"
        "och miljödomstolen enligt lag.\n")
    assert "rättegångskostnad" in parsed.text
    assert "mark- och miljödomstolen" in parsed.text


def test_segment_offsets_index_the_stored_text_exactly():
    parsed = parse_se_judgment_pdf(HD_PDF)
    for segment in parsed.segments:
        assert parsed.text[segment.char_start:segment.char_end].strip()


def test_a_double_spaced_extraction_still_joins_its_lines():
    """Born-digital extraction of the same courts' PDFs comes out both ways. Read
    literally, the double-spaced kind never joins a line and every citation stays broken
    across the wrap that produced it."""
    single = parse_se_judgment_pdf("SKÄL\nEnligt 2 kap. 15 §\ntryckfrihetsförordningen gäller detta.\n")
    double = parse_se_judgment_pdf("SKÄL\n\nEnligt 2 kap. 15 §\n\ntryckfrihetsförordningen gäller detta.\n")
    assert "2 kap. 15 § tryckfrihetsförordningen" in single.text
    assert "2 kap. 15 § tryckfrihetsförordningen" in double.text


# ── the archived snapshot ────────────────────────────────────────────────────
def test_an_id_the_list_returns_is_never_probed():
    """The paged list settles 16,800 of the 17,228 ids for free. Probing them anyway
    would turn a six-hundred-request job into a seventeen-thousand-request one."""
    client = _Client(pages=[[{"id": "live-1"}], []])
    adapter = SwedishCaseLawBulkAdapter(client=client)
    assert adapter._is_withdrawn("live-1") is False
    assert not any("/publiceringar/live-1" in c for c in client.calls)


def test_an_unlisted_id_the_detail_route_still_serves_is_not_withdrawn():
    """These are the 391 judgments the list merely hides. The live adapter reaches them
    by expanding the group, with the publisher's own PDF and metadata — importing a
    cleaned copy from the snapshot instead would be a downgrade."""
    client = _Client(pages=[[{"id": "live-1"}], []], detail={"hidden-1": JUDGMENT})
    assert SwedishCaseLawBulkAdapter(client=client)._is_withdrawn("hidden-1") is False


def test_an_empty_200_is_what_withdrawn_looks_like():
    """The status code cannot be the test: the service answers 200 with no body."""
    client = _Client(pages=[[{"id": "live-1"}], []], detail={"gone-1": None})
    assert SwedishCaseLawBulkAdapter(client=client)._is_withdrawn("gone-1") is True


def test_an_unreachable_service_raises_instead_of_importing_everything():
    """"the network is down" and "the publisher withdrew it" must not produce the same
    import — the second would archive 17,000 good documents behind worse ones."""
    client = _Client(pages=[[]])
    with pytest.raises(FetchError, match="nothing can be shown to be withdrawn"):
        SwedishCaseLawBulkAdapter(client=client)._is_withdrawn("anything")


def test_mode_all_asks_the_service_nothing():
    client = _Client(pages=[[]])
    adapter = SwedishCaseLawBulkAdapter(mode="all", path="/nonexistent", client=client)
    assert list(adapter.discover(None)) == []
    assert client.calls == []


def test_the_snapshot_law_names_resolve_through_the_grammar_s_own_tables():
    """Four references in five carry a law's name and no SFS number. Resolving them
    anywhere but the Swedish grammar's tables would mint a parallel set of ids."""
    assert _work_id({"sfs": "1962:700"}) == "se/sfs/1962/700"
    assert _work_id({"law": "miljöbalken"}) == "se/sfs/1998/808"
    assert _work_id({"law": "RB"}) == "se/sfs/1942/740"
    assert _work_id({"law": "en lag som inte finns"}) is None


def test_the_snapshot_anchor_is_the_form_the_swedish_grammar_mints():
    assert _anchor({"chapter": "2", "paragraphs": "15", "symbol": "§"}) == "2 kap. 15 §"
    assert _anchor({"chapter": None, "paragraphs": "1", "symbol": "§"}) == "1 §"
    assert _anchor({"paragraphs": "57 och 57 a", "symbol": "§§"}) == "57 och 57 a §§"
    assert _anchor({"paragraphs": ""}) is None


def test_duplicate_references_collapse_to_one_edge():
    row = {"legal_references": json.dumps([
        {"chapter": "2", "paragraphs": "15", "symbol": "§", "law": "tryckfrihetsförordningen"},
        {"chapter": "2", "paragraphs": "15", "symbol": "§", "law": "tryckfrihetsförordningen"},
        {"chapter": "2", "paragraphs": "19", "symbol": "§", "law": "tryckfrihetsförordningen"},
    ])}
    relations = _reference_relations(row)
    assert [(r.dst_id, r.dst_anchor) for r in relations] == [
        ("se/sfs/1949/105", "2 kap. 15 §"), ("se/sfs/1949/105", "2 kap. 19 §")]


def test_unparseable_references_do_not_lose_the_document():
    assert _reference_relations({"legal_references": "not json"}) == []
    assert _reference_relations({}) == []


def test_the_court_s_label_is_stripped_from_the_docket():
    assert _malnummer({"malnummer": "Mål nr 6728-25"}) == ["6728-25"]
    assert _malnummer({"malnummer": "T 2067-25"}) == ["T 2067-25"]
    assert _malnummer({"malnummer": ""}) == []


# ── the catalogue contract ───────────────────────────────────────────────────
def test_both_sources_are_in_the_catalogue_with_truthful_incremental_modes():
    catalogue = {c["key"]: c for c in registry.source_catalog()}
    assert catalogue["se-domstol"]["jurisdiction"] == "SE"
    assert catalogue["se-domstol-bulk"]["jurisdiction"] == "SE"
    assert registry.INCREMENTAL_MODE["se-domstol"] == "full-walk"
    assert registry.INCREMENTAL_MODE["se-domstol-bulk"] == "bulk"


def test_every_declared_option_is_accepted_by_its_constructor():
    catalogue = {c["key"]: c for c in registry.source_catalog()}
    for key in ("se-domstol", "se-domstol-bulk"):
        names = [o["name"] if isinstance(o, dict) else o.name
                 for o in catalogue[key]["options"]]
        registry.ADAPTERS[key](**{name: None for name in names})
