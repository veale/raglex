from __future__ import annotations

import json
import zipfile

import pytest

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade
from raglex.settings import SettingsStore
from raglex.static_bundle import (
    _display_coverage_year,
    _group_source_entries,
    apply_placeholders,
    build_sources_summary,
    build_bundle,
    format_export_date,
    load_config,
    render_index_html,
    render_sources_html,
    save_config,
    slugify_filename,
)
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


def _hold(config: Config, stable_id: str, title: str, text: str) -> None:
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)
    record = Record(
        source="uk-legislation",
        stable_id=stable_id,
        doc_type=DocType.LEGISLATION,
        title=title,
        text=text,
        raw_bytes=text.encode(),
        extracted_via=ExtractedVia.STRUCTURED,
    )
    record.ensure_payload_hash()
    path = textstore.put(record.payload_hash, record.text or "")
    textstore.put_segments(record.payload_hash, record.segments)
    cat.upsert_document(record, text_path=str(path))
    cat.close()


def _source_item(label, count, year_from, year_to, *, section="Case law"):
    return {"section": section, "label": label, "count": count,
            "year_from": year_from, "year_to": year_to,
            "domains": ["example.test"], "sources": ["test"], "manual": False}


def test_sources_group_court_levels_even_when_the_level_is_a_suffix():
    entries = [
        _source_item("Thüringer Landessozialgericht", 1222, 2010, 2025),
        _source_item("Landessozialgericht Berlin-Brandenburg", 800, 2005, 2026),
        _source_item("Amtsgericht Aachen", 4, 2021, 2024),
        _source_item("Amtsgericht Bonn", 6, 2020, 2026),
    ]
    grouped = _group_source_entries("Germany", entries)
    social = next(row for row in grouped if row["label"].startswith("Landessozialgerichte"))
    assert social["count"] == 2022 and social["year_from"] == 2005
    assert social["year_to"] == 2026 and len(social["details"]) == 2
    local = next(row for row in grouped if row["label"].startswith("Amtsgerichte"))
    assert local["count"] == 10 and len(local["details"]) == 2


def test_council_of_europe_guidance_groups_by_institution_not_person():
    section = "Guidance, reports and commentary"
    entries = [
        _source_item("Louise Drammeh", 6, 2010, 2011, section=section),
        _source_item("Aoife Nolan", 2, 2019, 2019, section=section),
        _source_item("Council of Europe Committee of Ministers", 4, 2023, 2024,
                     section=section),
        _source_item("Comité des Ministres du Conseil de l'Europe", 2, 2019, 2025,
                     section=section),
    ]
    grouped = _group_source_entries("Council of Europe", entries)
    ministers = next(row for row in grouped if row["label"] == "Committee of Ministers")
    assert ministers["count"] == 6 and len(ministers["details"]) == 2
    other = next(row for row in grouped if row["label"].startswith("Other Council"))
    assert other["count"] == 8 and {r["label"] for r in other["details"]} == {
        "Louise Drammeh", "Aoife Nolan"}


def test_scottish_coverage_does_not_predate_the_court_of_session():
    assert _display_coverage_year(
        "cases", "uk-caselaw", "scotcs", 1028, 2026) is None
    assert _display_coverage_year(
        "cases", "uk-caselaw", "scotcs", 1532, 2026) == 1532
    assert _display_coverage_year(
        "cases", "uk-caselaw", "scotcs", 2030, 2026) == 2026


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("RAGLEX_STATIC_BUNDLE", "RAGLEX_STATIC_BUNDLE_LAST",
                "RAGLEX_STATIC_EXPORT_ATTRIBUTION"):
        monkeypatch.delenv(key, raising=False)


def test_slugs_are_filename_safe_and_unique(tmp_path):
    config = _config(tmp_path)
    settings = SettingsStore(config.settings_path)
    saved = save_config(settings, {"items": [
        {"stable_id": "a", "slug": "../../etc/passwd"},
        {"stable_id": "b", "slug": "GDPR.html"},
        {"stable_id": "c", "slug": "gdpr"},          # collides with the one above
        {"stable_id": "d", "title": "Online Safety Act 2023"},  # falls back to the title
    ]}, config)
    assert [i["slug"] for i in saved["items"]] == [
        "etc-passwd", "gdpr", "gdpr-2", "online-safety-act-2023"]
    # persisted, and readable back through the environment the store promotes into
    assert load_config(config)["items"][1]["slug"] == "gdpr"
    assert json.loads(config.settings_path.read_text())["RAGLEX_STATIC_BUNDLE"]


def test_slugify_keeps_a_usable_stem():
    assert slugify_filename("GDPR.html") == "gdpr"
    assert slugify_filename("  ") == "document"
    assert slugify_filename("Reg (EU) 2016/679") == "reg-eu-2016-679"


def test_placeholders_substitute_before_sanitising():
    when = format_export_date("2026-05-27T09:00:00+00:00")
    assert when == "27 May 2026"
    text = apply_placeholders("Exported <DateExported> · <count> items",
                              when=__import__("datetime").datetime(2026, 5, 27), count=3)
    assert text == "Exported 27 May 2026 · 3 items"


def test_sources_config_is_optional_and_round_trips(tmp_path):
    config = _config(tmp_path)
    settings = SettingsStore(config.settings_path)
    assert load_config(config)["sources_page"] is False
    saved = save_config(settings, {
        "sources_page": True,
        "sources_intro": "An operator-written introduction.",
    }, config)
    assert saved["sources_page"] is True
    assert saved["sources_intro"] == "An operator-written introduction."


def test_sources_summary_is_full_text_only_verbose_and_caps_future_years(tmp_path):
    config = _config(tmp_path)
    cat = Catalogue(config.catalogue_path)
    textstore = TextStore(config.text_dir)

    def hold(source, stable_id, kind, title, when, *, court=None, url=None, text="full text"):
        record = Record(source=source, stable_id=stable_id, doc_type=kind, title=title,
                        court=court, decision_date=when, landing_url=url, text=text or None,
                        raw_bytes=(text or "").encode(), extracted_via=ExtractedVia.STRUCTURED)
        record.ensure_payload_hash()
        path = str(textstore.put(record.payload_hash, record.text)) if record.text else None
        cat.upsert_document(record, text_path=path)

    from datetime import date
    hold("ca-caselaw", "bcca/2024/1", DocType.JUDGMENT, "A v B", date(2024, 1, 1),
         court="bcca", url="https://www.canlii.org/en/bc/bcca/doc/2024/1.html")
    hold("eu-cellar", "eu/ag/1", DocType.OPINION, "Opinion", date(2032, 1, 1),
         court="Advocate General", url="https://curia.europa.eu/juris/document/document.jsf")
    hold("eu-berec", "eu/berec/1", DocType.OPINION, "BEREC Opinion", date(2024, 1, 1),
         court="BEREC", url="https://berec.europa.eu/opinion/1")
    hold("fr-dila", "fr/ce/1", DocType.JUDGMENT, "Decision", date(201, 1, 1),
         court="Conseil d'État", url="https://legifrance.gouv.fr/ceta/id/1")
    hold("fr-dila", "fr/ce/2", DocType.JUDGMENT, "Décision", date(2025, 1, 1),
         court="Conseil d’État", url="https://legifrance.gouv.fr/ceta/id/2")
    # Metadata-only India must affect the whole-corpus number but appear nowhere on the
    # sources page, including its source list.
    hold("in-caselaw", "insc/2025/1", DocType.JUDGMENT, "No text", date(2025, 1, 1),
         court="insc", url="https://example.in/1", text="")
    cat.refresh_corpus_shape_stats()
    cat.close()

    facade = Facade(config)
    summary = build_sources_summary(facade, current_year=2026)
    assert summary["corpus_total"] == 6
    assert summary["full_text_total"] == 5
    assert [j["name"] for j in summary["jurisdictions"]] == [
        "European Union", "France", "Canada"]
    countries = {j["name"]: j for j in summary["jurisdictions"]}
    france = countries["France"]["entries"][0]
    assert france["label"] == "Conseil d’État"
    assert france["count"] == 2
    assert france["year_from"] == france["year_to"] == 2025
    canada = countries["Canada"]["entries"][0]
    assert canada["label"] == "British Columbia Court of Appeal"
    assert canada["domains"] == ["canlii.org"]
    eu_entries = countries["European Union"]["entries"]
    ag = next(item for item in eu_entries
              if item["section"] == "Opinions of the Advocates General")
    assert ag["section"] == "Opinions of the Advocates General"
    assert ag["count"] == 1
    assert ag["domains"] == ["curia.europa.eu"]
    assert ag["year_to"] == 2026
    berec = next(item for item in eu_entries if item["domains"] == ["berec.europa.eu"])
    assert berec["section"] != "Opinions of the Advocates General"

    page = render_sources_html(summary, intro="How these were collected.", facade=facade)
    assert "How these were collected." in page
    assert "British Columbia Court of Appeal (1 document, 2024)" in page
    assert 'href="https://canlii.org/"' in page
    assert "third-party bulk download, not scraped" in page
    assert "India" not in page and "example.in" not in page


def test_index_groups_by_jurisdiction_and_states_both_counts():
    page = render_index_html(
        [
            {"filename": "gdpr.html", "title": "GDPR", "short": "GDPR",
             "jurisdiction": "European Union", "documents": 1200, "mentions": 4800,
             "pending": 34,
             "exported": "27 May 2026", "note": "The <b>consolidated</b> text."},
            {"filename": "osa.html", "title": "Online Safety Act 2023",
             "jurisdiction": "United Kingdom", "documents": 0, "mentions": 0,
             "exported": "27 May 2026", "note": ""},
        ],
        title="Statutes",
        intro='Exported <dateexported>. <a href="https://example.test">Home</a>'
              '<script>alert(1)</script>',
    )
    assert 'href="gdpr.html"' in page                       # relative, one level, no dirs
    # Both totals, and the citation count is the bigger one: a document can cite twice.
    # One sentence, in commas — the annotation reads as prose, not as a field list.
    assert ("Last updated 27 May 2026, cited by 1,200 documents, "
            "4,800 citations in all, 34 pending CJEU cases.") in page
    # …and a law with nothing before the Court says nothing about it
    assert "pending CJEU" not in page.split("osa.html")[1]
    assert "·" not in page.split("<body")[1]                # no dotted fields in the list
    assert "<h2>" in page and "European Union" in page and "United Kingdom" in page
    assert '<span class="export-short">GDPR:</span>' in page
    assert "The <b>consolidated</b> text." in page          # per-item line, simple markup
    assert 'href="https://example.test"' in page
    assert "<script>alert(1)</script>" not in page          # sanitised like the attribution
    assert "<dateexported>" not in page                     # placeholder was substituted


def test_index_sources_link_sits_between_intro_and_statutes():
    page = render_index_html(
        [{"filename": "x.html", "title": "Act", "jurisdiction": "United Kingdom",
          "documents": 1, "mentions": 1, "exported": "27 May 2026"}],
        title="Statutes", intro="The description.", corpus_total=6_000_000,
        sources_page=True)
    assert "6,000,000 documents analysed" in page
    assert 'href="sources.html"' in page
    assert page.index("The description.") < page.index("sources.html") < page.index("x.html")


def test_index_falls_back_when_an_edition_has_no_jurisdiction():
    page = render_index_html(
        [{"filename": "x.html", "title": "Some Instrument", "documents": 3,
          "mentions": 3, "exported": "27 May 2026"}],
        title="Statutes", intro="")
    assert "Other instruments" in page
    assert "Last updated 27 May 2026, cited by 3 documents, 3 citations in all." in page


def test_blank_lines_in_the_settings_prose_become_paragraphs():
    """A textarea carries no markup, so its blank lines are the only structure the
    writer has: they must survive into the page."""
    page = render_index_html(
        [{"filename": "x.html", "title": "Some Instrument", "documents": 1,
          "mentions": 1, "exported": "27 May 2026",
          "note": "First line.\nStill the first paragraph.\n\nA second paragraph."}],
        title="Statutes",
        intro="An opening line.\n\nA second thought entirely.")
    assert page.count('<p class="attribution">') == 2
    assert "An opening line." in page and "A second thought entirely." in page
    assert page.count('<p class="export-note">') == 2
    assert "First line.<br>Still the first paragraph." in page
    assert "<p class=\"export-note\">A second paragraph.</p>" in page


def test_build_writes_folder_and_zip_with_per_item_notes(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setenv("RAGLEX_STATIC_EXPORT_ATTRIBUTION", "Held by <b>the operator</b>.")
    _hold(config, "ukpga/2023/50", "Online Safety Act 2023", "Section 1\nA provision.")
    _hold(config, "ukpga/2018/12", "Data Protection Act 2018", "Section 1\nAnother.")
    settings = SettingsStore(config.settings_path)
    save_config(settings, {
        "items": [
            {"stable_id": "ukpga/2023/50", "slug": "osa", "title": "Online Safety Act 2023",
             "short": "OSA", "note": "Only <i>this</i> file says so."},
            {"stable_id": "ukpga/2018/12", "slug": "dpa", "title": "Data Protection Act 2018"},
        ],
        "index_title": "Statutes",
        "index_text": "Exported <dateexported>.",
        "sources_page": True,
        "sources_intro": "The full-text corpus behind these editions.",
    }, config)

    facade = Facade(config)
    seen: list[dict] = []
    result = build_bundle(facade, {"zip": True}, lambda **p: seen.append(p))

    out = tmp_path / "exports" / "site"
    assert result["documents"] == 2
    assert sorted(p.name for p in out.glob("*.html")) == [
        "dpa.html", "index.html", "osa.html", "sources.html"]

    osa = (out / "osa.html").read_text(encoding="utf-8")
    dpa = (out / "dpa.html").read_text(encoding="utf-8")
    # shared line on both; the item's own line only on its own file, beneath the shared one
    assert osa.count("Held by <b>the operator</b>.") == 1
    assert "Only <i>this</i> file says so." in osa
    assert "Only <i>this</i> file says so." not in dpa
    assert osa.index("Held by") < osa.index("Only <i>this</i>")

    # Every edition links back to the index by its title — from the contents column,
    # under the name the set gave the law, where a reader looks for navigation.
    assert '<p class="contents-back"><a href="index.html">Back to Statutes</a></p>' in osa
    assert '<p class="contents-back"><a href="index.html">Back to Statutes</a></p>' in dpa
    assert '<p class="contents-title">OSA</p>' in osa
    assert '<p class="contents-title">Data Protection Act 2018</p>' in dpa

    # The tab, not the <h1>: the short name the operator gave the law, and the set.
    assert "<title>Crossreferenced OSA - Statutes</title>" in osa
    assert "<title>Crossreferenced Data Protection Act 2018 - Statutes</title>" in dpa

    index = (out / "index.html").read_text(encoding="utf-8")
    assert '<a href="osa.html">' in index and '<a href="dpa.html">' in index
    assert '<a href="sources.html">' in index
    assert "2 documents analysed to make this system" in index
    assert "<title>Statutes</title>" in index
    sources = (out / "sources.html").read_text(encoding="utf-8")
    assert "The full-text corpus behind these editions." in sources
    assert "United Kingdom (2 full-text documents)" in sources

    with zipfile.ZipFile(result["zip"]) as archive:
        assert sorted(archive.namelist()) == [
            "dpa.html", "index.html", "osa.html", "sources.html"]

    # progress is descriptive: which edition, its position, and what is happening
    messages = [p.get("item", "") for p in seen]
    assert any("Online Safety Act 2023 (1 of 2)" in m for m in messages)
    assert any("index.html" in m for m in messages)
    assert seen[-1]["total"] == 3  # two editions + the index step


def test_rerender_reuses_the_built_payload(tmp_path, monkeypatch):
    """Editing a note must not re-read the corpus — that is the hours-long half."""
    config = _config(tmp_path)
    _hold(config, "ukpga/2023/50", "Online Safety Act 2023", "Section 1\nA provision.")
    settings = SettingsStore(config.settings_path)
    save_config(settings, {"items": [{"stable_id": "ukpga/2023/50", "slug": "osa"}]}, config)
    facade = Facade(config)
    build_bundle(facade, {"zip": False})

    calls: list[str] = []
    from raglex import static_bundle

    monkeypatch.setattr(static_bundle, "build_static_export_cache",
                        lambda *a, **k: calls.append("built"))
    save_config(settings, {"items": [
        {"stable_id": "ukpga/2023/50", "slug": "osa", "note": "A late addition."}]}, config)
    build_bundle(facade, {"zip": False, "refresh": False})

    assert calls == []  # nothing rebuilt
    assert "A late addition." in (tmp_path / "exports" / "site" / "osa.html").read_text()


def test_build_without_items_is_an_error_not_an_empty_site(tmp_path):
    facade = Facade(_config(tmp_path))
    assert "error" in build_bundle(facade, {})


def test_the_scheduler_sees_an_edit_without_a_restart(tmp_path, monkeypatch):
    """The API edits the set; the SCHEDULER (another container) exports it. apply_to_env
    never overwrites a variable already in the process, so a stale value in the
    environment must not win over the file the other process just wrote."""
    config = _config(tmp_path)
    save_config(SettingsStore(config.settings_path),
                {"items": [{"stable_id": "ukpga/2023/50", "slug": "osa"}]}, config)
    monkeypatch.setenv("RAGLEX_STATIC_BUNDLE", json.dumps(
        {"items": [{"stable_id": "stale/1", "slug": "stale"}]}))

    assert [i["slug"] for i in load_config(config)["items"]] == ["osa"]
    # with no Config to locate the file, the environment is all there is
    assert [i["slug"] for i in load_config()["items"]] == ["stale"]


def test_index_title_can_be_rendered_as_wordart_and_is_off_by_default():
    entries = [{"filename": "gdpr.html", "title": "GDPR", "jurisdiction": "European Union",
                "documents": 1, "mentions": 1, "exported": "27 May 2026"}]
    plain = render_index_html(entries, title="Statutes", intro="")
    assert "wordart" not in plain                       # opt-in, and costs nothing when off

    fancy = render_index_html(entries, title="Statutes", intro="", wordart=True)
    assert '<span class="wordart rainbow">' in fancy
    assert 'data-text="Statutes"' in fancy
    assert "linear-gradient(to right, #b306a9" in fancy   # the rainbow theme itself
    assert "text-shadow: 0.02em 0.02em 0 #b9b2a4" in fancy
    # The heading is still a heading with the title as its text.
    assert "<h1>" in fancy and ">Statutes</span>" in fancy
    # …and it degrades to plain ink where the gradient can't be clipped, or on paper.
    assert "@supports not ((-webkit-background-clip: text)" in fancy
    assert "@media print" in fancy


def test_wordart_choice_round_trips_through_the_stored_plan(tmp_path):
    config = _config(tmp_path)
    settings = SettingsStore(config.settings_path)
    assert load_config(config)["index_wordart"] is False
    saved = save_config(settings, {"items": [], "index_wordart": True}, config)
    assert saved["index_wordart"] is True
    assert load_config(config)["index_wordart"] is True


def test_a_configured_site_address_produces_a_sitemap_and_canonicals(tmp_path, monkeypatch):
    """The whole sitemap branch only runs when a base url is set, so nothing exercised it
    — and it carried `started[:10]` over a datetime, which raised at the very END of a
    build, after every page had been written and with no output to show for it."""
    config = _config(tmp_path)
    monkeypatch.setenv("RAGLEX_STATIC_BASE_URL", "https://law.example.org/")
    _hold(config, "ukpga/2018/12", "Data Protection Act 2018", "Section 1\nA provision.")
    settings = SettingsStore(config.settings_path)
    save_config(settings, {
        "items": [{"stable_id": "ukpga/2018/12", "slug": "dpa",
                   "title": "Data Protection Act 2018"}],
        "index_title": "Statutes", "index_text": "Exported <dateexported>.",
    }, config)

    result = build_bundle(Facade(config), {}, lambda **p: None)
    assert "error" not in result

    out = tmp_path / "exports" / "site"
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://law.example.org/dpa.html</loc>" in sitemap
    assert "<loc>https://law.example.org/index.html</loc>" in sitemap
    assert "<lastmod>" in sitemap and "<urlset" in sitemap
    robots = (out / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://law.example.org/sitemap.xml" in robots
    # and the page it points at claims that address as its canonical
    page = (out / "dpa.html").read_text(encoding="utf-8")
    assert '<link rel="canonical" href="https://law.example.org/dpa.html">' in page


def test_without_a_configured_address_no_sitemap_is_invented(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.delenv("RAGLEX_STATIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAGLEX_PUBLIC_URL", raising=False)
    _hold(config, "ukpga/2018/12", "Data Protection Act 2018", "Section 1\nA provision.")
    save_config(SettingsStore(config.settings_path), {
        "items": [{"stable_id": "ukpga/2018/12", "slug": "dpa", "title": "DPA"}],
        "index_title": "Statutes", "index_text": "x",
    }, config)
    build_bundle(Facade(config), {}, lambda **p: None)
    out = tmp_path / "exports" / "site"
    assert not (out / "sitemap.xml").exists()
    assert not (out / "robots.txt").exists()


# --- themed subsections within a country ------------------------------------

_THEMES = [
    {"name": "Data Protection and Privacy", "tag": "data-protection-and-privacy"},
    {"name": "Platform Regulation", "tag": "platform-regulation"},
    {"name": "Policing and Security", "tag": "policing-and-security"},
]


def _themed_entries():
    return [
        {"filename": "dsa.html", "title": "Digital Services Act", "jurisdiction": "European Union",
         "documents": 90, "mentions": 400, "tags": ["platform-regulation"]},
        {"filename": "gdpr.html", "title": "General Data Protection Regulation",
         "jurisdiction": "European Union", "documents": 5000, "mentions": 90000,
         "tags": ["Data Protection and Privacy"]},          # matched case-insensitively
        {"filename": "led.html", "title": "Law Enforcement Directive",
         "jurisdiction": "European Union", "documents": 120, "mentions": 800,
         "tags": ["data-protection-and-privacy", "policing-and-security"]},
        {"filename": "eidas.html", "title": "eIDAS Regulation", "jurisdiction": "European Union",
         "documents": 8, "mentions": 20, "tags": []},
    ]


def _headings(page: str) -> list[str]:
    import re
    return re.findall(r"<h3>(.*?)</h3>", page)


def _order(page: str, *filenames: str) -> list[str]:
    return sorted(filenames, key=lambda f: page.index(f'href="{f}"'))


def test_themes_appear_in_the_operators_order_not_the_laws():
    """The sequence of subsections is an editorial judgement, so it comes from the
    configured list — not from the order the laws were added, and not alphabetically."""
    page = render_index_html(_themed_entries(), title="Statutes", intro="",
                             groups=_THEMES)

    assert _headings(page) == ["Data Protection and Privacy", "Platform Regulation",
                               "Policing and Security", "Other instruments"]
    # …and reversing the configuration reverses the page
    page = render_index_html(_themed_entries(), title="Statutes", intro="",
                             groups=list(reversed(_THEMES)))
    assert _headings(page)[:3] == ["Policing and Security", "Platform Regulation",
                                   "Data Protection and Privacy"]


def test_within_a_theme_the_most_cited_law_comes_first():
    page = render_index_html(_themed_entries(), title="Statutes", intro="",
                             groups=_THEMES)
    # GDPR (90,000 citations) above the LED (800), whichever order they were configured in
    assert _order(page, "gdpr.html", "led.html") == ["gdpr.html", "led.html"]


def test_a_law_with_two_themes_is_listed_under_both():
    """The Law Enforcement Directive is a privacy instrument and a policing instrument.
    Making the operator pick one would misrepresent the law to save a line."""
    page = render_index_html(_themed_entries(), title="Statutes", intro="",
                             groups=_THEMES)
    assert page.count('href="led.html"') == 2


def test_an_untagged_law_is_listed_last_rather_than_dropped():
    page = render_index_html(_themed_entries(), title="Statutes", intro="",
                             groups=_THEMES)
    assert _headings(page)[-1] == "Other instruments"
    assert 'href="eidas.html"' in page
    # tag it, and the catch-all heading disappears
    tagged = [{**e, "tags": e["tags"] or ["platform-regulation"]} for e in _themed_entries()]
    assert "Other instruments" not in "".join(_headings(
        render_index_html(tagged, title="Statutes", intro="", groups=_THEMES)))


def test_a_dotted_rule_closes_each_subsection_and_none_opens_the_first():
    """MS Word's shape: the country keeps its one solid rule, each theme is closed by a
    dotted one of the same width, and nothing sits between the country and its first
    theme."""
    page = render_index_html(_themed_entries(), title="Statutes", intro="",
                             groups=_THEMES)
    body = page.split("<main>")[1]
    assert body.count('<p class="export-rule">') == 4          # one per subsection
    # nothing between the country heading and the first italic subheading
    between = body.split("</h2>")[1].split("<h3>")[0]
    assert "export-rule" not in between
    assert ".export-rule" in page and "1px dotted var(--ink)" in page
    assert "max-width: 52rem" in page                          # the country rule's width
    assert "font-style: italic" in page


def test_an_unthemed_set_renders_exactly_as_it_always_did():
    """A set that has never been themed must not grow headings or rules."""
    page = render_index_html(_themed_entries(), title="Statutes", intro="", groups=[])
    assert "<h3>" not in page and 'class="export-rule"' not in page
    # …and the operator's own order is preserved, not re-sorted by citations
    assert _order(page, "dsa.html", "gdpr.html") == ["dsa.html", "gdpr.html"]
    assert page.index('href="dsa.html"') < page.index('href="gdpr.html"')


def test_a_country_no_theme_reaches_stays_one_plain_list():
    page = render_index_html(
        [{"filename": "osa.html", "title": "Online Safety Act 2023",
          "jurisdiction": "United Kingdom", "documents": 1, "mentions": 1, "tags": []},
         {"filename": "dpa.html", "title": "Data Protection Act 2018",
          "jurisdiction": "United Kingdom", "documents": 9, "mentions": 9, "tags": []}],
        title="Statutes", intro="", groups=_THEMES)
    assert "<h3>" not in page
    assert page.index('href="osa.html"') < page.index('href="dpa.html"')


def test_the_eea_relevance_note_is_dropped_from_the_index():
    """A statement about territorial scope, not part of the name — and forty repetitions
    of the same eight words down a page of EU instruments."""
    from raglex.static_bundle import strip_eea_relevance

    assert strip_eea_relevance(
        "Regulation (EU) 2022/2065 … (Digital Services Act) (Text with EEA relevance)"
    ) == "Regulation (EU) 2022/2065 … (Digital Services Act)"
    assert strip_eea_relevance("Directive 2002/58/EC (Text with EEA relevance).") \
        == "Directive 2002/58/EC"
    assert strip_eea_relevance("Data Protection Act 2018") == "Data Protection Act 2018"

    page = render_index_html(
        [{"filename": "dsa.html", "jurisdiction": "European Union", "documents": 1,
          "mentions": 1, "title": "Digital Services Act (Text with EEA relevance)"}],
        title="Statutes", intro="")
    assert "EEA relevance" not in page
    assert ">Digital Services Act</a>" in page


def test_themes_and_tags_survive_a_save_and_reload(tmp_path):
    """The plan is one settings row; a theme the operator arranged has to come back in
    the order they arranged it, and a law's tags have to come back with the law."""
    config = _config(tmp_path)
    settings = SettingsStore(config.settings_path)
    saved = save_config(settings, {
        "groups": [{"name": "Platform Regulation"},
                   {"name": "Data Protection and Privacy", "tag": "Privacy"},
                   {"name": ""},                                  # nameless → dropped
                   {"name": "Duplicate", "tag": "privacy"}],      # same tag → dropped
        "items": [{"stable_id": "32022R2065", "slug": "dsa",
                   "tags": ["Platform Regulation", "platform-regulation", ""]}],
    })
    assert [g["name"] for g in saved["groups"]] == ["Platform Regulation",
                                                    "Data Protection and Privacy"]
    # a group with no tag of its own is keyed on its heading
    assert saved["groups"][0]["tag"] == "platform-regulation"
    assert saved["groups"][1]["tag"] == "privacy"
    # a law's tags are de-duplicated by the same key, keeping what was typed
    assert saved["items"][0]["tags"] == ["Platform Regulation"]
    assert load_config(config)["groups"] == saved["groups"]
