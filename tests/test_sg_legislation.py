"""Singapore legislation: SSO parsing, identity, and the seed importer.

Singapore has no ELI and no search API, so identity is SSO's own act code — and the seed
snapshot's names are truncated at 50 characters, so the tests pin the two things that make
the seed usable: matching a truncated name to a code by prefix, and recovering the full
title from an Act's front matter when no match exists.
"""

from __future__ import annotations

import pytest

from raglex.adapters.sg_legislation import (
    BrowseEntry, Provision, SGLegislationAdapter, browse_results_count, name_key,
    parse_act_page, parse_browse, provisions_to_segments, sg_act_id, sg_sl_id,
    title_from_frontmatter,
)
from raglex.config import Config
from raglex.facade import Facade


# ── identity ────────────────────────────────────────────────────────────────

def test_stable_ids_key_on_the_sso_code():
    assert sg_act_id("CoA1967") == "sg/act/coa1967"
    assert sg_sl_id("SCJA1969-N2") == "sg/sl/scja1969-n2"


def test_name_key_normalises_for_prefix_matching():
    # the seed truncates at 50 chars — its key must be a prefix of the full title's key
    full = name_key("Accounting and Corporate Regulatory Authority Act 2004")
    trunc = name_key("Accounting and Corporate Regulatory Authority Act ")
    assert full.startswith(trunc)


def test_title_recovered_from_front_matter():
    fm = ("THE STATUTES OF THE REPUBLIC OF SINGAPORE ACCOUNTANTS ACT 2004 "
          "2020 REVISED EDITION This revised edition incorporates all amendments…")
    assert title_from_frontmatter(fm) == "Accountants Act 2004"
    assert title_from_frontmatter("no title here") is None


# ── browse listing ──────────────────────────────────────────────────────────

_BROWSE = """
<a href="/Act/AA2004" class="dropdown-item add-to-collection" data-legisTitle="Accountants Act 2004">x</a>
<a href="/Act/AA2004" class="add-to-collection" data-legisTitle="Accountants Act 2004">y</a>
<a href="/SL/SCJA1969-N2?DocDate=19970926" class="add-to-collection" data-legisTitle="Accountant-General Appointed to be Accountant for Supreme Court">z</a>
<span>523 results</span>
"""


def test_parse_browse_dedups_and_reads_code_and_kind():
    entries = parse_browse(_BROWSE)
    assert entries == [
        BrowseEntry(code="AA2004", title="Accountants Act 2004", subsidiary=False),
        BrowseEntry(code="SCJA1969-N2",
                    title="Accountant-General Appointed to be Accountant for Supreme Court",
                    subsidiary=True),
    ]
    assert browse_results_count(_BROWSE) == 523


# ── act page parsing (incl. the lazy-load signal) ───────────────────────────

def _act_html(bodies: dict[str, tuple[str, str]], toc: list[str]) -> str:
    """Build an SSO-shaped act page: `bodies` are the rendered provisions, `toc` the full
    section list (a large Act renders fewer bodies than the TOC lists)."""
    provs = "".join(
        f'<div class="prov1"><table><tr><td class="prov1Hdr" id="pr{n}-">{cap}</td></tr>'
        f'</table><table><tr><td class="prov1Txt"><strong>{n}.</strong>&#xA0;{txt}</td>'
        f'</tr></table></div>'
        for n, (cap, txt) in bodies.items())
    anchors = "".join(f'<a href="#pr{n}-">s{n}</a>' for n in toc)
    return (f"<html><head><title>Companies Act 1967 - Singapore Statutes Online</title></head>"
            f"<body>{anchors}{provs}</body></html>")


def test_parse_act_reads_title_provisions_and_toc():
    html = _act_html({"1": ("Short title", "This Act is the Companies Act 1967."),
                      "2": ("Division into Parts", "This Act is divided into Parts.")},
                     toc=["1", "2"])
    act = parse_act_page(html)
    assert act.title == "Companies Act 1967"
    assert [p.num for p in act.provisions] == ["1", "2"]
    assert act.provisions[0].caption == "Short title"
    assert "Companies Act 1967" in act.provisions[0].text
    assert act.lazy is False   # TOC == rendered


def test_lazy_flag_set_when_toc_exceeds_rendered_bodies():
    html = _act_html({"1": ("Short title", "…")}, toc=["1", "2", "3", "4", "5"])
    act = parse_act_page(html)
    assert act.lazy is True and act.toc_nums == ["1", "2", "3", "4", "5"]


def test_provisions_to_segments_offsets_index_into_the_text():
    text, segs = provisions_to_segments([
        Provision(num="1", caption="Short title", text="This Act is the X Act."),
        Provision(num="2", caption=None, text="Second section body."),
    ])
    assert len(segs) == 2
    for s in segs:
        assert 0 <= s.char_start <= s.char_end <= len(text)
        assert s.kind == "section"
    assert text[segs[0].char_start:segs[0].char_end].startswith("1")


# ── ongoing adapter: lazy backfill via ?ProvIds ─────────────────────────────

class _FakeClient:
    """Serves the base page once (lazy: 1 body, TOC of 3) then one body per ?ProvIds call."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, params=None, **kw):
        self.calls.append(f"{url}?{params}")
        if params and "ProvIds" in params:
            num = params["ProvIds"].removeprefix("pr").rstrip("-")
            html = _act_html({num: (f"Cap {num}", f"Body of section {num}.")}, toc=["1", "2", "3"])
        else:
            html = _act_html({"1": ("Short title", "First.")}, toc=["1", "2", "3"])

        class _R:
            text = html
        return _R()


def test_fetch_backfills_lazy_loaded_provisions():
    client = _FakeClient()
    adapter = SGLegislationAdapter(client=client)
    from raglex.core.models import Stub

    rec = adapter.fetch(Stub(stable_id="sg/act/coa1967",
                             landing_url="https://sso.agc.gov.sg/Act/CoA1967",
                             hints={"code": "CoA1967"}))
    assert rec.stable_id == "sg/act/coa1967"
    # all three sections present after backfilling 2 and 3
    assert "Body of section 2." in rec.text and "Body of section 3." in rec.text
    assert len([s for s in rec.segments]) == 3
    assert rec.extra["sso_code"] == "CoA1967" and rec.extra["is_authoritative"] is False


# ── seed importer ────────────────────────────────────────────────────────────

@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def _write_seed(tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    docs = pa.table({
        "name": ["Accountants Act 2004", "Accounting and Corporate Regulatory Authority Act "],
        "doc_type": ["act", "act"], "parent_act": ["", ""],
        "page_count": [123, 87], "num_sections": [2, 1]})
    sections = pa.table({
        "doc_name": ["Accountants Act 2004", "Accountants Act 2004",
                     "Accounting and Corporate Regulatory Authority Act "],
        "doc_type": ["act", "act", "act"], "parent_act": ["", "", ""],
        "section_title": ["Unsectioned", "1. Short title", "1. Short title"],
        "part": ["", "PART 1", "PART 1"], "division": ["", "", ""],
        "text": ["THE STATUTES OF THE REPUBLIC OF SINGAPORE ACCOUNTANTS ACT 2004 2020 REVISED EDITION",
                 "This Act is the Accountants Act 2004.",
                 "This Act is the ACRA Act 2004."]})
    pq.write_table(docs, tmp_path / "documents.parquet")
    pq.write_table(sections, tmp_path / "sections.parquet")
    return str(tmp_path)


def test_seed_imports_sections_and_recovers_full_title_without_reconcile(facade, tmp_path):
    path = _write_seed(tmp_path)
    # reconcile=False → no network; identity falls back to name-slug, title from front matter
    st = facade.import_sg_seed(dir_path=path, reconcile=False)
    assert st["documents"] == 2 and st["imported"] == 2
    body = facade.document_body("sg/act/accountants-act-2004")
    assert body["text"] and "Short title" in "".join(
        s["label"] for s in body["segments"])
    doc = facade.get_document("sg/act/accountants-act-2004")
    assert doc["title"] == "Accountants Act 2004"        # full, not truncated
    assert doc["doc_type"] == "legislation"


def test_seed_reconciles_truncated_name_to_sso_code(facade, tmp_path, monkeypatch):
    path = _write_seed(tmp_path)
    # fake the browse index so no network is hit
    from raglex.adapters import sg_legislation as sg

    def fake_browse(self, *, max_pages=None):
        if not self.subsidiary:
            yield sg.BrowseEntry("AA2004", "Accountants Act 2004", False)
            yield sg.BrowseEntry("ACRAA2004",
                                 "Accounting and Corporate Regulatory Authority Act 2004", False)
    monkeypatch.setattr(sg.SGLegislationAdapter, "browse_index", fake_browse)

    st = facade.import_sg_seed(dir_path=path, reconcile=True)
    assert st["reconciled"] == 2 and st["unmatched"] == 0
    # keyed by the real SSO code, full title, landing url present
    doc = facade.get_document("sg/act/acraa2004")
    assert doc["title"] == "Accounting and Corporate Regulatory Authority Act 2004"
    assert doc["landing_url"] == "https://sso.agc.gov.sg/Act/ACRAA2004"


def test_seed_reimport_is_idempotent(facade, tmp_path):
    path = _write_seed(tmp_path)
    facade.import_sg_seed(dir_path=path, reconcile=False)
    again = facade.import_sg_seed(dir_path=path, reconcile=False)
    assert again["imported"] == 0 and again["skipped"] == 2
