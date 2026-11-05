"""Test doubles. Nothing here touches the network, a model or a real Chroma server.

FakeEmbedder   a hashed bag-of-words vector, so questions that share words are "similar"
FakeChroma     the few Chroma client calls the API makes, with cosine search and a where filter
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import numpy as np

from app.config import Settings
from app.schemas import Answer, Citation, Passage

DIM = 64


def vector_for(text: str, dim: int = DIM) -> list[float]:
    vec = np.zeros(dim, dtype=np.float64)
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = float(np.linalg.norm(vec)) or 1.0
    return (vec / norm).tolist()


class FakeEmbedder:
    def __init__(self, model: str = "fake-embed", dim: int = DIM) -> None:
        self.model, self.dim, self.calls = model, dim, 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return vector_for(text, self.dim)


class FakeCollection:
    def __init__(self, name: str, metadata: dict[str, Any]) -> None:
        self.name = name
        self.metadata = metadata
        self.rows: dict[str, tuple[str, list[float], dict[str, Any]]] = {}

    def add_chunk(self, chunk_id: str, text: str, meta: dict[str, Any], dim: int = DIM) -> None:
        self.rows[chunk_id] = (text, vector_for(text, dim), meta)

    def count(self) -> int:
        return len(self.rows)

    def get(self, ids: list[str] | None = None, limit: int | None = None, offset: int = 0,
            include: list[str] | None = None) -> dict[str, Any]:
        keys = ids if ids is not None else list(self.rows)
        keys = keys[offset : offset + limit] if limit is not None else keys
        out: dict[str, Any] = {"ids": keys}
        if "documents" in (include or []):
            out["documents"] = [self.rows[k][0] for k in keys]
        if "metadatas" in (include or []):
            out["metadatas"] = [self.rows[k][2] for k in keys]
        if "embeddings" in (include or []):
            out["embeddings"] = [self.rows[k][1] for k in keys]
        return out

    def query(self, query_embeddings: list[list[float]], n_results: int,
              where: dict[str, Any] | None = None, include: list[str] | None = None) -> dict[str, Any]:
        q = np.asarray(query_embeddings[0])
        scored = []
        for chunk_id, (_text, vec, meta) in self.rows.items():
            if where and any(meta.get(k) != v for k, v in where.items()):
                continue
            v = np.asarray(vec)
            if v.shape != q.shape:
                raise ValueError("dimension mismatch")
            cos = float(v @ q / ((np.linalg.norm(v) * np.linalg.norm(q)) or 1.0))
            scored.append((1.0 - cos, chunk_id))
        scored.sort()
        top = scored[:n_results]
        return {"ids": [[c for _, c in top]], "distances": [[d for d, _ in top]]}


class FakeChroma:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.down = False

    def heartbeat(self) -> int:
        if self.down:
            raise ConnectionError("chroma is down")
        return 1

    def make(self, name: str = "rahbar", **meta: Any) -> FakeCollection:
        meta.setdefault("embed_model", "fake-embed")
        meta.setdefault("dimension", DIM)
        meta.setdefault("index_id", "idx-1")
        self.collections[name] = FakeCollection(name, meta)
        return self.collections[name]

    def get_collection(self, name: str) -> FakeCollection:
        if self.down:
            raise ConnectionError("chroma is down")
        if name not in self.collections:
            raise ValueError(f"Collection {name} does not exist")
        return self.collections[name]


def meta(source: str, section: str, page: int = 1, version: str = "2026", superseded: bool = False
         ) -> dict[str, Any]:
    return {"source": source, "section": section, "page": page, "doc_version": version,
            "superseded": superseded}


def passage(n: int = 1, score: float = 0.9, text: str = "Attendance must be 80 percent.",
            superseded: bool = False) -> Passage:
    return Passage(chunk_id=f"c{n}", text=text, source=f"doc{n}.pdf", section=f"{n}. Section {n}",
                   page=n, score=score, doc_version="2026", superseded=superseded)


class FakeRetriever:
    """Returns fixed passages; counts calls. Has an embedder so the cache path is exercised."""

    name = "fake"

    def __init__(self, passages: list[Passage] | None = None, index: str = "idx-1") -> None:
        self.passages = passages if passages is not None else [passage(1), passage(2, 0.7)]
        self.index = index
        self.calls: list[str] = []
        self.embedder = FakeEmbedder()

    def retrieve(self, question: str, k: int) -> list[Passage]:
        self.calls.append(question)
        return [p.model_copy() for p in self.passages[:k]]

    def embed_query(self, text: str) -> list[float] | None:
        return self.embedder.embed_query(text)

    def index_id(self) -> str:
        return self.index


class ScriptedProvider:
    """Plays back a list of results: an Answer is returned, an Exception is raised."""

    name = "scripted"

    def __init__(self, *results: Answer | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        self.calls += 1
        result = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def good_answer(p: Passage | None = None, text: str = "Attendance must be at least 80 percent.") -> Answer:
    p = p or passage(1)
    return Answer(reasoning="r", answer=text,
                  citations=[Citation(source=p.source, section=p.section, page=p.page)],
                  answered_from_context=True, confidence="high")


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(retriever="chroma", llm_provider="stub", embed_model="fake-embed",
                                relevance_floor=0.5, cache_threshold=0.9, cache_enabled=True,
                                top_k=5, candidate_pool=10)
    base.update(overrides)
    return Settings(**base)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
