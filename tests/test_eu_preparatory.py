from raglex.adapters.eu_preparatory import EUPreparatoryAdapter, preparatory_subtype, printed_aliases
from raglex.core.models import DocType, ExtractedVia, Record, Stub


def test_sector_five_subtypes_and_printed_aliases():
    assert preparatory_subtype("52021PC0554")[0] == "proposals"
    assert preparatory_subtype("52021SC0551")[0] == "staff-working"
    assert "COM(2021) 554" in printed_aliases("52021PC0554")
    assert "SWD/2024/123" in printed_aliases("52024SC0123", "SWD(2024) 123 final")


def test_fetch_adds_procedure_edges_and_correct_type(monkeypatch):
    def base_fetch(self, stub):
        return Record(source=self.source, stable_id=stub.stable_id,
                      doc_type=DocType.LEGISLATION, title=stub.stable_id,
                      raw_bytes=b"x", text="COM(2021) 554 final",
                      extracted_via=ExtractedVia.STRUCTURED, extra={})

    from raglex.adapters import eu_legislation
    monkeypatch.setattr(eu_legislation.EULegislationAdapter, "fetch", base_fetch)
    ad = EUPreparatoryAdapter(celex="52021PC0554")
    stub = Stub(stable_id="52021PC0554", hints={
        "title": "Proposal for a Regulation", "adopted_as": ["32023R0839"],
        "related_to": ["52021SC0551"],
    })
    rec = ad.fetch(stub)
    assert rec.source == "eu-preparatory" and rec.doc_type == DocType.PREPARATORY
    assert rec.title == "Proposal for a Regulation"
    assert {r.relationship_type.value for r in rec.relations} == {"adopted_as", "related_to"}
    assert "COM(2021) 554" in rec.extra["aliases"]
