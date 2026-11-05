"""Ollama provider: ollama-host or ollama-incluster (section 3.5).

Both modes speak the same HTTP API; they differ only in OLLAMA_URL (the laptop at
host.minikube.internal:11434, or an in-cluster Service).

Output is constrained twice. `format` carries the Answer JSON schema, which makes Ollama
decode only tokens that fit it (constrained decoding); then the reply is parsed and validated
with Pydantic anyway, because a schema guarantees shape, not truth.
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Passage
from ...services.prompt import answer_schema, build_messages, parse_answer
from .errors import ProviderError
from .usage import record_usage


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings   # ollama_url, llm_model, temperature
        self.name = settings.llm_provider   # ollama-host or ollama-incluster, shown in responses
        self._url = f"{settings.ollama_url.rstrip('/')}/api/chat"

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        payload = {
            "model": self.settings.llm_model,
            "messages": build_messages(question, passages),
            "stream": False,
            "format": answer_schema(),
            # Low temperature: facts and JSON belong at 0 to 0.3 (the value is in the ConfigMap).
            "options": {"temperature": self.settings.temperature},
        }
        try:
            response = httpx.post(self._url, json=payload, timeout=self.settings.llm_timeout_seconds)
            response.raise_for_status()
            body = response.json()
            content = body["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderError(f"ollama request failed: {exc!r}") from exc
        record_usage(int(body.get("prompt_eval_count", 0)), int(body.get("eval_count", 0)))
        try:
            return parse_answer(content)
        except ValidationError as exc:
            raise ProviderError(f"ollama reply is not a valid Answer: {exc.error_count()} errors") from exc
