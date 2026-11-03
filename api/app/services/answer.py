"""Turns a question into an AskResponse. This is where the query pipeline lives (section 3.2).

    1. validate the question                      (schemas.AskRequest, before we get here)
    2. cache check, skipped for follow-ups        (cache.py, followup.py)
    3. embed the question                         (retriever.embed_query, same model as the indexer)
    4. retrieve top-k, superseded docs filtered   (retrieval/chroma.py)
    5. relevance floor: nothing relevant -> abstain WITHOUT calling the model
    6. augment + generate a schema-constrained answer     (prompt.py, providers/)
    7. validate: parse, check citations, retry once, then fall back   (validate.py)
    8. store the turn, and a cache entry if it is safe to cache

Each step is its own module so each can be unit-tested; this function only wires them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.usage import take_usage
from ..retrieval.base import Retriever
from ..routes import metrics
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from .cache import SemanticCache
from .followup import is_followup
from .turns import TurnStore
from .validate import generate_validated

log = logging.getLogger("rahbar.answer")

ABSTAIN_TEXT = (
    "The documents in this corpus do not cover that question, so I cannot answer it. "
    "Try rephrasing it, or ask about the academic rules, fees, grading, the student "
    "handbook or the annual reports."
)


def _refs(passages: list[Passage]) -> list[RetrievedRef]:
    return [
        RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section, page=p.page,
                     score=round(p.score, 4), doc_version=p.doc_version, superseded=p.superseded)
        for p in passages
    ]


def _cache_payload(response: AskResponse) -> dict[str, Any]:
    return response.model_dump(exclude={"request_id", "cached"})


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
    cache: SemanticCache | None = None,
    turns: TurnStore | None = None,
    session_id: str | None = None,
) -> AskResponse:
    request_id = request_id_var.get()
    started = time.perf_counter()

    # --- follow-up detection -------------------------------------------------------------
    previous = turns.last_question(session_id) if turns else None
    followup = is_followup(question, previous is not None)
    # A follow-up leans on the earlier turn, so retrieval sees both questions.
    search_text = f"{previous} {question}" if (followup and previous) else question

    # --- semantic cache (never for a follow-up) -----------------------------------------
    index_id = ""
    embedding: list[float] | None = None
    if cache is not None and settings.cache_enabled and not followup:
        try:
            embedding = retriever.embed_query(question)
            index_id = retriever.index_id()
        except Exception as exc:
            log.warning("cache skipped: could not embed the question", extra={"error": repr(exc)})
        if embedding is not None and index_id:
            hit = cache.lookup(index_id, embedding)
            if hit is not None:
                metrics.record_cache("hit")
                response = AskResponse(**hit.payload, request_id=request_id, cached=True)
                _finish(turns, session_id, question, response, followup)
                log.info("answered", extra=_log_fields(
                    retriever, provider, settings, response, cache_result="hit",
                    similarity=round(hit.similarity, 4), followup=followup,
                    seconds=time.perf_counter() - started))
                return response
            metrics.record_cache("miss")
        else:
            metrics.record_cache("bypass")
    else:
        metrics.record_cache("bypass")

    # --- retrieval ----------------------------------------------------------------------
    t0 = time.perf_counter()
    passages = retriever.retrieve(search_text, k=settings.top_k)
    metrics.RETRIEVAL_SECONDS.observe(time.perf_counter() - t0)
    top_score = max((p.score for p in passages), default=0.0)
    metrics.TOP_SCORE.observe(max(top_score, 0.0))

    # --- honest abstention: nothing relevant retrieved -----------------------------------
    if not passages or top_score < settings.relevance_floor:
        metrics.ABSTENTIONS.labels(reason="floor").inc()
        response = AskResponse(
            request_id=request_id, answer=ABSTAIN_TEXT, citations=[],
            answered_from_context=False, confidence="high", retrieved=_refs(passages),
            provider=provider.name,
        )
        _finish(turns, session_id, question, response, followup)
        log.info("answered", extra=_log_fields(
            retriever, provider, settings, response, cache_result="miss" if embedding else "bypass",
            followup=followup, abstain_reason="below relevance floor",
            floor=settings.relevance_floor, top_score=round(top_score, 4),
            seconds=time.perf_counter() - started))
        return response

    # --- generation, validation, citation check -----------------------------------------
    t0 = time.perf_counter()
    generation = generate_validated(provider, question, passages)
    metrics.GENERATION_SECONDS.labels(provider=provider.name).observe(time.perf_counter() - t0)
    usage = take_usage()
    metrics.TOKENS.labels(direction="input").inc(usage.input_tokens)
    metrics.TOKENS.labels(direction="output").inc(usage.output_tokens)
    if generation.citations_removed:
        metrics.CITATIONS_REMOVED.inc(generation.citations_removed)
    if generation.fell_back:
        metrics.FALLBACKS.inc()

    answer: Answer = generation.answer
    citations: list[Citation] = generation.citations or []
    if not answer.answered_from_context:
        if not generation.fell_back:
            metrics.ABSTENTIONS.labels(reason="model").inc()
        citations = []

    response = AskResponse(
        request_id=request_id,
        answer=answer.answer.strip() or ABSTAIN_TEXT,
        citations=citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=_refs(passages),
        provider=provider.name,
    )

    # --- cache only what is safe to serve to everyone ------------------------------------
    cacheable = (
        cache is not None and embedding is not None and index_id
        and not followup
        and response.answered_from_context
        and not generation.fell_back
        and bool(citations)
    )
    if cacheable:
        assert cache is not None and embedding is not None
        cache.store(index_id, question, embedding, _cache_payload(response))

    _finish(turns, session_id, question, response, followup)
    log.info("answered", extra=_log_fields(
        retriever, provider, settings, response,
        cache_result="miss" if embedding is not None else "bypass", followup=followup,
        attempts=generation.attempts, fell_back=generation.fell_back,
        citations_removed=generation.citations_removed, cached_now=bool(cacheable),
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        top_score=round(top_score, 4), seconds=time.perf_counter() - started))
    return response


def _finish(
    turns: TurnStore | None, session_id: str | None, question: str, response: AskResponse,
    followup: bool,
) -> None:
    if turns is not None:
        turns.add(session_id, question, response.answered_from_context, response.cached)


def _log_fields(
    retriever: Retriever, provider: LLMProvider, settings: Settings, response: AskResponse,
    **extra: Any,
) -> dict[str, Any]:
    """The one line per request that makes a wrong answer diagnosable (section 4.8).
    Never includes the question text's secrets or any credential: only ids and numbers."""
    fields: dict[str, Any] = {
        "retriever": retriever.name,
        "provider": provider.name,
        "top_k": settings.top_k,
        "chunk_ids": [r.chunk_id for r in response.retrieved],
        "scores": [r.score for r in response.retrieved],
        "abstained": not response.answered_from_context,
        "answered_from_context": response.answered_from_context,
        "n_citations": len(response.citations),
    }
    if "seconds" in extra:
        extra["seconds"] = round(extra["seconds"], 3)
    fields.update(extra)
    return fields
