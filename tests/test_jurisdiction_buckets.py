"""A source's jurisdiction is whatever its ``SourceInfo`` says, everywhere.

``adapter-authoring.md`` states that ``SourceInfo`` is the sole naming and jurisdiction
schema and that no screen may keep its own country table. The facade kept one anyway: a
tuple of fifteen key prefixes, and any source whose key began with none of them was filed
under **Other**.

That is not a cosmetic bug, because "Other" is where a person stops looking. On
2026-08-14 it held 358,176 documents — the whole of Austria (138,968), Finland (101,285),
Estonia (50,106), Slovakia (35,249) and Sweden (17,467) — every one of which had declared
its jurisdiction correctly in the registry when it was written. Explore, the jurisdiction
facet, and every count drawn from them showed a corpus that did not know it held Finland.

The prefix table survives for the legacy keys that predate the registry (``bailii``,
``westlaw``, ``hol``, ``ico``) and for two imported corpora with no adapter. It must never
again be the thing a new source has to be added to.
"""

from __future__ import annotations

import pytest

from raglex.adapters import registry
from raglex.facade import Facade, _registered_jurisdictions


@pytest.fixture(scope="module")
def jurisdiction_of():
    return Facade.__new__(Facade)._jurisdiction_of


def test_no_registered_source_is_filed_under_other(jurisdiction_of):
    """The whole class of bug, stated once: if a source declared a jurisdiction, the
    corpus must use it."""
    orphans = {
        key: info.jurisdiction
        for key, info in registry.SOURCE_INFO.items()
        if info.jurisdiction and info.jurisdiction != ""
        and jurisdiction_of(key) == "Other"
    }
    assert not orphans, (
        f"{len(orphans)} sources declare a jurisdiction and are still bucketed as "
        f"'Other': {sorted(orphans)[:12]}. Facade._jurisdiction_of must read "
        f"SourceInfo, not a prefix table.")


def test_every_declared_jurisdiction_code_has_a_label():
    """A code with no entry in JURISDICTION_LABELS resolves to nothing and falls through
    to the prefix table, which is how a whole country goes quiet."""
    missing = sorted({info.jurisdiction for info in registry.SOURCE_INFO.values()
                      if info.jurisdiction
                      and info.jurisdiction not in registry.JURISDICTION_LABELS})
    assert not missing, f"jurisdiction codes with no label: {missing}"


@pytest.mark.parametrize("source,expected", [
    ("se-domstol", "Sweden"),
    ("se-domstol-bulk", "Sweden"),
    ("at-vwgh", "Austria"),
    ("at-dsb", "Austria"),
    ("fi-kko", "Finland"),
    ("fi-saadokset", "Finland"),
    ("ee-lahend", "Estonia"),
    ("sk-ress", "Slovakia"),
])
def test_the_five_jurisdictions_that_were_lost(source, expected):
    assert Facade.__new__(Facade)._jurisdiction_of(source) == expected


@pytest.mark.parametrize("source,expected", [
    ("bailii", "United Kingdom"),
    ("westlaw", "United Kingdom"),
    ("hol", "United Kingdom"),
    ("ico", "United Kingdom"),
    ("ci-caselaw", "Channel Islands"),
    ("offshore-caselaw", "Offshore & int'l commercial"),
])
def test_the_legacy_keys_the_prefix_table_still_answers_for(source, expected):
    """These have no adapter and so no SourceInfo; the fallback is what serves them."""
    assert Facade.__new__(Facade)._jurisdiction_of(source) == expected


def test_an_unknown_source_is_still_other(jurisdiction_of):
    assert jurisdiction_of("something-nobody-registered") == "Other"
    assert jurisdiction_of("") == "Other"
    assert jurisdiction_of(None) == "Other"


def test_the_registry_lookup_is_case_insensitive(jurisdiction_of):
    assert jurisdiction_of("SE-Domstol") == "Sweden"


def test_the_registry_mapping_covers_the_whole_catalogue():
    """Every source in the catalogue that names a jurisdiction is in the cached map, so
    the fix cannot quietly cover only the sources someone remembered to list."""
    mapped = _registered_jurisdictions()
    declared = {k.lower() for k, i in registry.SOURCE_INFO.items()
                if i.jurisdiction and i.jurisdiction in registry.JURISDICTION_LABELS
                and registry.JURISDICTION_LABELS[i.jurisdiction] != "Other"}
    assert declared <= set(mapped)
