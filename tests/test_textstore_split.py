"""Text storage spanning two roots.

The corpus outgrew the machine that serves it: the whole text projection is tens of
gigabytes on another host, where a random read measured 22.57 ms against 0.046 ms
locally. So the jurisdictions being searched are copied local and the rest stay
remote — and the failure mode that arrangement invites is silent migration. A repair
that walks the corpus calling ``put`` would copy every document it touched onto the
small fast disk until it filled, halfway through a job, with no warning.

The rule these tests pin: a write goes where the document already lives, and only a
genuinely new payload lands in the primary root.
"""

from __future__ import annotations

from raglex.core.models import Segment
from raglex.storage import TextStore


def _split(tmp_path):
    local, remote = tmp_path / "local", tmp_path / "remote"
    remote.mkdir(parents=True, exist_ok=True)
    return TextStore(local, fallback=remote), local, remote


def test_a_single_root_behaves_exactly_as_before(tmp_path):
    """No fallback configured → nothing about the old behaviour changes."""
    ts = TextStore(tmp_path / "t")
    ts.put("a" * 64, "hello")
    assert ts.get("a" * 64) == "hello"
    assert ts.locate("a" * 64) == "local"
    assert ts.locate("b" * 64) is None


def test_reads_fall_through_to_the_remote_root(tmp_path):
    ts, _local, remote = _split(tmp_path)
    h = "c" * 64
    # something only the remote root holds, as a copied corpus would be
    p = remote / h[:2] / h[2:4] / f"{h}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("remote body", encoding="utf-8")
    assert ts.get(h) == "remote body"
    assert ts.locate(h) == "fallback"


def test_the_local_copy_wins_when_both_hold_it(tmp_path):
    ts, local, remote = _split(tmp_path)
    h = "d" * 64
    for root, body in ((remote, "old"), (local, "new")):
        p = root / h[:2] / h[2:4] / f"{h}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    assert ts.get(h) == "new"
    assert ts.locate(h) == "local"


def test_a_repair_does_not_migrate_a_remote_document(tmp_path):
    """The failure this design exists to prevent. The mojibake repair alone rewrote
    28,681 documents; if each landed locally the disk would fill mid-job."""
    ts, local, remote = _split(tmp_path)
    h = "e" * 64
    p = remote / h[:2] / h[2:4] / f"{h}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("needs repair", encoding="utf-8")

    ts.put(h, "repaired")

    assert p.read_text(encoding="utf-8") == "repaired"      # fixed in place
    assert not (local / h[:2] / h[2:4] / f"{h}.txt").exists()  # and NOT copied local
    assert ts.locate(h) == "fallback"


def test_a_new_document_lands_locally(tmp_path):
    """A fresh harvest goes to the fast disk, which is the point of having one."""
    ts, local, remote = _split(tmp_path)
    h = "f" * 64
    ts.put(h, "freshly harvested")
    assert (local / h[:2] / h[2:4] / f"{h}.txt").exists()
    assert not (remote / h[:2] / h[2:4] / f"{h}.txt").exists()
    assert ts.locate(h) == "local"


def test_migration_is_possible_but_only_when_asked(tmp_path):
    ts, local, remote = _split(tmp_path)
    h = "1" * 64
    p = remote / h[:2] / h[2:4] / f"{h}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("bring me local", encoding="utf-8")

    ts.put_local(h, ts.get(h))

    assert ts.locate(h) == "local"
    assert ts.get(h) == "bring me local"
    assert p.exists()          # the remote copy is left alone, not deleted


def test_segments_are_written_beside_the_text_they_describe(tmp_path):
    ts, local, remote = _split(tmp_path)
    h = "2" * 64
    p = remote / h[:2] / h[2:4] / f"{h}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("body", encoding="utf-8")

    ts.put_segments(h, [Segment(label="1.", kind="paragraph", level=1,
                                char_start=0, char_end=4)])

    assert (remote / h[:2] / h[2:4] / f"{h}.seg.json").exists()
    assert not (local / h[:2] / h[2:4] / f"{h}.seg.json").exists()
    assert [s.label for s in ts.get_segments(h)] == ["1."]


def test_a_missing_fallback_mount_is_ignored_not_fatal(tmp_path):
    """An unmounted remote must not look like an empty corpus, and must not stop the
    service starting."""
    ts = TextStore(tmp_path / "local", fallback=tmp_path / "not-mounted")
    assert ts.fallback is None
    ts.put("3" * 64, "still works")
    assert ts.get("3" * 64) == "still works"


def test_the_fallback_can_come_from_the_environment(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("RAGLEX_TEXT_FALLBACK_DIR", str(remote))
    ts = TextStore(tmp_path / "local")
    assert ts.fallback == remote


# -- where a NEW document goes -------------------------------------------------
def test_a_new_out_of_scope_harvest_stays_remote(tmp_path):
    """Without this the 2.9M-document French collection would land on a disk sized
    for the UK and EU slices the first time its watch ran."""
    local, remote = tmp_path / "local", tmp_path / "remote"
    remote.mkdir(parents=True)
    ts = TextStore(local, fallback=remote, local_sources={"uk-caselaw", "eu-cellar"})

    ts.put("4" * 64, "a French decision", source="fr-dila")
    assert ts.locate("4" * 64) == "fallback"

    ts.put("5" * 64, "a UK judgment", source="uk-caselaw")
    assert ts.locate("5" * 64) == "local"


def test_no_scope_configured_keeps_everything_local(tmp_path):
    ts, _local, _remote = _split(tmp_path)
    ts.put("6" * 64, "anything", source="fr-dila")
    assert ts.locate("6" * 64) == "local"


def test_an_unwritable_remote_does_not_lose_the_document(tmp_path):
    """A harvest that cannot store its text is worse than one that stores it in the
    wrong place."""
    import os

    local, remote = tmp_path / "local", tmp_path / "remote"
    remote.mkdir(parents=True)
    ts = TextStore(local, fallback=remote, local_sources={"uk-caselaw"})
    os.chmod(remote, 0o500)
    try:
        ts.put("7" * 64, "out of scope", source="fr-dila")
        assert ts.locate("7" * 64) == "local"
        assert ts.get("7" * 64) == "out of scope"
    finally:
        os.chmod(remote, 0o700)


def test_scope_can_come_from_the_environment(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("RAGLEX_TEXT_LOCAL_SOURCES", "uk-caselaw, eu-cellar")
    ts = TextStore(tmp_path / "local", fallback=remote)
    assert ts.local_sources == {"uk-caselaw", "eu-cellar"}


def test_scope_never_overrides_where_a_document_already_lives(tmp_path):
    """An in-scope document that happens to sit remote is still repaired in place —
    only localise_text moves things."""
    local, remote = tmp_path / "local", tmp_path / "remote"
    remote.mkdir(parents=True)
    ts = TextStore(local, fallback=remote, local_sources={"uk-caselaw"})
    h = "8" * 64
    p = remote / h[:2] / h[2:4] / f"{h}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("lives remote", encoding="utf-8")

    ts.put(h, "repaired", source="uk-caselaw")

    assert ts.locate(h) == "fallback"
    assert p.read_text(encoding="utf-8") == "repaired"
