from __future__ import annotations

import json
import re
from datetime import date

from raglex.config import Config
from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from raglex.resolve import Resolver
from raglex.static_export import StaticLawExporter
from raglex.storage import Catalogue, TextStore


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path,
        catalogue_path=tmp_path / "catalogue.sqlite",
        raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing",
        embed_model=None,
    )


def _store(cat: Catalogue, textstore: TextStore, record: Record) -> None:
    record.ensure_payload_hash()
    path = textstore.put(record.payload_hash, record.text or "")
    textstore.put_segments(record.payload_hash, record.segments)
    cat.upsert_document(record, text_path=str(path))


def test_static_export_contains_law_mentions_snippets_and_public_links(tmp_path):
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)

    law_text = (
        "Article 1 Subject matter\n1. This Regulation lays down rules.\n\n"
        "Article 15 Right of access\n"
        "1. The data subject has the right of access.\n"
        "3. The controller shall provide a copy."
    )
    article_15 = law_text.index("Article 15")
    law = Record(
        source="eu-legislation",
        stable_id="32016R0679",
        doc_type=DocType.LEGISLATION,
        title=(
            "Regulation (EU) 2016/679 of the European Parliament and of the Council "
            "of 27 April 2016 on the protection of natural persons with regard to the "
            "processing of personal data and on the free movement of such data, and "
            "repealing Directive 95/46/EC (General Data Protection Regulation)"
        ),
        decision_date=date(2016, 4, 27),
        language="en",
        source_language="en",
        landing_url="https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32016R0679",
        text=law_text,
        raw_bytes=law_text.encode(),
        segments=[
            Segment("Article 1 Subject matter", 0, article_15, kind="article"),
            Segment("Article 15 Right of access", article_15, len(law_text), kind="article"),
        ],
        extracted_via=ExtractedVia.STRUCTURED,
        extra={
            "currency": {
                "as_at": "2026-06-19", "up_to_date": False,
                "unapplied_count": 63,
            },
            "source_last_modified": "Wed, 22 Jul 2026 18:56:31 GMT",
        },
    )
    _store(cat, textstore, law)

    citing_text = (
        "Article 1 GDPR states the scope. The claimant relied on Article 15 GDPR when "
        "asking for a copy, and Article 15(3) GDPR for the form of that copy."
    )
    article_1_start = citing_text.index("Article 1 GDPR")
    article_15_start = citing_text.index("Article 15 GDPR")
    article_15_3_start = citing_text.index("Article 15(3) GDPR")
    citer = Record(
        source="uk-caselaw",
        stable_id="ewhc/admin/2024/10",
        doc_type=DocType.JUDGMENT,
        title="Example v Commissioner",
        court="ewhc",
        decision_date=date(2024, 2, 1),
        language="en",
        source_language="en",
        landing_url="https://www.bailii.org/ew/cases/EWHC/Admin/2024/10.html",
        text=citing_text,
        raw_bytes=citing_text.encode(),
        relations=[
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Article 1 GDPR",
                dst_id="32016R0679",
                dst_anchor="Article 1",
                context_start=article_1_start,
                context_end=article_1_start + len("Article 1 GDPR"),
                resolution_status=ResolutionStatus.PENDING,
            ),
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Article 15 GDPR",
                dst_id="32016R0679",
                dst_anchor="Article 15",
                context_start=article_15_start,
                context_end=article_15_start + len("Article 15 GDPR"),
                resolution_status=ResolutionStatus.PENDING,
            ),
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Article 15(3) GDPR",
                dst_id="32016R0679",
                dst_anchor="Article 15(3)",
                context_start=article_15_3_start,
                context_end=article_15_3_start + len("Article 15(3) GDPR"),
                resolution_status=ResolutionStatus.PENDING,
            ),
        ],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, citer)
    Resolver(cat).run()
    # A stale relation-span projection must not make the static page mark unrelated
    # nearby words. The exporter validates/re-locates the raw citation on read.
    cat.conn.execute(
        "UPDATE relations SET context_start=context_start-9, context_end=context_end-9 "
        "WHERE src_id=? AND raw_citation_string=?",
        ("ewhc/admin/2024/10", "Article 15 GDPR"),
    )
    cat.commit()
    cat.close()

    result = StaticLawExporter(config).build("32016R0679")
    page = result.html.decode()

    assert result.documents == 1
    assert result.mentions == 3
    assert result.filename.startswith("regulation-eu-2016-679-")
    assert "General Data Protection Regulation" in page
    assert "Article 15 Right of access" in page
    assert "Example v Commissioner" in page
    assert "Article%2015%20GDPR" in page
    assert "fetch(" not in page
    assert 'font-family: Times, "Times New Roman", serif' in page
    assert "--paper: #ffffff" in page
    assert 'id="contents-search"' not in page
    assert 'id="result-search"' not in page
    assert "<footer" not in page
    assert "Static snapshot" not in page
    assert "Document generated from a dataset held and maintained by" in page

    match = re.search(
        r'<script id="raglex-data" type="application/json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert match
    data = json.loads(match.group(1))
    assert data["counts"]["art:15"] == 1
    assert data["counts"]["exact:art15(3)"] == 1
    group = data["groups"][0]
    exact = group["snippets"][group["snippet_indices"]["exact:art15(3)"][0]]
    assert exact["text"][exact["mark"][0]:exact["mark"][1]] == "Article 15(3) GDPR"
    article_15_snippet = group["snippets"][group["snippet_indices"]["bare:art:15"][0]]
    assert (
        article_15_snippet["text"][
            article_15_snippet["mark"][0]:article_15_snippet["mark"][1]
        ]
        == "Article 15 GDPR"
    )
    article_15_section = next(
        section for section in data["law"]["sections"]
        if section["label"].startswith("Article 15")
    )
    third_paragraph = next(
        paragraph for paragraph in article_15_section["paragraphs"]
        if paragraph["text"].startswith("3.")
    )
    assert third_paragraph["indent"] == 1
    assert third_paragraph["marks"][0]["key"] == "exact:art15(3)"
    assert data["flags"]["United Kingdom"].startswith("data:image/svg+xml;base64,")
    assert data["groups"][0]["links"][0]["url"].startswith("https://www.bailii.org/")
    assert data["law"]["currency"]["as_at"] == "2026-06-19"
    assert data["law"]["currency"]["unapplied_count"] == 63
    assert "Publisher text current to" in page
    assert "63 publisher-recorded effects not yet applied" in page


def test_static_export_names_the_previous_law_behind_inherited_mentions(tmp_path):
    """An inherited mention is attributed to the instrument that was actually cited.

    The page must be able to say "3 mentions of a similar provision in Directive
    95/46/EC" rather than "3 via previous law", and must keep that route separable from
    the mentions of the current text.
    """
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)

    current_text = "Article 15 Right of access\n1. The data subject has the right."
    current = Record(
        source="eu-legislation", stable_id="32016R0679", doc_type=DocType.LEGISLATION,
        title="Regulation (EU) 2016/679 of the European Parliament and of the Council "
              "of 27 April 2016 on the protection of natural persons",
        text=current_text, raw_bytes=current_text.encode(),
        segments=[Segment("Article 15 Right of access", 0, len(current_text),
                          kind="article")],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    previous_text = "Article 12 Right of access\nMember States shall guarantee."
    previous = Record(
        source="eu-legislation", stable_id="31995L0046", doc_type=DocType.LEGISLATION,
        title="Directive 95/46/EC of the European Parliament and of the Council of "
              "24 October 1995 on the protection of individuals",
        text=previous_text, raw_bytes=previous_text.encode(),
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, current)
    _store(cat, textstore, previous)

    old_citer_text = "The court applied Article 12 of Directive 95/46/EC directly."
    old_start = old_citer_text.index("Article 12")
    old_citer = Record(
        source="uk-caselaw", stable_id="ewca/civ/2010/1", doc_type=DocType.JUDGMENT,
        title="Old v Registrar", court="ewca", decision_date=date(2010, 5, 1),
        text=old_citer_text, raw_bytes=old_citer_text.encode(),
        relations=[TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string="Article 12", dst_id="31995L0046",
            dst_anchor="Article 12", context_start=old_start,
            context_end=old_start + len("Article 12"),
            resolution_status=ResolutionStatus.PENDING,
        )],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    new_citer_text = "The claimant relied on Article 15 GDPR."
    new_start = new_citer_text.index("Article 15")
    new_citer = Record(
        source="uk-caselaw", stable_id="ewhc/admin/2024/11", doc_type=DocType.JUDGMENT,
        title="New v Commissioner", court="ewhc", decision_date=date(2024, 2, 1),
        text=new_citer_text, raw_bytes=new_citer_text.encode(),
        relations=[TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string="Article 15 GDPR", dst_id="32016R0679",
            dst_anchor="Article 15", context_start=new_start,
            context_end=new_start + len("Article 15 GDPR"),
            resolution_status=ResolutionStatus.PENDING,
        )],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, old_citer)
    _store(cat, textstore, new_citer)
    Resolver(cat).run()
    cat.upsert_provision_mappings(
        "32016R0679", "31995L0046",
        [{"current_anchor": "Article 15", "previous_anchor": "Article 12"}])
    cat.commit()
    cat.close()

    data = StaticLawExporter(config).build_data("32016R0679")
    laws = {law["id"]: law for law in data["previous_laws"]}
    assert laws["31995L0046"]["label"] == "Directive 95/46/EC"   # not the wordy title
    assert data["previous_counts"]["31995L0046"]["art:15"] == 1
    assert data["direct_counts"]["art:15"] == 1                  # the two routes separate
    assert data["counts"]["art:15"] == 2
    by_id = {group["id"]: group for group in data["groups"]}
    assert by_id["ewca/civ/2010/1"]["previous_mentions_by_key"]["art:15"] == {
        "31995L0046": 1}
    assert by_id["ewhc/admin/2024/11"]["previous_mentions_by_key"] == {}
    assert data["law"]["jurisdiction"] == "European Union"
    assert data["law"]["short_title"] == "Regulation (EU) 2016/679"

    # Both mapped provisions travel with the page, so a reader can judge the claim of
    # similarity side by side instead of taking the mapping on trust.
    comparison = data["comparisons"]["art:15"][0]
    assert comparison["previous_id"] == "31995L0046"
    assert comparison["previous_label"] == "Directive 95/46/EC"
    assert comparison["previous_provision_label"].startswith("Article 12")
    assert "Member States shall guarantee" in comparison["previous_text"]
    assert "the right" in comparison["current_text"]

    page = StaticLawExporter(config).build("32016R0679").html.decode()
    assert "via previous law" not in page
    assert "of a similar provision in" in page
    assert 'id="compare-dialog"' in page
    assert "Member States shall guarantee" in page


def test_unplaceable_subprovision_pinpoints_roll_into_the_provision(tmp_path):
    """A pinpoint that corresponds to nothing gets no badge of its own.

    Citations to "s. 11(4)" of a section that runs (1), (2), (3) — a mangled capture, or
    a number the drafter never used — used to be bunched at the foot of the section as a
    row of "[1 mention]" badges, one per misreading, each opening a list of one. They are
    counted under the section instead, which is the only place they can be read honestly.
    """
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)

    law_text = ("s. 11 Right to prevent processing\n"
                "1. An individual is entitled.\n"
                "2. The court may order.\n")
    law = Record(
        source="uk-legislation", stable_id="ukpga/1998/29",
        doc_type=DocType.LEGISLATION, title="Data Protection Act 1998",
        text=law_text, raw_bytes=law_text.encode(),
        segments=[Segment("s. 11 Right to prevent processing", 0, len(law_text),
                          kind="section")],
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, law)

    citing = ("The judge considered s. 11(1) and then s. 11(4) and s. 11(9) of the Act.")
    relations = []
    for pinpoint in ("s. 11(1)", "s. 11(4)", "s. 11(9)"):
        at = citing.index(pinpoint)
        relations.append(TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=pinpoint, dst_id="ukpga/1998/29",
            dst_anchor=pinpoint, context_start=at, context_end=at + len(pinpoint),
            resolution_status=ResolutionStatus.PENDING,
        ))
    _store(cat, textstore, Record(
        source="uk-caselaw", stable_id="ewhc/qb/2015/1", doc_type=DocType.JUDGMENT,
        title="Example v Registrar", court="ewhc", decision_date=date(2015, 3, 1),
        text=citing, raw_bytes=citing.encode(), relations=relations,
        extracted_via=ExtractedVia.STRUCTURED))
    Resolver(cat).run()
    cat.commit()
    cat.close()

    data = StaticLawExporter(config).build_data("ukpga/1998/29")
    section = data["law"]["sections"][0]
    badges = [mark["label"] for para in section["paragraphs"] for mark in para["marks"]]
    # (1) exists as a drafting line and keeps its own badge; (4) and (9) do not exist…
    assert badges == ["s. 11(1)"]
    # …but their citer is still counted against the section as a whole.
    assert data["counts"][section["key"]] == 1
    assert "exact:s11(4)" not in {
        mark["key"] for para in section["paragraphs"] for mark in para["marks"]}


def test_static_export_escapes_script_terminators_and_sanitises_attribution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RAGLEX_STATIC_EXPORT_ATTRIBUTION",
        '<strong>Maintained</strong><script>alert(1)</script>'
        '<a href="javascript:alert(2)">bad link</a>',
    )
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)
    text = "Section 1\nA harmless </script> string in the source."
    record = Record(
        source="uk-legislation",
        stable_id="ukpga/2024/1",
        doc_type=DocType.LEGISLATION,
        title="Example Act",
        text=text,
        raw_bytes=text.encode(),
        extracted_via=ExtractedVia.STRUCTURED,
    )
    _store(cat, textstore, record)
    cat.close()

    page = StaticLawExporter(config).build("ukpga/2024/1").html.decode()
    data_block = page.split('<script id="raglex-data" type="application/json">', 1)[1].split(
        "</script>", 1)[0]
    assert "<\\/script>" in data_block
    assert "<strong>Maintained</strong>" in page
    assert "<script>alert(1)</script>" not in page
    assert 'href="javascript:' not in page


# -- the provision's "Mentioned by …" line ----------------------------------
def test_provision_headings_name_their_citers_and_subsections_do_not():
    """At the provision, who cites it is worth naming; inside it, it is not — a case name
    set against a numbered sub-paragraph breaks the law's own shape, which is the thing
    the reader came for. So the heading gets prose and the paragraphs keep [N mentions]."""
    from raglex.static_export import _SCRIPT, _STYLE

    # the heading builds the prose line, not a badge row
    assert "const line = mentionsLine(section.key, section.label);" in _SCRIPT
    assert "appendBadges(heading," not in _SCRIPT
    # …and a numbered paragraph still gets its terse badge
    assert "appendBadges(body, mark.key, mark.label" in _SCRIPT
    # passthrough citers are a separate sentence, never folded into the first
    assert "Also mentioned by " in _SCRIPT
    assert "citing a similar provision in " in _SCRIPT
    # a named citer opens the list AT its own row
    assert 'openMentions(key, label, "all", g.id)' in _SCRIPT
    assert 'data-doc="${esc(group.id)}"' in _SCRIPT
    # small, quiet prose — and the names survive printing even though the buttons don't
    assert ".mentions-line {" in _STYLE and "font-size: .88rem" in _STYLE
    assert ".cite-link { color: var(--ink); text-decoration: none; }" in _STYLE


# -- what the page tells a machine about itself -----------------------------
_SEO_DATA = {
    "law": {
        "stable_id": "ukpga/2018/12", "title": "Data Protection Act 2018",
        "short_title": "DPA 2018", "jurisdiction": "uk", "cite": "2018 c. 12",
        "language": "en",
        "links": [{"url": "https://www.legislation.gov.uk/ukpga/2018/12",
                   "label": "legislation.gov.uk"}],
        "sections": [
            {"id": "s-1", "label": "s. 1 Overview", "kind": "section", "level": 1,
             "key": "s1", "text": "This Act makes provision about the processing of "
                                  "personal data and about the Commissioner.",
             "paragraphs": [{"text": "This Act makes provision about the processing of "
                                     "personal data and about the Commissioner.",
                             "indent": 0}]},
        ],
    },
    "counts": {"all": 1234},
}


def test_the_page_is_readable_without_running_its_script():
    """The instrument is in the HTML, not only in the JSON the script renders.

    An edition is a payload plus a renderer, so with JavaScript off — or to anything that
    does not run it — <main> was empty and the whole law invisible. The same text is now
    also markup, and the script clears it before drawing the interactive version, so a
    reader sees exactly one copy.
    """
    from raglex.static_export import _SCRIPT, render_static_page

    page = render_static_page(title=_SEO_DATA["law"]["title"],
                              data_json=json.dumps(_SEO_DATA))
    assert '<section id="s-1" class="law-section level-1">' in page
    assert "<h2>s. 1 Overview</h2>" in page
    assert "This Act makes provision about the processing" in page
    assert '<a href="#s-1"><span>s. 1 Overview</span></a>' in page
    # …and the script atomically replaces the law, or the reader gets either two copies
    # or an empty/partial one while a large edition is still being rendered
    assert 'const renderedLaw = document.createDocumentFragment();' in _SCRIPT
    assert '$("law").replaceChildren(renderedLaw);' in _SCRIPT
    assert '$("contents-nav").textContent = "";' in _SCRIPT


def test_each_prerendered_provision_says_its_mentions_are_loading():
    """The visible law precedes the large JSON payload in a single-file export, so it
    can reassure a reader at every provision while that payload is still arriving."""
    from raglex.static_export import _HTML_TEMPLATE, _SCRIPT, _STYLE, render_static_page

    data = json.loads(json.dumps(_SEO_DATA))
    data["counts"]["s1"] = 1234
    page = render_static_page(title=data["law"]["title"],
                              data_json=json.dumps(data))
    law_before_payload = page.split('<script id="raglex-data"', 1)[0]
    assert '[ Loading 1,234 mentions<span class="loading-dots">...</span> ]' \
        in law_before_payload
    assert 'class="mentions-loading" role="status"' in law_before_payload
    # No-JS readers see the ordinary prerendered law, not a status that never completes.
    assert '.mentions-loading {\n  display: none;' in _STYLE
    assert '.mentions-pending .mentions-loading' in _STYLE
    assert 'document.documentElement.classList.add("mentions-pending")' in _HTML_TEMPLATE
    # Successful rendering removes the opt-in class; a failed renderer gets an honest
    # terminal message from the small, early head script.
    assert 'document.documentElement.classList.remove("mentions-pending");' in _SCRIPT
    assert 'Mentions could not be loaded.' in _HTML_TEMPLATE


def test_a_provision_with_no_known_mentions_is_still_visibly_checked():
    from raglex.static_export import render_static_page

    data = json.loads(json.dumps(_SEO_DATA))
    data["counts"] = {"all": 0}
    page = render_static_page(title=data["law"]["title"], data_json=json.dumps(data))
    assert '[ Checking for mentions<span class="loading-dots">...</span> ]' in page


def test_the_head_says_what_the_page_is():
    from raglex.static_export import render_static_page

    page = render_static_page(title=_SEO_DATA["law"]["title"],
                              data_json=json.dumps(_SEO_DATA),
                              canonical="https://example.org/dpa.html")
    head = page.split("</head>")[0]
    # a description written from the law's own opening words, not a template
    assert "This Act makes provision about the processing" in head
    assert '<link rel="canonical" href="https://example.org/dpa.html">' in head
    assert 'property="og:title"' in head and 'name="twitter:card"' in head
    assert 'name="robots"' in head
    assert '<html lang="en">' in page
    # the citable labels people actually search for
    assert "DPA 2018 s. 1 Overview" in head


def test_structured_data_calls_legislation_legislation():
    from raglex.static_export import render_static_page

    page = render_static_page(title=_SEO_DATA["law"]["title"],
                              data_json=json.dumps(_SEO_DATA),
                              canonical="https://example.org/dpa.html")
    blocks = re.findall(r'application/ld\+json">(.*?)</script>', page, re.S)
    assert len(blocks) == 2
    node = json.loads(blocks[0].replace("\\u003c", "<").replace("\\u003e", ">"))
    assert node["@type"] == "Legislation"
    assert node["legislationIdentifier"] == "2018 c. 12"
    assert node["legislationJurisdiction"] == "United Kingdom"
    assert node["identifier"] == "ukpga/2018/12"
    assert "https://www.legislation.gov.uk/ukpga/2018/12" in node["sameAs"]
    assert json.loads(blocks[1].replace("\\u003c", "<"))["@type"] == "BreadcrumbList"


def test_no_canonical_is_better_than_a_guessed_one():
    # a wrong canonical tells a search engine the real page is somewhere it is not
    from raglex.static_export import render_static_page

    page = render_static_page(title=_SEO_DATA["law"]["title"],
                              data_json=json.dumps(_SEO_DATA))
    assert "rel=\"canonical\"" not in page
    assert 'property="og:url"' not in page
    assert 'name="description"' in page      # everything else still there


# -- the page's own script, checked without a browser -----------------------
def test_page_script_only_touches_ids_and_names_that_exist():
    """The whole interactive half of an edition is one IIFE, so a single undefined name
    at its top level takes out every listener bound after it — silently, on a page that
    still looks right. That is exactly what happened: the pending-proceedings block read
    ``DATA`` where the payload is called ``data``, and from there the mentions dialog's
    [ close ] button, its backdrop, "+ show more" and the sort control were never bound.
    Nothing here needs a browser: the payload has one name, and every element the script
    reaches for has to be in the template."""
    from raglex.static_export import _HTML_TEMPLATE, _SCRIPT

    assert re.search(r"\bDATA\b", _SCRIPT) is None, "the payload is bound as `data`"
    wanted = set(re.findall(r'\$\("([^"]+)"\)', _SCRIPT))
    present = set(re.findall(r'id="([^"]+)"', _HTML_TEMPLATE))
    assert wanted, "the script addresses elements by id"
    assert wanted <= present, f"script reaches for ids the page has not got: {wanted - present}"


def test_hidden_beats_an_author_display_rule():
    """``+ show more`` hides itself by setting ``.hidden`` — which the browser honours
    with a ``display: none`` weaker than any author rule, so ``.more { display: block }``
    kept it on screen with nothing left to show."""
    from raglex.static_export import _SCRIPT, _STYLE

    assert '$("more-results").hidden = visible.length >= rows.length;' in _SCRIPT
    assert "[hidden] { display: none !important; }" in _STYLE


# -- the bytes a reader downloads -------------------------------------------
def test_the_page_leaves_out_what_it_never_reads():
    """A section shipped its text twice — once whole, once as the paragraphs the script
    actually renders — and every row carried builder bookkeeping the page never opens.
    Dropped on the way into the page, not into the cache: an edition already built gets
    the smaller file on its next render rather than an hours-long rebuild."""
    from raglex.static_export import _slim_for_page

    data = {
        "stats": {"documents": 1, "mentions": 2},
        "law": {
            "title": "Example Act",
            "provision_mappings": [{"heavy": "x" * 100}],
            "sections": [
                {"key": "s:1", "label": "s. 1", "kind": "section", "text": "One.\nTwo.",
                 "paragraphs": [{"text": "One."}, {"text": "Two."}]},
                # no paragraphs: the whole text is all the page will have
                {"key": "s:2", "label": "s. 2", "text": "Three."},
            ],
        },
        "groups": [{
            "id": "d/1", "cite": "R v A", "doc_type": "cases", "has_text": True,
            "raw_ext": "html", "relationships": ["mentions"], "target_keys": ["s:1"],
            "links": [{"url": "https://example.org/a", "label": "source"}],
            "snippets": [{"text": "…", "raw": "<p>…</p>",
                          "passage_url": "https://example.org/a#:~:text=x"}],
        }],
    }
    slim = _slim_for_page(data)

    assert "stats" not in slim and "provision_mappings" not in slim["law"]
    assert "text" not in slim["law"]["sections"][0]      # the paragraphs carry it
    assert slim["law"]["sections"][1]["text"] == "Three."  # nothing else does
    group = slim["groups"][0]
    assert not {"doc_type", "has_text", "raw_ext", "relationships", "target_keys"} & set(group)
    assert group["cite"] == "R v A"
    # the excerpt's link is split against the row's own url, and rejoins to the same thing
    assert group["snippets"][0]["passage_url"] == [0, "#:~:text=x"]
    assert "raw" not in group["snippets"][0]
    # the source payload is untouched — `build` reads its own statistics back out of it
    assert data["stats"]["documents"] == 1
    assert data["law"]["sections"][0]["text"] == "One.\nTwo."
    assert data["groups"][0]["snippets"][0]["passage_url"] == "https://example.org/a#:~:text=x"
    # and slimming a page that has already been slimmed changes nothing
    assert _slim_for_page(slim) == slim


def test_the_script_rejoins_a_split_passage_link():
    from raglex.static_export import _SCRIPT

    assert "function passageUrl(snippet, group)" in _SCRIPT
    assert "if (!Array.isArray(stored)) return stored || \"\";" in _SCRIPT
    assert "snippetHtml(snippet, group)" in _SCRIPT


# -- what is still before the Court -----------------------------------------
def test_pending_proceedings_are_counted_in_english_and_marked_up_in_yellow():
    """"8 pending actions for annulment", not "8 action for annulments" — the head noun
    inflects, and a parenthetical qualifier ("(urgent, PPU)") keeps its capitals."""
    from raglex.static_export import _SCRIPT, _STYLE

    assert "function pluralKind(label)" in _SCRIPT
    assert "const at = head.search(/\\s+(?:for|of|to|against|by|under)\\s+/);" in _SCRIPT
    assert "`${number(n)} pending ${lowerFirst(n === 1 ? label : pluralKind(label))}`" in _SCRIPT
    # each count filters the list, and the whole line ends with the way into all of it
    assert 'class="pending-count" data-kind="${esc(g.label)}"' in _SCRIPT
    assert 'class="pending-all" data-kind=""' in _SCRIPT
    assert 'openPending(button.dataset.kind || "")' in _SCRIPT
    # highlighter yellow, clipped to the words as they wrap
    assert ".pending-highlight {" in _STYLE
    assert "background: #ffff00;" in _STYLE
    assert "box-decoration-break: clone;" in _STYLE


# -- the page's own furniture ------------------------------------------------
def test_the_contents_column_names_the_law_and_the_set_it_belongs_to():
    """There is no [ contents ] button any more: it existed only to hide the one piece of
    navigation the page has. The column heads itself instead — this law, then the way
    back to the set."""
    from raglex.static_export import _HTML_TEMPLATE, _SCRIPT, _STYLE, _sidebar_head

    assert "contents-toggle" not in _HTML_TEMPLATE
    assert "contents-toggle" not in _SCRIPT
    assert "sidebar-closed" not in _STYLE
    assert "__SIDEBAR_HEAD__" in _HTML_TEMPLATE

    head = _sidebar_head("GDPR", {"href": "index.html", "title": "UCL Digital Laws"})
    assert '<p class="contents-title">GDPR</p>' in head
    assert '<a href="index.html">Back to UCL Digital Laws</a>' in head
    # a standalone edition belongs to no set, so it is offered no way back to one
    assert "contents-back" not in _sidebar_head("GDPR", None)
    # …and the contents' own link styling must not swallow the way back
    assert ".contents nav a, .contents nav button {" in _STYLE


def test_an_eu_title_drops_the_eea_footnote():
    """"(Text with EEA relevance)" is part of the citation of record and says nothing
    about the law. It goes from the heading and the contents column — never from the
    payload, whose title is what the published FILENAME is slugged from."""
    from raglex.static_export import _display_title, _sidebar_head

    full = ("Regulation (EU) 2016/679 of the European Parliament and of the Council of "
            "27 April 2016 on the protection of natural persons … (Text with EEA relevance)")
    assert _display_title(full).endswith("natural persons …")
    assert _display_title("Directive 2002/58/EC (Text with EEA relevance.)") \
        == "Directive 2002/58/EC"
    # nothing to strip, nothing changed
    assert _display_title("Data Protection Act 2018") == "Data Protection Act 2018"
    assert _display_title(None) == ""
    assert "EEA relevance" not in _sidebar_head(full, None)


def test_a_pending_row_is_two_lines_with_a_way_to_the_notice():
    """Case number, parties in italic and the kind of proceeding in brackets on one line;
    the provisions it turns on and a link to EUR-Lex on the next. No date, and no bordered
    pill — this page sets everything else in plain prose."""
    from raglex.static_export import _SCRIPT, _STYLE

    assert "pending-label" not in _SCRIPT and "pending-label" not in _STYLE
    assert "row.date" not in _SCRIPT.split("function pendingRowHtml")[1].split("\n  }")[0]
    assert '<span class="pending-kind">(${kind})</span>' in _SCRIPT
    assert '(row.anchors || []).map(esc).join(", ")' in _SCRIPT
    # the CELEX number IS the address of the notice, so no payload field is needed
    assert '"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:"' in _SCRIPT
    assert 'externalLink(url, "EUR-Lex →")' in _SCRIPT
    # the Court's fictitious-name disclaimer is not part of a party name
    assert "The name of the present case is a fictitious name" in _SCRIPT


def test_a_delivered_ag_opinion_is_a_link_to_the_opinion():
    """An Opinion delivered is readable months before the judgment, so saying so is not
    an annotation — it is a way in. It links to the Opinion's OWN celex, carried in the
    payload, because an urgent reference gets a View (CV) rather than an Opinion (CC) and
    swapping descriptors would send that reader nowhere."""
    from raglex.static_export import _SCRIPT

    assert "function agOpinionHtml(row)" in _SCRIPT
    assert "celexUrl(row.ag_id) || ecliUrl(row.ag_id)" in _SCRIPT
    # the corpus holds many of the Court's own documents under an ECLI, and EUR-Lex
    # resolves those too
    assert '"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=ecli:"' in _SCRIPT
    assert 'row.ag ? agOpinionHtml(row) : ""' in _SCRIPT
    # …and the notice keeps its own link, at the end of the second line
    assert 'const url = celexUrl(row.id);' in _SCRIPT
    # a payload built before ag_id existed still links, via the ordinary descriptor
    assert 'notice.slice(0, 5) + "CC" + notice.slice(7)' in _SCRIPT
