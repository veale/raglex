"""Citation re-anchor — the repair for offsets that drifted when a source was reparsed
(text regenerated) without re-extraction. The pure algorithm re-locates each raw string in
the new text; the facade job + the reparse hook fix the stored ``citations`` offsets."""

from __future__ import annotations

from raglex.citations.reanchor import reanchor
from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade


# ── pure algorithm ────────────────────────────────────────────────────────────
def _rows(*items):
    return [{"citation_id": i, "raw": raw, "char_start": s, "char_end": e}
            for i, (raw, s, e) in enumerate(items)]


def test_reanchor_after_inserted_newlines():
    # old flat text the offsets were computed on:
    old = "He discussed Coco v Clarke [1968] FSR 415 and Saltman (1948) 65 RPC 203."
    a, b = "[1968] FSR 415", "(1948) 65 RPC 203"
    rows = _rows((a, old.index(a), old.index(a) + len(a)),
                 (b, old.index(b), old.index(b) + len(b)))
    # new text: paragraphing INSERTED blank lines (grows the text → every later offset drifts)
    new = "He discussed\n\nCoco v Clarke [1968] FSR 415 and\n\nSaltman (1948) 65 RPC 203."
    # precondition: the stored offsets now mis-slice the new text
    assert new[rows[0]["char_start"]:rows[0]["char_end"]] != a
    updates, unlocatable = reanchor(new, rows)
    assert unlocatable == 0
    moved = {cid: (s, e) for cid, s, e in updates}
    assert new[moved[0][0]:moved[0][1]] == a
    assert new[moved[1][0]:moved[1][1]] == b


def test_whitespace_flexible_match_across_inserted_newline():
    # a blank line is inserted before the citation AND the citation itself is split by a newline
    old = "see article L. 223-11 du code du travail here"
    raw = "article L. 223-11 du code du travail"
    rows = _rows((raw, old.index(raw), old.index(raw) + len(raw)))
    new = "see\n\narticle L. 223-11 du\ncode du travail here"
    updates, unlocatable = reanchor(new, rows)
    assert unlocatable == 0
    _cid, s, e = updates[0]
    # the matched span, whitespace-normalised, is the citation
    assert " ".join(new[s:e].split()) == raw


def test_repeated_string_maps_in_order():
    raw = "the Act"
    old = "the Act says X. Later the Act says Y."
    i1, i2 = old.index(raw), old.index(raw, old.index(raw) + 1)
    rows = _rows((raw, i1, i1 + len(raw)), (raw, i2, i2 + len(raw)))
    new = "the Act says X.\n\nLater the Act says Y."   # blank line before the 2nd sentence
    j1, j2 = new.index(raw), new.index(raw, new.index(raw) + 1)
    updates, unlocatable = reanchor(new, rows)
    assert unlocatable == 0
    # the first occurrence didn't move; the second did — and maps to the SECOND "the Act",
    # not cross-matched onto the first
    moved = {cid: (s, e) for cid, s, e in updates}
    assert moved == {1: (j2, j2 + len(raw))}
    assert new[j1:j1 + len(raw)] == raw  # first still correct in place


def test_unchanged_offsets_are_not_reported():
    text = "plain [2011] IESC 26 text"
    raw = "[2011] IESC 26"
    rows = _rows((raw, text.index(raw), text.index(raw) + len(raw)))
    updates, unlocatable = reanchor(text, rows)
    assert updates == [] and unlocatable == 0


def test_unlocatable_left_untouched():
    text = "this text has no such citation"
    rows = _rows(("[1999] UKHL 1", 5, 18))
    updates, unlocatable = reanchor(text, rows)
    assert updates == [] and unlocatable == 1


# ── facade job + reparse hook ─────────────────────────────────────────────────
def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )


def _seed_drifted_doc(cat, ts, *, stable_id="x/1"):
    """A stored doc whose text has ALREADY been reparsed (newlines inserted) but whose
    citation offsets still point into the pre-reparse positions — the drift state."""
    new_text = "He discussed\n\nCoco v Clarke [1968] FSR 415 and\n\nSaltman (1948) 65 RPC 203."
    rec = Record(source="drifty", stable_id=stable_id, doc_type=DocType.JUDGMENT,
                 title="D", text=new_text, extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    ts.put(rec.payload_hash, new_text)
    cat.upsert_document(rec)
    # offsets computed on the OLD (flat) text — now wrong for new_text
    old = "He discussed Coco v Clarke [1968] FSR 415 and Saltman (1948) 65 RPC 203."
    for raw in ("[1968] FSR 415", "(1948) 65 RPC 203"):
        cat.conn.execute(
            "INSERT INTO citations (src_id, raw, candidate_id, char_start, char_end, method, created_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            (stable_id, raw, None, old.index(raw), old.index(raw) + len(raw), "grammar"))
    cat.commit()
    return new_text


def test_reanchor_source_fixes_drifted_offsets(tmp_path):
    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, _rs, ts):
        new_text = _seed_drifted_doc(cat, ts)
        # precondition: the stored offsets currently mis-slice the text
        before = cat.citations_for("x/1")
        assert any(new_text[c["char_start"]:c["char_end"]] != c["raw"] for c in before)

    res = facade.reanchor_source(source="drifty")
    assert res["offsets_fixed"] == 2
    assert res["docs_reanchored"] == 1
    assert res["unlocatable"] == 0

    with facade._open() as (cat, _rs, ts):
        after = cat.citations_for("x/1")
        text = ts.get(cat.get_document("x/1")["payload_hash"])
        # every citation now slices to exactly its raw string
        assert all(text[c["char_start"]:c["char_end"]] == c["raw"] for c in after)


def test_reanchor_is_idempotent(tmp_path):
    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, _rs, ts):
        _seed_drifted_doc(cat, ts)
    facade.reanchor_source(source="drifty")
    # a second pass finds nothing left to move
    res2 = facade.reanchor_source(source="drifty")
    assert res2["offsets_fixed"] == 0 and res2["docs_reanchored"] == 0
