"""Runtime configuration. Env-driven; sensible single-operator defaults.

Secrets (API keys) come from the environment / secret store, never config rows
(§6d safety rail). Paths default under ``./data`` so a fresh checkout runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Config:
    data_dir: Path
    catalogue_path: str | Path  # a filesystem path (SQLite) or a postgresql:// DSN
    raw_dir: Path
    text_dir: Path
    settings_path: Path
    topic_threshold: float
    embed_provider: str
    embed_model: str | None

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.environ.get("RAGLEX_DATA_DIR", "data")).expanduser()
        # Load the UI-editable settings file into the environment first (env still
        # wins — apply_to_env never overrides a real env var), so the reads below
        # transparently pick up file-stored keys.
        from .settings import SettingsStore

        settings_path = Path(os.environ.get("RAGLEX_SETTINGS", data_dir / "settings.json"))
        SettingsStore(settings_path).apply_to_env()
        return cls(
            settings_path=settings_path,
            data_dir=data_dir,
            # RAGLEX_DB_URL (a postgresql://… DSN) selects the Postgres spine (§7);
            # otherwise the portable SQLite file. Passed straight to Catalogue,
            # which detects the backend from the string.
            catalogue_path=(
                os.environ.get("RAGLEX_DB_URL")
                or os.environ.get("RAGLEX_CATALOGUE")
                or str(data_dir / "catalogue.sqlite")
            ),
            raw_dir=Path(os.environ.get("RAGLEX_RAW_DIR", data_dir / "raw")),
            text_dir=Path(os.environ.get("RAGLEX_TEXT_DIR", data_dir / "text")),
            topic_threshold=float(os.environ.get("RAGLEX_TOPIC_THRESHOLD", "3.0")),
            # Default to the zero-dep offline provider; set these to use a real
            # model (e.g. openrouter + a legal/multilingual model, §6a/§6d).
            embed_provider=os.environ.get("RAGLEX_EMBED_PROVIDER", "local-hashing"),
            embed_model=os.environ.get("RAGLEX_EMBED_MODEL") or None,
        )

    def make_provider(self):
        """Construct the configured embedding provider (§6d)."""
        from .embeddings import get_provider

        kwargs = {"model": self.embed_model} if self.embed_model else {}
        return get_provider(self.embed_provider, **kwargs)
