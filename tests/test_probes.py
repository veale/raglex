"""The C-604/22 class of defect: extractor regression tests + the corpus-
integrity probe suite and its bounded repairs (ops/probes.py)."""

from __future__ import annotations

from raglex.citations import extract_citations
from raglex.ops.probes import (
    run_probes,
    run_repair,
)

# the exact CJEU citation form from the live C-604/22 text (incl. the NBSP + the
# spaced commas the Formex text projection produces)
CJEU_FORM = (
    "the Court’s case-law on that directive, Directive 95/46, is also applicable, "
    "in principle, to that regulation (judgment of 17\xa0June 2021 , M.I.C.M ., "
    "C‑597/19 , EU:C:2021:492 , paragraph 107 ).\n\n"
    "34 It should also be borne in mind that Article 4(1) requires interpretation."
)


# -- extractor regression ----------------------------------------------------
def test_case_paragraph_not_carried_to_legislation():
    cites = extract_citations(CJEU_FORM)
    ecli = [c for c in cites if c.method == "ecli"]
    assert ecli and ecli[0].pinpoint == "para 107"  # the case keeps its pinpoint
    # and NO carry-forward re-attributes that paragraph to the directive
    cf = [c for c in cites if c.method == "carry_forward"]
    assert not any(c.raw.lower().startswith("para") for c in cf), cf
    # while a genuine bare Article still carries forward to the directive
    assert any(c.raw.startswith("Article 4") and c.candidate_id == "31995L0046"
               for c in cf), cf


def test_distant_paragraph_still_carries_forward():
    # a 'paragraph N of Schedule …' far from any case citation is legislation-speak
    text = ("The Freedom of Information Act 2000 governs the request. "
            "Its exemptions matter here, and paragraph 2 applies to the notice. " * 2)
    cites = extract_citations(text)
    cf = [c for c in cites if c.method == "carry_forward" and c.raw.lower().startswith("para")]
    assert cf, "legislation paragraph carry-forward must survive the case guard"


# -- probes over a seeded catalogue ------------------------------------------
def _seed(catalogue):
    for sid, dt in [("case/a", "judgment"), ("case/b", "judgment"), ("law/x", "legislation")]:
        catalogue.conn.execute(
            "INSERT INTO documents (stable_id, source, doc_type, title, version, is_latest, "
            "has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
            "VALUES (?,?,?,?,1,1,1,0,'harvest','[]','live','2026-01-01')",
            (sid, "t", dt, sid))
    # the poisoned pattern: case citation at 100–113, para carry-forward at 116
    catalogue.conn.execute(
        "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, pinpoint, "
        "char_start, char_end, method, created_at) VALUES "
        "('case/a', 'EU:C:2021:492', 'case', 'case/b', 'para 107', 100, 113, 'ecli', '2026-01-01')")
    catalogue.conn.execute(
        "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, pinpoint, "
        "char_start, char_end, method, created_at) VALUES "
        "('case/a', 'paragraph 107', 'directive', 'law/x', 'para 107', 116, 129, "
        "'carry_forward', '2026-01-01')")
    # a LEGITIMATE carry-forward far from any case citation — must survive repair
    catalogue.conn.execute(
        "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, pinpoint, "
        "char_start, char_end, method, created_at) VALUES "
        "('case/a', 'Article 4', 'directive', 'law/x', 'Article 4', 900, 909, "
        "'carry_forward', '2026-01-01')")
    # the inferred edge the poisoned citation minted + a legit resolved edge + a self-edge
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, candidate_id, resolution_status, "
        "relationship_type, extracted_via, dst_anchor) VALUES "
        "('case/a', 'law/x', 'law/x', 'resolved', 'mentions', 'inferred', 'para 107')")
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, candidate_id, resolution_status, "
        "relationship_type, extracted_via, dst_anchor) VALUES "
        "('case/a', 'law/x', 'law/x', 'resolved', 'mentions', 'inferred', 'Article 4')")
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, resolution_status, relationship_type, "
        "extracted_via) VALUES ('case/a', 'case/a', 'resolved', 'mentions', 'regex')")
    catalogue.conn.commit()


def test_probes_find_the_defects(catalogue):
    _seed(catalogue)
    by_name = {p.name: p for p in run_probes(catalogue)}
    assert by_name["case_paragraph_carry_forward"].count == 1
    assert by_name["case_paragraph_carry_forward"].samples[0]["candidate_id"] == "law/x"
    assert by_name["para_pinpoint_on_eu_instrument"].count == 1
    assert by_name["self_citation"].count == 1
    assert by_name["resolved_dst_missing"].count == 0
    # every probe ran (none swallowed by an exception)
    assert all(p.count >= 0 for p in by_name.values())


def test_repair_is_bounded_to_the_poisoned_rows(catalogue):
    _seed(catalogue)
    out = run_repair(catalogue, "case_paragraph_carry_forward")
    assert out == {"citations_deleted": 1, "inferred_edges_deleted": 1}
    # the phantom rows are gone; the legitimate Article edge + citation survive
    rows = catalogue.conn.execute(
        "SELECT raw FROM citations WHERE src_id = 'case/a' ORDER BY char_start").fetchall()
    assert [r["raw"] for r in rows] == ["EU:C:2021:492", "Article 4"]
    anchors = [r["dst_anchor"] for r in catalogue.conn.execute(
        "SELECT dst_anchor FROM relations WHERE src_id = 'case/a' "
        "AND extracted_via = 'inferred'").fetchall()]
    assert anchors == ["Article 4"]
    # probe now clean; repair is re-runnable and a no-op
    assert run_probes(catalogue, only=["case_paragraph_carry_forward"])[0].count == 0
    assert run_repair(catalogue, "case_paragraph_carry_forward")["citations_deleted"] == 0


def test_self_citation_repair(catalogue):
    _seed(catalogue)
    assert run_repair(catalogue, "self_citation") == {"self_edges_deleted": 1}
    assert run_probes(catalogue, only=["self_citation"])[0].count == 0


def test_resolved_dst_missing_repair_reopens_not_deletes(catalogue):
    catalogue.conn.execute(
        "INSERT INTO documents (stable_id, source, doc_type, title, version, is_latest, "
        "has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
        "VALUES ('doc/a','t','judgment','A',1,1,1,0,'harvest','[]','live','2026-01-01')")
    # one edge resolved to a vanished target, one to a real one
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, candidate_id, resolution_status, "
        "relationship_type, extracted_via) VALUES ('doc/a','doc/ghost','doc/ghost','resolved','cites','regex')")
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, candidate_id, resolution_status, "
        "relationship_type, extracted_via) VALUES ('doc/a','doc/a','doc/a','resolved','cites','regex')")
    catalogue.conn.commit()
    assert run_probes(catalogue, only=["resolved_dst_missing"])[0].count == 1
    assert run_repair(catalogue, "resolved_dst_missing") == {"edges_reopened": 1}
    # the ghost edge is reopened (not deleted); the valid edge is untouched
    ghost = catalogue.conn.execute(
        "SELECT resolution_status, dst_id FROM relations WHERE candidate_id='doc/ghost'").fetchone()
    assert ghost["resolution_status"] == "pending" and ghost["dst_id"] is None
    assert catalogue.conn.execute(
        "SELECT resolution_status FROM relations WHERE dst_id='doc/a'").fetchone()["resolution_status"] == "resolved"
    assert run_probes(catalogue, only=["resolved_dst_missing"])[0].count == 0


def test_duplicate_spans_repair_dedups_exact_spans(catalogue):
    catalogue.conn.execute(
        "INSERT INTO documents (stable_id, source, doc_type, title, version, is_latest, "
        "has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
        "VALUES ('doc/a','t','judgment','A',1,1,1,0,'harvest','[]','live','2026-01-01')")
    for _ in range(3):  # same span extracted three times
        catalogue.conn.execute(
            "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, char_start, char_end, "
            "method, created_at) VALUES ('doc/a','X','case','doc/x',10,20,'grammar','2026-01-01')")
    catalogue.conn.execute(  # a distinct span survives untouched
        "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, char_start, char_end, "
        "method, created_at) VALUES ('doc/a','Y','case','doc/y',30,40,'grammar','2026-01-01')")
    catalogue.conn.commit()
    assert run_probes(catalogue, only=["duplicate_spans"])[0].count == 1
    assert run_repair(catalogue, "duplicate_spans") == {"duplicate_citations_deleted": 2}
    assert catalogue.conn.execute("SELECT COUNT(*) AS n FROM citations").fetchone()["n"] == 2
    assert run_probes(catalogue, only=["duplicate_spans"])[0].count == 0


def test_anachronistic_eu_citation_probe_and_repair(catalogue):
    for sid, when in [("old/1902", "1902-10-07"), ("new/2020", "2020-01-01")]:
        catalogue.conn.execute(
            "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, "
            "version, is_latest, has_text, has_embedding, added_by, topic_tags, "
            "upstream_status, fetched_at) VALUES (?,?,?,?,?,1,1,1,0,'harvest','[]','live','2026-01-01')",
            (sid, "t", "judgment", sid, when))
    for src in ("old/1902", "new/2020"):
        catalogue.conn.execute(
            "INSERT INTO relations (src_id, dst_id, resolution_status, relationship_type, "
            "extracted_via) VALUES (?, '32016L0680', 'resolved', 'mentions', 'regex')", (src,))
        catalogue.conn.execute(
            "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, method, created_at) "
            "VALUES (?, 'LED', 'named', '32016L0680', 'eu_named', '2026-01-01')", (src,))
    catalogue.conn.commit()
    from raglex.ops.probes import run_probes, run_repair

    p = run_probes(catalogue, only=["anachronistic_eu_citation"])[0]
    assert p.count == 1 and p.samples[0]["src_id"] == "old/1902"
    out = run_repair(catalogue, "anachronistic_eu_citation")
    assert out == {"edges_deleted": 1, "citations_deleted": 1}
    # the legitimate 2020 citation survives; probe now clean
    left = catalogue.conn.execute(
        "SELECT src_id FROM relations WHERE dst_id = '32016L0680'").fetchall()
    assert [r["src_id"] for r in left] == ["new/2020"]
    assert run_probes(catalogue, only=["anachronistic_eu_citation"])[0].count == 0


def test_led_acronym_guard():
    from raglex.citations import extract_citations

    noise = "JUDGMENT APPEALED FROM AFFIRMED. THE EVIDENCE LED AT TRIAL WAS RULED OUT."
    assert not [c for c in extract_citations(noise) if c.candidate_id == "32016L0680"]
    real = "the processing falls under Article 4 of the LED and the LED generally"
    hits = [c for c in extract_citations(real) if c.candidate_id == "32016L0680"]
    assert len(hits) == 2 and hits[0].pinpoint == "Article 4"


def _mk_case(catalogue, sid, when):
    catalogue.conn.execute(
        "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, version, "
        "is_latest, has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
        "VALUES (?,?,?,?,?,1,1,1,0,'harvest','[]','live','2026-01-01')",
        (sid, "t", "judgment", sid, when))


def test_forward_citation_probe_flags_only_case_to_case(catalogue):
    _mk_case(catalogue, "ewhc/2000/1", "2000-01-01")
    _mk_case(catalogue, "ewhc/2020/9", "2020-01-01")
    # legislation whose stored date is a consolidation, later than a citing judgment
    catalogue.conn.execute(
        "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, version, "
        "is_latest, has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
        "VALUES ('sor/87-7','t','legislation','Reg',date('2006-03-22'),1,1,1,0,'harvest','[]','live','2026-01-01')")
    # a case citing a case decided 20y AFTER it — impossible
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, resolution_status, relationship_type, extracted_via) "
        "VALUES ('ewhc/2000/1','ewhc/2020/9','resolved','mentions','regex')")
    # a case citing legislation dated later (consolidation) — legitimate, must NOT flag
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, resolution_status, relationship_type, extracted_via) "
        "VALUES ('ewhc/2000/1','sor/87-7','resolved','mentions','regex')")
    catalogue.conn.commit()

    by_name = {p.name: p for p in run_probes(catalogue, only=["forward_citation"])}
    fc = by_name["forward_citation"]
    assert fc.count == 1
    assert fc.samples[0]["dst_id"] == "ewhc/2020/9"


def test_misdated_case_probe_and_repair(catalogue):
    # slug says 2025, stored date says 1202 — the date is wrong
    catalogue.conn.execute(
        "INSERT INTO documents (stable_id, source, doc_type, title, decision_date, version, "
        "is_latest, has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
        "VALUES ('ewhc/admin/2025/1471','t','judgment','R (Tompson)',date('1202-06-13'),1,1,1,0,"
        "'harvest','[]','live','2026-01-01')")
    # a correctly-dated case must not be flagged
    _mk_case(catalogue, "ewhc/admin/2024/50", "2024-03-01")
    catalogue.conn.commit()

    by_name = {p.name: p for p in run_probes(catalogue, only=["misdated_case"])}
    md = by_name["misdated_case"]
    assert md.count == 1
    assert md.samples[0]["stable_id"] == "ewhc/admin/2025/1471"

    from raglex.ops.probes import repair_misdated_case
    assert repair_misdated_case(catalogue)["dates_cleared"] == 1
    # the contradicted date is nulled (not guessed), the good one untouched
    row = catalogue.conn.execute(
        "SELECT decision_date FROM documents WHERE stable_id='ewhc/admin/2025/1471'").fetchone()
    assert row["decision_date"] is None
    good = catalogue.conn.execute(
        "SELECT decision_date FROM documents WHERE stable_id='ewhc/admin/2024/50'").fetchone()
    assert str(good["decision_date"]).startswith("2024")


# -- phantom US reporters ----------------------------------------------------
def _seed_us_reporters(catalogue):
    """A French judgment whose "10 p. 100" (= 10 per cent) became a Pacific Reporter
    case, a Canadian judgment citing a bare "12 F. 13", and — as the control — a real
    US judgment citing the same single-letter series, plus a real F.3d citation."""
    for sid, source in [("fr/case/1", "fr-dila"), ("ca/case/1", "ca-caselaw"),
                        ("us/case/1", "us-caselaw")]:
        catalogue.conn.execute(
            "INSERT INTO documents (stable_id, source, doc_type, title, version, is_latest, "
            "has_text, has_embedding, added_by, topic_tags, upstream_status, fetched_at) "
            "VALUES (?,?, 'judgment', ?, 1, 1, 1, 0, 'harvest', '[]', 'live', '2026-01-01')",
            (sid, source, sid))
    rows = [
        ("fr/case/1", "10 p. 100", "us/p/10/100"),      # French per-cent → phantom
        ("us/case/1", "163 P. 1002", "us/p/163/1002"),  # bare Pacific, even in US material
        ("ca/case/1", "12 F. 13", "us/f/12/13"),        # single letter outside the US
        ("us/case/1", "12 F. 13", "us/f/12/13"),        # …the same, at home: legitimate
        ("ca/case/1", "347 F.3d 1200", "us/f3d/347/1200"),   # unambiguous: always kept
    ]
    for src, raw, cand in rows:
        catalogue.conn.execute(
            "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, char_start, "
            "char_end, method, created_at) VALUES (?,?,'case',?,0,5,'us_reporter','2026-01-01')",
            (src, raw, cand))
        catalogue.conn.execute(
            "INSERT INTO relations (src_id, candidate_id, raw_citation_string, "
            "resolution_status, relationship_type, extracted_via) "
            "VALUES (?,?,?,'pending','mentions','regex')", (src, cand, raw))
    # a human-made edge to the same phantom target — curation is never deleted
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, candidate_id, resolution_status, "
        "relationship_type, extracted_via) "
        "VALUES ('fr/case/1', 'us/p/10/100', 'pending', 'mentions', 'manual')")
    catalogue.conn.commit()


def test_ambiguous_us_reporter_probe_counts_only_the_phantoms(catalogue):
    _seed_us_reporters(catalogue)
    probe = run_probes(catalogue, only=["ambiguous_us_reporter"])[0]
    # both bare-Pacific rows (wherever they sit) + the non-US single letter
    assert probe.count == 3
    assert {s["candidate_id"] for s in probe.samples} == {
        "us/p/10/100", "us/p/163/1002", "us/f/12/13"}
    assert probe.repairable


def test_ambiguous_us_reporter_repair_spares_us_material_and_curation(catalogue):
    _seed_us_reporters(catalogue)
    out = run_repair(catalogue, "ambiguous_us_reporter")
    assert out == {"citations_deleted": 3, "edges_deleted": 3}

    kept = [(r["src_id"], r["candidate_id"]) for r in catalogue.conn.execute(
        "SELECT src_id, candidate_id FROM citations ORDER BY candidate_id").fetchall()]
    assert kept == [("us/case/1", "us/f/12/13"), ("ca/case/1", "us/f3d/347/1200")]
    manual = catalogue.conn.execute(
        "SELECT extracted_via FROM relations WHERE candidate_id = 'us/p/10/100'"
    ).fetchall()
    assert [r["extracted_via"] for r in manual] == ["manual"]
    # clean afterwards, and re-runnable
    assert run_probes(catalogue, only=["ambiguous_us_reporter"])[0].count == 0
    assert run_repair(catalogue, "ambiguous_us_reporter")["citations_deleted"] == 0
