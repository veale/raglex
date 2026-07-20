"""Corpus-wide shorthand store: a short name learned in one document, applied in
another — but only under gates tight enough that it can't manufacture false links.

The gates under test (the owner's specification): the citing document must already
cite the parent by some other means; a case short-name still needs a pincite; an
ambiguous shorthand is never guessed; an in-document definition always wins.
"""

from __future__ import annotations

from datetime import date

from raglex.citations import extract_document
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.storage import TextStore


def _doc(catalogue, ts, stable_id, text, **kw):
    rec = Record(source=kw.get("source", "x"), stable_id=stable_id,
                 doc_type=kw.get("doc_type", DocType.JUDGMENT),
                 decision_date=date(2024, 1, 1), text=text, raw_bytes=text.encode(),
                 extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def _run(catalogue, ts, stable_id, text, **kw):
    _doc(catalogue, ts, stable_id, text, **kw)
    extract_document(catalogue, ts, stable_id)
    return [c for c in catalogue.citations_for(stable_id)]


def _global(cites):
    return {(c["candidate_id"], c["pinpoint"]) for c in cites
            if c["method"] == "shorthand_global"}


# "Suncor Energy Inc v Canada, 2021 FC 138 … [Suncor]" — the OSCOLA short-title
# convention, the definition that seeds the store.
DEF_A = "Suncor Energy Inc v Canada, 2021 FC 138 at para 64 [Suncor]. The appeal failed."


def test_shorthand_learned_in_one_document_links_in_another(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "fc/2020/1", DEF_A)
    assert catalogue.count_learned_shorthands() >= 1

    # B cites the parent in full AND uses the short name with a pincite → linked
    cites = _run(catalogue, ts, "fc/2020/2",
                 "The court applied 2021 FC 138. Suncor, at para 30, is decisive.")
    assert ("fc/2021/138", "para 30") in _global(cites)


def test_stored_shorthand_never_applies_without_the_parent_cited(catalogue, tmp_path):
    # The parent-cited gate — the whole point of the feature. A document that never
    # cites Suncor Energy must not link a bare "Suncor" to it, pincite or no.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "fc/2020/1", DEF_A)
    cites = _run(catalogue, ts, "fc/2020/3", "Suncor, at para 30, was not followed here.")
    assert not _global(cites)


def test_stored_case_shortname_requires_a_pincite(catalogue, tmp_path):
    # A case short-name is an ordinary word; without a paragraph pincite it is far
    # too weak a signal, exactly as the in-document rule already holds.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "fc/2020/1", DEF_A)
    bare = _run(catalogue, ts, "fc/2020/4",
                "The court applied 2021 FC 138. Suncor is a large company.")
    assert not _global(bare)
    pincited = _run(catalogue, ts, "fc/2020/5",
                    "The court applied 2021 FC 138. Suncor, at para 12, says otherwise.")
    assert ("fc/2021/138", "para 12") in _global(pincited)


def test_ambiguous_shorthand_is_not_guessed(catalogue, tmp_path):
    # "Vector" is registered against two different cases. In a document citing BOTH
    # parents there is no basis to choose, so nothing links; in a document citing only
    # one, the document itself has disambiguated it and that one links.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "fc/2019/1", "Vector Energy Ltd v Canada, 2021 FC 138 at para 1 [Vector].")
    _run(catalogue, ts, "scc/2019/1", "Vector Holdings v Ontario, 2008 SCC 9 at para 2 [Vector].")

    both = _run(catalogue, ts, "fc/2019/2",
                "Both 2021 FC 138 and 2008 SCC 9 were cited. Vector, at para 7, is relevant.")
    assert not _global(both)

    one = _run(catalogue, ts, "fc/2019/3",
               "Only 2021 FC 138 was cited. Vector, at para 7, is relevant.")
    assert _global(one) == {("fc/2021/138", "para 7")}


def test_in_document_definition_beats_the_stored_one(catalogue, tmp_path):
    # B defines "Suncor" for itself, against a different case, while also citing the
    # case the store maps "Suncor" to. The document's own definition wins.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "fc/2020/1", DEF_A)
    cites = _run(catalogue, ts, "scc/2020/1",
                 "Suncor Nova Scotia Ltd v Ontario, 2008 SCC 9 [Suncor]. The court also "
                 "considered 2021 FC 138. Suncor, at para 12, controls.")
    linked = {(c["candidate_id"], c["pinpoint"]) for c in cites
              if c["method"] in ("shorthand", "shorthand_global")}
    assert ("scc/2008/9", "para 12") in linked
    assert ("fc/2021/138", "para 12") not in linked


def test_statute_abbreviation_links_on_a_bare_mention(catalogue, tmp_path):
    # The asymmetry the owner asked for: an initialism hosted by a statute is
    # distinctive enough to link without a pincite — still only where the parent is cited.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "uksc/2020/1",
         'This turns on the Data Protection Act 2018 (the "DPA") throughout.')
    cites = _run(catalogue, ts, "uksc/2020/2",
                 "The Data Protection Act 2018 governs. Section 2 of the DPA is engaged.")
    assert any(cid == "ukpga/2018/12" for cid, _ in _global(cites))


def test_common_initialism_needs_more_than_a_bare_mention(catalogue, tmp_path):
    # "CA" is a Court of Appeal a hundred times for every time it is the Competition
    # Act, so it drops back to the pincite rule even when its parent IS cited — and in
    # a document that doesn't cite the parent it links nothing at all.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "ca/2020/1",
         'The Competition Act, RSC 1985, c C-34 (the "CA") governs mergers.')
    cited_parent = _run(catalogue, ts, "ca/2020/2",
                        "Under RSC 1985, c C-34 the test is clear. The CA applies here.")
    assert not _global(cited_parent)
    no_parent = _run(catalogue, ts, "ca/2020/3", "The CA allowed the appeal.")
    assert not _global(no_parent)


def test_population_is_idempotent(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "fc/2020/1", DEF_A)
    extract_document(catalogue, ts, "fc/2020/1")
    first = catalogue.count_learned_shorthands()
    extract_document(catalogue, ts, "fc/2020/1")
    assert catalogue.count_learned_shorthands() == first
    # and idempotent across processes too — the in-memory "already stored" filter is an
    # optimisation, not the guarantee; the ON CONFLICT is
    from raglex.citations.stage import reset_shorthand_cache
    reset_shorthand_cache()
    extract_document(catalogue, ts, "fc/2020/1")
    assert catalogue.count_learned_shorthands() == first


def test_kill_switch_disables_both_halves(catalogue, tmp_path, monkeypatch):
    ts = TextStore(tmp_path / "text")
    monkeypatch.setenv("RAGLEX_SHORTHAND_GLOBAL", "0")
    _run(catalogue, ts, "fc/2020/1", DEF_A)
    assert catalogue.count_learned_shorthands() == 0
