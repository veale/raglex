"""The rule engine (§4a) — evaluate stored rules, write provenance-tagged results.

Tagging is a *re-derivable projection* (§1.2): evaluation writes rows into
``document_tags`` recording which rule + version assigned each tag, so a noisy
rule is corrected by editing it and re-running, never by hand-patching rows. Every
rule supports a **dry-run preview** ("this would tag N documents, here's a
sample") before it writes — essential when a broad ``literal`` over-matches.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..storage.catalogue import Catalogue
from .predicates import DocView
from .tree import evaluate, root_method, validate_tree

log = logging.getLogger("raglex.tagging")


@dataclass(slots=True)
class PreviewResult:
    tag: str
    evaluated: int
    matched: int
    sample: list[tuple[str, str | None]] = field(default_factory=list)

    def summary(self) -> str:
        return f"[preview] tag={self.tag!r} would tag {self.matched}/{self.evaluated} documents"


@dataclass(slots=True)
class RunResult:
    rule_id: int
    tag: str
    evaluated: int
    matched: int
    written: int

    def summary(self) -> str:
        return (
            f"[rule {self.rule_id}] tag={self.tag!r} matched={self.matched}/{self.evaluated} "
            f"written={self.written}"
        )


def _text_loader(text_path: Any):
    def load() -> str | None:
        if not text_path:
            return None
        try:
            return Path(text_path).read_text(encoding="utf-8")
        except OSError:
            return None

    return load


def _docview(row: Mapping[str, Any]) -> DocView:
    return DocView(row=row, _load_text=_text_loader(row["text_path"]))


class RuleEngine:
    def __init__(self, catalogue: Catalogue) -> None:
        self.catalogue = catalogue

    # -- authoring ---------------------------------------------------------
    def add_rule(
        self, tag: str, condition_tree: dict, *, scope: dict | None = None, note: str | None = None
    ) -> int:
        """Validate (compile regex, check structure) then store a rule (§4a)."""
        validate_tree(condition_tree)
        return self.catalogue.add_rule(tag, condition_tree, scope=scope, note=note)

    # -- dry run (§4a) -----------------------------------------------------
    def preview(
        self, tag: str, condition_tree: dict, *, scope: dict | None = None, sample_size: int = 10
    ) -> PreviewResult:
        """Evaluate without writing — the safety net before a broad rule runs."""
        validate_tree(condition_tree)
        result = PreviewResult(tag=tag, evaluated=0, matched=0)
        for row in self.catalogue.iter_documents(scope):
            result.evaluated += 1
            if evaluate(condition_tree, _docview(row)):
                result.matched += 1
                if len(result.sample) < sample_size:
                    result.sample.append((row["stable_id"], row["title"]))
        return result

    # -- execution ---------------------------------------------------------
    def run_rule(self, rule_id: int) -> RunResult:
        rule = self.catalogue.get_rule(rule_id)
        if rule is None:
            raise KeyError(f"no rule {rule_id}")

        tree = json.loads(rule["condition_tree_json"])
        scope = json.loads(rule["scope_json"])
        tag = rule["tag"]
        method = root_method(tree)

        # Re-derivable projection (§4a): clear this rule's prior tags, then re-apply.
        self.catalogue.remove_rule_tags(rule_id, tag)
        run_id = self.catalogue.start_rule_run(rule_id, rule["version"], scope)

        evaluated = matched = written = 0
        for row in self.catalogue.iter_documents(scope):
            evaluated += 1
            if evaluate(tree, _docview(row)):
                matched += 1
                if self.catalogue.upsert_document_tag(
                    row["stable_id"],
                    tag,
                    method=method,
                    assigned_by_rule_id=rule_id,
                    rule_version=rule["version"],
                ):
                    written += 1  # may be < matched when a manual tag already wins
        self.catalogue.finish_rule_run(run_id, evaluated=evaluated, matched=matched)
        result = RunResult(rule_id, tag, evaluated, matched, written)
        log.info(result.summary())
        return result

    def run_all(self, *, enabled_only: bool = True) -> list[RunResult]:
        return [
            self.run_rule(rule["rule_id"])
            for rule in self.catalogue.list_rules(enabled_only=enabled_only)
        ]

    def run_on_document(self, stable_id: str) -> list[str]:
        """Evaluate all enabled rules against one freshly-ingested document (§4a:
        'on ingest, every new/changed document is run through all enabled rules').
        Returns the tags applied."""
        row = self.catalogue.get_document(stable_id)
        if row is None:
            return []

        applied: list[str] = []
        doc = _docview(row)
        for rule in self.catalogue.list_rules(enabled_only=True):
            tree = json.loads(rule["condition_tree_json"])
            if evaluate(tree, doc):
                if self.catalogue.upsert_document_tag(
                    stable_id,
                    rule["tag"],
                    method=root_method(tree),
                    assigned_by_rule_id=rule["rule_id"],
                    rule_version=rule["version"],
                ):
                    applied.append(rule["tag"])
        return applied
