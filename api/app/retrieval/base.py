"""The retriever interface. The rest of the API only ever sees this."""

from __future__ import annotations

from typing import Protocol

from ..schemas import Passage


class Retriever(Protocol):
    name: str

    def retrieve(self, question: str, k: int) -> list[Passage]:
        """Return up to k passages, best first."""
        ...

    def embed_query(self, text: str) -> list[float] | None:
        """The question's embedding, for the semantic cache; None if there is no embedder."""
        ...

    def index_id(self) -> str:
        """Identifies the current version of the index; the cache is keyed by it."""
        ...
