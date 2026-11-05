"""Prometheus metrics at /metrics (section 4.8).

Request count and latency are wired per route. The rest answer the questions an on-call
engineer asks when an answer is wrong or slow:

    rahbar_retrieval_seconds        how long embedding + search took (stage C)
    rahbar_generation_seconds       how long the model took (stage F)
    rahbar_cache_requests_total     hit / miss / bypass, and rahbar_cache_hit_ratio from them
    rahbar_abstentions_total        declined questions, by reason (floor = nothing relevant
                                    was retrieved, model = the model declined)
    rahbar_fallbacks_total          the model output failed validation twice
    rahbar_llm_tokens_total         tokens in and out
    rahbar_citations_removed_total  citations that matched no retrieved chunk and were dropped
    rahbar_top_score                similarity of the best retrieved passage
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
RETRIEVAL_SECONDS = Histogram(
    "rahbar_retrieval_seconds",
    "Embedding the question and searching the index",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
GENERATION_SECONDS = Histogram(
    "rahbar_generation_seconds",
    "Time spent in the LLM provider",
    ["provider"],
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 40, 90),
)
CACHE_REQUESTS = Counter(
    "rahbar_cache_requests_total", "Semantic cache lookups", ["result"]  # hit | miss | bypass
)
CACHE_HIT_RATIO = Gauge(
    "rahbar_cache_hit_ratio", "Cache hits / (hits + misses) in this process since it started"
)
ABSTENTIONS = Counter("rahbar_abstentions_total", "Questions the system declined", ["reason"])
FALLBACKS = Counter("rahbar_fallbacks_total", "Model output failed validation twice")
TOKENS = Counter("rahbar_llm_tokens_total", "LLM tokens", ["direction"])  # input | output
CITATIONS_REMOVED = Counter(
    "rahbar_citations_removed_total", "Model citations that matched no retrieved chunk"
)
TOP_SCORE = Histogram(
    "rahbar_top_score",
    "Similarity of the best retrieved passage",
    buckets=(0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0),
)

_hits = 0
_misses = 0


def record_cache(result: str) -> None:
    """Count a lookup and refresh the hit ratio. `bypass` (follow-ups, cache off) is counted
    but does not lower the ratio: it never asked the cache."""
    global _hits, _misses
    CACHE_REQUESTS.labels(result=result).inc()
    if result == "hit":
        _hits += 1
    elif result == "miss":
        _misses += 1
    if _hits + _misses:
        CACHE_HIT_RATIO.set(_hits / (_hits + _misses))


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
