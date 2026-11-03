"""POST /api/ask. The Ingress sends /api/* here unchanged, so the prefix stays."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..schemas import AskRequest, AskResponse
from ..services.answer import answer_question

log = logging.getLogger("rahbar.ask")
router = APIRouter(prefix="/api")


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request) -> AskResponse:
    state = request.app.state
    try:
        return answer_question(
            body.question, state.retriever, state.provider, state.settings,
            cache=state.cache, turns=state.turns, session_id=body.session_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # A dependency (Chroma, the embedding service) failed. That is a 503 the client can
        # retry, never a stack trace. Model output problems never reach here: they are
        # retried and then answered with a canned fallback (services/validate.py).
        log.exception("question failed", extra={"error": repr(exc)})
        raise HTTPException(status_code=503, detail="The answering service is temporarily unavailable.") from exc
