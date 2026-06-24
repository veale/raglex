from __future__ import annotations

from raglex.adapters.leg_effects import (
    ChangeEffect,
    normalise_leg_uri,
    parse_changes_feed,
    parse_unapplied_effects,
    summarise_effects,
)

CHANGES_FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:ukm="http://www.legislation.gov.uk/namespaces/metadata">
 <entry><content type="text/xml">
  <ukm:effect type="words substituted" applied="true"
      affectinguri="http://www.legislation.gov.uk/id/ukpga/2018/12"
      affecteduri="http://www.legislation.gov.uk/id/ukpga/Eliz2/1-2/37"
      affectedprovisions="s. 19AC(2)">
   <ukm:affectedtitle>Registration Service Act 1953</ukm:affectedtitle></ukm:effect>
 </content></entry>
 <entry><content type="text/xml">
  <ukm:effect type="s. 5 inserted" applied="false"
      affectinguri="http://www.legislation.gov.uk/id/ukpga/2018/12"
      affecteduri="http://www.legislation.gov.uk/id/ukpga/2000/36"
      affectedprovisions="s. 5">
   <ukm:affectedtitle>Freedom of Information Act 2000</ukm:affectedtitle></ukm:effect>
 </content></entry>
</feed>"""

SAMPLE = b"""<?xml version="1.0"?>
<Legislation xmlns:ukm="http://www.legislation.gov.uk/namespaces/metadata">
 <ukm:Metadata>
  <ukm:UnappliedEffects>
   <ukm:UnappliedEffect Type="words inserted"
       AffectingClass="UnitedKingdomPublicGeneralAct" AffectingYear="2025"
       AffectingNumber="8" AffectingURI="http://www.legislation.gov.uk/id/ukpga/2025/8"
       AffectedSectionRef="section-6"/>
   <ukm:UnappliedEffect Type="s. 7 repealed"
       AffectingURI="http://www.legislation.gov.uk/id/ukpga/2025/8/section/3"
       AffectedSectionRef="section-7"/>
   <ukm:UnappliedEffect Type="Commencement Order"
       CommencingClass="UnitedKingdomStatutoryInstrument" CommencingYear="2024"
       CommencingNumber="1"/>
  </ukm:UnappliedEffects>
 </ukm:Metadata>
</Legislation>"""


# -- parser -----------------------------------------------------------------
def test_parse_unapplied_effects_reads_affecting_and_types():
    effs = parse_unapplied_effects(SAMPLE)
    assert len(effs) == 3
    assert effs[0].affecting_id == "ukpga/2025/8"  # from URI, provision stripped
    assert effs[1].affecting_id == "ukpga/2025/8"  # /section/3 reduced to the instrument
    assert effs[2].is_commencement and effs[2].commencing_id == "uksi/2024/1"


def test_summarise_effects_dedupes_affecting():
    s = summarise_effects(parse_unapplied_effects(SAMPLE))
    assert s["outstanding"] == 3
    assert s["affecting"] == ["ukpga/2025/8", "uksi/2024/1"]  # sorted + deduped


def test_normalise_leg_uri_forms():
    assert normalise_leg_uri("http://www.legislation.gov.uk/id/ukpga/2018/12/section/166") == "ukpga/2018/12"
    assert normalise_leg_uri("http://www.legislation.gov.uk/ukpga/2000/36") == "ukpga/2000/36"
    # pre-1963 regnal id keeps four segments (monarch/session/number)
    assert normalise_leg_uri("http://www.legislation.gov.uk/id/ukpga/Eliz2/1-2/37/section/19AC/2") == "ukpga/Eliz2/1-2/37"
    assert normalise_leg_uri("nonsense") is None
    assert normalise_leg_uri(None) is None


def test_parse_changes_feed_reads_affected_and_applied():
    effs = parse_changes_feed(CHANGES_FEED)
    assert len(effs) == 2
    a, b = effs
    assert a.affected_id == "ukpga/Eliz2/1-2/37" and a.applied is True
    assert b.affected_id == "ukpga/2000/36" and b.applied is False  # the actionable one
    assert b.affecting_id == "ukpga/2018/12" and b.affected_provision == "s. 5"
    assert b.affected_title == "Freedom of Information Act 2000"


def test_malformed_xml_is_tolerated():
    assert parse_unapplied_effects(b"not xml at all") == []
    assert parse_unapplied_effects(b"") == []


# -- the re-check queue (backoff / clear / due) -----------------------------
def test_effects_queue_schedules_then_backs_off(catalogue):
    catalogue.record_outstanding_effects("ukpga/2018/12", 3, ["ukpga/2025/8"], base_days=21)
    rows = catalogue.list_effects_refresh()
    assert len(rows) == 1 and rows[0]["checks"] == 0
    first_next = rows[0]["next_check_at"]
    # a re-check that still finds effects backs off (checks increments, gap widens)
    catalogue.record_outstanding_effects("ukpga/2018/12", 2, ["ukpga/2025/8"], base_days=21)
    rows = catalogue.list_effects_refresh()
    assert rows[0]["checks"] == 1 and rows[0]["next_check_at"] > first_next


def test_effects_queue_clears_when_incorporated(catalogue):
    catalogue.record_outstanding_effects("ukpga/2018/12", 2, ["ukpga/2025/8"])
    catalogue.record_outstanding_effects("ukpga/2018/12", 0, [])  # editors caught up
    assert catalogue.list_effects_refresh() == []


def test_due_effects_refresh_returns_only_past_due(catalogue):
    catalogue.record_outstanding_effects("uksi/2004/3391", 1, [])
    # not due yet (scheduled weeks out)
    assert catalogue.due_effects_refresh(limit=10) == []
    catalogue.conn.execute(
        "UPDATE effects_refresh SET next_check_at = '2000-01-01T00:00:00' WHERE stable_id = ?",
        ("uksi/2004/3391",),
    )
    catalogue.conn.commit()
    due = catalogue.due_effects_refresh(limit=10)
    assert [r["stable_id"] for r in due] == ["uksi/2004/3391"]
