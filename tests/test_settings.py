from __future__ import annotations

import json

from raglex.settings import SettingsStore


def test_file_value_resolves_and_masks(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    s = SettingsStore(tmp_path / "settings.json")
    s.update({"OPENROUTER_API_KEY": "sk-secret-12345", "ZOTERO_LIBRARY_ID": "99"})

    assert s.resolve("OPENROUTER_API_KEY") == "sk-secret-12345"
    masked = {row["key"]: row for row in s.masked()["settings"]}
    # secret never returned in clear; non-secret shown plainly
    assert masked["OPENROUTER_API_KEY"]["display"] == "••••2345"
    assert masked["OPENROUTER_API_KEY"]["source"] == "file"
    assert masked["ZOTERO_LIBRARY_ID"]["display"] == "99"


def test_env_overrides_file(tmp_path, monkeypatch):
    s = SettingsStore(tmp_path / "settings.json")
    s.update({"OPENROUTER_API_KEY": "from-file"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    value, source = s.effective("OPENROUTER_API_KEY")
    assert value == "from-env" and source == "env"


def test_apply_to_env_does_not_override_real_env(tmp_path, monkeypatch):
    s = SettingsStore(tmp_path / "settings.json")
    s.update({"OPENROUTER_API_KEY": "from-file", "VOYAGE_API_KEY": "voyage-file"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-env")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    s.apply_to_env()
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "real-env"  # env wins
    assert os.environ["VOYAGE_API_KEY"] == "voyage-file"  # file fills the gap


def test_update_clears_on_empty_and_ignores_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    s = SettingsStore(tmp_path / "settings.json")
    s.update({"ZOTERO_API_KEY": "k", "NOT_A_SETTING": "x"})
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw == {"ZOTERO_API_KEY": "k"}  # unknown ignored
    s.update({"ZOTERO_API_KEY": ""})
    assert json.loads((tmp_path / "settings.json").read_text()) == {}  # cleared


def test_static_export_attribution_is_editable_html(tmp_path, monkeypatch):
    monkeypatch.delenv("RAGLEX_STATIC_EXPORT_ATTRIBUTION", raising=False)
    s = SettingsStore(tmp_path / "settings.json")
    value = 'Maintained by <a href="https://example.test/profile">Example</a>.'
    s.update({"RAGLEX_STATIC_EXPORT_ATTRIBUTION": value})

    rows = {row["key"]: row for row in s.masked()["settings"]}
    row = rows["RAGLEX_STATIC_EXPORT_ATTRIBUTION"]
    assert row["kind"] == "html"
    assert row["display"] == value


def test_facade_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    from raglex.config import Config
    from raglex.facade import Facade

    config = Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing", embed_model=None,
    )
    f = Facade(config)
    f.update_settings({"ZOTERO_API_KEY": "zkey", "ZOTERO_LIBRARY_ID": "42"})
    import os

    assert os.environ.get("ZOTERO_API_KEY") == "zkey"  # applied to env this process
    # zotero import now uses stored creds (fails only on the network call, not on creds)
    rows = {r["key"]: r for r in f.get_settings()["settings"]}
    assert rows["ZOTERO_LIBRARY_ID"]["display"] == "42"


def test_apply_to_env_refreshes_a_value_it_promoted_itself(tmp_path, monkeypatch):
    """The cross-process refresh. The API writes a UI change into the file; every OTHER
    process catches up by calling apply_to_env on its next tick. It used to only fill a
    blank, so a value promoted at boot was frozen for the life of the process — which is
    how the scheduler kept promoting jobs against a max-concurrent of 1 while the UI, the
    file and every fresh process said 2, and a queue sat still with a slot free."""
    import os

    monkeypatch.delenv("RAGLEX_MAX_CONCURRENT_JOBS", raising=False)
    path = tmp_path / "settings.json"

    def written_elsewhere(value):
        """The API container writing the file — this process's environment is untouched."""
        path.write_text(json.dumps({"RAGLEX_MAX_CONCURRENT_JOBS": value} if value else {}))

    written_elsewhere("1")
    store = SettingsStore(path)          # the scheduler, booting
    store.apply_to_env()
    assert os.environ["RAGLEX_MAX_CONCURRENT_JOBS"] == "1"

    written_elsewhere("2")               # the operator raises it in the UI
    store.apply_to_env()
    assert os.environ["RAGLEX_MAX_CONCURRENT_JOBS"] == "2"

    written_elsewhere(None)              # …and clearing it withdraws it, so an
    store.apply_to_env()                 # "unpause" also crosses the process boundary
    assert os.environ.get("RAGLEX_MAX_CONCURRENT_JOBS") in (None, "")


def test_apply_to_env_never_overwrites_a_value_it_did_not_promote(tmp_path, monkeypatch):
    """Refreshing our own promotion must not become a licence to clobber the deployment's
    environment: `env > file` still holds."""
    import os

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"RAGLEX_MAX_CONCURRENT_JOBS": "1"}))
    store = SettingsStore(path)
    store.apply_to_env()

    monkeypatch.setenv("RAGLEX_MAX_CONCURRENT_JOBS", "9")   # the deployment owns it now
    path.write_text(json.dumps({"RAGLEX_MAX_CONCURRENT_JOBS": "4"}))
    store.apply_to_env()
    assert os.environ["RAGLEX_MAX_CONCURRENT_JOBS"] == "9"
