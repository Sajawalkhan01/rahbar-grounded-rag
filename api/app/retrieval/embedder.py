"""Embeds the user's question with the SAME model the indexer used (section 3.2, step 3).

The model name comes from EMBED_MODEL, the ConfigMap key the indexer reads too. The pin
check (pin.py) compares it with what the index recorded, so the two cannot drift silently.
"""

from __future__ import annotations

import time
from typing import Protocol

import httpx

# nomic-embed-text expects this prefix on questions; the indexer prefixes documents with
# "search_document: " (indexer/pipeline/embed.py). Both halves are needed for good recall.
QUERY_PREFIX = "search_query: "


class Embedder(Protocol):
    model: str

    def embed_query(self, text: str) -> list[float]: ...


class EmbeddingError(RuntimeError):
    """The embedding service could not be reached or answered badly."""


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 30.0) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/api/embed"
        self._timeout = timeout

    def embed_query(self, text: str) -> list[float]:
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                response = httpx.post(
                    self._url,
                    json={"model": self.model, "input": QUERY_PREFIX + text},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                vector = response.json()["embeddings"][0]
                return [float(x) for x in vector]
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last = exc
                if attempt == 1:
                    time.sleep(0.5)
        raise EmbeddingError(f"embedding with {self.model!r} failed: {last!r}")
