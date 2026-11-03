"""Hosted free-tier provider (Groq, Google AI Studio, OpenRouter): any OpenAI-compatible API.

    HOSTED_BASE_URL  e.g. https://api.groq.com/openai/v1   (ConfigMap)
    LLM_MODEL        the provider's model name             (ConfigMap)
    LLM_API_KEY      the key                               (Kubernetes Secret, from `make secret`)

The key arrives only as an environment variable. It is sent in the Authorization header and is
never logged: the error messages below deliberately omit request headers.

What leaves the cluster: the question and the retrieved passages (public university
documents). docs/adr/0004-llm-provider-and-data-governance.md explains why that is acceptable
for this corpus and what would change for private documents.
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Passage
from ...services.prompt import build_messages, parse_answer
from .errors import ProviderError
from .usage import record_usage


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings   # hosted_base_url, llm_model, llm_api_key, temperature
        if not settings.hosted_base_url:
            raise ValueError("LLM_PROVIDER=hosted needs HOSTED_BASE_URL")
        self._url = f"{settings.hosted_base_url.rstrip('/')}/chat/completions"

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        if not self.settings.llm_api_key:
            raise ProviderError("LLM_API_KEY is not set (create the Secret with `make secret`)")
        payload = {
            "model": self.settings.llm_model,
            "messages": build_messages(question, passages),
            "temperature": self.settings.temperature,
            # JSON mode: the widest-supported enforcement across free tiers. The reply is
            # still validated against the Answer schema below.
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        try:
            response = httpx.post(self._url, json=payload, headers=headers,
                                  timeout=self.settings.llm_timeout_seconds)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            # Status only: the response body and request headers could echo the key.
            raise ProviderError(f"hosted provider returned HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ProviderError(f"hosted provider request failed: {type(exc).__name__}") from exc
        usage = body.get("usage") or {}
        record_usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        try:
            return parse_answer(content)
        except ValidationError as exc:
            raise ProviderError(f"hosted reply is not a valid Answer: {exc.error_count()} errors") from exc
