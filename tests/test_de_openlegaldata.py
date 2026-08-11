"""Germany — the Open Legal Data Länder corpus, and the citation work it needed.

The corpus's German case law was the seven federal courts. This source adds 424k
decisions from 918 courts, which broke three assumptions at once: that a German court
is one of nine abbreviations, that a German judgment cites EU law in English, and that
a document's short name for an instrument is defined within 90 characters of the
citation. Every test here is one of those.
"""

from __future__ import annotations

from raglex.adapters.de_openlegaldata import (
    DeOpenLegalDataAdapter,
    _court_display,
    _law_relations,
    _section_anchor,
    stable_id_for,
)
from raglex.citations import de_courts, de_laws, extract_citations
from raglex.citations.german import case_alias, case_citations, german_citations
from raglex.formats.olg_html import parse_olg


def _ids(text):
    return {(c.method, c.candidate_id) for c in extract_citations(text)}


# -- the court table ----------------------------------------------------------
def test_a_court_is_found_by_every_spelling_a_citation_uses():
    # "OVG Münster" is how German practice cites the court whose NAME is
    # "Oberverwaltungsgericht Nordrhein-Westfalen" — the seat appears nowhere in the
    # name, so a name-only table can never resolve the commonest form.
    for spelling in ("OVG Münster", "OVG Nordrhein-Westfalen",
                     "Oberverwaltungsgericht Nordrhein-Westfalen", "OVGNRW",
                     "ECLI:DE:OVGNRW:2024:0115.13A1234.20.00"):
        assert de_courts.court_key(spelling) == "OVGNRW", spelling


def test_a_spelling_that_names_ten_courts_names_none_of_them():
    # The upstream alias lists pair each court's type with its Bundesland, so all ten
    # NRW Verwaltungsgerichte claim "VG Nordrhein-Westfalen" and all three NRW
    # Oberlandesgerichte claim "OLG Nordrhein-Westfalen". Admitting those would file a
    # citation of any one of them under whichever court happened to be registered first.
    assert de_courts.find("VG Nordrhein-Westfalen") is None
    assert de_courts.find("OLG Nordrhein-Westfalen") is None
    # …while the state form of a court that IS its Land's only one survives.
    assert de_courts.court_key("OVG Nordrhein-Westfalen") == "OVGNRW"


def test_the_register_and_the_dump_disagree_and_the_better_spelling_wins():
    # The register's court names were OCR'd from a printed directory: "Biidingen" for
    # Büdingen, "Waldbröi" for Waldbröl, "GroB-Gerau" for Groß-Gerau. The bulk dump
    # carries the decisions' own metadata and is right.
    for spelling, name in (("AG Büdingen", "Amtsgericht Büdingen"),
                           ("AG Waldbröl", "Amtsgericht Waldbröl"),
                           ("AG Groß-Gerau", "Amtsgericht Groß-Gerau"),
                           ("AG Aschersleben", "Amtsgericht Aschersleben")):
        assert de_courts.court_name(spelling) == name, spelling
    # …and the ASCII transliteration resolves to the same court as the umlaut.
    assert de_courts.court_name("AG Huenfeld") == "Amtsgericht Hünfeld"


def test_a_tribunal_sharing_its_host_courts_ecli_token_keeps_its_own_key():
    # The Anwaltsgerichtshof NRW sits at, and mints ECLIs under, the OLG Hamm. The
    # token therefore resolves to the OLG (the primary court of that seat), while the
    # tribunal keys on its own slug so its dockets don't merge into the OLG's.
    assert de_courts.by_code("OLGHAM").name == "Oberlandesgericht Hamm"
    agh = de_courts.find("Anwaltsgerichtshof Nordrhein-Westfalen")
    assert agh is not None and agh.name == "Anwaltsgerichtshof Nordrhein-Westfalen"


# -- German case citation, now that the Länder courts exist -------------------
def test_a_lander_court_docket_citation_resolves():
    # Before the court table, the case grammar knew nine courts; a citation of any
    # court below them matched nothing, so the corpus's own Länder decisions were
    # unreachable by the way they are actually cited.
    found = {c.candidate_id for c in case_citations(
        "Vgl. OVG Münster, Beschluss vom 15.1.2024 – 13 A 1234/20; "
        "VG Köln, Urteil vom 17.6.2025 – 1 L 1930/22; LAG Köln, 4 Sa 12/20; "
        "LSG NRW, 5 KR 12/18.")}
    assert found == {"de:case:OVGNRW:13A1234/20", "de:case:VGK:1L1930/22",
                     "de:case:LAGK:4SA12/20", "de:case:LSGNRW:5KR12/18"}


def test_the_alias_a_citation_mints_is_the_one_the_decision_registers():
    # The harvested decision registers case_alias(court.name, file_number); a citation
    # mints case_alias("OVG Münster", …). They have to be the same string or the
    # citation never resolves to the decision.
    assert (case_alias("Oberverwaltungsgericht Nordrhein-Westfalen", "13 A 1234/20")
            == case_alias("OVG Münster", "13 A 1234/20")
            == "de:case:OVGNRW:13A1234/20")


def test_a_report_series_is_still_not_a_court():
    # "SozR 4-1500" was minting de:case:BSG:ZR4-1500, the most-cited German "case" in
    # the corpus. Widening the court and register alternations must not undo that.
    assert not [c for c in case_citations("SozR 4-1500 § 96 Nr. 1 und BGHR StPO Abs. 3")
                if c.candidate_id]


def test_a_continental_report_series_is_not_a_bracketless_neutral_citation():
    # "BGH vom 8.11.2007 BGHZ 174, 101" has the exact shape of a Canadian/Indian
    # bracketless neutral citation — year, all-caps token, number — and was minting
    # bghz/2007/174, a "case" that is really the official BGH civil reports. Same for
    # the Austrian "RK 24.3.2006" (Rechtskraft, when a conviction became final).
    assert not [c for c in extract_citations("(BGH vom 8.11.2007 BGHZ 174, 101)")
                if c.candidate_id]
    assert not [c for c in extract_citations("vom 20.3.2006 RK 24.3.2006")
                if c.candidate_id]
    # …and the real thing still resolves.
    assert any(c.candidate_id == "scc/2024/1" for c in extract_citations("2024 SCC 1"))


# -- EU law as a German judgment writes it ------------------------------------
def test_an_eu_instrument_cited_in_german_resolves_to_its_celex():
    # The numeric grammar was English-only, so "Richtlinie 2002/58/EG" — the ordinary
    # way a German court cites the ePrivacy Directive — was invisible.
    got = _ids("Art. 15 Abs. 1 der Richtlinie 2002/58/EG und Art. 6 Abs. 1 lit. f der "
               "Verordnung (EU) 2016/679 sowie der Beschluss 2010/87/EU.")
    assert ("eu_instrument_numeric_de", "32002L0058") in got
    assert ("eu_instrument_numeric_de", "32016R0679") in got
    assert ("eu_instrument_numeric_de", "32010D0087") in got


def test_the_german_pinpoint_lands_in_the_formex_vocabulary():
    # "Art. 6 Abs. 1 lit. f" and "Article 6(1)(f)" are the same provision, and must
    # spell the same anchor or the two routes cite different things.
    cites = {c.candidate_id: c for c in extract_citations(
        "Nach Art. 6 Abs. 1 lit. f der Verordnung (EU) 2016/679 ist dies zulässig.")}
    assert cites["32016R0679"].pinpoint == "Article 6(1)(f)"


def test_a_german_law_abbreviation_that_is_an_eu_act_keeps_its_celex():
    got = _ids("Nach Art. 15 DSGVO und Art. 267 AEUV sowie Art. 8 EMRK.")
    assert {"32016R0679", "12016E", "echr/convention"} <= {cid for _m, cid in got}


# -- instruments the judgment merely NAMES ------------------------------------
def test_an_instrument_named_without_a_provision_is_still_a_reference():
    # The German grammar reads a reference that runs from a § to an abbreviation, so a
    # judgment ABOUT the TKG that says "das Telekommunikationsgesetz" or "das TKG"
    # produced no edge at all for most of its mentions.
    got = _ids("Das Telekommunikationsgesetz gilt. Auch das TKG und die DSGVO sind "
               "einschlägig, ebenso das Digitale-Dienste-Gesetz.")
    assert ("de_instrument_name", "de/gesetz/tkg") in got
    assert ("de_instrument_abbrev", "de/gesetz/tkg") in got
    assert ("de_instrument_abbrev", "32016R0679") in got
    assert ("de_instrument_name", "de/gesetz/ddg") in got


def test_a_statute_filed_under_a_law_report_is_not_a_bare_mention():
    # "BGHR StPO Abs. 3 Verfahrenshindernis 2" cites a REPORT whose filing key is the
    # StPO. Reading that as the judgment reaching for the statute would attach a
    # statutory edge to every headnote reference in the corpus.
    assert not [c for c in de_laws.instrument_citations("BGHR StPO Abs. 3 Verfahrenshindernis 2)")]


def test_the_named_instrument_pass_does_not_double_a_pinpointed_reference():
    # "Art. 15 DSGVO" is one reference. Reporting the instrument again as a bare
    # mention of itself would double every pinpointed citation in the corpus.
    cites = [c for c in german_citations("Nach Art. 15 DSGVO besteht ein Anspruch.")
             if c.candidate_id == "32016R0679"]
    assert len(cites) == 1 and cites[0].pinpoint == "Article 15"


# -- the shorthand a German judgment defines ----------------------------------
def test_a_cued_shorthand_is_trusted_across_the_instruments_own_title():
    # VG Köln, ECLI:DE:VGK:2025:0617.1L1930.22.00. The generic rule looks 90 characters
    # past the citation and refuses a gap containing a 12-letter word; a German EU
    # citation's official title is 120 characters of exactly that, so the judgment's own
    # name for the instrument it is about was lost, and with it every later "der EKEK".
    text = ("Streitig sind Normen des TKG sowie der damit umgesetzten Richtlinie (EU) "
            "2018/1972 des Europäischen Parlaments und des Rates vom 11. Dezember 2018 "
            "über den europäischen Kodex für die elektronische Kommunikation (fortan: "
            "EKEK). Der EKEK sieht in Art. 3 Abs. 2 vor, dass dies gilt.")
    cites = extract_citations(text)
    assert any(c.candidate_id == "32018L1972" and c.pinpoint == "Article 3" for c in cites)


def test_a_shorthand_the_document_defines_beats_the_phantom_german_law():
    # "Art. 4 MiCAR" minted de/gesetz/micar — a German statute that does not exist —
    # beside the regulation the same document had just named in full.
    cites = {c.method: c for c in extract_citations(
        "Die Verordnung (EU) 2023/1114 des Europäischen Parlaments und des Rates vom "
        "31. Mai 2023 über Märkte für Kryptowerte (im Folgenden: „MiCAR“) gilt. "
        "Nach Art. 4 Abs. 1 lit. b MiCAR ist dies zulässig.")}
    assert cites["de_shorthand_law"].candidate_id == "32023R1114"
    assert cites["de_shorthand_law"].pinpoint == "Article 4(1)(b)"
    assert not any(c.candidate_id == "de/gesetz/micar" for c in cites.values())


def test_an_uncued_parenthetical_across_a_long_title_still_defines_nothing():
    # The gate is traded, not removed: the explicit cue is what earns the long window.
    # An OJ reference in the same position must not become the instrument's short name.
    text = ("Die Richtlinie (EU) 2019/633 des Europäischen Parlaments und des Rates vom "
            "17. April 2019 über unlautere Handelspraktiken (ABl. L 111 vom 25.4.2019, "
            "S. 59) gilt. Die ABl regelt dies.")
    assert not [c for c in extract_citations(text) if c.candidate_id == "de/gesetz/abl"]
    assert not [c for c in extract_citations(text) if c.method == "shorthand"]


def test_german_quotation_marks_define_a_shorthand():
    # „…“ is the German house style and appeared nowhere in the shorthand patterns, so
    # a definition written the ordinary German way matched nothing.
    text = ("Die Verordnung (EU) 2023/1114 des Europäischen Parlaments und des Rates vom "
            "31. Mai 2023 über Märkte für Kryptowerte (im Folgenden: „Krypto-VO“) gilt. "
            "Die Krypto-VO verlangt in Art. 9 Auskunft.")
    assert any(c.candidate_id == "32023R1114" and c.method == "shorthand"
               for c in extract_citations(text))


# -- the judgment body ---------------------------------------------------------
_HTML = """<h2>Tenor</h2>
<div><dl class="RspDL"><dt/><dd><p>Die Klage wird abgewiesen.</p></dd></dl></div>
<h2>Gründe</h2>
<div>
 <dl class="RspDL"><dt><a name="rd_1">1</a></dt><dd><p>Der Kläger begehrt Auskunft.</p></dd></dl>
 <dl class="RspDL"><dt><a name="rd_2">2</a></dt><dd><p>Die Klage ist unbegründet.</p></dd></dl>
</div>"""


def test_the_randnummer_is_the_segment_label():
    # "BGH, Urt. v. 5.9.2018 – 2 StR 454/17, Rn. 12" points at a paragraph. A chunk
    # that has lost its number cannot be cited back, so the Randnummer is the unit.
    parsed = parse_olg(_HTML)
    labels = [s.label for s in parsed.segments if s.kind == "paragraph"]
    assert "1" in labels and "2" in labels
    assert parsed.metadata["zones"] == ["Tenor", "Gründe"]
    # every segment indexes exactly its own text
    assert all(parsed.text[s.char_start:s.char_end].strip() for s in parsed.segments)
    # …and the zone heading is not also emitted as a body paragraph
    assert parsed.text.count("Gründe") == 1


def test_the_markdown_rendition_is_the_fallback_and_only_the_fallback():
    md = "## Tenor\n\n:   Die Klage wird abgewiesen.\n\n## Gründe\n\n1\n:   Der Kläger begehrt Auskunft."
    from_html = parse_olg(_HTML, markdown=md)
    assert [s.label for s in from_html.segments if s.kind == "paragraph"] == ["Tenor", "1", "2"]
    from_md = parse_olg("", markdown=md)
    assert "Der Kläger begehrt Auskunft." in from_md.text
    assert [s.label for s in from_md.segments if s.kind == "paragraph"] == ["Tenor", "1"]


def test_unbalanced_markup_still_yields_a_judgment():
    # The Länder registers publish stray and unclosed tags often enough that an XML
    # parse fails on a slice of the corpus, and a decision lost to a stray tag is a
    # decision the corpus does not hold.
    parsed = parse_olg("<h2>Gründe<div><p>Text eins.<p>Text zwei.</div>")
    assert "Text eins." in parsed.text and "Text zwei." in parsed.text


# -- the record ----------------------------------------------------------------
def test_a_decision_with_an_ecli_is_keyed_by_it_so_it_dedups_against_de_rii():
    assert stable_id_for("ECLI:DE:BGH:2018:050918U2STR454.17.0", "bgh-2018-09-05") \
        == "ECLI:DE:BGH:2018:050918U2STR454.17.0"
    assert stable_id_for("", "ovg-nrw-2024-01-15-13-a-123420") \
        == "de/openlegaldata/ovg-nrw-2024-01-15-13-a-123420"


def test_an_unknown_court_is_recovered_from_the_ecli():
    # 8,433 rows carry the placeholder court "Unknown court", and their ECLIs say which
    # court it really was. The difference is a facet row called "Unknown court" versus
    # the judgment appearing under its own court.
    assert _court_display({"name": "Unknown court", "slug": "unknown"},
                          "ECLI:DE:BVerwG:2019:120919U1C2.19.0") == "Bundesverwaltungsgericht"
    assert _court_display({"name": "Verwaltungsgericht Köln", "slug": "vg-koln"}, "") \
        == "Verwaltungsgericht Köln"


def test_a_luxembourg_decision_mirrored_in_the_register_is_not_german_case_law():
    adapter = DeOpenLegalDataAdapter(path="/nonexistent")
    row = {"slug": "eugh-2020-07-16-c-31118", "ecli": "ECLI:EU:C:2020:559",
           "court": {"slug": "eugh", "name": "Europäischer Gerichtshof"},
           "date": "2020-07-16", "file_number": "C-311/18"}
    assert adapter._stub(row, "") is None
    assert DeOpenLegalDataAdapter(path="/nonexistent", include_eu=True)._stub(row, "") is not None


def test_the_upstream_law_markers_become_resolvable_edges():
    markers = ('[{"start": 10, "end": 20, "text": "\\u00a7 171b GVG", "references": '
               '[{"ref_type": "RefType.LAW", "book": "gvg", "section": "171b"}]}]')
    rels = _law_relations(markers, text_len=500)
    assert [(r.dst_id, r.dst_anchor) for r in rels] == [("de/gesetz/gvg", "§ 171b")]
    assert rels[0].context_start == 10
    # an offset past the end of the text we derived anchors to nothing, so it is dropped
    assert _law_relations(markers, text_len=5)[0].context_start is None


def test_a_range_marker_is_not_folded_into_a_lettered_provision():
    # Upstream folds "§§ 611 ff. BGB" into the section slug "611f", which reads as one
    # of the § 611a family — a different provision from the one cited.
    assert _section_anchor("611f", "§§ 611 ff BGB") == "611"
    assert _section_anchor("611a", "§ 611a BGB") == "611a"
    assert _section_anchor("171b", "§ 171b GVG") == "171b"


def test_the_landing_url_is_minted_locally_because_ecli_resolvers_do_not_answer():
    # There is no working resolver for a Länder ECLI — ECLI:DE:OVGNRW:… resolves
    # nowhere — so the link a reader (or a static export) follows has to come from the
    # slug the register publishes alongside it.
    adapter = DeOpenLegalDataAdapter(path="/nonexistent")
    stub = adapter._stub({"slug": "ovg-nrw-2024-01-15-13-a-123420", "ecli": "",
                          "court": {"slug": "ovgnrw", "name": "Oberverwaltungsgericht "
                                                             "Nordrhein-Westfalen"},
                          "date": "2024-01-15", "file_number": "13 A 1234/20"}, "")
    assert stub.landing_url == "https://de.openlegaldata.io/case/ovg-nrw-2024-01-15-13-a-123420"


def test_the_bulk_walk_does_not_write_the_api_cursor():
    # One cursor space per source. The API path's cursor is created_date — when the
    # register ingested the decision — and the bulk path's stamp would be a DECISION
    # date, so letting the local walk set it would leave the weekly watch comparing a
    # date against a datetime from a different clock.
    adapter = DeOpenLegalDataAdapter(path="/nonexistent")
    row = {"slug": "vg-koln-2025-06-17-1-l-193022", "ecli": "", "date": "2025-06-17",
           "court": {"slug": "vg-koln", "name": "Verwaltungsgericht Köln"},
           "file_number": "1 L 1930/22"}
    assert adapter._stub(row, "").hints["watermark"] is None
    api_row = dict(row, created_date="2025-06-20T03:15:03Z")
    assert adapter._stub(api_row, "", watermark="2025-06-20T03:15:03")\
        .hints["watermark"] == "2025-06-20T03:15:03"
