"""Remote embedding + reranking over an **MCP sidecar** (§6c/§6d).

The models RagLex wants — a multilingual legal embedder (BGE-M3, voyage-law-2) and a
cross-encoder reranker (bge-reranker-v2-m3, Cohere rerank) — are heavy, GPU-shaped, and
change far more often than the corpus does. Baking them into the API image would mean a
multi-gigabyte deploy every time you A/B a model, and would put a torch import in the
path of a process whose job is to serve JSON.

So they live in a **separate MCP server** (`raglex-ml`), and this module is the client.
Two reasons MCP rather than a bare REST sidecar:

- the same server is directly usable by an agent (Claude et al.) for ad-hoc embedding or
  reranking, without going through RagLex at all;
- the transport, schema, and error semantics are already specified, so a third-party
  embedding server that speaks MCP drops in with no adapter.

The sidecar exposes three tools::

    embed(texts: list[str], input_type: str | None) -> {"vectors": [[float]], "model": str,
                                                        "model_version": str, "dimensions": int}
    rerank(query: str, passages: list[str])          -> {"scores": [float]}
    health()                                          -> {"ok": bool, "model": str, ...}

Nothing here imports a model. If the sidecar is unreachable the provider raises (so the
embed stage stops and retries later rather than writing zero vectors), while the reranker
degrades to RRF order — a reranker is a precision *improvement*, and a search that returns
slightly worse ordering beats a search that returns an error.
"""

from __future__ import annotations

import json
import logging
import os

from .provider import _FamilyMixin

log = logging.getLogger("raglex.embeddings.remote")

DEFAULT_URL = "http://raglex-ml:9000/mcp"


class MCPToolClient:
    """Minimal streamable-HTTP MCP tool caller.

    Deliberately not the full MCP client library: this is a synchronous
    ``call_tool(name, args) -> dict`` over one endpoint, called from the embed stage and
    the search path, and pulling in an async client would mean an event loop inside
    FastAPI's thread pool.
    """

    def __init__(self, url: str, *, timeout: float = 120.0, token: str | None = None) -> None:
        self.url = url.rstrip("/") + "/"
        self.timeout = timeout
        self.token = token or os.environ.get("RAGLEX_ML_TOKEN")
        self._session_id: str | None = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    @staticmethod
    def _parse(resp) -> dict:
        """A streamable-HTTP MCP reply is either JSON or an SSE frame carrying JSON."""
        body = resp.text
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise RuntimeError("no data frame in MCP SSE response")
        return json.loads(body)

    def _initialise(self, client) -> None:
        resp = client.post(self.url, headers=self._headers(), json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "raglex", "version": "0.1.0"}},
        })
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id") or self._session_id
        self._parse(resp)
        client.post(self.url, headers=self._headers(),
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict) -> dict:
        import httpx

        # follow_redirects: some MCP servers 307 between /mcp and /mcp/ (a 307 preserves the
        # POST method + body, so following it is correct); without this the handshake dies.
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            if self._session_id is None:
                self._initialise(client)
            resp = client.post(self.url, headers=self._headers(), json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            resp.raise_for_status()
            payload = self._parse(resp)
        if "error" in payload:
            raise RuntimeError(f"{name}: {payload['error']}")
        result = payload.get("result", {})
        if result.get("structuredContent"):
            return result["structuredContent"]
        for block in result.get("content", []):
            if block.get("type") == "text":
                return json.loads(block["text"])
        raise RuntimeError(f"{name}: unreadable MCP result")


class MCPEmbeddingProvider(_FamilyMixin):
    """An embedding provider backed by the ML sidecar (§6d).

    The family (provider, model, model_version, dimensions) must be stable and known
    *before* the first embed call, because it keys every stored vector and decides which
    vectors are comparable. So it is configuration here, and ``health()`` verifies that
    the sidecar agrees — a silent model swap behind the same family name would corrupt
    the index by mixing incomparable vectors under one key.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        model: str = "bge-m3",
        model_version: str = "1",
        dimensions: int = 1024,
        max_input_tokens: int = 8192,
        batch_size: int = 32,
    ) -> None:
        self.name = "mcp"
        self.model = model
        self.model_version = model_version
        self.dimensions = dimensions
        self.max_input_tokens = max_input_tokens
        self.batch_size = batch_size
        self.client = MCPToolClient(url or os.environ.get("RAGLEX_ML_URL") or DEFAULT_URL)

    def embed(self, texts: list[str], *, input_type: str | None = None) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i: i + self.batch_size]
            res = self.client.call_tool(
                "embed", {"texts": chunk, "input_type": input_type, "model": self.model})
            vectors = res["vectors"]
            if len(vectors) != len(chunk):
                raise RuntimeError(f"embed returned {len(vectors)} vectors for {len(chunk)} inputs")
            dims = res.get("dimensions") or (len(vectors[0]) if vectors else self.dimensions)
            if dims != self.dimensions:
                # Storing these would silently mix incomparable vectors into one family.
                raise RuntimeError(
                    f"sidecar returned {dims}-dim vectors but this family is "
                    f"{self.dimensions}-dim — configure RAGLEX_EMBED_DIMENSIONS to match, "
                    f"which starts a NEW family and re-embeds")
            out.extend(vectors)
        return out

    def health(self) -> bool:
        try:
            res = self.client.call_tool("health", {})
        except Exception as exc:  # noqa: BLE001 — health is a probe, not a raise
            log.warning("ML sidecar unreachable at %s: %s", self.client.url, exc)
            return False
        return bool(res.get("ok", True))


class MCPReranker:
    """Cross-encoder reranking over the sidecar (§6c) — the precision stage.

    Degrades to the fused RRF order if the sidecar is unreachable: a reranker improves
    ordering, so its absence must cost quality, never availability.
    """

    def __init__(self, *, url: str | None = None, model: str | None = None,
                 top_n: int = 50) -> None:
        self.client = MCPToolClient(url or os.environ.get("RAGLEX_ML_URL") or DEFAULT_URL)
        self.model = model or os.environ.get("RAGLEX_RERANK_MODEL") or "bge-reranker-v2-m3"
        # Cross-encoders are quadratic in the wrong places: score the head of the fused
        # list, keep the tail in its RRF order beneath it.
        self.top_n = top_n

    def rerank(self, query: str, candidates: list) -> list:
        if not candidates:
            return candidates
        head, tail = candidates[: self.top_n], candidates[self.top_n:]
        try:
            res = self.client.call_tool("rerank", {
                "query": query, "passages": [c.chunk_text for c in head], "model": self.model})
            scores = res["scores"]
            if len(scores) != len(head):
                raise RuntimeError("rerank returned the wrong number of scores")
        except Exception as exc:  # noqa: BLE001
            log.warning("rerank unavailable (%s); keeping RRF order", exc)
            return candidates
        ranked = sorted(zip(head, scores), key=lambda p: p[1], reverse=True)
        return [_rescored(c, s) for c, s in ranked] + tail


def _rescored(cand, score: float):
    from ..retrieval.hybrid import Candidate

    return Candidate(
        doc_id=cand.doc_id, chunk_id=cand.chunk_id, score=float(score),
        chunk_text=cand.chunk_text, structural_unit=cand.structural_unit,
        char_start=cand.char_start, char_end=cand.char_end,
    )
