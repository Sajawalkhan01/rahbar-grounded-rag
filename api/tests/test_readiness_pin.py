"""Readiness, liveness and the embedding-pin guardrail (sections 4.3 and 4.4)."""

from __future__ import annotations

import logging
from typing import Any

import fakeredis
import pytest
from app.readiness import Readiness
from app.retrieval.chroma import ChromaRetriever
from app.retrieval.pin import compare_pin
from app.services.cache import SemanticCache
from fastapi.testclient import TestClient

from tests.fakes import DIM, FakeChroma, FakeEmbedder, meta, settings


def _ready(chroma: FakeChroma, embedder: FakeEmbedder | None = None, redis_ok: bool = True,
           **overrides: Any) -> tuple[bool, dict[str, str]]:
    s = settings(**overrides)
    retriever = ChromaRetriever(s, embedder=embedder or FakeEmbedder(), client=chroma)
    client = fakeredis.FakeRedis()
    if not redis_ok:
        client.connection_pool.connection_kwargs  # noqa: B018 (keep the fake; break ping below)
        client.ping = lambda: (_ for _ in ()).throw(ConnectionError("redis is down"))  # type: ignore[method-assign]
    return Readiness(s, retriever, SemanticCache(client, 0.9, 60)).check()


def _healthy_chroma() -> FakeChroma:
    chroma = FakeChroma()
    chroma.make().add_chunk("a", "text", meta("a.pdf", "1. A"))
    return chroma


def test_ready_when_everything_is_in_place() -> None:
    ready, checks = _ready(_healthy_chroma())
    assert ready, checks
    assert checks["pin"] == "ok" and checks["redis"] == "ok"


def test_not_ready_when_chroma_is_unreachable() -> None:
    chroma = _healthy_chroma()
    chroma.down = True
    ready, checks = _ready(chroma)
    assert not ready and checks["chroma"].startswith("unreachable")


def test_not_ready_when_the_collection_is_missing() -> None:
    ready, checks = _ready(FakeChroma())
    assert not ready and "not found" in checks["collection"]


def test_not_ready_when_the_collection_is_empty() -> None:
    chroma = FakeChroma()
    chroma.make()
    ready, checks = _ready(chroma)
    assert not ready and "empty" in checks["collection"]


def test_not_ready_when_redis_is_unreachable() -> None:
    ready, checks = _ready(_healthy_chroma(), redis_ok=False)
    assert not ready and "redis unreachable" in checks["redis"]


def test_embedding_model_mismatch_refuses_readiness_and_logs_both_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chroma = _healthy_chroma()                         # the index says "fake-embed"
    with caplog.at_level(logging.ERROR, logger="rahbar.ready"):
        ready, checks = _ready(chroma, embed_model="another-model")
    assert not ready
    assert "another-model" in checks["pin"] and "fake-embed" in checks["pin"]
    record = next(r for r in caplog.records if "pin mismatch" in r.getMessage())
    assert record.api_embed_model == "another-model" and record.index_embed_model == "fake-embed"  # type: ignore[attr-defined]


def test_a_wrong_model_name_is_named_even_when_that_model_cannot_be_reached() -> None:
    class Unreachable(FakeEmbedder):
        def embed_query(self, text: str) -> list[float]:
            raise ConnectionError("model not installed")

    ready, checks = _ready(_healthy_chroma(), embedder=Unreachable(), embed_model="another-model")
    assert not ready and "another-model" in checks["pin"] and "fake-embed" in checks["pin"]


def test_vector_dimension_mismatch_refuses_readiness() -> None:
    ready, checks = _ready(_healthy_chroma(), embedder=FakeEmbedder(dim=DIM * 2))
    assert not ready and "dimension mismatch" in checks["pin"]


def test_compare_pin_cases() -> None:
    assert compare_pin({"embed_model": "m", "dimension": 8}, "m", 8).ok
    assert not compare_pin({"embed_model": "m", "dimension": 8}, "n", 8).ok
    assert not compare_pin({"embed_model": "m", "dimension": 8}, "m", 9).ok
    missing = compare_pin({}, "m", 8)
    assert not missing.ok and "does not record" in missing.reason


def test_a_dummy_index_is_refused_by_a_real_api() -> None:
    """The week-1 dummy index records dummy-768 (indexer/pipeline/dummy.py)."""
    chroma = FakeChroma()
    chroma.make(embed_model="dummy-768", dimension=768).add_chunk("a", "t", meta("d.md", "1"))
    ready, checks = _ready(chroma)
    assert not ready and "dummy-768" in checks["pin"]


# ----------------------------------------------------------------- HTTP endpoints
@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_healthz_never_touches_dependencies_even_when_they_are_broken(client: TestClient) -> None:
    class Broken:
        name = "chroma"

        def check(self) -> None:
            raise AssertionError("liveness must not call readiness")

    client.app.state.readiness = Broken()  # type: ignore[attr-defined]
    assert client.get("/healthz").status_code == 200


def test_readyz_returns_503_naming_the_failed_dependency(client: TestClient) -> None:
    class NotReady:
        def check(self) -> tuple[bool, dict[str, str]]:
            return False, {"chroma": "ok", "pin": "embedding model mismatch: API uses 'a', index 'b'"}

    client.app.state.readiness = NotReady()  # type: ignore[attr-defined]
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "pin" in r.json()["failed"] and "chroma" not in r.json()["failed"]


def test_readyz_is_200_in_stub_mode(client: TestClient) -> None:
    assert client.get("/readyz").status_code == 200


def test_the_api_starts_when_chroma_is_unreachable_and_reports_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the Chroma client used to connect in __init__, so the API crashed at startup
    (CrashLoopBackOff) whenever Chroma was down. It must start, stay alive, and say not ready."""
    monkeypatch.setenv("RETRIEVER", "chroma")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("CHROMA_PORT", "1")           # nothing listens here
    monkeypatch.setenv("CACHE_ENABLED", "false")
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        ready = c.get("/readyz")
        assert ready.status_code == 503 and "chroma" in ready.json()["failed"]
        assert c.post("/api/ask", json={"question": "What is the attendance rule?"}).status_code == 503
