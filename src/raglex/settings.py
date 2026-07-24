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
    SettingSpec("RAGLEX_EMBED_PROVIDER", "Embedding provider", False, "Embeddings",
                "local-hashing | tei (open weights via TEI/vLLM) | openrouter | mcp"),
    SettingSpec("RAGLEX_EMBED_MODEL", "Embedding model", False, "Embeddings",
                "Qwen/Qwen3-Embedding-0.6B | BAAI/bge-m3 | openai/text-embedding-3-small"),
    SettingSpec("RAGLEX_EMBED_DIMENSIONS", "Embedding dimensions", False, "Embeddings",
                "must match the model; changing it starts a NEW vector family and re-embeds"),
    SettingSpec("RAGLEX_EMBED_MODEL_VERSION", "Embedding model revision", False, "Embeddings",
                "pin the HF revision so a silent weight update can't split the family"),
    SettingSpec("RAGLEX_EMBED_INSTRUCTION", "Query instruction", False, "Embeddings",
                "task prefix for instruction-tuned embedders; blank = legal default for Qwen3"),
    SettingSpec("RAGLEX_TEI_URL", "Embedding server URL", False, "Embeddings",
                "http://raglex-tei:8080 — any OpenAI-compatible /v1/embeddings server"),
    SettingSpec("RAGLEX_ML_URL", "ML sidecar MCP URL", False, "Embeddings",
                "http://raglex-ml:9000/mcp — serves embed + rerank"),
    SettingSpec("RAGLEX_ML_TOKEN", "ML sidecar token", True, "Embeddings"),
    SettingSpec("RAGLEX_RERANKER", "Reranker", False, "Embeddings", "identity | tei | mcp"),
    SettingSpec("RAGLEX_RERANK_MODEL", "Reranker model", False, "Embeddings", "bge-reranker-v2-m3"),
    SettingSpec("RAGLEX_RERANK_URL", "Reranker server URL", False, "Embeddings",
                "TEI /rerank endpoint; blank = same as embedding server"),
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
    SettingSpec("RAGLEX_NZ_API_KEY", "NZ Legislation API key", True, "Sources",
                "PCO Developer API — apply at legislation.govt.nz; the NZ site is "
                "bot-walled so there is no scraping fallback without this"),
    SettingSpec("RAGLEX_COURTLISTENER_TOKEN", "CourtListener API token", True, "Sources",
                "free at courtlistener.com/profile/api-token/ — v4 rejects anonymous "
                "requests, so US case law needs this. One account per project: Free Law "
                "Project forbids extra accounts to work around the limits"),
    # The free tier's three concurrent rolling windows. Exposed (rather than hard-coded)
    # only so an operator with a Free Law Project membership or commercial partnership
    # can raise them — leave blank for the free numbers. Raising them beyond what the
    # account actually has just converts refusals into 429s.
    SettingSpec("RAGLEX_COURTLISTENER_PER_MINUTE", "CourtListener requests/minute", False,
                "Sources", "blank = 5 (free tier); an academic/commercial membership is "
                "typically 20. Also sets the pacing floor — 5/min paces at 12s, 20/min at 3s"),
    SettingSpec("RAGLEX_COURTLISTENER_PER_HOUR", "CourtListener requests/hour", False,
                "Sources", "blank = 50 (free tier); a membership is typically 1000"),
    SettingSpec("RAGLEX_COURTLISTENER_PER_DAY", "CourtListener requests/day", False,
                "Sources", "blank = 125 (free tier). 0/none = no daily cap, for a "
                "membership that only limits per-minute and per-hour. A case costs one "
                "request per opinion, so 125/day is roughly 40-100 cases"),
    SettingSpec("RAGLEX_COURTLISTENER_QUEUE_RESERVE", "CourtListener queue share", False,
                "Sources", "0.6 — the fraction of the daily quota the unattended "
                "US-citation queue may spend, leaving the rest for on-demand lookups"),
    SettingSpec("RAGLEX_CANLII_API_KEY", "CanLII API key", True, "Sources",
                "granted individually — apply via canlii.org/en/feedback/feedback.html. "
                "Metadata + citator only (never full text): resolves Canadian citations "
                "into stubs with verified CanLII links and enriches held decisions"),
    # Conservative defaults deliberately below CanLII's documented ceiling (~5,000/day);
    # exposed only so a key granted more isn't held to them.
    SettingSpec("RAGLEX_CANLII_PER_MINUTE", "CanLII requests/minute", False, "Sources",
                "blank = 20 (paces requests 3s apart). Also sets the pacing floor"),
    SettingSpec("RAGLEX_CANLII_PER_HOUR", "CanLII requests/hour", False, "Sources",
                "blank = 900"),
    SettingSpec("RAGLEX_CANLII_PER_DAY", "CanLII requests/day", False, "Sources",
                "blank = 4000 — below the documented ~5,000/day ceiling. A targeted case "
                "costs 1 request, +2-3 with the citator"),
    SettingSpec("EURLEX_USERNAME", "EUR-Lex webservice user", False, "Sources"),
    SettingSpec("EURLEX_PASSWORD", "EUR-Lex webservice password", True, "Sources"),
    # PISTE (piste.gouv.fr) OAuth2 client-credentials — one app can subscribe to
    # BOTH Judilibre (fr-judilibre) and Légifrance (fr-legislation), so the two
    # French adapters share these. Register a free app at piste.gouv.fr, subscribe
    # it to the Judilibre + Légifrance APIs, and paste the app's client id/secret.
    SettingSpec("PISTE_CLIENT_ID", "PISTE client id (Légifrance OAuth2)", True, "Sources",
                "OAuth2 client-credentials; free after registration at piste.gouv.fr"),
    SettingSpec("PISTE_CLIENT_SECRET", "PISTE client secret", True, "Sources"),
    # Judilibre authenticates with a static API key sent in the `KeyId` header (NOT
    # OAuth) — the app's API key from the PISTE Authentification tab. Falls back to
    # PISTE_CLIENT_ID when blank (on PISTE the client id doubles as the KeyId).
    SettingSpec("PISTE_KEY_ID", "PISTE KeyId (Judilibre)", True, "Sources",
                "Judilibre API key sent as the KeyId header; blank = reuse PISTE_CLIENT_ID"),
    # Point the French adapters at the PISTE sandbox instead of production (the FR
    # services are evolving; sandbox lets you verify shapes without touching prod quota).
    SettingSpec("PISTE_SANDBOX", "PISTE sandbox mode", False, "Sources", "1 = sandbox-*.piste.gouv.fr"),
    SettingSpec("RAGLEX_PROXY", "Outbound proxy (all traffic)", True, "Network",
                "socks5://user:pass@host:1080 | http://host:8080"),
    SettingSpec("RAGLEX_SCRAPER", "Scraper engine", False, "Network",
                "httpx | stealth (Camoufox) | scrapling-mcp | playwright"),
    SettingSpec("RAGLEX_SCRAPLING_MCP_URL", "Scrapling MCP URL", False, "Network",
                "http://scrapling-mcp:8000/mcp — a shared stealth-fetch service; blank = in-process Camoufox"),
    SettingSpec("RAGLEX_SCRAPLING_MCP_KEY", "Scrapling MCP key", True, "Network"),
    SettingSpec("RAGLEX_AUTOHARVEST", "Auto-drain worklist (refs/tick)", False, "Network",
                "0 = off; e.g. 25 — the scheduler slowly fetches routable citations each tick"),
    SettingSpec("RAGLEX_AUTOEMBED", "Auto-index backlog (docs/tick)", False, "Embeddings",
                "0 = off; e.g. 500 — the scheduler indexes that many un-embedded documents "
                "each tick, building the keyword (FTS) + vector index search reads. Resumable: "
                "changing the embedding model re-queues the whole corpus as a new family"),
    SettingSpec("RAGLEX_MISS_TTL_DAYS", "Absent-reference cooldown (days)", False, "Network",
                "90 — days to skip a reference the source said does not exist (404). Only genuine absences land here"),
    SettingSpec("RAGLEX_RETRY_TTL_HOURS", "Unreachable-reference cooldown (hours)", False, "Network",
                "6 — hours to skip a reference we merely couldn't fetch (timeout, 5xx). Short: the document probably exists"),
    SettingSpec("RAGLEX_API_TOKEN", "API bearer token", True, "Network",
                "blank = open. Set it and the REST API + MCP endpoint both require it"),
    SettingSpec("RAGLEX_ALERT_WEBHOOK", "Alert webhook URL", True, "Network",
                "ntfy.sh/your-topic — pushes source-failure and drain-stalled alerts"),
    SettingSpec("RAGLEX_MAX_CONCURRENT_JOBS", "Max concurrent jobs", False, "Jobs",
                "how many jobs run at once; extras queue and start as slots free (default 6). "
                "Lower it on a busy box so heavy imports don't starve interactive queries"),
    SettingSpec("RAGLEX_SCHEDULER_PAUSED", "Pause scheduled jobs", False, "Jobs",
                "1 = pause the scheduler's recurring jobs and due watches (manual and queued "
                "jobs still run); blank/0 = normal"),
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
