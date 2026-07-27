import json

from raglex.adapters.eu_dgcomp import (
    TFEU,
    dgcomp_legal_basis_relations,
    parse_dgcomp_cases,
)


def test_parse_dgcomp_operative_english_decision_attachments_only():
    payload = {"AT.40861": {
        "metadata": {
            "caseTitle": ["Construction chemicals"],
            "caseLegalBasis": ['{"label":"Art. 101 TFEU"}'],
        },
        "caseAttachments": [{"metadata": {"attachmentLink": ["publicity.pdf"]}}],
        "decisions": [{
            "metadata": {
                "decisionAdoptionDate": ["2026-07-20"],
                "decisionTypes": ['{"label":"Initiation of Proceedings"}'],
            },
            "decisionAttachments": [{"metadata": {
                "attachmentLanguage": ["EN"],
                "attachmentLink": ["https://ec.europa.eu/decision.pdf"],
                "attachmentIdSequence": ["15376"],
                "attachmentPublicationBusinessDate": ["2026-07-20"],
                "attachmentCategory": ['{"label":"Notice"}'],
            }}],
        }],
    }}
    row = parse_dgcomp_cases(json.dumps(payload))[0]
    assert row["stable_id"] == "eu/dgcomp/at/40861/15376"
    assert row["url"].endswith("decision.pdf")


def test_dgcomp_structured_tfeu_legal_basis():
    relations = dgcomp_legal_basis_relations(
        ["Art. 101 TFEU", "Art. 102 TFEU + Art. 54 EEA"]
    )
    assert [r.dst_anchor for r in relations] == ["Article 101", "Article 102"]
    assert {r.dst_id for r in relations} == {TFEU}
