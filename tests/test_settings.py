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


def test_facade_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    from raglex.config import Config
    from raglex.facade import Facade

    config = Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        topic_threshold=3.0, embed_provider="local-hashing", embed_model=None,
    )
    f = Facade(config)
    f.update_settings({"ZOTERO_API_KEY": "zkey", "ZOTERO_LIBRARY_ID": "42"})
    import os

    assert os.environ.get("ZOTERO_API_KEY") == "zkey"  # applied to env this process
    # zotero import now uses stored creds (fails only on the network call, not on creds)
    rows = {r["key"]: r for r in f.get_settings()["settings"]}
    assert rows["ZOTERO_LIBRARY_ID"]["display"] == "42"
