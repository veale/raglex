"""Corpus-wide shorthand store: a short name learned in one document, applied in
another — but only under gates tight enough that it can't manufacture false links.

The gates under test (the owner's specification): SEVERAL DOCUMENTS must have
independently established the name; the citing document must already cite the parent by
some other means; a case short-name still needs a pincite; an ambiguous shorthand is
never guessed; an in-document definition always wins.
"""

from __future__ import annotations

from datetime import date

from raglex.citations import extract_document
from raglex.citations.extractor import SHORTHAND_MIN_DOCS
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


def _establish(catalogue, ts, text, *, prefix, n=SHORTHAND_MIN_DOCS):
    """Have ``n`` DISTINCT documents define the same shorthand — what the store now
    requires before the name travels. The reload TTL is per-process and the cache is
    reset per test, so the last write is visible to the next extraction."""
    from raglex.citations.stage import reset_shorthand_cache

    for i in range(n):
        _run(catalogue, ts, f"{prefix}/{i}", text)
    reset_shorthand_cache()


def test_a_shorthand_travels_once_enough_documents_establish_it(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _establish(catalogue, ts, DEF_A, prefix="fc/2020/def")
    assert catalogue.count_learned_shorthands() >= 1

    # B cites the parent in full AND uses the short name with a pincite → linked
    cites = _run(catalogue, ts, "fc/2020/2",
                 "The court applied 2021 FC 138. Suncor, at para 30, is decisive.")
    assert ("fc/2021/138", "para 30") in _global(cites)


def test_one_documents_shorthand_stays_in_that_document(catalogue, tmp_path):
    """The popularity gate. A name a single document invented is exactly the shape of
    the damage the store did — "the BSB" was filed against the Human Rights Act by ONE
    judgment and then rendered 54 links in another. Below the threshold the definition
    still works where it was made; it just doesn't travel."""
    ts = TextStore(tmp_path / "text")
    own = _run(catalogue, ts, "fc/2020/1",
               DEF_A + " Suncor, at para 12, is decisive.")
    assert ("fc/2021/138", "para 12") in {
        (c["candidate_id"], c["pinpoint"]) for c in own if c["method"] == "shorthand"}

    from raglex.citations.stage import reset_shorthand_cache
    reset_shorthand_cache()
    cites = _run(catalogue, ts, "fc/2020/2",
                 "The court applied 2021 FC 138. Suncor, at para 30, is decisive.")
    assert not _global(cites)

    # a second document agreeing is still not enough; the third settles it
    _run(catalogue, ts, "fc/2020/3", DEF_A)
    reset_shorthand_cache()
    assert not _global(_run(catalogue, ts, "fc/2020/4",
                            "The court applied 2021 FC 138. Suncor, at para 30, holds."))
    _run(catalogue, ts, "fc/2020/5", DEF_A)
    reset_shorthand_cache()
    assert _global(_run(catalogue, ts, "fc/2020/6",
                        "The court applied 2021 FC 138. Suncor, at para 30, holds."))


def test_stored_shorthand_never_applies_without_the_parent_cited(catalogue, tmp_path):
    # The parent-cited gate — the whole point of the feature. A document that never
    # cites Suncor Energy must not link a bare "Suncor" to it, pincite or no.
    ts = TextStore(tmp_path / "text")
    _establish(catalogue, ts, DEF_A, prefix="fc/2020/def")
    cites = _run(catalogue, ts, "fc/2020/3", "Suncor, at para 30, was not followed here.")
    assert not _global(cites)


def test_stored_case_shortname_requires_a_pincite(catalogue, tmp_path):
    # A case short-name is an ordinary word; without a paragraph pincite it is far
    # too weak a signal, exactly as the in-document rule already holds.
    ts = TextStore(tmp_path / "text")
    _establish(catalogue, ts, DEF_A, prefix="fc/2020/def")
    bare = _run(catalogue, ts, "fc/2020/4",
                "The court applied 2021 FC 138. Suncor is a large company.")
    assert not _global(bare)
    pincited = _run(catalogue, ts, "fc/2020/5",
                    "The court applied 2021 FC 138. Suncor, at para 12, says otherwise.")
    assert ("fc/2021/138", "para 12") in _global(pincited)


def test_contested_shorthand_never_travels(catalogue, tmp_path):
    # "Vector" is registered against two different cases, so the store contradicts
    # itself about what the word means and NEITHER reading travels — not even into a
    # document that cites only one of the two parents.
    #
    # That last clause is the 2026-08 change. The rule used to let a contested name
    # through whenever exactly one of its candidates was cited, reasoning that the
    # document had disambiguated it. It hadn't: a document citing act X says nothing
    # about what an abbreviation it never defines means, so the test fires on
    # coincidence — and a contested entry is contested precisely because it was
    # mislearned. On the live corpus "PACE" was held against six acts, none of them
    # the Police and Criminal Evidence Act 1984, and a judgment that never spells PACE
    # out had "s. 8(1) of PACE" recorded as a citation of RIPA s.8(1) — RIPA being the
    # one owner it happened to cite. A document that defines its own shorthand never
    # needed the store; the in-document pass runs first and wins the span.
    ts = TextStore(tmp_path / "text")
    _establish(catalogue, ts, "Vector Energy Ltd v Canada, 2021 FC 138 at para 1 [Vector].",
               prefix="fc/2019/def")
    _establish(catalogue, ts, "Vector Holdings v Ontario, 2008 SCC 9 at para 2 [Vector].",
               prefix="scc/2019/def")

    both = _run(catalogue, ts, "fc/2019/2",
                "Both 2021 FC 138 and 2008 SCC 9 were cited. Vector, at para 7, is relevant.")
    assert not _global(both)

    one = _run(catalogue, ts, "fc/2019/3",
               "Only 2021 FC 138 was cited. Vector, at para 7, is relevant.")
    assert not _global(one)


def test_in_document_definition_beats_the_stored_one(catalogue, tmp_path):
    # B defines "Suncor" for itself, against a different case, while also citing the
    # case the store maps "Suncor" to. The document's own definition wins.
    ts = TextStore(tmp_path / "text")
    _establish(catalogue, ts, DEF_A, prefix="fc/2020/def")
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
    #
    # The well-known UK statutes now take a more direct route than the learned store:
    # naming the Act in full unlocks its conventional abbreviations for that document
    # (stage._UNLOCKED_BY_FULL_NAME), so "the DPA" resolves as a named alias. What
    # matters is that the bare mention links to the Act, not which pass got there.
    ts = TextStore(tmp_path / "text")
    _run(catalogue, ts, "uksc/2020/1",
         'This turns on the Data Protection Act 2018 (the "DPA") throughout.')
    cites = _run(catalogue, ts, "uksc/2020/2",
                 "The Data Protection Act 2018 governs. Section 2 of the DPA is engaged.")
    linked = {(c["candidate_id"], c["method"]) for c in cites}
    assert any(cid == "ukpga/2018/12" for cid, _ in linked)
    assert any(cid == "ukpga/2018/12" and method in ("named_alias", "shorthand_global")
               for cid, method in linked)


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


def test_rescanning_one_document_cannot_manufacture_popularity(catalogue, tmp_path):
    """The count is DOCUMENTS, not extractions. A counter incremented on conflict would
    have let a single document push its own shorthand over the threshold simply by being
    rescanned three times — and the corpus is rescanned whenever a grammar changes."""
    ts = TextStore(tmp_path / "text")
    from raglex.citations.stage import reset_shorthand_cache

    _doc(catalogue, ts, "fc/2020/1", DEF_A)
    for _ in range(4):
        extract_document(catalogue, ts, "fc/2020/1")
        reset_shorthand_cache()
    row = catalogue.browse_learned_shorthands(query="suncor", limit=5)[0][0]
    assert row["doc_count"] == 1
    assert not _global(_run(catalogue, ts, "fc/2020/2",
                            "The court applied 2021 FC 138. Suncor, at para 30, holds."))


def test_kill_switch_disables_both_halves(catalogue, tmp_path, monkeypatch):
    ts = TextStore(tmp_path / "text")
    monkeypatch.setenv("RAGLEX_SHORTHAND_GLOBAL", "0")
    _run(catalogue, ts, "fc/2020/1", DEF_A)
    assert catalogue.count_learned_shorthands() == 0


# -- curating the store ---------------------------------------------------------
def _seed(catalogue, *, docs=SHORTHAND_MIN_DOCS):
    rows = [
        {"shorthand": "Suncor", "candidate_id": "fc/2021/138",
         "entity_kind": "case", "is_abbrev": False},
        {"shorthand": "FMIOA", "candidate_id": "ukpga/2000/36",
         "entity_kind": "act", "is_abbrev": True},
        # the junk the store actually accumulated
        {"shorthand": "Article 8", "candidate_id": "echr/convention",
         "entity_kind": "treaty", "is_abbrev": True},
        {"shorthand": "appellant", "candidate_id": "echr/convention",
         "entity_kind": "treaty", "is_abbrev": True},
        {"shorthand": "may make", "candidate_id": "echr/convention",
         "entity_kind": "treaty", "is_abbrev": True},
    ]
    for i in range(docs):      # established by enough documents to be applicable
        catalogue.add_learned_shorthands(rows, doc_id=f"seed/{i}")


def test_the_store_can_be_browsed_and_the_junk_isolated(catalogue):
    _seed(catalogue)
    rows, total = catalogue.browse_learned_shorthands(state="all", limit=50)
    assert total == 5
    bad, n_bad = catalogue.browse_learned_shorthands(state="invalid", limit=50)
    assert n_bad == 3
    assert {r["shorthand"] for r in bad} == {"Article 8", "appellant", "may make"}
    # search covers both the name and what it points at
    hit, n = catalogue.browse_learned_shorthands(query="echr", limit=50)
    assert n == 3 and all(r["candidate_id"] == "echr/convention" for r in hit)


def test_blocking_survives_the_document_being_rescanned(catalogue):
    """The store is insert-only, so deleting a row is undone by the next rescan of the
    document that defined it. Blocking is the decision that sticks."""
    _seed(catalogue)
    assert catalogue.set_learned_shorthand("FMIOA", "ukpga/2000/36", blocked=1) == 1
    assert "FMIOA" not in [s[0] for s in
                           catalogue.learned_shorthand_map().get("ukpga/2000/36", [])]
    # re-learning it (as a rescan would) does not un-block it
    catalogue.add_learned_shorthands([{"shorthand": "FMIOA", "candidate_id": "ukpga/2000/36",
                                       "entity_kind": "act", "is_abbrev": True}])
    assert "FMIOA" not in [s[0] for s in
                           catalogue.learned_shorthand_map().get("ukpga/2000/36", [])]
    assert catalogue.set_learned_shorthand("FMIOA", "ukpga/2000/36", blocked=0) == 1
    assert "FMIOA" in [s[0] for s in
                       catalogue.learned_shorthand_map().get("ukpga/2000/36", [])]


def test_purge_defaults_to_a_dry_run(catalogue):
    _seed(catalogue)
    dry = catalogue.purge_invalid_learned_shorthands()
    assert dry["dry_run"] and dry["invalid"] == 3 and dry["deleted"] == 0
    assert catalogue.count_learned_shorthands() == 5
    done = catalogue.purge_invalid_learned_shorthands(dry_run=False)
    assert done["deleted"] == 3 and catalogue.count_learned_shorthands() == 2
    assert {r["shorthand"] for r in
            catalogue.browse_learned_shorthands(limit=50)[0]} == {"Suncor", "FMIOA"}


def test_purge_counts_the_document_local_rows_separately(catalogue):
    """The report boilerplate the read-side rules cannot see — "Medium Neutral",
    "Library Sheet" — looks like a name and carries no colon, so ``valid_shorthand``
    passes it. What gives it away is that one document ever established it. Widening the
    purge to those rows is the only way to shift them, and it deletes ~91% of the store,
    so it is opt-in and counted first."""
    _seed(catalogue, docs=1)              # nothing has reached the threshold
    catalogue.add_learned_shorthands(
        [{"shorthand": "Medium Neutral", "candidate_id": "nswca/2017/103",
          "entity_kind": "case", "is_abbrev": False}], doc_id="report/1")
    dry = catalogue.purge_invalid_learned_shorthands()
    assert dry["invalid"] == 3            # only the unlearnable, by default
    assert dry["document_local"] == 3     # Suncor, FMIOA, Medium Neutral
    wide = catalogue.purge_invalid_learned_shorthands(include_local=True)
    assert wide["invalid"] == 6 and wide["deleted"] == 0
    done = catalogue.purge_invalid_learned_shorthands(
        dry_run=False, include_local=True)
    assert done["deleted"] == 6 and catalogue.count_learned_shorthands() == 0


def test_backfill_recovers_doc_counts_from_recorded_uses(catalogue, tmp_path):
    """The million rows already stored record only ``first_doc``, so on the day the
    threshold ships they all read as document-local and NOTHING travels. The evidence is
    in the citations table, which has always recorded one row per use with its source
    document — but keyed on the whole matched span ("Suncor, at para 30"), so it takes
    ``shorthand_name_from_use`` to group them back into names."""
    ts = TextStore(tmp_path / "text")
    for i in range(4):
        _run(catalogue, ts, f"fc/2020/{i}", DEF_A + " Suncor, at para 30, applies.")
    # …then wipe the counts, as an upgraded database has them
    catalogue.conn.execute("UPDATE learned_shorthands SET doc_count = 0")
    catalogue.conn.execute("DELETE FROM learned_shorthand_docs")
    catalogue.conn.commit()
    assert catalogue.learned_shorthand_map() == {}

    dry = catalogue.backfill_learned_shorthand_doc_counts()
    assert dry["dry_run"] and dry["pairs_at_threshold"] >= 1 and dry["updated"] == 0
    assert catalogue.learned_shorthand_map() == {}
    done = catalogue.backfill_learned_shorthand_doc_counts(dry_run=False)
    assert done["updated"] >= 1
    assert "Suncor" in [s[0] for s in
                        catalogue.learned_shorthand_map().get("fc/2021/138", [])]


def test_shorthand_name_survives_the_round_trip_through_a_use():
    from raglex.citations.extractor import shorthand_name_from_use as name_of

    assert name_of("Suncor, at para 30") == "Suncor"
    assert name_of("Suncor at paras 41-42") == "Suncor"
    assert name_of("judgment in Digital Rights, paragraph 57") == "Digital Rights"
    assert name_of("section 2 of the DPA") == "DPA"
    assert name_of("s. 3(2) of the FMIOA") == "FMIOA"
    assert name_of("BPRs, regs 3, 13(1) and 13(4)") == "BPRs"
    assert name_of("the Vienna Convention") == "the Vienna Convention"


def test_an_abbreviation_the_corpus_agrees_on_is_searchable(catalogue, tmp_path):
    """No statute is TITLED "CPIA", so the name practitioners actually use was the one
    way of naming an authority that search could not follow. The store knows it; the
    ≥3-document gate is what makes it safe to search on."""
    from datetime import date as _date

    from raglex.core.models import DocType, ExtractedVia, Record

    rec = Record(source="uk-legislation", stable_id="ukpga/1996/25",
                 doc_type=DocType.LEGISLATION, decision_date=_date(1996, 1, 1),
                 title="Criminal Procedure and Investigations Act 1996",
                 text="An Act …", raw_bytes=b"x", extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec)
    assert catalogue.documents_by_shorthand("CPIA") == []
    for i in range(SHORTHAND_MIN_DOCS):
        catalogue.add_learned_shorthands(
            [{"shorthand": "the CPIA", "candidate_id": "ukpga/1996/25",
              "entity_kind": "act", "is_abbrev": True}], doc_id=f"uk/{i}")
    assert catalogue.documents_by_shorthand("the cpia") == ["ukpga/1996/25"]
    # blocked or below-threshold names are not a search index
    catalogue.set_learned_shorthand("the CPIA", "ukpga/1996/25", blocked=1)
    assert catalogue.documents_by_shorthand("the CPIA") == []


def test_a_deleted_shorthand_is_gone(catalogue):
    _seed(catalogue)
    assert catalogue.delete_learned_shorthand("Suncor", "fc/2021/138") == 1
    assert catalogue.count_learned_shorthands() == 4


# --- jurisdiction: the one disambiguator admitted after the coincidence test ----

def test_jurisdiction_resolves_a_cross_border_homonym():
    """"Human Rights Act" is held against the Canadian Act and the UK one. That is a
    genuine homonym, not mislearning, and the citing document's own legal system
    settles it independently of what else it cites — which is exactly what the removed
    "cites exactly one of them" test could not do."""
    from raglex.citations.stage import _resolved_by_jurisdiction as resolved

    owners = {"ca/act/h-6", "ukpga/1998/42"}
    assert resolved(owners, "ukpga/1998/42", "GB")
    assert resolved(owners, "ca/act/h-6", "CA")
    # and where the host system is unknown, nothing is resolved
    assert not resolved(owners, "ukpga/1998/42", None)


def test_supranational_owners_keep_a_name_contested():
    """The counter-example that makes home-preference unsafe: "the ECHR" is held
    against both the Convention and the Human Rights Act, and in a UK judgment it means
    the CONVENTION. Preferring the document's own jurisdiction would mint the Act, so a
    supranational owner is never excluded and the name stays withheld."""
    from raglex.citations.stage import _resolved_by_jurisdiction as resolved

    assert not resolved({"echr/convention", "ukpga/1998/42"}, "ukpga/1998/42", "GB")
    assert not resolved({"32022R1925", "ewhc/ch/2021/1246"}, "ewhc/ch/2021/1246", "GB")


def test_an_all_domestic_contest_is_still_mislearning():
    """PACE's six owners are all UK — nothing external can resolve that, and it is the
    case the whole guard exists for."""
    from raglex.citations.stage import _resolved_by_jurisdiction as resolved

    pace = {"ukpga/1967/58", "ukpga/1987/38", "ukpga/1988/33",
            "ukpga/2000/23", "ukpga/2016/19", "uksi/2014/1704"}
    assert not resolved(pace, "ukpga/2000/23", "GB")


def test_unidentifiable_owner_blocks_resolution():
    from raglex.citations.stage import _resolved_by_jurisdiction as resolved

    # an id whose system can't be told is inexcludable, so the name stays contested
    assert not resolved({"ukpga/1998/42", "mystery-source-id"}, "ukpga/1998/42", "GB")
