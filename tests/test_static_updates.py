from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from raglex.static_updates import render_cached_updates, render_updates_atom, render_updates_html


def _data():
    return {
        "generated_at": "2026-10-12T09:00:00+00:00",
        "law": {
            "stable_id": "32022R2065", "short_title": "Regulation (EU) 2022/2065",
            "title": "Digital Services Act", "jurisdiction": "European Union",
        },
        "groups": [
            {
                "id": "case/new", "title": "New case", "cite": "New case [2026] EU 2",
                "date": "2026-09-10", "added": "2026-10-11T12:00:00+00:00",
                "kind": "cases", "jurisdiction": "Germany", "court": "Federal Court",
                "source_label": "Court register",
                "links": [{"url": "https://example.test/new", "label": "source"}],
                "snippets": [{"text": "The DSA applies here.", "mark": [4, 7],
                              "where": "paragraph 12", "raw": "DSA"}],
            },
            {
                "id": "case/old", "title": "Old case", "cite": "Old case [2025] EU 1",
                "date": "2025-02-01", "added": "2026-10-01T12:00:00+00:00",
                "kind": "cases", "jurisdiction": "France", "court": "Court of Cassation",
                "source_label": "Court register", "links": [], "snippets": [],
            },
            {
                "id": "statute/1", "title": "A statute", "cite": "A statute 2026",
                "date": "2026-08-01", "added": "2026-10-12T08:00:00+00:00",
                "kind": "legislation", "jurisdiction": "Ireland", "court": None,
                "source_label": "Statute book", "links": [], "snippets": [],
            },
        ],
    }


def test_updates_page_defaults_to_cases_and_exports_the_current_view_as_csv():
    page = render_updates_html(
        _data(), slug="dsa-updates", atom_filename="dsa-updates.atom",
        canonical="https://laws.example/dsa-updates.html").decode()
    assert "Citing updates for Regulation (EU) 2022/2065" in page
    assert '<option value="cases" selected>Cases</option>' in page
    assert "Statutes and other legislation" in page
    assert "Date added to RagLex" in page and "Date of judgment or document" in page
    assert "Export current view as CSV" in page
    assert 'font-family: "Times New Roman", Times, serif' in page
    assert "font-size: 12pt" in page and "color: #000" in page
    assert "background: #fff200" in page
    assert "font-size: 11" not in page and "text-transform: uppercase" not in page
    assert 'href="dsa-updates.atom"' in page
    # Payload is inert JSON rather than interpolated HTML; the script escapes it on use.
    payload = page.split('<script id="updates-data" type="application/json">', 1)[1]
    rows = json.loads(payload.split("</script>", 1)[0])
    assert {row["kind"] for row in rows} == {"cases", "legislation"}


def test_atom_is_cases_only_and_newest_added_case_leads():
    atom = render_updates_atom(
        _data(), page_url="https://laws.example/dsa-updates.html",
        feed_url="https://laws.example/dsa-updates.atom")
    root = ET.fromstring(atom)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    assert [entry.findtext("a:title", namespaces=ns) for entry in entries] == [
        "New case [2026] EU 2", "Old case [2025] EU 1"]
    assert "A statute" not in atom.decode()
    assert root.findtext("a:updated", namespaces=ns) == "2026-10-11T12:00:00Z"
    assert "The DSA applies here." in entries[0].findtext("a:content", namespaces=ns)


def test_cached_renderer_uses_one_stem_for_html_and_atom(tmp_path):
    cache = tmp_path / "dsa.json"
    cache.write_text(json.dumps(_data()), encoding="utf-8")
    page, atom = render_cached_updates(
        {"_path": str(cache)}, slug="dsa", base_url="https://laws.example")
    assert b"dsa-updates.atom" in page
    assert b"https://laws.example/dsa-updates.html" in atom
    assert b"https://laws.example/dsa-updates.atom" in atom
