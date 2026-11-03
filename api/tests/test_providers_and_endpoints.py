"""LLM providers (no network: httpx.post is replaced), the /api/ask endpoint, metrics and logs."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from app.config import get_settings
from app.providers.llm import get_provider
from app.providers.llm.errors import ProviderError
from app.providers.llm.hosted import HostedProvider
from app.providers.llm.ollama import OllamaProvider
from app.providers.llm.usage import take_usage
from fastapi.testclient import TestClient

from tests.fakes import passage, settings

REPLY = {"reasoning": "r", "answer": "80 percent.", "confidence": "high", "answered_from_context": True,
         "citations": [{"source": "doc1.pdf", "section": "1. Section 1", "page": 1}]}
SECRET = "sk-test-SECRET-value-1234567890"


def _fake_post(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | None = None,
               status: int = 200, text: str | None = None) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def post(url: str, **kwargs: Any) -> httpx.Response:
        seen["url"] = url
        seen.update(kwargs)
        request = httpx.Request("POST", url)
        if text is not None:
            return httpx.Response(status, text=text, request=request)
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr(httpx, "post", post)
    return seen


def test_ollama_sends_schema_low_temperature_and_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_post(monkeypatch, {"message": {"content": json.dumps(REPLY)},
                                    "prompt_eval_count": 321, "eval_count": 45})
    s = settings(llm_provider="ollama-host", ollama_url="http://host:11434", temperature=0.2)
    answer = OllamaProvider(s).generate("q?", [passage(1)])
    assert answer.answered_from_context and answer.citations[0].page == 1
    assert seen["url"] == "http://host:11434/api/chat"
    payload = seen["json"]
    assert payload["format"]["properties"].keys() >= {"answer", "citations", "answered_from_context"}
    assert payload["options"]["temperature"] == 0.2 and 0 <= payload["options"]["temperature"] <= 0.3
    assert seen["timeout"] == s.llm_timeout_seconds
    usage = take_usage()
    assert (usage.input_tokens, usage.output_tokens) == (321, 45)


def test_ollama_prose_instead_of_json_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_post(monkeypatch, {"message": {"content": "I think the answer is 80 percent."}})
    with pytest.raises(ProviderError):
        OllamaProvider(settings()).generate("q?", [passage(1)])


@pytest.mark.parametrize("status", [500, 404])
def test_ollama_http_errors_are_provider_errors(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _fake_post(monkeypatch, {"error": "boom"}, status=status)
    with pytest.raises(ProviderError):
        OllamaProvider(settings()).generate("q?", [passage(1)])


def test_ollama_connection_failure_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(ProviderError):
        OllamaProvider(settings()).generate("q?", [passage(1)])


def test_hosted_sends_the_key_as_a_bearer_token_and_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_post(monkeypatch, {"choices": [{"message": {"content": json.dumps(REPLY)}}],
                                    "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
    s = settings(llm_provider="hosted", hosted_base_url="https://api.example.com/v1/", llm_api_key=SECRET)
    assert HostedProvider(s).generate("q?", [passage(1)]).answered_from_context
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert seen["json"]["response_format"] == {"type": "json_object"}
    assert 0 <= seen["json"]["temperature"] <= 0.3


def test_hosted_errors_never_contain_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_post(monkeypatch, {"error": f"bad key {SECRET}"}, status=401)
    s = settings(llm_provider="hosted", hosted_base_url="https://api.example.com/v1", llm_api_key=SECRET)
    with pytest.raises(ProviderError) as info:
        HostedProvider(s).generate("q?", [passage(1)])
    assert SECRET not in str(info.value) and "401" in str(info.value)


def test_hosted_needs_a_key_and_a_url() -> None:
    with pytest.raises(ValueError):
        HostedProvider(settings(llm_provider="hosted", hosted_base_url=""))
    p = HostedProvider(settings(llm_provider="hosted", hosted_base_url="https://x/v1", llm_api_key=""))
    with pytest.raises(ProviderError):
        p.generate("q?", [passage(1)])


def test_provider_factory_selects_by_name() -> None:
    assert get_provider(settings(llm_provider="stub")).name == "stub"
    assert get_provider(settings(llm_provider="ollama-host")).name == "ollama-host"
    assert get_provider(settings(llm_provider="ollama-incluster")).name == "ollama-incluster"
    with pytest.raises(ValueError):
        get_provider(settings(llm_provider="nonsense"))


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELEVANCE_FLOOR", "0.61")
    monkeypatch.setenv("CACHE_THRESHOLD", "0.88")
    monkeypatch.setenv("TOP_K", "7")
    monkeypatch.setenv("HYBRID", "false")
    s = get_settings()
    assert (s.relevance_floor, s.cache_threshold, s.top_k, s.hybrid) == (0.61, 0.88, 7, False)


# ---------------------------------------------------------------------------- endpoints
@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_unknown_fields_and_bad_session_ids_are_rejected(client: TestClient) -> None:
    assert client.post("/api/ask", json={"question": "What is the rule?", "session_id": "../etc"}).status_code == 422
    assert client.post("/api/ask", json={}).status_code == 422


def test_a_dependency_failure_is_a_503_not_a_stack_trace(client: TestClient) -> None:
    class Boom:
        name = "boom"

        def retrieve(self, q: str, k: int) -> list[Any]:
            raise ConnectionError("chroma went away")

        def embed_query(self, t: str) -> None:
            return None

        def index_id(self) -> str:
            return "x"

    client.app.state.retriever = Boom()  # type: ignore[attr-defined]
    r = client.post("/api/ask", json={"question": "What is the attendance rule?"})
    assert r.status_code == 503 and "temporarily unavailable" in r.json()["detail"]
    assert "chroma went away" not in r.text


def test_metrics_cover_the_required_signals(client: TestClient) -> None:
    client.post("/api/ask", json={"question": "What is the attendance rule?"})
    text = client.get("/metrics").text
    for name in ("rahbar_requests_total", "rahbar_request_seconds", "rahbar_retrieval_seconds",
                 "rahbar_generation_seconds", "rahbar_cache_requests_total", "rahbar_abstentions_total",
                 "rahbar_fallbacks_total", "rahbar_llm_tokens_total", "rahbar_cache_hit_ratio"):
        assert name in text, name
    assert 'rahbar_llm_tokens_total{direction="input"}' in text


def test_one_log_line_per_request_has_the_diagnostic_fields(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="rahbar.answer"):
        client.post("/api/ask", json={"question": "What is the attendance rule?"},
                    headers={"X-Request-ID": "trace-me"})
    record = next(r for r in caplog.records if r.getMessage() == "answered")
    assert record.top_k == 5  # type: ignore[attr-defined]
    assert record.chunk_ids == ["stub-001", "stub-002", "stub-003"]  # type: ignore[attr-defined]
    assert len(record.scores) == 3 and record.abstained is False  # type: ignore[attr-defined]
    assert record.provider == "stub"  # type: ignore[attr-defined]


def test_the_log_formatter_puts_the_request_id_on_every_line(client: TestClient) -> None:
    import io

    from app.logjson import JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())      # the same formatter the API installs on stdout
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client.post("/api/ask", json={"question": "What is the attendance rule?"},
                    headers={"X-Request-ID": "rid-json-1"})
    finally:
        root.removeHandler(handler)
    lines = [json.loads(ln) for ln in stream.getvalue().splitlines() if ln.startswith("{")]
    assert lines and all("request_id" in ln for ln in lines)
    ours = [ln for ln in lines if ln["logger"] in ("rahbar.answer", "rahbar.api")]
    assert ours and all(ln["request_id"] == "rid-json-1" for ln in ours)


def test_the_api_key_never_appears_in_logs(monkeypatch: pytest.MonkeyPatch,
                                           caplog: pytest.LogCaptureFixture) -> None:
    _fake_post(monkeypatch, {"error": "nope"}, status=401)
    s = settings(llm_provider="hosted", hosted_base_url="https://x/v1", llm_api_key=SECRET)
    from app.services.answer import answer_question

    from tests.fakes import FakeRetriever

    with caplog.at_level(logging.DEBUG):
        r = answer_question("What is the attendance rule?", FakeRetriever(), HostedProvider(s), s)
    assert not r.answered_from_context                           # fell back
    assert SECRET not in caplog.text and SECRET not in repr(s)
