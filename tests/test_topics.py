from __future__ import annotations

from datetime import date

from raglex.core.models import Stub
from raglex.topics import cheap_match, confirm, fold


def test_fold_strips_accents_and_case():
    assert fold("Données Personnelles") == "donnees personnelles"


def test_cheap_match_keeps_in_scope_court():
    stub = Stub(stable_id="x", court="ukftt-grc", title="Some appeal")
    assert cheap_match(stub) is True


def test_cheap_match_keeps_on_vocab_hit_multilingual():
    stub = Stub(stable_id="x", court="de-bverwg", title="Urteil zum Datenschutz")
    assert cheap_match(stub) is True


def test_cheap_match_defers_when_unsure():
    stub = Stub(stable_id="x", court="uksc", title="Smith v Jones")
    assert cheap_match(stub) is None


def test_confirm_keeps_topical_text_and_tags_it():
    result = confirm("This decision concerns personal data and the GDPR (2016/679).")
    assert result.keep is True
    assert "data_protection" in result.tags
    assert result.score >= 3.0


def test_confirm_drops_off_topic():
    result = confirm("A contract dispute about a delivery of bricks.")
    assert result.keep is False
    assert result.tags == ()
