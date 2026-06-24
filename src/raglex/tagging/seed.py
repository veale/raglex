"""Seed rules — re-express §4's hardcoded topic logic as editable §4a rules.

The design is explicit that §4a *subsumes* §4: "the existing topic filters become
just the first few rules." This builds those rules from the same multilingual
vocabularies, so nothing is lost and everything becomes editable data:

  - a ``literal``-OR rule per topic tag (data_protection, foi) over the vocab terms;
  - a ``field`` rule tagging in-scope-by-construction sources (ICO/DPC/EDPB/GRC).

Run once to bootstrap a fresh catalogue (``raglex tag seed``); thereafter edit/add
rules through the engine rather than touching code.
"""

from __future__ import annotations

from ..topics.vocab import IN_SCOPE_COURTS, VOCABULARIES
from .engine import RuleEngine


def topic_rule(tag: str, terms: list[str]) -> dict:
    """An OR of folded-substring literals over the document text."""
    return {
        "op": "OR",
        "children": [{"predicate": "literal", "args": {"value": t}} for t in terms],
    }


def in_scope_source_rule(sources: list[str]) -> dict:
    return {"predicate": "field", "args": {"field": "source", "op": "in", "value": sources}}


def seed(engine: RuleEngine) -> list[int]:
    """Create the seed rule set; returns the new rule ids."""
    rule_ids: list[int] = []
    for tag, vocab in VOCABULARIES.items():
        rid = engine.add_rule(
            tag,
            topic_rule(tag, sorted(vocab)),
            note=f"seeded from §4 {tag} vocabulary",
        )
        rule_ids.append(rid)

    # In-scope-by-construction sources are tagged data_protection directly (§4).
    dp_sources = sorted(s for s in IN_SCOPE_COURTS if s in {"ico", "dpc", "edpb", "cnil"})
    if dp_sources:
        # match our adapter source keys (e.g. 'uk-grc') and regulator codes
        rule_ids.append(
            engine.add_rule(
                "data_protection",
                {
                    "op": "OR",
                    "children": [
                        in_scope_source_rule(["uk-grc", *dp_sources]),
                    ],
                },
                note="in-scope-by-construction DP sources (§4)",
            )
        )
    return rule_ids
