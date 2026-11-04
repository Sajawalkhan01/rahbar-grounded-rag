"""The real retriever: hybrid (dense + BM25) search over Chroma, read-only (sections 3.2, 4.4).

Per question:
  1. Embed the question with the pinned model (embedder.py).
  2. Decide whether older document versions are wanted (versions.py). By default the metadata
     filter `superseded == False` removes every chunk of an outdated document.
  3. Dense search: Chroma's own query() returns the nearest `candidate_pool` chunks (cosine
     distance), with the metadata filter passed as `where`.
  4. Keyword search: BM25 over the chunk text, restricted the same way. The text is read out
     of Chroma once per index version and kept in memory (no vectors are copied).
  5. Fuse the two rankings with reciprocal rank fusion and keep the top k.
  6. Report each passage's cosine SIMILARITY (1 - distance) as its score. The relevance floor
     in services/answer.py is applied to that number, so abstention does not depend on which
     half of the hybrid found the passage.

This module never writes to Chroma. The indexer Job is the only writer.

Limits (docs/PIPELINE.md): BM25 is built in each replica's memory from the chunk text, so a
re-index is picked up on the next snapshot refresh (`SNAPSHOT_TTL`), not instantly; and the
tokenizer is English-only, so Urdu text is matched by the dense half alone.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..config import Settings
from ..schemas import Passage
from .bm25 import BM25
from .embedder import Embedder, OllamaEmbedder
from .versions import wants_older_versions

log = logging.getLogger("rahbar.retrieval")

SNAPSHOT_TTL = 30.0   # seconds between checks of whether the index changed
PAGE_SIZE = 500
EMBED_LRU = 128


@dataclass
class _Snapshot:
    """Everything read from one version of the index (identified by `index_id`)."""

    index_id: str
    ids: list[str]
    texts: list[str]
    metas: list[dict[str, Any]]
    bm25: BM25
    superseded: list[bool]
    superseded_versions: set[str]
    current_versions: set[str]


class ChromaRetriever:
    name = "chroma"

    def __init__(
        self,
        settings: Settings,
        embedder: Embedder | None = None,
        client: Any = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self.embedder: Embedder = embedder or OllamaEmbedder(settings.ollama_url, settings.embed_model)
        self._snapshot: _Snapshot | None = None
        self._checked_at = 0.0
        self._lock = threading.Lock()
        self._embeddings: OrderedDict[str, list[float]] = OrderedDict()

    # ------------------------------------------------------------------ index state
    @property
    def client(self) -> Any:
        """The Chroma client, created on first use rather than at startup.

        chromadb.HttpClient connects as soon as it is built. Building it in __init__ made the
        API crash and restart whenever Chroma was down or still starting, instead of starting,
        reporting "not ready" on /readyz and waiting. Now an unreachable Chroma raises here,
        where readiness and the ask route handle it, and the next call tries again.
        """
        if self._client is None:
            import chromadb  # the thin client, from chromadb-client in requirements.txt
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.HttpClient(
                host=self.settings.chroma_host,
                port=self.settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client

    def collection(self) -> Any:
        return self.client.get_collection(self.settings.collection)

    def index_id(self) -> str:
        """The id the indexer wrote on its last run. The semantic cache is keyed by it."""
        meta = self.collection().metadata or {}
        return str(meta.get("index_id", ""))

    def _load(self, collection: Any, index_id: str) -> _Snapshot:
        ids: list[str] = []
        texts: list[str] = []
        metas: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = collection.get(
                limit=PAGE_SIZE, offset=offset, include=["documents", "metadatas"]
            )
            if not page["ids"]:
                break
            ids.extend(page["ids"])
            texts.extend(page["documents"])
            metas.extend(page["metadatas"])
            offset += len(page["ids"])
        superseded = [bool(m.get("superseded", False)) for m in metas]
        return _Snapshot(
            index_id=index_id,
            ids=ids,
            texts=texts,
            metas=metas,
            bm25=BM25(texts),
            superseded=superseded,
            superseded_versions={str(m.get("doc_version", "")) for m, s in zip(metas, superseded) if s},
            current_versions={str(m.get("doc_version", "")) for m, s in zip(metas, superseded) if not s},
        )

    def snapshot(self) -> _Snapshot:
        """The in-memory copy of the index, refreshed when the indexer has replaced it."""
        now = time.monotonic()
        with self._lock:
            if self._snapshot is not None and now - self._checked_at < SNAPSHOT_TTL:
                return self._snapshot
            collection = self.collection()
            index_id = str((collection.metadata or {}).get("index_id", ""))
            if self._snapshot is None or self._snapshot.index_id != index_id:
                self._snapshot = self._load(collection, index_id)
                log.info("index snapshot loaded",
                         extra={"index_id": index_id, "chunks": len(self._snapshot.ids)})
            self._checked_at = now
            return self._snapshot

    # ------------------------------------------------------------------- retrieval
    def embed_query(self, text: str) -> list[float] | None:
        """Embed a question once per process; the cache check and the search share the result."""
        with self._lock:
            if text in self._embeddings:
                self._embeddings.move_to_end(text)
                return self._embeddings[text]
        vector = self.embedder.embed_query(text)
        with self._lock:
            self._embeddings[text] = vector
            while len(self._embeddings) > EMBED_LRU:
                self._embeddings.popitem(last=False)
        return vector

    def retrieve(self, question: str, k: int) -> list[Passage]:
        snap = self.snapshot()
        include_old = not self.settings.filter_superseded or wants_older_versions(
            question, snap.superseded_versions, snap.current_versions
        )
        where = None if include_old else {"superseded": False}
        allowed = [True] * len(snap.ids) if include_old else [not s for s in snap.superseded]
        index = {chunk_id: i for i, chunk_id in enumerate(snap.ids)}

        query_vec = np.asarray(self.embed_query(question), dtype=np.float32)
        norm = float(np.linalg.norm(query_vec)) or 1.0
        query_vec = query_vec / norm

        pool = max(self.settings.candidate_pool, k)
        collection = self.collection()
        hits = collection.query(
            query_embeddings=[query_vec.tolist()], n_results=pool, where=where,
            include=["distances"],
        )
        dense_ids: list[str] = list(hits["ids"][0])
        similarity = {cid: 1.0 - float(d) for cid, d in zip(dense_ids, hits["distances"][0])}

        rankings: list[list[str]] = [dense_ids]
        if self.settings.hybrid:
            rankings.append([snap.ids[i] for i, _ in snap.bm25.top(question, pool, allowed)])

        fused: dict[str, float] = {}
        for ranking in rankings:
            for rank, chunk_id in enumerate(ranking, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (self.settings.rrf_k + rank)
        best = sorted(fused, key=lambda c: fused[c], reverse=True)[:k]

        # A passage only the keyword search found has no dense distance yet: read its stored
        # vector and compute the cosine, so every score means the same thing.
        missing = [c for c in best if c not in similarity]
        if missing:
            rows = collection.get(ids=missing, include=["embeddings"])
            for chunk_id, vec in zip(rows["ids"], rows["embeddings"]):
                v = np.asarray(vec, dtype=np.float32)
                if v.shape != query_vec.shape:
                    raise ValueError(
                        f"query vector has {query_vec.shape[0]} dimensions, the index has "
                        f"{v.shape[0]}: the embedding model does not match the index"
                    )
                similarity[chunk_id] = float(v @ query_vec / (np.linalg.norm(v) or 1.0))

        passages = []
        for chunk_id in best:
            i = index[chunk_id]
            meta = snap.metas[i]
            passages.append(Passage(
                chunk_id=chunk_id,
                text=snap.texts[i],
                source=str(meta.get("source", "")),
                section=str(meta.get("section", "")),
                page=_int_or_none(meta.get("page")),
                score=similarity.get(chunk_id, 0.0),
                doc_version=str(meta.get("doc_version", "")),
                superseded=snap.superseded[i],
            ))
        return passages


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
