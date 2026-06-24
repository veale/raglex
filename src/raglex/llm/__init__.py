"""Shared LLM seam — one resilient, batched, OpenAI-shaped client (§5).

The treatment classifier and the narrative citation extractor are both *optional*
LLM passes layered on the deterministic grammars; they share this client so there
is one place to configure the model and one place that degrades gracefully.
"""

from __future__ import annotations

from .client import LLMClient, LLMConfig

_DEFAULT: LLMClient | None = None


def get_llm_client(config: LLMConfig | None = None) -> LLMClient:
    """Process-wide default client (config from env/settings), or a custom one."""
    global _DEFAULT
    if config is not None:
        return LLMClient(config)
    if _DEFAULT is None:
        _DEFAULT = LLMClient(LLMConfig.from_env())
    return _DEFAULT


__all__ = ["LLMClient", "LLMConfig", "get_llm_client"]
