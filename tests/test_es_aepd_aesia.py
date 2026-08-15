"""Spain: the AEPD's enforcement register, AESIA's AI Act guides, and the grammar.

Three things are under test here and they fail in different ways:

* the **grammar**, whose failure is a citation that exists in the text and not in the
  graph — the most common shape of silent incompleteness in this corpus;
* the **untethered pincite**, which is the same failure one step later: the reference is
  found but has nothing to attach to, so a guide entirely about Article 9 records no
  edge to Article 9;
* the **adapters**, whose failure is a truncated backfill that reports itself complete.
"""

from __future__ import annotations

import re

import pytest

from raglex.adapters._governing_instrument import AI_ACT, GDPR, default_instrument
from raglex.adapters.es_aepd import (
    AEPDResolutionsAdapter,
    appealed_decision,
    feed_items,
    file_number,
    last_page,
    listing_items,
    procedure,
    stable_id,
)
from raglex.adapters.es_aesia import guide_slug, listing_items as aesia_items
from raglex.adapters.registry import ADAPTERS, INCREMENTAL_MODE, SOURCE_INFO, get_adapter
from raglex.citations.extractor import extract_citations
from raglex.citations.spanish import es_law_id, spanish_citations


def pins(text: str) -> set[tuple[str | None, str | None]]:
    return {(c.candidate_id, c.pinpoint) for c in extract_citations(text)}


# ---------------------------------------------------------------------------
# the provision ladder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "el artículo 5.1.d) del RGPD",
    "el artículo 5, apartado 1, letra d), del Reglamento (UE) 2016/679",
    "el art. 5.1 d) del Reglamento General de Protección de Datos",
    "el artículo 5, apdo. 1, let. d) del RGPD",
])
def test_every_spelling_of_one_provision_lands_on_one_anchor(text):
    """Spain writes a subdivision with dots as readily as with words, and mixes the two
    inside a single citation. Reading only one notation splits a provision's citers in
    half — and the half that is lost is whichever the document happened to prefer."""
    assert ("32016R0679", "Article 5(1)(d)") in pins(text), text


def test_the_ladder_goes_all_the_way_down():
    assert ("32016R0679", "Article 83(5)(b)") in pins("el artículo 83.5.b) del RGPD")
    # a numbered definition, which Spanish writes with a closing parenthesis only
    assert ("32016R0679", "Article 4(11)") in pins("el artículo 4.11) del RGPD")
    # bis/ter belong to the article NUMBER: Article 43 bis is not a part of Article 43
    assert ("32016R0679", "Article 43 bis") in pins(
        "el artículo 43 bis del Reglamento (UE) 2016/679")


def test_a_list_gives_each_article_its_own_edge_and_a_range_its_members():
    """"artículos 15 a 22 del RGPD" is the AEPD's standard recital of the data-subject
    rights and is a citation of all eight, not of the two endpoints."""
    found = pins("los artículos 15 a 22 del RGPD")
    assert {("32016R0679", f"Article {n}") for n in range(15, 23)} <= found
    # …and the subdivisions of the first member do not leak onto the rest
    listed = pins("los artículos 6.1 y 9 del RGPD")
    assert ("32016R0679", "Article 6(1)") in listed
    assert ("32016R0679", "Article 9") in listed
    assert ("32016R0679", "Article 9(1)") not in listed
    # a dotted rung rides on its own token, so each member of a list keeps its own
    both = pins("artículos 6.1.f) y 9.2.a) del RGPD")
    assert {("32016R0679", "Article 6(1)(f)"),
            ("32016R0679", "Article 9(2)(a)")} <= both


def test_annexes_recitals_and_disposiciones():
    assert ("32024R1689", "Annex III, point 2") in pins(
        "el anexo III, punto 2 del Reglamento (UE) 2024/1689")
    # the Commission's Spanish drafting prefers the reverse order for the same provision
    assert ("32024R1689", "Annex III, point 2") in pins(
        "el punto 2 del anexo III del Reglamento (UE) 2024/1689")
    assert ("32016R0679", "Recital 47") in pins("el considerando 47 del RGPD")
    # numbered in ordinal WORDS, which no other language in the corpus does
    assert ("es:ley:lo-3-2018", "Disposición adicional primera") in pins(
        "la disposición adicional primera de la LOPDGDD")


def test_both_word_orders_because_spanish_uses_both():
    """"el RGPD, en sus artículos 13 y 14" is the AEPD's standard opening. A grammar
    that only knew the pinpoint-first order read it as no citation at all."""
    found = pins("el RGPD, en sus artículos 13 y 14")
    assert {("32016R0679", "Article 13"), ("32016R0679", "Article 14")} <= found
    # …including with the law's date wedged in, which is how Spain names a statute
    assert ("es:ley:l-39-2015", "Artículo 77") in pins(
        "la Ley 39/2015, de 1 de octubre, en su artículo 77")


def test_the_possessive_is_what_keeps_an_article_from_being_given_to_two_laws():
    """"la Ley Orgánica 3/2018 modifica el artículo 77 de la Ley 39/2015" states one
    provision of one law. The host-first pattern requires ``su``/``sus`` for exactly
    this reason — the mistake the French grammar records having made."""
    found = pins("la Ley Orgánica 3/2018 modifica el artículo 77 de la Ley 39/2015")
    assert ("es:ley:l-39-2015", "Artículo 77") in found
    assert ("es:ley:lo-3-2018", "Artículo 77") not in found


def test_a_spanish_act_keeps_the_spanish_notation():
    """The EU anchor has to be Formex because that is how the held Articles are
    labelled. A Spanish act is not held, so the anchor's only job is to make every
    spelling of one provision group with the others — hence one dotted form, always."""
    for text in ("el artículo 72.1.b) de la LOPDGDD",
                 "el artículo 72, apartado 1, letra b), de la LOPDGDD"):
        assert ("es:ley:lo-3-2018", "Artículo 72.1.b)") in pins(text), text


def test_the_id_encodes_the_form_of_the_instrument():
    """Ley 3/2018 and Ley Orgánica 3/2018 are different laws published the same year."""
    assert es_law_id("Ley Orgánica", "3", "2018") == "es:ley:lo-3-2018"
    assert es_law_id("Ley", "3", "2018") == "es:ley:l-3-2018"
    assert es_law_id("Real Decreto", "1720", "2007") == "es:ley:rd-1720-2007"
    assert es_law_id("Real Decreto-ley", "14", "2019") == "es:ley:rdl-14-2019"
    assert es_law_id("Real Decreto Legislativo", "1", "2007") == "es:ley:rdleg-1-2007"


# ---------------------------------------------------------------------------
# not everything that looks Spanish is
# ---------------------------------------------------------------------------

def test_a_short_ambiguous_name_needs_spanish_around_it():
    """This pass runs over every document in the corpus. "CE" is the Constitución in
    Madrid and the Conseil d'État in Paris; "CC" is the Código Civil and a great many
    other things. Both are read only inside an explicit article frame, and only with
    Spanish vocabulary beside them."""
    assert ("es:ley:constitucion-1978", "Artículo 18.4") in pins(
        "se vulnera el artículo 18.4 CE, según reiterada doctrina constitucional")
    assert not [c for c in spanish_citations(
        "L'art. 12 CE a jugé que la loi du 6 janvier 1978 s'applique.")]
    assert not [c for c in spanish_citations(
        "See art. 5 CC for the position under English law.")]
    # a bare "CE" is never the Constitution, however Spanish the sentence
    assert not [c for c in spanish_citations("conforme a la CE y a la ley española")
                if c.candidate_id == "es:ley:constitucion-1978"]


def test_an_unambiguous_acronym_does_not_need_propping_up():
    """RGPD is the GDPR in four languages and an initialism of nothing else, so
    requiring a Spanish word beside it only loses citations. RIA is a Spanish river and
    an English regulatory impact assessment, and stays guarded."""
    assert ("32016R0679", None) in pins("Guía de adaptación al RGPD")
    assert not [c for c in spanish_citations("a RIA process for the department")]


# ---------------------------------------------------------------------------
# the untethered pincite — the point of citation_default_instrument
# ---------------------------------------------------------------------------

def test_a_bare_spanish_article_carries_forward_to_the_named_instrument():
    """An AESIA guide names the Regulation once and then writes "el artículo 9" for
    forty pages. Before Spanish cues were added to the carry-forward pass, none of
    those produced an edge: a guide entirely about Article 9 cited Article 9 nowhere."""
    found = pins(
        "Esta guía desarrolla el Reglamento (UE) 2024/1689. "
        "El artículo 9 exige un sistema de gestión de riesgos. "
        "Véase el anexo III, punto 2, el punto 5 del anexo IV y el considerando 27. "
        "El artículo 6.1.f) no resulta aplicable.")
    assert {("32024R1689", "Article 9"),
            ("32024R1689", "Annex III, point 2"),
            ("32024R1689", "Annex IV, point 5"),
            ("32024R1689", "Recital 27"),
            ("32024R1689", "Article 6(1)(f)")} <= found


def test_a_carried_forward_pincite_is_rendered_for_the_host_it_landed_on():
    """The notation depends on the instrument, so it cannot be chosen before the host
    is known: ``Artículo 72.1.b)`` on a Spanish act, ``Article 72(1)(b)`` on an EU one."""
    assert ("es:ley:lo-3-2018", "Artículo 72.1.b)") in pins(
        "De acuerdo con la Ley Orgánica 3/2018. El artículo 72.1.b) tipifica la infracción.")


def test_the_documents_own_title_beats_the_registers_default():
    """A single-subject register still publishes the occasional document about another
    law, and its bare articles belong to that law, not to the register's regime."""
    assert default_instrument("Guía Vigilancia humana", AI_ACT) == AI_ACT
    assert default_instrument("Guía sobre el Reglamento (UE) 2016/679", AI_ACT) == GDPR
    assert default_instrument("Guía sobre la LOPDGDD", GDPR) == {
        "id": "es:ley:lo-3-2018", "kind": "act"}
    # two instruments in one title is not an answer; the register's regime stands
    assert default_instrument(
        "Guía sobre el RGPD y el Reglamento de Servicios Digitales", AI_ACT) == AI_ACT


# ---------------------------------------------------------------------------
# AESIA
# ---------------------------------------------------------------------------

AESIA_PAGE = b"""<div class="grid">
 <a href="https://aesia.digital.gob.es/storage/media/01-guia-introductoria-al-reglamento-de-ia.pdf"
    target="_blank" title="01 Gu\xc3\xada introductoria al reglamento de IA" class="block">x</a>
 <a href="https://aesia.digital.gob.es/storage/media/05-guia-de-gestion-de-riesgos.pdf"
    target="_blank" title="05 Gu\xc3\xada de gesti\xc3\xb3n de riesgos" class="block">x</a>
 <a href="https://aesia.digital.gob.es/storage/media/16-manual-de-checklist-de-guias-de-requisitos.pdf"
    target="_blank" title="16 Manual de checklist de gu\xc3\xadas de requisitos" class="block">x</a>
 <a href="https://aesia.digital.gob.es/storage/Checklists%20y%20ejemplos.zip">bundle</a>
</div>"""


def test_aesia_reads_the_last_card_too():
    """A flat grid of identically-nested cards with no terminator after the last one.
    Splitting on the opening marker is the only way to keep the sixteenth — the rule
    that cost the ISC page 106 of its 107 records when it was got wrong there."""
    rows = aesia_items(AESIA_PAGE)
    assert [r["series_number"] for r in rows] == ["01", "05", "16"]
    assert rows[-1]["title"] == "Manual de checklist de guías de requisitos"
    # the ZIP of checklists is a bundle of spreadsheets, not a document
    assert all(r["url"].endswith(".pdf") for r in rows)


def test_the_aesia_slug_keeps_the_series_number():
    """The guides are cited as "guía 05" in the checklists, and AESIA revises them in
    place under the same filename — so the number is identity, and a revision is a new
    version of one document rather than a second document."""
    assert guide_slug(
        "https://aesia.digital.gob.es/storage/media/05-guia-de-gestion-de-riesgos.pdf"
    ) == "05-guia-de-gestion-de-riesgos"


def test_an_empty_aesia_grid_raises_rather_than_reporting_nothing():
    """"The listing markup changed" and "AESIA withdrew every guide" must not produce
    the same outcome (AGENTS.md §3)."""
    adapter = ADAPTERS["es-aesia-guias"]()
    adapter._client = _FakeClient({"": b"<html><body>nothing here</body></html>"})
    with pytest.raises(ValueError):
        list(adapter.discover(None))


# ---------------------------------------------------------------------------
# AEPD resoluciones
# ---------------------------------------------------------------------------

AEPD_LISTING = b"""<article class="node node--type-resolucion-reclamacion node--promoted
    node--view-mode-teaser clearfix">
  <div class="field field--name-title field--type-string"><h2>PS-00355-2025</h2></div>
  <div class="field field--name-body field--type-text-with-summary"><p>1 / 12
   Expediente N.\xc2\xba: EXP202401234 RESOLUCI\xc3\x93N DE PROCEDIMIENTO SANCIONADOR</p></div>
  <div class="field field--name-fichero"><div class="field__item">
    <a href="/documento/ps-00355-2025.pdf">PS-00355-2025</a></div></div>
  <div class="field field--name-fecha-firma"><div class="field__item">
    <time datetime="2025-11-04T00:00:00Z">4 de Noviembre de 2025</time></div></div>
</article>
<article class="node node--type-resolucion-reclamacion node--view-mode-teaser clearfix">
  <div class="field field--name-title field--type-string"><h2>REPOSICION-PS-00503-2024</h2></div>
  <div class="field field--name-fichero"><div class="field__item">
    <a href="/documento/reposicion-ps-00503-2024.pdf">x</a></div></div>
  <div class="field field--name-fecha-firma"><div class="field__item">
    <time datetime="2026-07-23T00:00:00Z">23 de Julio de 2026</time></div></div>
</article>
<nav aria-label="Paginaci\xc3\xb3n"><ul class="pagination">
  <li><a href="?page=1" class="page-link">2</a></li>
  <li class="pager__item--last"><a href="?page=4689">Last</a></li>
</ul></nav>"""

AEPD_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Resoluciones</title>
<item><title>PD-00110-2026</title>
  <link>https://www.aepd.es/documento/pd-00110-2026.pdf</link>
  <description>RESOLUCI\xc3\x93N DE PROCEDIMIENTO DE DERECHOS</description>
  <pubDate/></item>
</channel></rss>"""


def test_aepd_listing_reads_the_promoted_rows_too():
    """Deeper pages add a ``node--promoted`` class to every article. Matching the whole
    class attribute returned nothing from page 2 onwards — 46,890 documents behind a
    parse that looked like it worked on page 1."""
    rows = listing_items(AEPD_LISTING)
    assert [r["title"] for r in rows] == ["PS-00355-2025", "REPOSICION-PS-00503-2024"]
    assert rows[0]["date"].isoformat() == "2025-11-04"
    assert rows[0]["url"] == "https://www.aepd.es/documento/ps-00355-2025.pdf"
    assert "PROCEDIMIENTO SANCIONADOR" in rows[0]["summary"]
    assert last_page(AEPD_LISTING) == 4689


def test_the_file_number_names_the_procedure_and_the_appeal_names_its_target():
    """A REPOSICION- prefixes the number of the decision it challenges. The relation is
    in the number and nowhere else in either document, so not minting it loses it."""
    assert stable_id("PS-00355-2025") == "es/aepd/resolucion/ps-00355-2025"
    assert procedure("PS-00355-2025") == ("PS", "Procedimiento sancionador")
    assert procedure("TD-00148-2016") == ("TD", "Tutela de derechos")
    assert procedure("REPOSICION-PS-00503-2024") == ("PS", "Procedimiento sancionador")
    assert appealed_decision("REPOSICION-PS-00503-2024") == (
        "es/aepd/resolucion/ps-00503-2024")
    assert appealed_decision("PS-00503-2024") is None
    assert file_number("ps/00355/2025") == "PS-00355-2025"


def test_the_resoluciones_feed_invents_no_date_it_was_not_given():
    """Every item's ``<pubDate/>`` is empty. A feed-discovered stub therefore carries no
    date rather than today's, which would back-date the whole archive on a re-read."""
    rows = feed_items(AEPD_FEED)
    assert rows == [{
        "title": "PD-00110-2026",
        "url": "https://www.aepd.es/documento/pd-00110-2026.pdf",
        "date": None,
        "summary": "RESOLUCIÓN DE PROCEDIMIENTO DE DERECHOS"}]


def test_a_walk_reports_a_cursor_and_the_adapter_accepts_it_back():
    adapter = AEPDResolutionsAdapter(client=_FakeClient({"": AEPD_LISTING}))
    stubs = list(adapter.discover(None, max_pages=1))
    assert [s.hints["resume_offset"] for s in stubs] == [0, 1]
    assert stubs[0].hints["feed_total"] == 46900
    # …and resuming reports the page the checkpoint named, one page early on purpose
    resumed = AEPDResolutionsAdapter(start_offset=5000)
    assert resumed.start_offset == 4990


# The 3 kB script AEPD's WAF serves with HTTP 200 instead of page 228.
AEPD_CHALLENGE = (b"<html><head></head><body><script type=\"text/javascript\">"
                  b"eval(function(p,a,c,k,e,d){...})</script>"
                  b"<!-- cookiesession8341 --></body></html>")


def test_a_waf_challenge_is_not_an_empty_page():
    """It arrives as 200 with 3 kB where a real page is 288 kB, and it carries none of
    the listing's markup. Read as an empty result set it ended the backfill 2,280
    documents into 46,900; read as a parser failure it would stop a walk that is working
    everywhere else."""
    from raglex.adapters.es_aepd import is_challenge

    assert is_challenge(AEPD_CHALLENGE)
    assert not is_challenge(AEPD_LISTING)
    # a real page the parser cannot read is NOT a challenge, however small — that is
    # the other bug, and it has to reach the parser-broken branch
    assert not is_challenge(b"<div class=\"js-pager__items\"></div>")


def test_a_blocked_page_is_retried_with_a_cache_busting_parameter():
    """The block is cached against the exact request line: three identical retries in a
    row returned the same script, a cookie jar changed nothing, and every page around it
    answered normally in the same session. One inert extra parameter answers at once."""
    class _WAF(_FakeClient):
        def get(self, url, params=None, **kwargs):
            self.calls.append((url, dict(params or {})))
            if params and params.get("page") == 1 and "items_per_page" not in params:
                return _FakeResponse(AEPD_CHALLENGE, url)
            return _FakeResponse(self.pages[str((params or {}).get("page", ""))], url)

    client = _WAF({"": AEPD_LISTING, "1": AEPD_LISTING})
    stubs = list(AEPDResolutionsAdapter(client=client).discover(None, max_pages=2))
    assert len(stubs) == 4                       # both pages, nothing lost
    assert client.calls[-1][1] == {"page": 1, "items_per_page": 10}


def test_an_unreachable_page_is_carried_to_the_end_not_raised_on_the_spot():
    """One page in 4,690. Raising where it happens cost the backfill the 44,000
    documents it had not reached yet; swallowing it would leave an unreported hole in an
    enforcement register, which is worse. So: yield everything reachable, then fail."""
    class _Blocked(_FakeClient):
        def get(self, url, params=None, **kwargs):
            self.calls.append((url, dict(params or {})))
            if params and params.get("page") == 1:
                return _FakeResponse(AEPD_CHALLENGE, url)
            return _FakeResponse(self.pages[str((params or {}).get("page", ""))], url)

    adapter = AEPDResolutionsAdapter(
        client=_Blocked({"": AEPD_LISTING, "1": AEPD_LISTING, "2": AEPD_LISTING}))
    stubs = []
    with pytest.raises(ValueError, match="unreachable after retry"):
        for stub in adapter.discover(None, max_pages=3):
            stubs.append(stub)
    # page 0 and page 2 survived; only page 1's ten are missing, and the job says so
    assert len(stubs) == 4


def test_an_unparseable_listing_page_raises_rather_than_ending_the_backfill():
    """A page inside a 4,690-page archive that parses to nothing is a broken parser, not
    the end of the register — the pager already said where that is. Returning quietly
    files a backfill that stopped at page 12 as complete."""
    adapter = AEPDResolutionsAdapter(
        client=_FakeClient({"": AEPD_LISTING, "1": b"<html>nothing</html>"}))
    with pytest.raises(ValueError):
        list(adapter.discover(None, max_pages=2))


def test_a_resolucion_declares_the_rgpd_and_records_its_procedure():
    adapter = AEPDResolutionsAdapter(client=_FakeClient({}))
    record = adapter.fetch(_stub_for(next(iter(listing_items(AEPD_LISTING)))))
    assert record is not None
    assert record.extra["citation_default_instrument"] == GDPR
    assert record.extra["procedure"] == "Procedimiento sancionador"
    assert record.extra["file_number"] == "PS-00355-2025"
    assert record.extra["is_appeal"] is False


# ---------------------------------------------------------------------------
# what a real Spanish document turned out to do
# ---------------------------------------------------------------------------

def test_a_documents_own_lettered_annex_is_not_annex_100_of_the_regulation():
    """AESIA's risk-management guide has its own Annexes A, B and C, and ``[ivxlc]+``
    reads "C" as the Roman numeral 100. With the AI Act declared as the guide's default
    instrument, every mention of its own Annex C became a citation of Annex 100 of a
    Regulation whose annexes stop at XIII. Annexes I, V and X are below the threshold
    and still resolve, which is why the guard is a plausibility ceiling and not a ban on
    single letters."""
    text = ("Esta guía desarrolla el Reglamento (UE) 2024/1689. "
            "8.3 ANEXO C - Tipos de riesgos comunes en el ámbito de la IA. "
            "Los sistemas del anexo III están sujetos al artículo 9.")
    found = pins(text)
    assert ("32024R1689", "Annex III") in found
    assert not [p for _id, p in found if p and p.startswith("Annex C")]
    assert not [p for _id, p in found if p == "Annex 100"]


def test_spanish_para_is_the_preposition_for_not_the_abbreviation_for_paragraph():
    """"un ejemplo … para 2 casos de uso" is "for 2 use cases". Spain writes *apartado*
    or *párr.*, never *para*, so in a document using Spanish provision vocabulary the
    English cue is the wrong reading — and it gave the AI Act a paragraph 2."""
    text = ("Esta guía desarrolla el Reglamento (UE) 2024/1689 y su artículo 9. "
            "Recoge un ejemplo del proceso para 2 casos de uso.")
    assert ("32024R1689", "Article 9") in pins(text)
    assert ("32024R1689", "para 2") not in pins(text)
    # …and an English document is untouched: "para 2" there really is paragraph 2
    english = pins("This guidance concerns Regulation (EU) 2024/1689. See para 2.")
    assert ("32024R1689", "para 2") in english


def test_the_aepd_feed_is_resolved_by_the_location_header_not_by_following_it():
    """``/recurso-multimedia/{slug}`` 301s to the file. HEAD returns 500 for most of the
    archive while GET returns the document, and a 500 is retried five times with
    exponential backoff — so resolving by HEAD turned fifty feed items into hours.
    Following the redirect instead downloads up to 8 MB the pipeline is about to fetch
    again. The Location header of an unfollowed request costs neither."""
    from raglex.adapters.eu_dpa_guidance import aepd_document_url

    class _Redirects:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, kwargs.get("follow_redirects")))
            target = {"guia": "/guias/x.pdf", "campana": "/documento/y.png"}.get(
                url.rsplit("/", 1)[-1], "")
            return _FakeResponse(b"", url, headers={"location": target})

    client = _Redirects()
    assert aepd_document_url(client, "https://www.aepd.es/recurso-multimedia/guia") == (
        "https://www.aepd.es/guias/x.pdf")
    # a campaign graphic is not a document
    assert aepd_document_url(client, "https://www.aepd.es/recurso-multimedia/campana") is None
    # a video redirects nowhere
    assert aepd_document_url(client, "https://www.aepd.es/recurso-multimedia/video") is None
    assert {m for m, _ in client.calls} == {"GET"}
    assert all(follow is False for _, follow in client.calls)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_the_two_new_spanish_sources_are_registered_under_spain():
    for key, kind in (("es-aesia-guias", "guidance"),
                      ("es-aepd-resoluciones", "administrative")):
        assert key in ADAPTERS
        assert SOURCE_INFO[key].jurisdiction == "ES"
        assert SOURCE_INFO[key].kind == kind
        assert get_adapter(key).source == key
        assert key in INCREMENTAL_MODE
        # every declared option is a keyword the constructor actually takes
        for option in SOURCE_INFO[key].options:
            ADAPTERS[key](**{option.name: None})


def test_a_supervisory_authoritys_determination_is_administrative_not_guidance():
    """``docs/adapter-authoring.md``: regulator determinations, sanctions and
    enforcement notices are administrative. The AEPD's guías stay guidance."""
    assert SOURCE_INFO["es-aepd-resoluciones"].kind == "administrative"
    assert SOURCE_INFO["es-aepd-guias"].kind == "guidance"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, content: bytes, url: str = "https://example.invalid/",
                 headers: dict | None = None):
        self.content, self.url = content, url
        self.headers = headers or {}


class _FakeClient:
    """Serves canned bodies keyed on the ``page`` parameter."""

    def __init__(self, pages: dict[str, bytes]):
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None, **kwargs) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        key = str((params or {}).get("page", ""))
        if key not in self.pages:
            raise AssertionError(f"unexpected page {key!r}")
        return _FakeResponse(self.pages[key], url)

    def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        return _FakeResponse(b"", url)


def _stub_for(row: dict):
    from raglex.core.models import Stub

    return Stub(stable_id=stable_id(row["title"]), landing_url=row["url"],
                raw_url=row["url"], title=row["title"], hint_date=row["date"], hints={})


@pytest.fixture(autouse=True)
def _pdf_bytes(monkeypatch):
    """``fetch`` is tested for its metadata, not for pdfminer. Serve one real-looking
    PDF and a fixed extraction so the test says nothing about PDF parsing."""
    import raglex.adapters.es_aepd as module

    monkeypatch.setattr(module, "text_or_ocr", lambda data, **kw: (
        "Se impone una sanción conforme al artículo 83.5 del RGPD. " * 10,
        False, [], "pdf"))

    original = _FakeClient.get

    def get(self, url: str, params: dict | None = None, **kwargs):
        if url.endswith(".pdf"):
            return _FakeResponse(b"%PDF-1.7\n", url)
        return original(self, url, params, **kwargs)

    monkeypatch.setattr(_FakeClient, "get", get)
    yield


def test_the_grammar_is_fast_enough_for_a_long_resolution():
    """The article-list token is atomic because it sits inside a repetition: without it
    a long document full of "artículo N" phrases with no host after them made ``re``
    revisit every optional rung of the ladder, and two French judgments once blew the
    stage's 90-second budget that way."""
    import time

    text = ("El artículo 5 y el artículo 6 y el artículo 7 sin instrumento alguno. " * 900
            + "Todo ello conforme al artículo 83.5.b) del RGPD.")
    start = time.monotonic()
    found = spanish_citations(text)
    assert time.monotonic() - start < 5.0
    assert ("32016R0679", "Article 83(5)(b)") in {
        (c.candidate_id, c.pinpoint) for c in found}
    assert not re.search(r"\bArticle 5\b", str([c.pinpoint for c in found]))
