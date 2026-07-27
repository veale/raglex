"""The app's feedback box → the `feedback` table (Bugs / Feature requests), kept alongside
the refinement-flags review queue."""

from __future__ import annotations

import os
import tempfile

from raglex.config import Config
from raglex.facade import Facade


def _facade() -> Facade:
    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def test_feedback_roundtrip_records_page_metadata_and_resolves():
    f = _facade()
    r = f.submit_feedback(kind="bug", message="Article 15 linked to the wrong case",
                          page="document:32016R0679", url="#/article/32016R0679",
                          metadata={"tab": "document", "role": "admin"})
    assert r["submitted"] is True and r["kind"] == "bug"
    rows = f.list_feedback(status="open")
    assert len(rows) == 1
    row = rows[0]
    assert row["message"].startswith("Article 15")
    assert row["page"] == "document:32016R0679"
    # metadata is stored as JSON and returned parsed
    assert isinstance(row["metadata"], dict) and row["metadata"]["tab"] == "document"
    # resolve removes it from the open queue
    assert f.resolve_feedback(feedback_id=row["feedback_id"])["updated"] == 1
    assert f.list_feedback(status="open") == []


def test_feedback_kind_is_clamped_and_empty_message_rejected():
    f = _facade()
    assert "error" in f.submit_feedback(kind="bug", message="   ")
    # an unknown kind falls back to 'bug'
    assert f.submit_feedback(kind="nonsense", message="x")["kind"] == "bug"
    assert f.submit_feedback(kind="feature", message="add Word export")["kind"] == "feature"
