from __future__ import annotations

from datetime import date

from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.resolve import Resolver, string_hash
from raglex.resolve.matchers import (
    match_caselaw_uri,
    match_celex,
    match_ecli,
    match_legislation_uri,
    match_uk_ncn,
)


# -- matchers ---------------------------------------------------------------
def test_match_ecli_normalises_case():
    c = match_ecli("see ecli:eu:c:2020:559 at para 5")
    assert c is not None and c.value == "ECLI:EU:C:2020:559" and c.method == "ecli"


def test_match_celex():
    c = match_celex("Regulation (EU) 2016/679, CELEX 32016R0679")
    assert c is not None and c.value == "32016R0679"


def test_match_uk_ncn_with_subdivision():
    c = match_uk_ncn("[2026] UKFTT 904 (GRC)")
    assert c is not None and c.value == "ukftt/grc/2026/904" and c.method == "uk_ncn"


def test_match_uk_ncn_plain():
    c = match_uk_ncn("[2024] UKSC 12")
    assert c is not None and c.value == "uksc/2024/12"


def test_match_legislation_uri():
    c = match_legislation_uri("http://www.legislation.gov.uk/id/ukpga/2000/36")
    assert c is not None and c.value == "ukpga/2000/36" and c.method == "legislation"


def test_match_legislation_uri_drops_section_fragment():
    c = match_legislation_uri("http://www.legislation.gov.uk/id/ukpga/2000/36/section/14/1")
    assert c is not None and c.value == "ukpga/2000/36"  # resolves to the Act


def test_match_caselaw_uri_document_path():
    c = match_caselaw_uri("https://caselaw.nationalarchives.gov.uk/ewca/civ/2015/454")
    assert c is not None and c.value == "ewca/civ/2015/454" and c.method == "caselaw_uri"


def test_match_caselaw_uri_new_style_uuid():
    c = match_caselaw_uri("https://caselaw.nationalarchives.gov.uk/d-abc123def")
    assert c is not None and c.value == "d-abc123def"


# -- resolver helpers -------------------------------------------------------
def _doc(catalogue, stable_id, *, ecli=None, citing=None):
    rels = []
    if citing:
        rels = [
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=citing,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            )
        ]
    rec = Record(
        source="t",
        stable_id=stable_id,
        doc_type=DocType.JUDGMENT,
        ecli=ecli,
        decision_date=date(2024, 1, 1),
        raw_bytes=stable_id.encode(),
        relations=rels,
    )
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec)
    return rec


# -- resolver behaviour -----------------------------------------------------
def test_resolves_edge_when_target_present(catalogue):
    _doc(catalogue, "ECLI:EU:C:2020:559")  # target node (stable_id == ECLI)
    _doc(catalogue, "src-1", citing="As held in ECLI:EU:C:2020:559 (Schrems II)")

    stats = Resolver(catalogue).run()
    assert stats.resolved == 1

    edge = catalogue.relations_for("src-1")[0]
    assert edge["resolution_status"] == "resolved"
    assert edge["dst_id"] == "ECLI:EU:C:2020:559"
    # raw string is kept for audit (§5b)
    assert "Schrems II" in edge["raw_citation_string"]


def test_resolves_intra_corpus_caselaw_uri(catalogue):
    _doc(catalogue, "ewca/civ/2015/454")  # a harvested UK case
    _doc(
        catalogue,
        "src-1",
        citing="see https://caselaw.nationalarchives.gov.uk/ewca/civ/2015/454",
    )
    assert Resolver(catalogue).run().resolved == 1
    assert catalogue.relations_for("src-1")[0]["dst_id"] == "ewca/civ/2015/454"


def test_unresolved_goes_to_worklist_then_resolves_on_harvest(catalogue):
    # Cite a target that isn't in the corpus yet.
    _doc(catalogue, "src-1", citing="[2024] UKSC 12")
    stats = Resolver(catalogue).run()
    assert stats.resolved == 0 and stats.still_pending == 1

    wl = catalogue.resolution_worklist()
    assert wl and wl[0]["raw_citation_string"] == "[2024] UKSC 12"
    assert catalogue.relations_for("src-1")[0]["resolution_status"] == "pending"

    # Harvest the target later; re-running resolution flips the edge live (§5b).
    _doc(catalogue, "uksc/2024/12")
    stats2 = Resolver(catalogue).run()
    assert stats2.resolved == 1
    assert catalogue.relations_for("src-1")[0]["dst_id"] == "uksc/2024/12"
    assert catalogue.resolution_worklist() == []  # cleared from the queue


def test_grows_alias_from_structured_resolution(catalogue):
    _doc(catalogue, "ECLI:EU:C:2020:559")
    _doc(catalogue, "src-1", citing="Schrems II ECLI:EU:C:2020:559")
    stats = Resolver(catalogue).run()
    assert stats.aliases_added == 1
    # the cached colloquial phrase now resolves by the cheap alias rung next time
    assert catalogue.get_alias("schrems ii ecli:eu:c:2020:559") == "ECLI:EU:C:2020:559"


def test_cite_count_ranks_worklist(catalogue):
    _doc(catalogue, "src-1", citing="[2024] UKSC 12")
    _doc(catalogue, "src-2", citing="[2024] UKSC 12")
    _doc(catalogue, "src-3", citing="[2024] EWHC 99 (Admin)")
    Resolver(catalogue).run()
    wl = catalogue.resolution_worklist()
    assert wl[0]["raw_citation_string"] == "[2024] UKSC 12"
    assert wl[0]["cite_count"] == 2


def test_string_hash_folds_variants_together():
    assert string_hash("Schrems II") == string_hash("schrems ii")
