"""Unified legislative-currency spine (§CUR) — the normaliser every jurisdiction's native
amendment/temporal vocabulary maps into, and the facade merge that surfaces it."""

from __future__ import annotations

from datetime import date

from raglex import leg_currency as C


def test_native_status_normalises_each_jurisdiction():
    # France états
    assert C.normalize_native("fr-etat", "VIGUEUR") == "in_force"
    assert C.normalize_native("fr-etat", "vigueur_diff") == "prospective"
    assert C.normalize_native("fr-etat", "ABROGE") == "repealed"
    assert C.normalize_native("fr-etat", "PERIME") == "expired"
    # Germany force enum (case/separator-insensitive)
    assert C.normalize_native("de-force", "PartiallyInForce") == "partially_in_force"
    assert C.normalize_native("de-force", "InForce") == "in_force"
    # Australia register status
    assert C.normalize_native("au-register", "Repealed") == "repealed"
    assert C.normalize_native("au-register", "Ceased") == "expired"
    # NZ + NL + UK + EU
    assert C.normalize_native("nz-pco", "not_in_force") == "prospective"
    assert C.normalize_native("nl-wti", "vervallen") == "repealed"
    assert C.normalize_native("uk-leg", "revised") == "in_force"
    assert C.normalize_native("eu-cellar", "no-longer-in-force") == "repealed"
    # unmapped / blank degrade to None (not a guess)
    assert C.normalize_native("fr-etat", "GLORP") is None
    assert C.normalize_native("fr-etat", None) is None


def test_more_severe_never_hides_a_repeal():
    assert C.more_severe("in_force", "repealed") == "repealed"
    assert C.more_severe("amended", "corrected") == "amended"
    assert C.more_severe(None, "in_force") == "in_force"
    assert C.more_severe("consolidated", "repealed") == "repealed"
    assert C.more_severe("consolidated", "in_force") == "consolidated"  # manifestation fact ranks above bare in-force


def test_status_meta_has_label_icon_tone_for_every_status():
    for s in C.CanonStatus:
        m = C.status_meta(s)
        assert m["label"] and m["icon"] and m["tone"].startswith("leg-")
    assert C.status_meta("nonsense")["label"] == C.STATUS_META[C.CanonStatus.UNKNOWN]["label"]


def test_currency_meta_roundtrip_drops_absent_fields():
    cur = C.Currency(native_status="VIGUEUR", scheme="fr-etat", in_force_from="2016-01-01",
                     point_in_time_capable=True,
                     provisions=[C.Provision(anchor="Article 6", native_status="ABROGE")]).normalized()
    meta = cur.to_meta()
    assert meta["status"] == "in_force"                 # filled from native
    assert meta["provisions"][0]["status"] == "repealed"
    assert "as_at" not in meta and "unapplied_count" not in meta   # absent fields dropped
    back = C.Currency.from_meta({"currency": meta})
    assert back is not None and back.scheme == "fr-etat"
    assert back.provisions[0].anchor == "Article 6" and back.provisions[0].status == "repealed"
    # a bag with no currency signal → None (nothing to surface)
    assert C.Currency.from_meta({"celex": "32016R0679"}) is None


class _V:
    def __init__(self, article, etat, dd=None, df=None):
        self.article, self.etat, self.date_debut, self.date_fin = article, etat, dd, df


def test_currency_from_french_versions_keeps_the_live_state_per_article():
    versions = [
        _V("Article 1", "MODIFIE", date(1978, 1, 6), date(2004, 8, 7)),   # superseded
        _V("Article 1", "VIGUEUR", date(2004, 8, 7)),                     # current → wins
        _V("Article 7", "ABROGE", date(2019, 6, 1)),
    ]
    cur = C.currency_from_french_versions("VIGUEUR", versions)
    assert cur.status == "in_force" and cur.point_in_time_capable is True
    by = {p.anchor: p for p in cur.provisions}
    assert by["Article 1"].status == "in_force"        # the live version, not the MODIFIE one
    assert by["Article 7"].status == "repealed"


def test_currency_for_eu_flags_consolidation_snapshots():
    cons = C.currency_for_eu("02016R0679-20180525")
    assert cons.status == "consolidated" and cons.as_at == "2018-05-25"
    base = C.currency_for_eu("32016R0679", in_force="in_force")
    assert base.status == "in_force" and base.point_in_time_capable is True


def _facade(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade
    return Facade(Config(data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite",
                         raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                         settings_path=tmp_path / "s.json",
                         embed_provider="local-hashing", embed_model=None))


def test_facade_merges_stored_native_currency(tmp_path):
    """A French act whose adapter stowed a repealed état in meta_json surfaces as repealed via
    the unified legislative_status — no change-edge needed — and its provisions come through."""
    import json
    f = _facade(tmp_path)
    cur = C.Currency(native_status="ABROGE", scheme="fr-etat", in_force_from="1978-01-06",
                     point_in_time_capable=True,
                     provisions=[C.Provision(anchor="Article 6", native_status="VIGUEUR")]).normalized()
    with f._open() as (cat, _r, _t):
        cat.conn.execute(
            "INSERT INTO documents (stable_id,source,doc_type,title,added_by,topic_tags,"
            "upstream_status,fetched_at,meta_json) VALUES (?,?,?,?, 'harvest','[]','live','2026-01-01',?)",
            ("fr/legi/LEGITEXT000006068624", "fr-legislation", "legislation", "Loi X",
             json.dumps({"currency": cur.to_meta()})))
        cat.conn.commit()
    st = f.legislative_status("fr/legi/LEGITEXT000006068624")
    assert st["status"] == "repealed"                      # from the native état, not an edge
    assert st["native_status"] == "ABROGE" and st["scheme"] == "fr-etat"
    assert st["point_in_time_capable"] is True and st["in_force_from"] == "1978-01-06"
    assert st["status_label"] and st["status_tone"].startswith("leg-")
    provs = {p["anchor"]: p for p in st["provisions"]}
    assert provs["Article 6"]["status"] == "in_force"      # provision still live within a repealed act
    assert st["degraded"] is False                          # we had a native signal


def test_facade_degraded_flag_when_only_absence_of_edges(tmp_path):
    """No native currency and no change-edges → 'in force' but flagged low-confidence, so the
    banner can hedge rather than over-claim currency."""
    f = _facade(tmp_path)
    with f._open() as (cat, _r, _t):
        cat.conn.execute(
            "INSERT INTO documents (stable_id,source,doc_type,title,added_by,topic_tags,"
            "upstream_status,fetched_at) VALUES (?,?,?,?, 'harvest','[]','live','2026-01-01')",
            ("uksi/2003/2426", "uk-legislation", "legislation", "PECR"))
        cat.conn.commit()
    st = f.legislative_status("uksi/2003/2426")
    assert st["status"] == "in_force" and st["degraded"] is True
