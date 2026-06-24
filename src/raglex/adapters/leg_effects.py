"""Parse legislation.gov.uk **unapplied effects** — the amendments the editors know
about but haven't yet written into the published text (the editorial lag, §0).

legislation.gov.uk is a *live mirror* of the Statute Law Database, which runs behind
the live statute book. The gap is machine-readable: every piece of legislation's XML
carries a ``<ukm:UnappliedEffects>`` block, one ``<ukm:UnappliedEffect>`` per pending
change, naming the *affecting* (amending) instrument, the *affected* provisions, and —
for commencements — the *commencing* order. We read it to (a) mint ``amended_by``
edges to the amending legislation (so the §5b worklist pulls it) and (b) flag the
affected instrument as having *outstanding effects*, so the scheduler re-checks it on a
slow cadence until the editors have incorporated everything (storage.catalogue's
``effects_refresh`` queue).

Two caveats from legislation.gov.uk's own docs are honoured here:
- An "effect" is *any* impact, not just a text edit — "inserted", "repealed", but also
  "modified", "applied", and commencements. ``Type`` tells you which; we keep it.
- Effects describe *future* states, so some referenced provisions don't exist in the
  current text yet — we never assume the affected/affecting ref resolves now.

Namespace-agnostic on purpose: the ``ukm`` block appears in CLML (``data.xml``) and,
embedded in the proprietary-metadata island, in the Akoma Ntoso (``data.akn``) the UK
adapter already fetches — so the same parser works on either without a second request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

# legislation.gov.uk "class" tokens → the URI type segment, for effects that give the
# affecting/commencing instrument by Class/Year/Number rather than a full URI.
_CLASS_TO_TYPE = {
    "unitedkingdompublicgeneralact": "ukpga",
    "unitedkingdomlocalact": "ukla",
    "unitedkingdomstatutoryinstrument": "uksi",
    "unitedkingdomchurchmeasure": "ukcm",
    "scottishact": "asp",
    "scottishparliamentact": "asp",
    "scottishstatutoryinstrument": "ssi",
    "walesact": "anaw",
    "actofsenedd": "asc",
    "actofseneddcymru": "asc",
    "nationalassemblyforwalesact": "anaw",
    "welshstatutoryinstrument": "wsi",
    "northernirelandact": "nia",
    "northernirelandorderincouncil": "nisi",
    "northernirelandstatutoryrule": "nisr",
    "actofthenorthernirelandassembly": "nia",
}


@dataclass(frozen=True, slots=True)
class UnappliedEffect:
    type: str | None            # "inserted" | "repealed" | "text amended" | "Commencement Order" | …
    affecting_id: str | None    # legislation stable_id of the amending instrument (ukpga/2025/8)
    affected_ref: str | None    # the affected provision (e.g. "section-6")
    commencing_id: str | None   # for commencement effects: the commencing order
    notes: str | None

    @property
    def is_commencement(self) -> bool:
        t = (self.type or "").lower()
        return "commencement" in t or self.commencing_id is not None


def _local(tag: str) -> str:
    """Strip any XML namespace, lower-cased — so {ns}UnappliedEffect → unappliedeffect."""
    return tag.rsplit("}", 1)[-1].lower()


def _attrs_by_local(el: ET.Element) -> dict[str, str]:
    """The element's attributes keyed by namespace-stripped, lower-cased name."""
    return {_local(k): v for k, v in el.attrib.items()}


def normalise_leg_uri(value: str | None) -> str | None:
    """A legislation.gov.uk URI (or id-URI) → the bare stable_id ``type/year/number``.

    ``http://www.legislation.gov.uk/id/ukpga/2025/8/section/3`` → ``ukpga/2025/8``.
    Returns None if it can't be reduced to a plausible type/year/number triple.
    """
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^https?://(?:www\.)?legislation\.gov\.uk/", "", v, flags=re.IGNORECASE)
    v = v.lstrip("/")
    if v.lower().startswith("id/"):
        v = v[3:]
    parts = v.split("/")
    if len(parts) < 3:
        return None
    # Pre-1963 Acts use a 4-segment regnal id — type/monarch/session/number
    # (ukpga/Eliz2/1-2/37) — where the second segment is alphabetic, not a year.
    # Assimilated EU law is also 4-segment (european/regulation/2016/0679). Keep four
    # segments when the second isn't a plain year; otherwise the usual type/year/number.
    n = 4 if (len(parts) >= 4 and not parts[1].isdigit()) else 3
    keep = parts[:n]
    if not all(keep):
        return None
    return "/".join(keep)


def _affecting_id(a: dict[str, str]) -> str | None:
    """Resolve the affecting instrument id from a URI, else from Class/Year/Number."""
    via_uri = normalise_leg_uri(a.get("affectinguri"))
    if via_uri:
        return via_uri
    cls = (a.get("affectingclass") or "").lower()
    typ = _CLASS_TO_TYPE.get(cls)
    year, num = a.get("affectingyear"), a.get("affectingnumber")
    if typ and year and num:
        return f"{typ}/{year}/{num}"
    return None


def _commencing_id(a: dict[str, str]) -> str | None:
    via_uri = normalise_leg_uri(a.get("commencinguri"))
    if via_uri:
        return via_uri
    cls = (a.get("commencingclass") or "").lower()
    typ = _CLASS_TO_TYPE.get(cls)
    year, num = a.get("commencingyear"), a.get("commencingnumber")
    if typ and year and num:
        return f"{typ}/{year}/{num}"
    return None


def parse_unapplied_effects(raw: bytes | str) -> list[UnappliedEffect]:
    """Every ``<ukm:UnappliedEffect>`` in the legislation XML, as structured records.

    Tolerant of malformed XML (returns ``[]``) and of either the CLML or AKN-embedded
    form. De-namespaced so it doesn't depend on a fixed ``ukm`` prefix/URI."""
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: list[UnappliedEffect] = []
    for el in root.iter():
        if _local(el.tag) != "unappliedeffect":
            continue
        a = _attrs_by_local(el)
        # Notes can be an attribute or a child <Note>/<Notes> element.
        notes = a.get("notes")
        if not notes:
            for child in el:
                if _local(child.tag) in ("notes", "note") and (child.text or "").strip():
                    notes = child.text.strip()
                    break
        out.append(UnappliedEffect(
            type=a.get("type"),
            affecting_id=_affecting_id(a),
            affected_ref=a.get("affectedsectionref") or a.get("affectedprovisionsref"),
            commencing_id=_commencing_id(a),
            notes=notes,
        ))
    return out


@dataclass(frozen=True, slots=True)
class ChangeEffect:
    """One row of the *affecting-side* "Changes to Legislation" feed
    (``/changes/affecting/{uri}/data.feed``): a change THIS instrument makes to another.

    Unlike ``UnappliedEffect`` (the affected-side backlog), this lists *every* effect an
    amending act produces — ``applied`` says whether it's been written into the affected
    text yet. This is the data that lets a freshly-imported amending act push its changes
    OUT to the (old, maybe-never-repulled) instruments it affects."""
    affected_id: str | None     # the instrument being changed (ukpga/Eliz2/1-2/37)
    affecting_id: str | None     # the amending instrument (this act)
    type: str | None             # "words substituted" | "repealed" | …
    applied: bool                # has the change been incorporated into the affected text?
    affected_provision: str | None
    affecting_provision: str | None
    affected_title: str | None


def parse_changes_feed(raw: bytes | str) -> list[ChangeEffect]:
    """Parse the Atom ``data.feed`` of the affecting-side Changes-to-Legislation results.
    Each ``<ukm:effect>`` describes a change this act makes to another instrument."""
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: list[ChangeEffect] = []
    for el in root.iter():
        if _local(el.tag) != "effect":
            continue
        a = _attrs_by_local(el)
        affected = normalise_leg_uri(a.get("affecteduri"))
        if not affected:
            cls = (a.get("affectedclass") or "").lower()
            typ = _CLASS_TO_TYPE.get(cls)
            if typ and a.get("affectedyear") and a.get("affectednumber"):
                affected = f"{typ}/{a['affectedyear']}/{a['affectednumber']}"
        title = None
        for child in el:
            if _local(child.tag) == "affectedtitle" and (child.text or "").strip():
                title = child.text.strip()
                break
        out.append(ChangeEffect(
            affected_id=affected,
            affecting_id=normalise_leg_uri(a.get("affectinguri")),
            type=a.get("type"),
            applied=(a.get("applied") or "").lower() == "true",
            affected_provision=a.get("affectedprovisions"),
            affecting_provision=a.get("affectingprovisions"),
            affected_title=title,
        ))
    return out


def summarise_effects(effects: list[UnappliedEffect]) -> dict:
    """Roll up parsed effects into the shape the pipeline stores on the record and the
    catalogue tracks: the outstanding count, the distinct amending instruments to queue,
    and a small type histogram for display."""
    affecting = sorted({e.affecting_id for e in effects if e.affecting_id}
                       | {e.commencing_id for e in effects if e.commencing_id})
    types: dict[str, int] = {}
    for e in effects:
        key = (e.type or "effect").strip()
        types[key] = types.get(key, 0) + 1
    return {"outstanding": len(effects), "affecting": affecting, "types": types}
