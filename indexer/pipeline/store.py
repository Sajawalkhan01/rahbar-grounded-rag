"""Stage 5: Store. The only code in the whole system that writes to Chroma.

Re-indexing while the API is serving (decision recorded in docs/adr/0001-indexer-as-job.md):

  * Same embedding model and vector size as the existing collection: UPSERT every new chunk
    (ids are stable: document + position), then delete the ids that no longer exist, then
    write fresh collection metadata. The collection is never empty and never missing, so
    readiness does not flap and no request sees "no collection".
  * A different model or vector size: the old vectors are meaningless next to the new ones,
    so the collection is deleted and rebuilt. The API's embedding-pin check keeps the pods
    out of the Service for that window.

Collection metadata carries the pin (`embed_model`, `dimension`) and `index_id`, a new value
on every run. The API keys its semantic cache by `index_id`, so a re-index invalidates every
cached answer without the indexer ever touching Redis.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .config import IndexerSettings
from .models import Chunk

WRITE_BATCH = 100
REQUIRED_METADATA = ("source", "section", "page", "doc_version", "superseded")


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


def validate(chunks: list[Chunk], vectors: list[list[float]]) -> int:
    """Refuse to write anything inconsistent. Returns the vector dimension.

    A half-written or mismatched index is worse than no index: the API would serve it.
    """
    if not chunks:
        raise ValueError("no chunks to store")
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    ids = [c.id for c in chunks]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})[:3]
        raise ValueError(f"duplicate chunk ids, e.g. {dupes}")
    dims = {len(v) for v in vectors}
    if len(dims) != 1 or 0 in dims:
        raise ValueError(f"vectors have inconsistent sizes {sorted(dims)}")
    for c in chunks:
        if not c.text.strip():
            raise ValueError(f"chunk {c.id} is empty")
        missing = [k for k in REQUIRED_METADATA if c.metadata().get(k) in (None, "")]
        if missing:
            raise ValueError(f"chunk {c.id} is missing metadata {missing}")
    return dims.pop()


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count."""
    dimension = validate(chunks, vectors)
    client = get_client(settings)
    metadata = {
        "hnsw:space": "cosine",
        "embed_model": settings.embed_model,
        "dimension": dimension,
        "index_id": uuid.uuid4().hex,
        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "chunks": len(chunks),
    }
    collection = _open_or_rebuild(client, settings, dimension)
    for start in range(0, len(chunks), WRITE_BATCH):
        part = slice(start, start + WRITE_BATCH)
        collection.upsert(
            ids=[c.id for c in chunks[part]],
            documents=[c.text for c in chunks[part]],
            embeddings=vectors[part],
            metadatas=[c.metadata() for c in chunks[part]],
        )
    _delete_stale(collection, {c.id for c in chunks})
    # hnsw:space is fixed at creation and cannot be modified, so leave it out here.
    collection.modify(metadata={k: v for k, v in metadata.items() if k != "hnsw:space"})
    count = int(collection.count())
    if count != len(chunks):
        raise RuntimeError(f"stored {count} chunks, expected {len(chunks)}")
    return count


def _open_or_rebuild(client: Any, settings: IndexerSettings, dimension: int) -> Any:
    try:
        existing = client.get_collection(settings.collection)
    except Exception:  # does not exist yet
        existing = None
    if existing is not None:
        meta = existing.metadata or {}
        if meta.get("embed_model") == settings.embed_model and meta.get("dimension") == dimension:
            return existing
        client.delete_collection(settings.collection)   # a different model: start over
    return client.create_collection(name=settings.collection, metadata={"hnsw:space": "cosine"})


def _delete_stale(collection: Any, keep: set[str]) -> None:
    stale = [i for i in collection.get(include=[])["ids"] if i not in keep]
    for start in range(0, len(stale), WRITE_BATCH):
        collection.delete(ids=stale[start : start + WRITE_BATCH])
