"""National DPA guidance registers and the citation grammars they need.

Each authority's index parser gets one fixture cut from the live page, because the
failure mode these guard against is not "the parse crashed" but "the parse quietly
returned half the register": the CNIL renders every item twice, and the DSK, the Belgian
APD and the Garante each publish several series through one view.
"""

from __future__ import annotations

from raglex.adapters.eu_dpa_guidance import (
    GARANTE_TYPE_IDS,
    _slug,
    aepd_stubs,
    cnil_stubs,
    datatilsynet_stubs,
    dsk_stubs,
    garante_search_url,
    garante_stubs,
    garante_text,
    gba_stubs,
    month_date,
    pdf_month,
)
from raglex.adapters.registry import ADAPTERS, SOURCE_INFO, get_adapter
from raglex.citations.extractor import extract_citations

# ---------------------------------------------------------------------------
# France
# ---------------------------------------------------------------------------

CNIL_LISTING = """
<div class="view-content">
 <div class="views-row">
  <div class="row liste"><div class="list-inner">
    <div class="collection"><span class="collection__type">Recommandations</span></div>
    <h3 class="ctn-gen-liste-titre">
      <a href="https://cnil.fr/sites/default/files/2026-06/recommandation_localisation.pdf">
      <div class="field-title">Recommandation localisation</div></a></h3>
  </div></div>
  <div class="grid"><div class="grid-inner">
    <div class="collection"><span class="collection__type">Recommandations</span></div>
    <h3 class="ctn-gen-liste-titre">
      <a href="https://cnil.fr/sites/default/files/2026-06/recommandation_localisation.pdf">
      <div class="field-title">Recommandation localisation</div></a></h3>
  </div></div>
 </div>
 <div class="views-row"><div class="row liste"><div class="list-inner">
    <div class="collection"><span class="collection__type">Guide</span></div>
    <h3 class="ctn-gen-liste-titre">
      <a href="/sites/default/files/2025-11/guide_securite.pdf">
      <div class="field-title">Guide de la sécurité</div></a></h3>
 </div></div></div>
</div>""".encode()


def test_cnil_grid_and_list_renderings_are_one_document():
    rows = cnil_stubs(CNIL_LISTING)
    # each .views-row prints the item twice; a naive link sweep doubles the corpus
    assert len(rows) == 2
    assert rows[0]["title"] == "Recommandation localisation"
    assert rows[0]["category"] == "Recommandations"
    # the médiathèque prints no date — the upload folder is the only one exposed
    assert rows[0]["date"].isoformat() == "2026-06-01"
    assert rows[1]["category"] == "Guide"
    assert rows[1]["url"].startswith("https://www.cnil.fr/sites/")


def test_upload_month_is_the_only_date_several_authorities_publish():
    assert pdf_month("https://x/sites/default/files/2026-06/a.pdf").isoformat() == "2026-06-01"
    assert pdf_month("https://x/files/2026-13/a.pdf") is None  # not a month
    assert pdf_month("https://x/a.pdf") is None


# ---------------------------------------------------------------------------
# Spain
# ---------------------------------------------------------------------------

def test_aepd_teaser_gives_title_pdf_and_a_real_publication_date():
    html = b"""<article class="node node--type-recurso-multimedia node--view-mode-teaser">
      <div class="group-text">
        <div class="field field--name-title"><h2>Calidad de los datos con IA</h2></div>
        <div class="field field--name-fichero field__item">
          <a href="/guias/calidad-datos-inteligencia-artificial.pdf">Ver documento</a></div>
        <div class="field field--name-fecha-publicacion">
          <time class="datetime" datetime="2026-07-21T07:00:52Z">21 de Julio de 2026</time>
        </div>
      </div></article>"""
    rows = aepd_stubs(html)
    assert rows[0]["title"] == "Calidad de los datos con IA"
    assert rows[0]["url"].endswith("/guias/calidad-datos-inteligencia-artificial.pdf")
    assert rows[0]["date"].isoformat() == "2026-07-21"


# ---------------------------------------------------------------------------
# Denmark
# ---------------------------------------------------------------------------

def test_datatilsynet_hub_keeps_the_topic_heading_and_skips_broken_links():
    html = b"""<main>
      <h3>Politi og retsvaesen</h3>
      <p><a href="/Media/F/2/Udveksling med politiet.pdf">Udveksling med politiet</a>
         <a href="https://www.datatilsynet.dk/regler-og-vejledning/politi">Saerlige regler</a>
         <a href="https://file:///C:/Users/B063755/Downloads/A20170041030.pdf">Betaenkning</a></p>
      <h3>Registreredes rettigheder</h3>
      <p><a href="/Media/A/1/Indsigtsret.pdf">Indsigtsret</a>
         <a href="/Media/A/1/Indsigtsret.pdf">Indsigtsret igen</a></p>
    </main>"""
    rows = datatilsynet_stubs(html)
    assert [(row["title"], row["category"]) for row in rows] == [
        ("Udveksling med politiet", "Politi og retsvaesen"),
        ("Indsigtsret", "Registreredes rettigheder"),
    ]
    # the sub-page link is not a document, and someone's local Downloads folder is
    # an editing accident rather than a URL worth fetching
    assert all(".pdf" in row["url"] and "file:" not in row["url"] for row in rows)


# ---------------------------------------------------------------------------
# Germany
# ---------------------------------------------------------------------------

def test_dsk_item_splits_the_printed_month_from_the_title_and_keeps_its_annex():
    html = b"""<ul class="thumbnail_list">
      <li class="thumbnail head"><div class="headline_table">2026</div></li>
      <li class="thumbnail odd">
        <a href="../media/oh/OH_Selbstauskuenfte_V2.pdf"><div class="headline_table">
          <div class="date"><b>Januar 2026</b> - Orientierungshilfe zur Einholung von
          Selbstauskuenften (V2.0)</div></div></a>
        <div class="hint-box"><small>
          <a href="../media/oh/OH_Selbstauskuenfte_V2_Anhang.pdf">Anhang</a></small></div>
      </li></ul>"""
    rows = dsk_stubs(html, "https://www.datenschutzkonferenz-online.de/orientierungshilfen.html",
                     "Orientierungshilfe")
    assert len(rows) == 1  # the year divider is not a document
    assert rows[0]["title"].startswith("Orientierungshilfe zur Einholung")
    assert rows[0]["date"].isoformat() == "2026-01-01"
    assert rows[0]["url"].endswith("/media/oh/OH_Selbstauskuenfte_V2.pdf")
    # the annex is part of the same document, not a second one
    assert rows[0]["extra_pdfs"] == [
        "https://www.datenschutzkonferenz-online.de/media/oh/OH_Selbstauskuenfte_V2_Anhang.pdf"]


def test_month_names_per_language():
    assert month_date("Dezember 2025", "de").isoformat() == "2025-12-01"
    assert month_date("Provvedimento del 17 aprile 2026 - Linee guida", "it").isoformat() \
        == "2026-04-17"
    assert month_date("Januar 2026", "it") is None   # right shape, wrong language
    assert month_date("", "de") is None


# ---------------------------------------------------------------------------
# Belgium
# ---------------------------------------------------------------------------

def test_gba_search_result_records_year_precision_rather_than_a_fake_date():
    html = b"""<div id="search-result">
      <div class="media"><div class="media-body">
        <h3 class="media-title"><a href="/publications/recommandation-01-2025.pdf">
          Recommandation 01/2026 marketing direct</a></h3>
        <span class="media-date">2025</span>
        <div class="media-description">Recommandation d'initiative 01/2026.</div>
      </div></div></div>"""
    rows = gba_stubs(html)
    assert rows[0]["title"] == "Recommandation 01/2026 marketing direct"
    # the register prints a year; 1 January is a placeholder and is marked as one
    assert rows[0]["date"].isoformat() == "2025-01-01"
    assert rows[0]["date_precision"] == "year"
    assert rows[0]["summary"].startswith("Recommandation d'initiative")


# ---------------------------------------------------------------------------
# Italy
# ---------------------------------------------------------------------------

def test_garante_is_keyed_on_the_doc_web_number_practitioners_cite():
    html = b"""<div class="card-risultato">
      <div class="label-risultato"><a href="/home/ricerca/-/search/tipologia/Linee%20guida"
        >Linee guida</a></div>
      <div class="data-risultato"><p>17/04/2026</p></div>
      <div class="d-flex"><div><strong>
      <a class="titolo-risultato" href="/web/guest/home/docweb/-/docweb-display/docweb/10241943">
      Provvedimento del 17 aprile 2026 - Linee Guida tracking pixel [10241943]</a>
      </strong>
      <p class="estratto-risultato">Il Garante adotta le linee guida[...]</p></div></div>
      <a href="/home/ricerca/-/search/argomento/Marketing">Marketing</a>
      <a href="/home/ricerca/-/search/argomento/Cookies">Cookies</a>
      </div>
      <div class="card-risultato"><div class="d-flex"><div><strong>
      <a class="titolo-risultato" href="/web/guest/home/docweb/-/docweb-display/docweb/10241943">
      duplicate</a></strong></div></div></div>"""
    rows = garante_stubs(html)
    assert len(rows) == 1
    assert rows[0]["docweb"] == "10241943"
    assert rows[0]["title"] == "Provvedimento del 17 aprile 2026 - Linee Guida tracking pixel"
    assert rows[0]["date"].isoformat() == "2026-04-17"
    # the card's own metadata, which the docweb page does not expose
    assert rows[0]["category"] == "Linee guida"
    assert rows[0]["topics"] == ["Marketing", "Cookies"]
    assert rows[0]["summary"].startswith("Il Garante adotta")
    url, params = garante_search_url("10516", 3)
    assert params["_g_gpdp5_search_GGpdp5SearchPortlet_cur"] == "3"
    assert params["_g_gpdp5_search_GGpdp5SearchPortlet_idsTipologia"] == "10516"


def test_garante_dates_a_card_whose_title_carries_no_date():
    """About a third of the archive is titled "Parere su istanza di accesso civico" with
    no date in it. Reading the date out of the title alone left those undated."""
    html = b"""<div class="card-risultato">
      <div class="data-risultato"><p>03/07/2026</p></div>
      <a class="titolo-risultato" href="/web/guest/home/docweb/-/docweb-display/docweb/10270087"
      >Parere su istanza di accesso civico [10270087]</a></div>"""
    assert garante_stubs(html)[0]["date"].isoformat() == "2026-07-03"


def test_garante_asks_for_every_tipologia_because_the_parent_does_not_expand():
    """``10533`` is the facet's "Provvedimenti (13702)" node, and querying it alone
    returns about eighty measures — the ones tagged with the parent itself. The other
    13,600 carry a child id and are absent, with no error and an ordinary-looking
    results page. The first version of this adapter asked for an id that is not in the
    tree at all and harvested 38 documents out of 13,800."""
    ids = GARANTE_TYPE_IDS.split(",")
    assert "10533" in ids and "10498" in ids and "10526" in ids
    assert "10516" in ids                      # linee guida, the old adapter's one hit
    assert "10515" not in ids                  # the id that was never in the tree
    assert len(ids) > 30
    # the whole tree in ONE query, so the walk is one ordered series with one cursor
    adapter = get_adapter("it-garante")
    series = list(adapter._series(1))
    assert len(series) == 1
    _url, params, _ctx = next(series[0])
    assert params["_g_gpdp5_search_GGpdp5SearchPortlet_idsTipologia"] == GARANTE_TYPE_IDS


def test_garante_resumes_on_the_page_its_checkpoint_names():
    """1,380 pages: a resumed backfill must not re-request all of them. Safe here only
    because the page size is fixed and the walk is a single series, so an item's
    position determines its page exactly — and ``resume_floor`` still backs off one
    page, so the restart is early rather than late."""
    adapter = get_adapter("it-garante", start_offset=5000)
    _url, params, _ctx = next(next(iter(adapter._series(1))))
    # 5000 - one page = 4990 → page 499 (0-based) → the portlet's 1-based cursor 500
    assert params["_g_gpdp5_search_GGpdp5SearchPortlet_cur"] == "500"


def test_garante_keep_current_asks_the_server_for_a_window():
    """The portlet's dataInizio/dataFine really filter (a January 2020 window returns
    January 2020), so keep-current is a handful of requests rather than 1,380."""
    adapter = get_adapter("it-garante", watch_days=180)
    list(adapter.discover("2026-08-01", max_pages=1))[:0]
    _url, params, _ctx = next(next(iter(adapter._series(1))))
    assert params["_g_gpdp5_search_GGpdp5SearchPortlet_dataInizio"] == "2026-02-02"


def test_garante_takes_the_richest_portlet_not_the_first():
    """Liferay renders navigation through the same classes as content, so the first
    match is a social-links block with 56 characters in it."""
    html = b"""<div class="testo">Seguici su linkedin instagram</div>
      <div id="div-to-print">Il Garante, visto il Regolamento (UE) 2016/679, adotta le
      seguenti linee guida in materia di tracking pixel nelle comunicazioni.</div>"""
    text = garante_text(html)
    assert "linee guida in materia di tracking pixel" in text
    assert "Seguici su" not in text


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------

def test_ids_keep_the_distinguishing_folder_and_drop_the_generic_one():
    # the CNIL's upload month distinguishes two files that share a name
    assert _slug("https://x/sites/default/files/2026-06/guide.pdf") == "2026-06-guide"
    assert _slug("https://x/sites/default/files/2025-11/guide.pdf") == "2025-11-guide"
    # …while a content folder that says nothing is left out
    assert _slug("https://www.aepd.es/guias/calidad-datos.pdf") == "calidad-datos"
    assert _slug("https://x/") == "document"


# ---------------------------------------------------------------------------
# grammars
# ---------------------------------------------------------------------------

def test_german_ds_gvo_spelling_is_the_gdpr_and_lit_is_a_pincite():
    """DS-GVO is standard German usage. The law pattern stopped at the hyphen, minting
    a phantom ``de/gesetz/ds`` that collected every GDPR article in the German corpus;
    and ``lit.`` was missing from the sub-provision vocabulary, so a reference carrying
    one could not match at all — the pattern has to run through to a law abbreviation."""
    cites = extract_citations("Nach Art. 6 Abs. 1 lit. f DS-GVO ist dies zulässig.")
    assert [(c.candidate_id, c.pinpoint) for c in cites] == [
        ("32016R0679", "Article 6(1)(f)")]
    assert [(c.candidate_id, c.pinpoint) for c in extract_citations(
        "Art. 5 Abs. 1 Buchst. a DSGVO")] == [("32016R0679", "Article 5(1)(a)")]
    assert not any(c.candidate_id == "de/gesetz/ds" for c in cites)


def test_italian_garante_forms_resolve_only_once_the_instrument_is_named():
    """``il Regolamento`` and ``il Codice`` are how the Garante refers back to
    instruments it named in full at the top. Corpus-wide both are ambiguous — "il
    Codice" is the Codice del consumo in an AGCM bulletin — so each needs its own
    introduction in the same document."""
    text = ("Visto il regolamento (UE) 2016/679 e il codice in materia di protezione "
            "dei dati personali, si richiama l'art. 5, par. 1, lett. a), del "
            "Regolamento nonché l'art. 122 del Codice.")
    pairs = {(c.candidate_id, c.pinpoint) for c in extract_citations(text)}
    assert ("32016R0679", "Article 5(1)(a)") in pairs
    assert ("it/dlgs/2003/196", "Articolo 122") in pairs
    # without the introduction the same sentence must not mint either edge
    bare = extract_citations("Si richiama l'art. 122 del Codice e l'art. 5 del Regolamento.")
    assert not any(c.method in ("it_gdpr_article", "it_privacy_code_article")
                   for c in bare)


def test_spanish_dotted_and_spelled_out_articles_are_the_same_pincite():
    for text in ("el artículo 5.1.d) del RGPD",
                 "el artículo 5, apartado 1, letra d), del Reglamento (UE) 2016/679"):
        assert ("32016R0679", "Article 5(1)(d)") in [
            (c.candidate_id, c.pinpoint) for c in extract_citations(text)], text
    assert any(c.candidate_id == "es:ley:lo-3-2018"
               for c in extract_citations("el artículo 11 de la LOPDGDD"))


def test_danish_splits_the_regulation_from_the_domestic_acts():
    cites = extract_citations(
        "Efter artikel 6, stk. 1, litra f, i databeskyttelsesforordningen og "
        "databeskyttelseslovens § 22, stk. 2, nr. 4.")
    pairs = {(c.candidate_id, c.pinpoint) for c in cites}
    # Formex anchor for the Regulation, the section sign for the Act
    assert ("32016R0679", "Article 6(1)(f)") in pairs
    assert ("dk:lov:databeskyttelsesloven", "§ 22, stk. 2, nr. 4") in pairs
    # a bare "forordningen" is any EU regulation, not this one
    assert not any(c.candidate_id == "32016R0679" for c in extract_citations(
        "Reglerne i artikel 5 i forordningen om markeder for kryptoaktiver."))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_every_new_dpa_source_is_registered_with_its_own_country():
    expected = {
        "fr-cnil-guidance": "FR", "es-aepd-guias": "ES", "dk-datatilsynet": "DK",
        "de-dsk": "DE", "be-gba": "BE", "it-garante": "IT",
    }
    for key, jurisdiction in expected.items():
        assert key in ADAPTERS
        assert SOURCE_INFO[key].jurisdiction == jurisdiction
        assert SOURCE_INFO[key].kind == "guidance"
        assert get_adapter(key).source == key


def test_a_multi_series_register_does_not_stop_at_the_first_exhausted_series():
    """The DSK, the Belgian APD and the Garante each publish several series through one
    view. An empty page means that SERIES is done; treating it as the end of the crawl
    silently dropped every series after the first."""
    # The Garante is deliberately NOT here any more: its whole tipologia tree is one
    # comma-separated query, which is what gives the 1,380-page walk a single cursor.
    for key, expected in (("de-dsk", 3), ("be-gba", 3), ("es-aepd-guias", 2)):
        adapter = get_adapter(key)
        assert len(list(adapter._series(1))) == expected, key
