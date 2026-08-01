from __future__ import annotations

import json
import zipfile

import pytest

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade
from raglex.settings import SettingsStore
from raglex.static_bundle import (
    apply_placeholders,
    build_bundle,
    format_export_date,
    load_config,
    render_index_html,
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


def test_index_groups_by_jurisdiction_and_states_both_counts():
    page = render_index_html(
        [
            {"filename": "gdpr.html", "title": "GDPR", "short": "GDPR",
             "jurisdiction": "European Union", "documents": 1200, "mentions": 4800,
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
            "4,800 citations in all.") in page
    assert "·" not in page.split("<body")[1]                # no dotted fields in the list
    assert "<h2>" in page and "European Union" in page and "United Kingdom" in page
    assert '<span class="export-short">GDPR:</span>' in page
    assert "The <b>consolidated</b> text." in page          # per-item line, simple markup
    assert 'href="https://example.test"' in page
    assert "<script>alert(1)</script>" not in page          # sanitised like the attribution
    assert "<dateexported>" not in page                     # placeholder was substituted


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
    }, config)

    facade = Facade(config)
    seen: list[dict] = []
    result = build_bundle(facade, {"zip": True}, lambda **p: seen.append(p))

    out = tmp_path / "exports" / "site"
    assert result["documents"] == 2
    assert sorted(p.name for p in out.glob("*.html")) == ["dpa.html", "index.html", "osa.html"]

    osa = (out / "osa.html").read_text(encoding="utf-8")
    dpa = (out / "dpa.html").read_text(encoding="utf-8")
    # shared line on both; the item's own line only on its own file, beneath the shared one
    assert osa.count("Held by <b>the operator</b>.") == 1
    assert "Only <i>this</i> file says so." in osa
    assert "Only <i>this</i> file says so." not in dpa
    assert osa.index("Held by") < osa.index("Only <i>this</i>")

    # every edition links back to the index, by its title, under the custom text
    assert 'Return to <a href="index.html">Statutes</a>.' in osa
    assert 'Return to <a href="index.html">Statutes</a>.' in dpa
    assert osa.index("Only <i>this</i>") < osa.index("Return to")

    # The tab, not the <h1>: the short name the operator gave the law, and the set.
    assert "<title>Crossreferenced OSA - Statutes</title>" in osa
    assert "<title>Crossreferenced Data Protection Act 2018 - Statutes</title>" in dpa

    index = (out / "index.html").read_text(encoding="utf-8")
    assert '<a href="osa.html">' in index and '<a href="dpa.html">' in index
    assert "<title>Statutes</title>" in index

    with zipfile.ZipFile(result["zip"]) as archive:
        assert sorted(archive.namelist()) == ["dpa.html", "index.html", "osa.html"]

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
