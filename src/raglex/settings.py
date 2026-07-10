"""Settings / secrets store (UI-editable, file-persisted).

One JSON file in the data dir holds the credentials and config the operator would
otherwise scatter across a dozen environment variables: embedding-provider keys,
Zotero login, future source credentials (EUR-Lex, PISTE). Because it lives under
the data dir, a single Docker bind mount persists it alongside the catalogue and
raw store — no `-e` soup.

Precedence is **environment variable > settings file**: an explicitly-set env var
always wins (so deployments can still inject secrets), the file is the editable
default, and the UI shows which source is in effect. Secrets are masked on read —
the file is written `0600` and full secret values are never returned to the UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str  # the canonical env-var name (so apply_to_env wires existing readers)
    label: str
    secret: bool
    group: str
    placeholder: str = ""


# The known settings the UI renders. Adding a credentialed source = one row here.
KNOWN_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("RAGLEX_EMBED_PROVIDER", "Embedding provider", False, "Embeddings", "local-hashing | openrouter | mcp"),
    SettingSpec("RAGLEX_EMBED_MODEL", "Embedding model", False, "Embeddings", "openai/text-embedding-3-small | bge-m3"),
    SettingSpec("RAGLEX_EMBED_DIMENSIONS", "Embedding dimensions", False, "Embeddings",
                "must match the model; changing it starts a NEW vector family and re-embeds"),
    SettingSpec("RAGLEX_ML_URL", "ML sidecar MCP URL", False, "Embeddings",
                "http://raglex-ml:9000/mcp — serves embed + rerank"),
    SettingSpec("RAGLEX_ML_TOKEN", "ML sidecar token", True, "Embeddings"),
    SettingSpec("RAGLEX_RERANKER", "Reranker", False, "Embeddings", "identity | mcp"),
    SettingSpec("RAGLEX_RERANK_MODEL", "Reranker model", False, "Embeddings", "bge-reranker-v2-m3"),
    SettingSpec("OPENROUTER_API_KEY", "OpenRouter API key", True, "Embeddings"),
    SettingSpec("VOYAGE_API_KEY", "Voyage API key", True, "Embeddings"),
    # LLM passes (citation extraction + treatment classification, §5). Any
    # OpenAI-chat-shaped endpoint; defaults target OpenRouter.
    SettingSpec("RAGLEX_LLM_BASE_URL", "LLM base URL", False, "LLM",
                "https://openrouter.ai/api/v1 | http://localhost:11434/v1"),
    SettingSpec("RAGLEX_LLM_MODEL", "LLM model", False, "LLM", "anthropic/claude-3.5-haiku"),
    SettingSpec("RAGLEX_LLM_API_KEY", "LLM API key", True, "LLM",
                "blank → reuse OPENROUTER_API_KEY / local keyless server"),
    SettingSpec("RAGLEX_LLM_ENABLED", "Enable LLM passes", False, "LLM", "1 | 0 (auto when a key is set)"),
    SettingSpec("ZOTERO_API_KEY", "Zotero API key", True, "Zotero"),
    SettingSpec("ZOTERO_LIBRARY_ID", "Zotero library ID", False, "Zotero", "numeric user/group id"),
    SettingSpec("ZOTERO_LIBRARY_TYPE", "Zotero library type", False, "Zotero", "users | groups"),
    SettingSpec("EURLEX_USERNAME", "EUR-Lex webservice user", False, "Sources"),
    SettingSpec("EURLEX_PASSWORD", "EUR-Lex webservice password", True, "Sources"),
    SettingSpec("PISTE_KEY_ID", "PISTE KeyId (Judilibre)", True, "Sources"),
    SettingSpec("RAGLEX_PROXY", "Outbound proxy (all traffic)", True, "Network",
                "socks5://user:pass@host:1080 | http://host:8080"),
    SettingSpec("RAGLEX_SCRAPER", "Scraper engine", False, "Network",
                "httpx | stealth | playwright"),
    SettingSpec("RAGLEX_AUTOHARVEST", "Auto-drain worklist (refs/tick)", False, "Network",
                "0 = off; e.g. 25 — the scheduler slowly fetches routable citations each tick"),
    SettingSpec("RAGLEX_MISS_TTL_DAYS", "Absent-reference cooldown (days)", False, "Network",
                "90 — days to skip a reference the source said does not exist (404). Only genuine absences land here"),
    SettingSpec("RAGLEX_RETRY_TTL_HOURS", "Unreachable-reference cooldown (hours)", False, "Network",
                "6 — hours to skip a reference we merely couldn't fetch (timeout, 5xx). Short: the document probably exists"),
    SettingSpec("RAGLEX_API_TOKEN", "API bearer token", True, "Network",
                "blank = open. Set it and the REST API + MCP endpoint both require it"),
    SettingSpec("RAGLEX_ALERT_WEBHOOK", "Alert webhook URL", True, "Network",
                "ntfy.sh/your-topic — pushes source-failure and drain-stalled alerts"),
)
_SPEC_BY_KEY = {s.key: s for s in KNOWN_SETTINGS}


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


class SettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_file(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def resolve(self, key: str) -> str | None:
        """Effective value: env wins over the file."""
        env = os.environ.get(key)
        if env is not None and env != "":
            return env
        return self._read_file().get(key) or None

    def effective(self, key: str) -> tuple[str | None, str]:
        """(value, source) where source ∈ {env, file, unset}.

        Note ``apply_to_env`` copies file values into the process environment so
        env-reading code picks them up — which would otherwise make every file-set
        value look (and lock) as 'env'. So an env value that *equals* the file value
        is treated as file-sourced (editable); only a genuinely different env var is
        an immutable 'env' override."""
        env = os.environ.get(key)
        file_val = self._read_file().get(key)
        if env not in (None, ""):
            if file_val not in (None, "") and env == file_val:
                return env, "file"
            return env, "env"
        if file_val not in (None, ""):
            return file_val, "file"
        return None, "unset"

    def update(self, patch: dict) -> dict:
        """Write provided keys to the file (env is never modified). Empty string
        clears a key. Returns the masked view."""
        data = self._read_file()
        for key, value in patch.items():
            if key not in _SPEC_BY_KEY:
                continue  # ignore unknown keys
            if value is None or value == "":
                data.pop(key, None)
                # also clear any value apply_to_env had promoted, so the change
                # takes effect live (and the UI doesn't keep showing the old value)
                if os.environ.get(key) == self._read_file().get(key):
                    os.environ.pop(key, None)
            else:
                data[key] = value
                # apply immediately so adapters pick it up without a restart, and so
                # source detection sees it as file-sourced (editable), not env-locked
                os.environ[key] = str(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), "utf-8")
        try:
            os.chmod(self.path, 0o600)  # secrets at rest
        except OSError:
            pass
        return self.masked()

    def masked(self) -> dict:
        """UI view: per known setting, its source + a non-revealing display value.
        Secrets show only whether set and a masked hint; env-overridden keys say so."""
        out = []
        for spec in KNOWN_SETTINGS:
            value, source = self.effective(spec.key)
            display = "" if value is None else (_mask(value) if spec.secret else value)
            out.append({
                "key": spec.key,
                "label": spec.label,
                "secret": spec.secret,
                "group": spec.group,
                "placeholder": spec.placeholder,
                "set": value is not None,
                "source": source,  # 'env' values can't be edited away in the file
                "display": display,
            })
        return {"settings": out, "path": str(self.path)}

    def apply_to_env(self) -> None:
        """Load file values into the environment WITHOUT overriding real env vars
        (env > file), so all existing env-reading code transparently picks them up."""
        for key, value in self._read_file().items():
            if key and os.environ.get(key) in (None, "") and value not in (None, ""):
                os.environ[key] = str(value)
