"""A small, resilient, batched LLM client (§5 — the "run an LLM over the chunk"
seam made concrete).

Design goals, in priority order:

1. **Resilient to API change.** Everything that drifts between providers and
   across versions is *configuration*, not code: the base URL, the chat-completions
   path, the model slug, the auth header shape, extra headers, whether the endpoint
   honours ``response_format=json_object``, the temperature, timeouts, retries. The
   wire format we target is the OpenAI *chat completions* shape because it is the
   one near-universal contract — OpenRouter, OpenAI, Together, Groq, Mistral,
   vLLM, llama.cpp's server, LM Studio and Ollama (``/v1``) all speak it. A new
   provider is a config row (base URL + model + key env), never a code change.

2. **Never breaks the pipeline.** Extraction/treatment classification are
   *enrichment* passes layered on deterministic grammars. So every public method
   degrades gracefully: no key / unreachable host / malformed JSON / quota error →
   it returns ``None`` (or skips that item) and the caller falls back to the
   heuristic. The LLM is never load-bearing for correctness, only for recall.

3. **Batched.** Per §5 the LLM pass runs over *many* citations at once. ``json``
   takes one prompt; ``json_batch`` packs a list of items into a single request
   that asks the model to return a JSON array aligned to the inputs by index, and
   chunks large lists into ``batch_size`` requests. Far fewer round-trips than
   one-call-per-citation.

Robust JSON handling: we ask for ``response_format=json_object`` when the endpoint
supports it, but never *rely* on it — ``_loads`` strips code fences and pulls the
first balanced JSON value out of free text, so a chatty model still parses.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LLMConfig:
    """Every knob, all overridable from env / the settings store. The defaults
    target OpenRouter, but pointing ``base_url`` at any OpenAI-shaped endpoint
    (local or hosted) is the whole switch."""

    base_url: str = "https://openrouter.ai/api/v1"
    chat_path: str = "/chat/completions"  # some gateways differ; keep it data
    model: str = "anthropic/claude-3.5-haiku"
    api_key_env: str = "OPENROUTER_API_KEY"
    api_key: str | None = None  # explicit key wins over the env var
    auth_scheme: str = "Bearer"  # "Bearer" | "" (some local servers want no auth)
    extra_headers: dict[str, str] = field(default_factory=dict)
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout: float = 60.0
    max_retries: int = 3
    backoff: float = 1.5  # seconds, exponential
    use_json_mode: bool = True  # send response_format=json_object
    batch_size: int = 20  # items per batched request
    enabled: bool = True  # master off-switch (degrade to heuristics)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LLMConfig":
        e = env if env is not None else os.environ

        def _b(key: str, default: bool) -> bool:
            v = e.get(key)
            return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

        def _f(key: str, default: float) -> float:
            try:
                return float(e[key])
            except (KeyError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(e[key])
            except (KeyError, ValueError):
                return default

        base = cls()
        return cls(
            base_url=e.get("RAGLEX_LLM_BASE_URL", base.base_url).rstrip("/"),
            chat_path=e.get("RAGLEX_LLM_CHAT_PATH", base.chat_path),
            model=e.get("RAGLEX_LLM_MODEL", base.model),
            api_key_env=e.get("RAGLEX_LLM_API_KEY_ENV", base.api_key_env),
            api_key=e.get("RAGLEX_LLM_API_KEY") or None,
            auth_scheme=e.get("RAGLEX_LLM_AUTH_SCHEME", base.auth_scheme),
            temperature=_f("RAGLEX_LLM_TEMPERATURE", base.temperature),
            max_tokens=_i("RAGLEX_LLM_MAX_TOKENS", base.max_tokens),
            timeout=_f("RAGLEX_LLM_TIMEOUT", base.timeout),
            max_retries=_i("RAGLEX_LLM_MAX_RETRIES", base.max_retries),
            use_json_mode=_b("RAGLEX_LLM_JSON_MODE", base.use_json_mode),
            batch_size=_i("RAGLEX_LLM_BATCH_SIZE", base.batch_size),
            # The provider is *enabled* once a model endpoint is configured. We
            # treat presence of a key (or a non-default, e.g. local, base URL) as
            # the intent to use it; callers still tolerate it being unreachable.
            enabled=_b("RAGLEX_LLM_ENABLED",
                       bool(e.get(e.get("RAGLEX_LLM_API_KEY_ENV", base.api_key_env))
                            or e.get("RAGLEX_LLM_API_KEY")
                            or e.get("RAGLEX_LLM_BASE_URL"))),
        )


class LLMClient:
    """OpenAI-chat-shaped client. Construct once, reuse; cheap to make."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        self._warned = False

    # -- capability ---------------------------------------------------------
    def _key(self) -> str | None:
        return self.config.api_key or os.environ.get(self.config.api_key_env)

    def available(self) -> bool:
        """Cheap gate: configured + (has a key OR points at a keyless local
        endpoint). No network call — callers tolerate runtime unreachability."""
        if not self.config.enabled:
            return False
        if self._key():
            return True
        # a non-default base URL implies a local/keyless server (Ollama, vLLM…)
        return self.config.base_url not in ("https://openrouter.ai/api/v1",)

    # -- core call ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.config.extra_headers}
        key = self._key()
        if key and self.config.auth_scheme:
            h["Authorization"] = f"{self.config.auth_scheme} {key}".strip()
        elif key:
            h["Authorization"] = key
        return h

    def _post(self, messages: list[dict[str, str]]) -> str | None:
        """One chat round-trip with retry/backoff. Returns the message content, or
        ``None`` on any failure (never raises into the pipeline)."""
        if not self.config.enabled:
            return None
        try:
            import httpx
        except ImportError:
            self._warn("httpx not installed; LLM passes disabled")
            return None

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.use_json_mode:
            body["response_format"] = {"type": "json_object"}

        url = self.config.base_url.rstrip("/") + self.config.chat_path
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                resp = httpx.post(url, headers=self._headers(), json=body,
                                  timeout=self.config.timeout)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise _Retryable(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                # OpenAI shape; tolerate minor variants (some return `text`).
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                return msg.get("content") or choice.get("text")
            except _Retryable as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — enrichment must never crash
                # 4xx other than 429 won't get better on retry: stop.
                last_exc = exc
                if not _is_transient(exc):
                    break
            time.sleep(self.config.backoff * (2 ** attempt))
        self._warn(f"LLM call failed ({last_exc}); falling back to heuristics")
        return None

    # -- JSON helpers -------------------------------------------------------
    def json(self, system: str, user: str) -> Any | None:
        """Single structured call → parsed JSON (dict/list), or None."""
        content = self._post(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return _loads(content) if content else None

    def json_batch(
        self, system: str, items: list[str], *, instruction: str
    ) -> list[Any | None]:
        """Classify/extract over many inputs with as few requests as possible.

        Each request presents a numbered list of items and asks for a JSON object
        ``{"results": [{"index": i, ...}, ...]}`` so we can re-align by index even
        if the model drops or reorders entries. Returns one parsed element per
        input (``None`` where the model gave nothing). Always length-aligned."""
        out: list[Any | None] = [None] * len(items)
        if not items or not self.available():
            return out
        size = max(1, self.config.batch_size)
        for base in range(0, len(items), size):
            window = items[base : base + size]
            numbered = "\n".join(f"[{i}] {_clip(t)}" for i, t in enumerate(window))
            user = (
                f"{instruction}\n\n"
                f"Return ONLY a JSON object of the form "
                f'{{"results": [{{"index": <int>, ...}}]}} with one entry per item '
                f"(use the exact index shown).\n\nItems:\n{numbered}"
            )
            parsed = self.json(system, user)
            for entry in _results_list(parsed):
                idx = entry.get("index") if isinstance(entry, dict) else None
                if isinstance(idx, int) and 0 <= idx < len(window):
                    out[base + idx] = entry
        return out

    def _warn(self, msg: str) -> None:
        if not self._warned:  # one line per process, not per call
            import logging

            logging.getLogger("raglex.llm").warning(msg)
            self._warned = True


class _Retryable(Exception):
    pass


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or "connect" in name or isinstance(exc, _Retryable)


def _results_list(parsed: Any) -> list[Any]:
    if isinstance(parsed, dict):
        for key in ("results", "items", "data", "citations", "classifications"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    return []


def _clip(text: str, limit: int = 1200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " …"


def _loads(content: str) -> Any | None:
    """Parse JSON out of a model response, tolerantly: try as-is, then strip
    ```json fences, then pull the first balanced {...} or [...] out of prose."""
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    if content.startswith("```"):
        inner = content.strip("`")
        inner = inner[4:] if inner[:4].lower() == "json" else inner
        try:
            return json.loads(inner.strip())
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = content.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(content)):
            if content[i] == opener:
                depth += 1
            elif content[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(content[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None
