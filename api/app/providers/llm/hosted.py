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

import re
import time

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Passage
from ...services.prompt import build_messages, parse_answer
from .errors import ProviderError
from .usage import record_usage


# A free tier limits tokens per minute and answers 429 with how long to wait. Waiting a few
# seconds (at most this many times, at most this long each) is cheaper than failing a question.
MAX_RATE_LIMIT_WAITS = 4
MAX_WAIT_SECONDS = 30.0


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
            response = self._post(payload, headers)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            # The status plus the provider's own short error message (it explains a 400 or a
            # retired model). Request headers are never included and the key is redacted.
            raise ProviderError(
                f"hosted provider returned HTTP {exc.response.status_code}: {self._reason(exc.response)}"
            ) from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ProviderError(f"hosted provider request failed: {type(exc).__name__}") from exc
        usage = body.get("usage") or {}
        record_usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        try:
            return parse_answer(content)
        except ValidationError as exc:
            raise ProviderError(f"hosted reply is not a valid Answer: {exc.error_count()} errors") from exc

    def _reason(self, response: httpx.Response) -> str:
        try:
            message = str(response.json()["error"]["message"])
        except (ValueError, KeyError, TypeError):
            message = response.text
        key = self.settings.llm_api_key
        if key:
            message = message.replace(key, "[redacted]")
        return message[:240]

    def _post(self, payload: dict[str, object], headers: dict[str, str]) -> httpx.Response:
        for waits in range(MAX_RATE_LIMIT_WAITS + 1):
            response = httpx.post(self._url, json=payload, headers=headers,
                                  timeout=self.settings.llm_timeout_seconds)
            if response.status_code != 429 or waits == MAX_RATE_LIMIT_WAITS:
                return response
            wait = self._retry_after(response)
            if wait is None or wait > MAX_WAIT_SECONDS:
                return response
            time.sleep(wait)
        return response  # pragma: no cover (the loop always returns)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Seconds to wait: the Retry-After header, else 'try again in 5.34s' from the message."""
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header) + 0.5
            except ValueError:
                pass
        match = re.search(r"try again in ([0-9.]+)\s*(ms|s)", response.text)
        if not match:
            return None
        seconds = float(match.group(1)) / (1000 if match.group(2) == "ms" else 1)
        return seconds + 0.5
