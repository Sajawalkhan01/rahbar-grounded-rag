"""Token usage side-channel. A provider records how many tokens the last call used; the
answer service reads it in the same request (a ContextVar is per request thread), so the
LLMProvider interface itself stays `generate(question, passages) -> Answer`."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


_usage: ContextVar[Usage] = ContextVar("llm_usage", default=Usage())


def record_usage(input_tokens: int, output_tokens: int) -> None:
    current = _usage.get()
    _usage.set(Usage(current.input_tokens + input_tokens, current.output_tokens + output_tokens))


def take_usage() -> Usage:
    """Return and reset the usage recorded since the last take."""
    value = _usage.get()
    _usage.set(Usage())
    return value
