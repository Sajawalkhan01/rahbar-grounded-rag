"""The semantic cache (section 3.3), in Redis. Why Redis: docs/adr/0003-cache-store-redis.md.

A hit is a stored question whose embedding has cosine similarity >= CACHE_THRESHOLD with the
new one, so "what is the attendance rule?" can be served from "what is the attendance
requirement?". The threshold comes from the ConfigMap; docs/PIPELINE.md shows how it was chosen.

Layout in Redis, per index version:
    rahbar:cache:<index_id>:ids        a list of entry ids (newest last, capped at MAX_ENTRIES)
    rahbar:cache:<index_id>:<entry>    a hash: question, embedding (float32 bytes), payload (JSON)

INVALIDATION: the indexer writes a new `index_id` into the Chroma collection on every run, and
every key above contains it. After a re-index the API asks for the NEW id, finds nothing, and
the old entries simply expire (TTL). The indexer never needs access to Redis, which the
NetworkPolicy would forbid anyway.

The cache is an optimisation: any Redis error is logged and treated as a miss.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger("rahbar.cache")
MAX_ENTRIES = 500


@dataclass
class CacheHit:
    payload: dict[str, Any]
    similarity: float
    question: str


def connect(host: str, port: int) -> Any:
    import redis

    return redis.Redis(host=host, port=port, socket_timeout=2.0, socket_connect_timeout=2.0)


def _unit(vec: Any) -> Any:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


class SemanticCache:
    def __init__(self, redis_client: Any, threshold: float, ttl_seconds: int) -> None:
        self.redis = redis_client
        self.threshold = threshold
        self.ttl = ttl_seconds

    def ping(self) -> str:
        try:
            self.redis.ping()
            return "ok"
        except Exception as exc:
            return f"redis unreachable: {exc!r}"

    @staticmethod
    def _ids_key(index_id: str) -> str:
        return f"rahbar:cache:{index_id}:ids"

    def nearest(self, index_id: str, embedding: list[float]) -> CacheHit | None:
        """The most similar stored question, whether or not it clears the threshold.
        The threshold experiment (eval/cache_threshold.py) uses this to see near misses."""
        try:
            ids = [i.decode() for i in self.redis.lrange(self._ids_key(index_id), 0, -1)]
            if not ids:
                return None
            pipe = self.redis.pipeline()
            for entry in ids:
                pipe.hget(f"rahbar:cache:{index_id}:{entry}", "embedding")
            blobs = pipe.execute()
            query = _unit(np.asarray(embedding, dtype=np.float32))
            best_i, best_sim = -1, -2.0
            for i, blob in enumerate(blobs):
                if not blob:
                    continue          # the entry expired but its id is still in the list
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape != query.shape:
                    continue
                sim = float(vec @ query)
                if sim > best_sim:
                    best_i, best_sim = i, sim
            if best_i < 0:
                return None
            row = self.redis.hmget(f"rahbar:cache:{index_id}:{ids[best_i]}", "payload", "question")
            if not row[0]:
                return None
            return CacheHit(json.loads(row[0]), best_sim, (row[1] or b"").decode())
        except Exception as exc:
            log.warning("cache lookup failed", extra={"error": repr(exc)})
            return None

    def lookup(self, index_id: str, embedding: list[float]) -> CacheHit | None:
        """A hit: the nearest stored question, if its similarity is at or above the threshold."""
        hit = self.nearest(index_id, embedding)
        if hit is None or hit.similarity < self.threshold:
            return None
        return hit

    def store(self, index_id: str, question: str, embedding: list[float],
              payload: dict[str, Any]) -> None:
        try:
            entry = uuid.uuid4().hex
            key = f"rahbar:cache:{index_id}:{entry}"
            ids_key = self._ids_key(index_id)
            vec = _unit(np.asarray(embedding, dtype=np.float32))
            pipe = self.redis.pipeline()
            pipe.hset(key, mapping={"question": question, "embedding": vec.tobytes(),
                                    "payload": json.dumps(payload)})
            pipe.expire(key, self.ttl)
            pipe.rpush(ids_key, entry)
            pipe.ltrim(ids_key, -MAX_ENTRIES, -1)
            pipe.expire(ids_key, self.ttl)
            pipe.execute()
        except Exception as exc:
            log.warning("cache store failed", extra={"error": repr(exc)})
