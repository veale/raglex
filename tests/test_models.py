from __future__ import annotations

from raglex.core.models import Record, DocType, sha256_bytes


def test_ensure_payload_hash_is_sha256_of_raw():
    rec = Record(source="x", stable_id="a", doc_type=DocType.JUDGMENT, raw_bytes=b"hello")
    digest = rec.ensure_payload_hash()
    assert digest == sha256_bytes(b"hello")
    # idempotent
    assert rec.ensure_payload_hash() == digest


def test_ensure_payload_hash_none_without_bytes():
    rec = Record(source="x", stable_id="a", doc_type=DocType.JUDGMENT)
    assert rec.ensure_payload_hash() is None
