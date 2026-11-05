"""Runtime configuration, read once from environment variables.

On the cluster these variables come from your ConfigMap and Secret (section 4.1).
Nothing here is baked into the image, so one image runs in every environment.
Add your own keys (relevance floor, cache settings, ...) the same way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _clean_secret(value: str) -> str:
    """Keep printable ASCII only. A key pasted into a terminal can carry an invisible control
    character (the end-of-paste escape, 0x1b), which a provider rejects as a malformed header."""
    return "".join(c for c in value if 32 < ord(c) < 127)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Which implementations to load. "stub" needs no cluster, no model, no network.
    retriever: str = "stub"          # RETRIEVER: stub | chroma
    llm_provider: str = "stub"       # LLM_PROVIDER: stub | ollama-host | ollama-incluster | hosted

    # The embedding pin (section 4.4). The indexer reads the same ConfigMap key.
    embed_model: str = "nomic-embed-text"

    # Vector store
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    collection: str = "rahbar"

    # Retrieval and generation
    top_k: int = 5
    temperature: float = 0.2
    llm_model: str = "llama3.2:3b"
    ollama_url: str = "http://host.minikube.internal:11434"
    hosted_base_url: str = ""        # e.g. an OpenAI-compatible endpoint
    llm_api_key: str = field(default="", repr=False)   # from the Secret; never log it

    log_level: str = "INFO"

    # Retrieval (sections 3.2 and 3.4)
    relevance_floor: float = 0.70    # below this best similarity, the corpus does not cover it
    candidate_pool: int = 20         # how many hits each of dense and BM25 contribute before fusion
    rrf_k: int = 60                  # reciprocal rank fusion constant
    hybrid: bool = True              # HYBRID: false gives dense-only retrieval (for experiments)
    filter_superseded: bool = True   # SUPERSEDED_FILTER: false only to reproduce failure mode E

    # Semantic cache (section 3.3)
    cache_enabled: bool = True
    cache_threshold: float = 0.94    # cosine similarity at or above this is a hit
    cache_ttl_seconds: int = 86400
    redis_host: str = "redis"
    redis_port: int = 6379

    # Generation
    llm_timeout_seconds: float = 90.0
    session_ttl_seconds: int = 3600  # how long a conversation's turns are kept


def get_settings() -> Settings:
    return Settings(
        retriever=os.environ.get("RETRIEVER", "stub"),
        llm_provider=os.environ.get("LLM_PROVIDER", "stub"),
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=os.environ.get("CHROMA_HOST", "chroma"),
        chroma_port=_int("CHROMA_PORT", 8000),
        collection=os.environ.get("CHROMA_COLLECTION", "rahbar"),
        top_k=_int("TOP_K", 5),
        temperature=_float("TEMPERATURE", 0.2),
        llm_model=os.environ.get("LLM_MODEL", "llama3.2:3b"),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.minikube.internal:11434"),
        hosted_base_url=os.environ.get("HOSTED_BASE_URL", ""),
        llm_api_key=_clean_secret(os.environ.get("LLM_API_KEY", "")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        relevance_floor=_float("RELEVANCE_FLOOR", 0.70),
        candidate_pool=_int("CANDIDATE_POOL", 20),
        rrf_k=_int("RRF_K", 60),
        hybrid=_bool("HYBRID", True),
        filter_superseded=_bool("SUPERSEDED_FILTER", True),
        cache_enabled=_bool("CACHE_ENABLED", True),
        cache_threshold=_float("CACHE_THRESHOLD", 0.94),
        cache_ttl_seconds=_int("CACHE_TTL_SECONDS", 86400),
        redis_host=os.environ.get("REDIS_HOST", "redis"),
        redis_port=_int("REDIS_PORT", 6379),
        llm_timeout_seconds=_float("LLM_TIMEOUT_SECONDS", 90.0),
        session_ttl_seconds=_int("SESSION_TTL_SECONDS", 3600),
    )
