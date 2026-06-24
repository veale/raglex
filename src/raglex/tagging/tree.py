"""Condition-tree evaluation (§4a).

A rule's condition is a boolean expression *tree* (not a flat list) — that's what
lets arbitrarily complex logic compose. Two node shapes:

    {"op": "AND" | "OR" | "NOT", "children": [ ...nodes... ]}
    {"predicate": "literal" | "regex" | "grep_like" | "field", "args": {...}}

Example mixing predicate types freely:
    {"op": "AND", "children": [
        {"predicate": "field",   "args": {"field": "court", "op": "eq", "value": "CJEU"}},
        {"op": "OR", "children": [
            {"predicate": "literal", "args": {"value": "2016/679"}},
            {"predicate": "literal", "args": {"value": "GDPR"}}]},
        {"op": "NOT", "children": [
            {"predicate": "field", "args": {"field": "doc_type", "op": "eq", "value": "opinion"}}]}]}
"""

from __future__ import annotations

from typing import Any, Mapping

from .predicates import PREDICATES, DocView, validate_pattern

_BOOL_OPS = {"AND", "OR", "NOT"}


def evaluate(node: Mapping[str, Any], doc: DocView) -> bool:
    if "op" in node:
        op = node["op"].upper()
        children = node.get("children", [])
        if op == "AND":
            return all(evaluate(c, doc) for c in children)
        if op == "OR":
            return any(evaluate(c, doc) for c in children)
        if op == "NOT":
            # NOT negates the conjunction of its children (1 child is the usual case).
            return not all(evaluate(c, doc) for c in children)
        raise ValueError(f"unknown boolean op {node['op']!r}")

    if "predicate" in node:
        ptype = node["predicate"]
        try:
            fn = PREDICATES[ptype]
        except KeyError:
            raise ValueError(f"unknown predicate type {ptype!r}") from None
        return fn(doc, node.get("args", {}))

    raise ValueError(f"malformed condition node: {node!r}")


def root_method(node: Mapping[str, Any]) -> str:
    """The ``method`` recorded on a tag for provenance (§4a). A single-predicate
    rule records that predicate's type; a composite tree records 'rule'."""
    if "predicate" in node and "op" not in node:
        return node["predicate"]
    return "rule"


def validate_tree(node: Mapping[str, Any]) -> None:
    """Structurally validate a condition tree (and compile any regex) before it is
    stored — a malformed or uncompilable rule is rejected at add time (§4a)."""
    if "op" in node:
        if node["op"].upper() not in _BOOL_OPS:
            raise ValueError(f"unknown boolean op {node['op']!r}")
        children = node.get("children")
        if not children:
            raise ValueError(f"{node['op']} node needs children")
        for child in children:
            validate_tree(child)
        return
    if "predicate" in node:
        ptype = node["predicate"]
        if ptype not in PREDICATES:
            raise ValueError(f"unknown or not-yet-supported predicate type {ptype!r}")
        args = node.get("args", {})
        if ptype == "regex":
            validate_pattern(args["pattern"], args.get("flags"))
        elif ptype in ("literal", "grep_like") and "value" not in args and "near" not in args:
            raise ValueError(f"{ptype} predicate needs 'value' (or 'near')")
        elif ptype == "field" and "field" not in args:
            raise ValueError("field predicate needs 'field'")
        return
    raise ValueError(f"malformed condition node: {node!r}")
