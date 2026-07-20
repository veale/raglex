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


def test_structured_resolution_does_not_grow_the_alias_table(catalogue):
    # Resolution keys off the edge's persisted candidate_id, so memoising every resolved
    # citation string as an alias buys nothing. The alias table holds *rules* only.
    _doc(catalogue, "ECLI:EU:C:2020:559")
    _doc(catalogue, "src-1", citing="Schrems II ECLI:EU:C:2020:559")
    stats = Resolver(catalogue).run()
    assert stats.resolved == 1
    assert catalogue.get_alias("schrems ii ecli:eu:c:2020:559") is None


def test_named_alias_resolves_a_by_name_reference(catalogue):
    # …and the rules the alias table DOES hold still resolve, via the raw_fold rung.
    _doc(catalogue, "ukpga/2018/12")
    _doc(catalogue, "src-1", citing="the DPA 2018")
    catalogue.put_alias("the dpa 2018", "ukpga/2018/12", source="named")
    assert Resolver(catalogue).run().resolved == 1
    assert catalogue.relations_for("src-1")[0]["dst_id"] == "ukpga/2018/12"


def test_resolve_pending_for_only_touches_the_new_document(catalogue):
    _doc(catalogue, "src-1", citing="[2024] UKSC 12")
    _doc(catalogue, "src-2", citing="[2023] UKSC 1")
    _doc(catalogue, "uksc/2024/12")
    assert catalogue.resolve_pending_for("uksc/2024/12") == 1
    assert catalogue.count_pending_relations() == 1  # src-2's edge is untouched


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


def test_dotted_reporter_abbreviations_resolve_to_the_undotted_alias(catalogue):
    """"[1948] 1 K.B. 223" and "[1948] 1 KB 223" are the same report, but plain
    case-folding keeps the stops, so the dotted form landed on a different alias key
    and silently failed to link. Wednesbury is held under the undotted form, so every
    dotted citation of it went unresolved."""
    catalogue.put_alias("[1948] 1 kb 223", "ewca/civ/1947/1", source="parallel")
    assert catalogue.get_alias("[1948] 1 k.b. 223") == "ewca/civ/1947/1"
    assert catalogue.get_alias("(1948) 1 kb 223") is None   # bracket style still distinct


def test_aliases_are_stored_de_dotted_so_both_spellings_converge(catalogue):
    """The write path normalises too, so a dotted alias and its undotted twin are one
    row rather than two — otherwise only whichever was minted first would resolve."""
    catalogue.put_alias("[1948] 1 K.B. 223", "ewca/civ/1947/1", source="parallel")
    assert catalogue.get_alias("[1948] 1 kb 223") == "ewca/civ/1947/1"
    assert catalogue.get_alias("[1948] 1 k.b. 223") == "ewca/civ/1947/1"


def test_citation_folding_keeps_decimal_pinpoints_intact(catalogue):
    """The de-dotting must not eat a decimal point: "5.2" is a pinpoint, not an
    abbreviation, and collapsing it to "52" would point at a different paragraph."""
    catalogue.put_alias("practice direction 5.2", "pd/5-2", source="named")
    assert catalogue.get_alias("practice direction 5.2") == "pd/5-2"
