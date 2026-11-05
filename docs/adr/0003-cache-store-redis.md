# ADR 0003: Redis for the semantic cache

## Status
Accepted · 4 Nov 2026

## Context
The handout allows the semantic cache to live in Redis or in a second Chroma collection. The
cache must: compare a new question with earlier ones by cosine similarity against a
configurable threshold; never store follow-ups, abstentions or invalid answers; expire after a
re-index; survive a pod restart; and be shared by all three API replicas so a question asked
on one replica can be answered on another.

## Decision
Use **Redis** (`redis:7.4.2-alpine`, a Deployment with a PVC and append-only persistence,
`k8s/base/redis.yaml`). Each entry is a hash holding the question, its unit-length embedding as
float32 bytes and the JSON response. The API loads the stored vectors for the current index and
takes the best cosine similarity with numpy (`api/app/services/cache.py`).

* **Threshold** comes from the ConfigMap (`CACHE_THRESHOLD`); how it was chosen is in
  `docs/PIPELINE.md`.
* **Invalidation on re-index**: every key contains the collection's `index_id`, which the
  indexer rewrites on each run. After a re-index the API asks for the new id, finds nothing,
  and the old entries expire by TTL. The indexer never needs Redis access, which the
  NetworkPolicy forbids anyway (only the API may reach Redis).
* **Never cached**: follow-ups, abstentions (below the relevance floor or declined by the model),
  fallbacks and anything whose citations did not validate (`api/app/services/answer.py`).
* Redis also holds the conversation turns used to recognise follow-ups.
* A Redis failure is treated as a cache miss, never as an error to the user; `/readyz` does
  report it, because the handout makes Redis part of readiness.

## Options considered
* **A second Chroma collection**: one less component, and similarity search is native. But the
  cache would live on the same volume and server as the index, writes from three API replicas
  would break the "only the indexer writes to Chroma" rule and its NetworkPolicy, and TTLs and
  per-index invalidation would have to be built by hand.
* **In-process dictionary per replica**: simplest, but not shared (a 3-replica deployment
  would have three cold caches), and lost on every restart.
* **Redis with the vector-search module (Redis Stack)**: server-side similarity, but a larger,
  less common image for a cache of at most a few hundred entries.

## Consequences
* Brute-force similarity over at most 500 entries per index is microseconds; it would need a
  real vector index if the cache grew to tens of thousands of entries.
* One more stateful component, with its own PVC and `Recreate` strategy (a ReadWriteOnce
  volume cannot be mounted by two pods during a rollout).
* A near-duplicate but different question (for example a different year) could be a false
  hit; the threshold experiment in `docs/PIPELINE.md` exists to prevent that.
