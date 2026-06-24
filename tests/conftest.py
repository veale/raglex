from __future__ import annotations

import os
from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _isolate_env():
    """The settings store deliberately writes into os.environ (apply_to_env); snapshot
    and restore it around every test so those writes don't leak between tests."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)

from raglex.core.models import DocType, Record, RelationshipType, TypedRelation
from raglex.storage import Catalogue, RawStore


@pytest.fixture
def catalogue() -> Catalogue:
    cat = Catalogue(":memory:")
    yield cat
    cat.close()


@pytest.fixture
def rawstore(tmp_path) -> RawStore:
    return RawStore(tmp_path / "raw")


def make_record(stable_id: str = "uksc/2024/1", **overrides) -> Record:
    defaults = dict(
        source="uk-caselaw",
        stable_id=stable_id,
        doc_type=DocType.JUDGMENT,
        title="Doe v Data Controller",
        court="uksc",
        decision_date=date(2024, 1, 15),
        language="en",
        raw_bytes=b"<akomaNtoso>data protection personal data</akomaNtoso>",
        raw_ext="xml",
        text="This case concerns data protection and personal data under the GDPR.",
        relations=[
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string="Case C-311/18 (Schrems II)",
            )
        ],
    )
    defaults.update(overrides)
    rec = Record(**defaults)
    rec.ensure_payload_hash()
    return rec
