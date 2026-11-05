"""Stage 4: Embed. One vector per chunk, from the pinned model only.

The pin is EMBED_MODEL (from the ConfigMap, the same key the API reads). The name goes into
the Chroma collection metadata in store.py, and the API refuses to become ready if its own
EMBED_MODEL differs (api/app/retrieval/pin.py).

nomic-embed-text was trained with task prefixes: documents are embedded as
"search_document: <text>" and questions as "search_query: <text>". The API adds the
query prefix (api/app/retrieval/embedder.py); this file adds the document prefix. Leaving
either out still "works" but retrieves worse, and the failure is silent.
"""

from __future__ import annotations

import time

import httpx

from .config import IndexerSettings

BATCH_SIZE = 16
REQUEST_TIMEOUT = 300.0
ATTEMPTS = 4
DOCUMENT_PREFIX = "search_document: "


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else.

    With Ollama: POST {ollama_url}/api/embed with {"model": ..., "input": [texts]}
    returns {"embeddings": [[...], ...]}. Batch the calls; one request per chunk is slow.
    """
    if not texts:
        return []
    vectors: list[list[float]] = []
    url = f"{settings.ollama_url.rstrip('/')}/api/embed"
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = [DOCUMENT_PREFIX + t for t in texts[start : start + BATCH_SIZE]]
            vectors.extend(_embed_batch(client, url, settings.embed_model, batch))
    dims = {len(v) for v in vectors}
    if len(vectors) != len(texts) or len(dims) != 1 or 0 in dims:
        raise RuntimeError(
            f"embedding returned {len(vectors)} vectors of sizes {sorted(dims)} for {len(texts)} texts"
        )
    return vectors


def _embed_batch(
    client: httpx.Client, url: str, model: str, batch: list[str]
) -> list[list[float]]:
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client.post(url, json={"model": model, "input": batch})
            response.raise_for_status()
            embeddings = response.json()["embeddings"]
            if len(embeddings) != len(batch):
                raise RuntimeError(f"asked for {len(batch)} embeddings, got {len(embeddings)}")
            return [list(map(float, e)) for e in embeddings]
        except (httpx.HTTPError, KeyError, RuntimeError, ValueError) as exc:
            last = exc
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
    raise RuntimeError(f"embedding failed after {ATTEMPTS} attempts: {last!r}")
