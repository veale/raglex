"""A provision whose only pinpoints are heuristic must not read as uncited.

Carry-forward edges are ``extracted_via='inferred'`` and ``document_mentions`` excludes
them on purpose — they are guesses, not citations. But the exclusion was silent, so
``citing_documents(target, anchor)`` answered "No citer pins specifically to 's. 16'"
for a provision that eighteen passages of Big Brother Watch v UK pin to. A reader has
no way to tell that apart from "no court has construed this provision", and for a
currency check that is the wrong direction to be wrong in.
"""

from __future__ import annotations

import pytest

from raglex.config import Config
from raglex.core.models import (
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.facade import Facade


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    )


def _edge(cat, src, dst, anchor, via):
    # ``relations_to`` only returns RESOLVED edges — a pending one is a hanging
    # reference, not yet a citation of anything.
    cat.add_relations(src, [TypedRelation(
        relationship_type=RelationshipType.MENTIONS, dst_id=dst, dst_anchor=anchor,
        raw_citation_string=anchor, extracted_via=via, context_start=0, context_end=10,
        resolution_status=ResolutionStatus.RESOLVED,
    )])


def test_inferred_only_anchor_is_reported_not_hidden(config):
    f = Facade(config)
    act = f.import_bytes(data=b"<p>section 16 of this Act</p>", filename="act.html",
                         doc_type="legislation", title="Test Act 2000")["stable_id"]
    judgment = f.import_bytes(data=b"<p>section 16 does the heavy lifting</p>",
                              filename="j.html", doc_type="judgment",
                              title="Alpha v Beta")["stable_id"]
    with f._open() as (cat, _rs, _ts):
        _edge(cat, judgment, act, "s. 16", ExtractedVia.INFERRED)

    got = f.citing_documents(act, anchor="s. 16")
    # the heuristic edge is still NOT presented as a citation …
    assert got["total"] == 0
    nav = " ".join(got["how_to_browse"])
    # … but the reply says it exists, and says what it is worth
    assert "carry-forward" in nav
    assert "1 document(s) DO pin" in nav
    assert got["provision"] == "s. 16"


def test_no_such_warning_when_there_is_genuinely_nothing(config):
    f = Facade(config)
    act = f.import_bytes(data=b"<p>section 16</p>", filename="a2.html",
                         doc_type="legislation", title="Other Act 2001")["stable_id"]
    got = f.citing_documents(act, anchor="s. 99")
    nav = " ".join(got["how_to_browse"])
    assert "No citer pins specifically" in nav
    assert "carry-forward" not in nav


def test_a_real_citer_is_unaffected_by_the_count(config):
    f = Facade(config)
    act = f.import_bytes(data=b"<p>section 16</p>", filename="a3.html",
                         doc_type="legislation", title="Third Act 2002")["stable_id"]
    real = f.import_bytes(data=b"<p>section 16 of the Third Act 2002</p>",
                          filename="r.html", doc_type="judgment",
                          title="Gamma v Delta")["stable_id"]
    ghost = f.import_bytes(data=b"<p>section 16</p>", filename="g.html",
                           doc_type="judgment", title="Epsilon v Zeta")["stable_id"]
    with f._open() as (cat, _rs, _ts):
        _edge(cat, real, act, "s. 16", ExtractedVia.REGEX)
        _edge(cat, ghost, act, "s. 16", ExtractedVia.INFERRED)

    got = f.citing_documents(act, anchor="s. 16")
    assert got["total"] == 1
    assert got["results"][0]["stable_id"] == real
    # the warning is for the EMPTY case only — with a real citer the list speaks for itself
    assert "carry-forward" not in " ".join(got["how_to_browse"])
