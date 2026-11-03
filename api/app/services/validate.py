"""Generate, validate, retry once, then fall back (section 3.4).

Structure can be guaranteed; truth cannot. The provider asks the model for JSON in the
Answer schema, then Pydantic checks it. If the reply is not valid, we ask once more, and if
it is still not valid, the user gets a safe canned answer instead of a 500.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from ..providers.llm.base import LLMProvider
from ..providers.llm.errors import ProviderError
from ..schemas import Answer, Citation, Passage
from .citations import validate_citations

log = logging.getLogger("rahbar.validate")

FALLBACK_TEXT = (
    "I could not produce a reliable answer from the documents just now. "
    "Please try rephrasing the question, or try again in a moment."
)
MAX_ATTEMPTS = 2   # the first try plus one retry


@dataclass
class Generation:
    answer: Answer
    attempts: int
    fell_back: bool = False
    error: str = ""
    citations: list[Citation] | None = None   # only citations that match a retrieved passage
    citations_removed: int = 0                # model citations that matched none


def fallback_answer() -> Answer:
    return Answer(
        reasoning="fallback: the model output could not be validated",
        answer=FALLBACK_TEXT,
        citations=[],
        answered_from_context=False,
        confidence="low",
    )


def generate_validated(
    provider: LLMProvider, question: str, passages: list[Passage]
) -> Generation:
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            answer = provider.generate(question, passages)
            # A provider may hand back an unchecked object; validate it again regardless.
            answer = Answer.model_validate(answer.model_dump())
            if answer.answered_from_context and not answer.answer.strip():
                raise ValueError("answered_from_context is true but the answer is empty")
            valid, removed = validate_citations(answer.citations, passages)
            # A grounded answer with no citation that matches the evidence is not grounded.
            if answer.answered_from_context and not valid:
                raise ValueError(
                    f"answer claims to be grounded but none of its {len(answer.citations)} "
                    "citations match a retrieved passage"
                )
            return Generation(answer, attempt, citations=valid, citations_removed=removed)
        except (ValidationError, ValueError, ProviderError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("model output rejected",
                        extra={"attempt": attempt, "error": last_error})
    return Generation(fallback_answer(), MAX_ATTEMPTS, fell_back=True, error=last_error,
                      citations=[])
