"""LLM seam tests — all offline. They exercise the resilient parsing/batching and
the classifier/extractor wiring with a fake client (no network), plus graceful
degradation when no LLM is configured."""

from __future__ import annotations

from raglex.citations import LLMCitationExtractor, extract_citations
from raglex.core.models import RelationshipType
from raglex.llm.client import LLMConfig, _loads
from raglex.treatment import HeuristicTreatmentClassifier, LLMTreatmentClassifier


class FakeClient:
    """Stands in for LLMClient: records prompts, returns scripted JSON, and reports
    available() so we can test both the enabled and degraded paths."""

    def __init__(self, scripted, *, available=True):
        self._scripted = scripted
        self._available = available
        self.batches = 0

    def available(self):
        return self._available

    def json(self, system, user):
        return self._scripted

    def json_batch(self, system, items, *, instruction):
        self.batches += 1
        return self._scripted(items) if callable(self._scripted) else self._scripted


# -- robust JSON parsing ----------------------------------------------------
def test_loads_tolerates_fences_and_prose():
    assert _loads('{"a": 1}') == {"a": 1}
    assert _loads('```json\n{"a": 1}\n```') == {"a": 1}
    assert _loads('Sure! Here you go: {"a": [1,2]} — done') == {"a": [1, 2]}
    assert _loads("not json at all") is None


def test_config_from_env_enables_only_when_configured():
    assert LLMConfig.from_env({}).enabled is False  # nothing set → off (auto)
    cfg = LLMConfig.from_env({"OPENROUTER_API_KEY": "sk-x"})
    assert cfg.enabled is True
    # a local keyless endpoint counts as configured intent too
    assert LLMConfig.from_env({"RAGLEX_LLM_BASE_URL": "http://localhost:11434/v1"}).enabled is True


# -- treatment classifier ---------------------------------------------------
def test_llm_treatment_classifier_uses_model_label():
    client = FakeClient([{"index": 0, "treatment": "overrules"}])
    clf = LLMTreatmentClassifier(client)
    out = clf.classify_batch([("the reasoning cannot stand", "case")])
    assert out == [RelationshipType.OVERRULES]


def test_llm_treatment_falls_back_to_heuristic_when_model_abstains():
    # model returns nothing for the item → heuristic cue ("distinguished") wins
    client = FakeClient([{"index": 0}])
    clf = LLMTreatmentClassifier(client)
    out = clf.classify_batch([("the court distinguished that authority", "case")])
    assert out == [RelationshipType.DISTINGUISHES]


def test_llm_treatment_degrades_when_unavailable():
    client = FakeClient([], available=False)
    clf = LLMTreatmentClassifier(client)
    # entirely heuristic; statute citation stays None (treatment is case-only)
    out = clf.classify_batch([("the court followed", "case"), ("see GDPR", "regulation")])
    assert out == [RelationshipType.FOLLOWS, None]


def test_llm_treatment_only_sends_case_citations():
    client = FakeClient([{"index": 0, "treatment": "applies"}])
    clf = LLMTreatmentClassifier(client)
    out = clf.classify_batch([("statute text", "regulation"), ("case prose", "case")])
    assert out[0] is None  # regulation never sent to the model
    assert out[1] == RelationshipType.APPLIES


# -- narrative citation extractor -------------------------------------------
def test_llm_extractor_anchors_quotes_and_drops_hallucinations():
    text = "The Court relied on its earlier data-retention judgment in reaching this view."

    def script(items):
        return [{"index": 0, "citations": [
            {"quote": "earlier data-retention judgment", "kind": "case",
             "candidate": "ECLI:EU:C:2014:238", "pinpoint": None},
            {"quote": "a phrase not in the text", "kind": "case"},  # must be dropped
        ]}]

    ext = LLMCitationExtractor(FakeClient(script))
    cites = ext.extract(text)
    assert len(cites) == 1
    c = cites[0]
    assert c.candidate_id == "ECLI:EU:C:2014:238" and c.method == "llm"
    assert text[c.char_start:c.char_end] == "earlier data-retention judgment"


def test_grammar_wins_overlap_with_llm():
    text = "see Case C-311/18 here"

    def script(items):
        # LLM tries to claim the same span with a worse candidate; grammar wins
        return [{"index": 0, "citations": [
            {"quote": "Case C-311/18", "kind": "case", "candidate": "WRONG"}]}]

    cites = extract_citations(text, llm=LLMCitationExtractor(FakeClient(script)))
    c = next(c for c in cites if "C-311" in c.raw)
    assert c.candidate_id == "62018CJ0311" and c.method == "cjeu_case_number"
