"""Unified legislative-currency model (§CUR) — the overarching schema every jurisdiction's
native amendment/temporal apparatus normalises *into*.

RagLex holds consolidated legislation from systems that each track "is this still good law,
and as at when?" in their own vocabulary:

- **UK** (legislation.gov.uk / CLML): typed *effects* with an ``applied`` flag (the editorial
  lag), ``prospective`` provisions, point-in-time by date-in-URI.
- **EU** (CELLAR / Formex): dated consolidation snapshots (``0``-sector CELEX), CDM
  repeal/amend/corrigendum edges.
- **France** (Légifrance / DILA LEGI): per-article *états* — ``VIGUEUR`` / ``VIGUEUR_DIFF`` /
  ``MODIFIE`` / ``ABROGE`` / ``ABROGE_DIFF`` / ``PERIME`` — each with a ``[dateDebut, dateFin]``
  window.
- **Germany** (NeuRIS / LegalDocML.de): ``legislationLegalForce`` = ``InForce`` /
  ``NotInForce`` / ``PartiallyInForce`` plus ``temporalCoverage`` intervals.
- **Netherlands** (BWB / WTI): ``geldigheidsdatum`` validity windows + a ``vervallen`` (lapsed)
  flag; LiDO relationship graph.

This module is the thin normaliser: native token → one canonical :class:`CanonStatus`, and a
:class:`Currency` / :class:`Provision` shape that persists in ``documents.meta_json['currency']``.
It **degrades gracefully** — a source that exposes only act-level force still produces a valid
Currency; one that exposes per-article états fills ``provisions`` too; one that exposes nothing
leaves fields absent rather than guessing. Nothing here does I/O; adapters/format parsers build
Currency at parse time and the facade merges it with the change-edge graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CanonStatus(StrEnum):
    """One vocabulary for currency across all jurisdictions. ``in_force``/``amended``/
    ``corrected``/``repealed`` are load-bearing (the facade + banner switch on them and the
    existing tests assert them); the rest add the distinctions the richer sources expose."""

    IN_FORCE = "in_force"
    PARTIALLY_IN_FORCE = "partially_in_force"   # some provisions commenced, some not (DE)
    PROSPECTIVE = "prospective"                 # enacted but not yet in force (UK/FR *_DIFF)
    AMENDED = "amended"                         # in force, but modified since enactment
    CORRECTED = "corrected"                     # a corrigendum touched it
    RECAST = "recast"                           # superseded by a recast instrument
    CONSOLIDATED = "consolidated"               # THIS expression is a dated consolidation
    REPEALED = "repealed"                       # no longer in force (repealed/revoked/abrogated)
    EXPIRED = "expired"                         # spent / lapsed by its own terms (FR PERIME)
    UNKNOWN = "unknown"


# Native source token (lower-cased) → canonical status, keyed by scheme. The scheme lets the
# same token mean different things across systems and keeps each mapping auditable.
_NATIVE: dict[str, dict[str, CanonStatus]] = {
    # France — Légifrance/DILA état juridique (per-article and per-text).
    "fr-etat": {
        "vigueur": CanonStatus.IN_FORCE,
        "vigueur_diff": CanonStatus.PROSPECTIVE,       # in force but not yet effective
        "modifie": CanonStatus.AMENDED,                # a superseded past version
        "modifie_mort_ne": CanonStatus.REPEALED,       # stillborn amendment, never applied
        "abroge": CanonStatus.REPEALED,
        "abroge_diff": CanonStatus.IN_FORCE,           # abrogation deferred → valid until dateFin
        "perime": CanonStatus.EXPIRED,
        "annule": CanonStatus.REPEALED,
        "disjoint": CanonStatus.UNKNOWN,
        "sans_etat": CanonStatus.UNKNOWN,
    },
    # Germany — NeuRIS legislationLegalForce enum.
    "de-force": {
        "inforce": CanonStatus.IN_FORCE,
        "in_force": CanonStatus.IN_FORCE,
        "notinforce": CanonStatus.REPEALED,            # ambiguous (repealed OR not-yet); repealed is the common case
        "not_in_force": CanonStatus.REPEALED,
        "partiallyinforce": CanonStatus.PARTIALLY_IN_FORCE,
        "partially_in_force": CanonStatus.PARTIALLY_IN_FORCE,
        "future": CanonStatus.PROSPECTIVE,
    },
    # Netherlands — BWB/WTI derived tokens.
    "nl-wti": {
        "geldig": CanonStatus.IN_FORCE,
        "in_werking": CanonStatus.IN_FORCE,
        "toekomstig": CanonStatus.PROSPECTIVE,
        "vervallen": CanonStatus.REPEALED,
        "ingetrokken": CanonStatus.REPEALED,
    },
    # UK — legislation.gov.uk document status / effect kinds.
    "uk-leg": {
        "revised": CanonStatus.IN_FORCE,
        "final": CanonStatus.IN_FORCE,
        "prospective": CanonStatus.PROSPECTIVE,
        "repealed": CanonStatus.REPEALED,
        "revoked": CanonStatus.REPEALED,
    },
    # EU — CELLAR/CDM in-force-status descriptor (resource_legal_in-force + EUR-Lex banner).
    "eu-cellar": {
        "in_force": CanonStatus.IN_FORCE,
        "in-force": CanonStatus.IN_FORCE,
        "no_longer_in_force": CanonStatus.REPEALED,
        "no-longer-in-force": CanonStatus.REPEALED,
        "not_yet_in_force": CanonStatus.PROSPECTIVE,
        "partially_in_force": CanonStatus.PARTIALLY_IN_FORCE,
    },
    # New Zealand — PCO v0 API status enums (act + secondary-legislation). Point-in-time via
    # versions is exposed, but NZ has **no** amends/repeals graph in v0, so the change edges
    # stay empty for NZ and only this act-level force status is populated (an explicit gap).
    "nz-pco": {
        "in_force": CanonStatus.IN_FORCE,
        "not_in_force": CanonStatus.PROSPECTIVE,     # enacted/made, not yet commenced
        "repealed": CanonStatus.REPEALED,
        "revoked": CanonStatus.REPEALED,
        "superseded": CanonStatus.REPEALED,
        "expired": CanonStatus.EXPIRED,
        "spent": CanonStatus.EXPIRED,
    },
    # Australia — Federal Register of Legislation (OData ``Title.status`` enum).
    "au-register": {
        "inforce": CanonStatus.IN_FORCE,
        "in_force": CanonStatus.IN_FORCE,
        "ceased": CanonStatus.EXPIRED,           # sunset / spent
        "repealed": CanonStatus.REPEALED,
        "nevereffective": CanonStatus.REPEALED,
        "never_effective": CanonStatus.REPEALED,
    },
}

# For UI + MCP: one place to turn a canonical status into a human label, an icon and a tone
# class. The frontend imports the same tone/icon names so backend and banner never drift.
STATUS_META: dict[str, dict[str, str]] = {
    CanonStatus.IN_FORCE: {"label": "In force", "icon": "✓", "tone": "leg-info"},
    CanonStatus.PARTIALLY_IN_FORCE: {"label": "Partly in force", "icon": "◐", "tone": "leg-amended"},
    CanonStatus.PROSPECTIVE: {"label": "Not yet in force", "icon": "🕓", "tone": "leg-info"},
    CanonStatus.AMENDED: {"label": "In force (amended)", "icon": "✏️", "tone": "leg-amended"},
    CanonStatus.CORRECTED: {"label": "Corrected", "icon": "✎", "tone": "leg-corrected"},
    CanonStatus.RECAST: {"label": "Recast", "icon": "⛔", "tone": "leg-repealed"},
    CanonStatus.CONSOLIDATED: {"label": "Consolidated version", "icon": "📑", "tone": "leg-info"},
    CanonStatus.REPEALED: {"label": "Repealed", "icon": "⛔", "tone": "leg-repealed"},
    CanonStatus.EXPIRED: {"label": "Expired / spent", "icon": "⌛", "tone": "leg-repealed"},
    CanonStatus.UNKNOWN: {"label": "Status unconfirmed", "icon": "ℹ️", "tone": "leg-info"},
}

# Precedence when several signals disagree — "worse for the reader relying on it" wins, so a
# repeal is never hidden behind an amendment. Consolidated is a manifestation fact, not a force
# fact, so it ranks below the force outcomes here and is surfaced as a flag instead.
_SEVERITY = [
    CanonStatus.REPEALED, CanonStatus.RECAST, CanonStatus.EXPIRED,
    CanonStatus.PROSPECTIVE, CanonStatus.PARTIALLY_IN_FORCE,
    CanonStatus.AMENDED, CanonStatus.CORRECTED,
    CanonStatus.CONSOLIDATED, CanonStatus.IN_FORCE, CanonStatus.UNKNOWN,
]
_RANK = {s: i for i, s in enumerate(_SEVERITY)}


def normalize_native(scheme: str, token: str | None) -> str | None:
    """Native source token → canonical status string, or ``None`` if unmapped/blank. Case- and
    separator-insensitive so ``"VIGUEUR_DIFF"``, ``"vigueur diff"`` and ``"PartiallyInForce"``
    all land."""
    if not token:
        return None
    key = str(token).strip().lower().replace(" ", "_").replace("-", "_")
    table = _NATIVE.get(scheme, {})
    hit = table.get(key) or table.get(key.replace("_", ""))
    return str(hit) if hit else None


def status_meta(status: str | None) -> dict[str, str]:
    """Label/icon/tone for a canonical status — the single source both UI and MCP read."""
    return STATUS_META.get(status or "", STATUS_META[CanonStatus.UNKNOWN])


def more_severe(a: str | None, b: str | None) -> str | None:
    """Of two canonical statuses, the one a reader most needs to see (repeal beats amendment
    beats in-force). ``None`` inputs defer to the other."""
    if a is None:
        return b
    if b is None:
        return a
    return a if _RANK.get(a, 99) <= _RANK.get(b, 99) else b


@dataclass(slots=True)
class Provision:
    """Currency of one addressable provision (article / section / § / artikel / paragraaf),
    keyed by the same ``anchor`` the citation resolver pinpoints to. Every field beyond
    ``anchor`` is optional — a source that gives only "this § is repealed" fills ``status``;
    one that gives the état window fills the dates too."""

    anchor: str
    status: str | None = None                # canonical status
    native_status: str | None = None         # the raw source token, preserved
    in_force_from: str | None = None         # ISO date the provision's current version began
    in_force_to: str | None = None           # ISO date it ceased / will cease (repeal/expiry)
    changed_by: list[str] = field(default_factory=list)   # amending instrument ids
    change_types: list[str] = field(default_factory=list)  # canonical relation kinds touching it

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "anchor": self.anchor, "status": self.status, "native_status": self.native_status,
            "in_force_from": self.in_force_from, "in_force_to": self.in_force_to,
            "changed_by": self.changed_by or None,
            "change_types": self.change_types or None,
        }.items() if v not in (None, [], "")}

    @classmethod
    def from_dict(cls, d: dict) -> "Provision":
        return cls(
            anchor=str(d.get("anchor") or ""),
            status=d.get("status"), native_status=d.get("native_status"),
            in_force_from=d.get("in_force_from"), in_force_to=d.get("in_force_to"),
            changed_by=list(d.get("changed_by") or []),
            change_types=list(d.get("change_types") or []),
        )


@dataclass(slots=True)
class Currency:
    """Act-level currency an adapter/format parser derives at parse time and stows on the
    record (``extra['currency']`` → ``meta_json['currency']``). The facade later merges it with
    the change-edge graph. ``scheme`` records which native vocabulary ``native_status`` is in,
    so the value stays auditable."""

    status: str | None = None                # canonical (from native_status, if derivable)
    native_status: str | None = None         # raw source token
    scheme: str | None = None                # which _NATIVE table native_status belongs to
    in_force_from: str | None = None         # commencement / entry-into-force (ISO)
    in_force_to: str | None = None           # repeal / expiry date (ISO)
    as_at: str | None = None                 # point-in-time date of THIS expression (consolidation)
    up_to_date: bool | None = None           # False when the source flags unapplied changes
    unapplied_count: int | None = None       # editorial-lag backlog (UK) if known
    point_in_time_capable: bool | None = None  # source supports "as at date D" retrieval
    provisions: list[Provision] = field(default_factory=list)

    def to_meta(self) -> dict:
        """The JSON-able bag stored under ``meta_json['currency']`` (absent fields dropped)."""
        out: dict = {}
        for k in ("status", "native_status", "scheme", "in_force_from", "in_force_to",
                  "as_at", "up_to_date", "unapplied_count", "point_in_time_capable"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.provisions:
            out["provisions"] = [p.to_dict() for p in self.provisions]
        return out

    @classmethod
    def from_meta(cls, meta: dict | None) -> "Currency | None":
        """Rebuild from a document's ``meta_json`` (accepts the whole bag or the ``currency``
        sub-dict). ``None`` when there's no currency block."""
        if not meta:
            return None
        block = meta.get("currency") if isinstance(meta.get("currency"), dict) else meta
        if not isinstance(block, dict) or not any(
                block.get(k) is not None for k in (
                    "status", "native_status", "in_force_from", "in_force_to", "as_at",
                    "up_to_date", "unapplied_count", "provisions")):
            return None
        return cls(
            status=block.get("status"), native_status=block.get("native_status"),
            scheme=block.get("scheme"), in_force_from=block.get("in_force_from"),
            in_force_to=block.get("in_force_to"), as_at=block.get("as_at"),
            up_to_date=block.get("up_to_date"), unapplied_count=block.get("unapplied_count"),
            point_in_time_capable=block.get("point_in_time_capable"),
            provisions=[Provision.from_dict(p) for p in (block.get("provisions") or [])],
        )

    def normalized(self) -> "Currency":
        """Fill ``status`` from ``native_status``+``scheme`` where it isn't already set. Returns
        self for chaining."""
        if self.status is None and self.native_status and self.scheme:
            self.status = normalize_native(self.scheme, self.native_status)
        for p in self.provisions:
            if p.status is None and p.native_status and self.scheme:
                p.status = normalize_native(self.scheme, p.native_status)
        return self


def currency_from_french_versions(doc_etat: str | None, versions: list) -> Currency:
    """Build a :class:`Currency` from the Légifrance parse (§FR). ``versions`` are the parser's
    per-article ``ArticleVersion``-likes (``.article``/``.etat``/``.date_debut``/``.date_fin``).
    Keeps only each article's *current* (VIGUEUR/ABROGE_DIFF or latest) état so the provision
    list is the live picture, not every historical version."""
    cur = Currency(native_status=doc_etat, scheme="fr-etat")
    best: dict[str, object] = {}
    for v in versions or []:
        anchor = getattr(v, "article", None) or getattr(v, "num", None)
        if not anchor:
            continue
        prev = best.get(anchor)
        # prefer a currently-effective état; else the one with the latest dateDebut
        if prev is None or _fr_version_rank(v) >= _fr_version_rank(prev):
            best[anchor] = v
    for anchor, v in best.items():
        etat = getattr(v, "etat", None)
        cur.provisions.append(Provision(
            anchor=str(anchor), native_status=etat,
            status=normalize_native("fr-etat", etat),
            in_force_from=_iso(getattr(v, "date_debut", None)),
            in_force_to=_iso(getattr(v, "date_fin", None)),
        ))
    cur.point_in_time_capable = True
    return cur.normalized()


def currency_for_eu(celex: str | None, in_force: str | None = None) -> Currency:
    """Currency for an EU act (§EU) from its CELEX identity: a sector-0 CELEX is a dated
    consolidation snapshot (status CONSOLIDATED + as-at date); everything else is point-in-time
    capable via the dated-CELEX mechanism, with force status left to the CDM change-edge graph
    unless EUR-Lex's in-force descriptor is supplied."""
    from .eu_law import is_consolidation, consolidation_date
    cur = Currency(scheme="eu-cellar", point_in_time_capable=True,
                   native_status=in_force)
    if celex and is_consolidation(celex):
        cur.status = str(CanonStatus.CONSOLIDATED)
        cur.as_at = consolidation_date(celex)
    return cur.normalized()


def _fr_version_rank(v) -> tuple:
    """Order French article versions so the "most current" wins: in-force états first, then by
    start date."""
    etat = (getattr(v, "etat", None) or "").strip().lower()
    live = 1 if etat in ("vigueur", "abroge_diff", "vigueur_diff") else 0
    d = getattr(v, "date_debut", None)
    return (live, str(d) if d else "")


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()[:10]  # date/datetime
    except AttributeError:
        s = str(value).strip()
        return s[:10] if s else None
