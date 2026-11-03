"""What /readyz checks (sections 4.3 and 4.4).

Liveness (/healthz) asks "is the process alive?" and touches nothing. Readiness asks "can this
pod take traffic NOW?", so it checks every dependency a question needs:

    chroma     reachable
    collection exists and is non-empty (an empty index would answer every question with nothing)
    pin        the index was built with this API's EMBED_MODEL and vector dimension
    redis      reachable (the semantic cache)

A failing check makes /readyz return 503 naming it, so the pod is withheld from the Service
but not restarted: readiness protects users, liveness protects the pod.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import Settings
from .retrieval.pin import compare_pin

log = logging.getLogger("rahbar.ready")
LOG_EVERY = 60.0     # repeat the same failure in the log at most this often


class Readiness:
    def __init__(self, settings: Settings, retriever: Any, cache: Any) -> None:
        self.settings = settings
        self.retriever = retriever
        self.cache = cache
        self._api_dimension: int | None = None
        self._last_logged: tuple[str, float] = ("", 0.0)

    def check(self) -> tuple[bool, dict[str, str]]:
        """(ready, {check name: "ok" or the reason it failed})."""
        if self.retriever.name == "stub":
            return True, {"retriever": "stub (no external dependencies checked)"}

        checks: dict[str, str] = {}
        collection = None
        try:
            self.retriever.client.heartbeat()
            checks["chroma"] = "ok"
        except Exception as exc:
            checks["chroma"] = f"unreachable: {exc!r}"
        else:
            try:
                collection = self.retriever.collection()
                count = int(collection.count())
                checks["collection"] = "ok" if count > 0 else (
                    f"collection {self.settings.collection!r} is empty: run the indexer Job"
                )
                if count > 0:
                    checks["collection"] = f"ok ({count} chunks)"
            except Exception as exc:
                checks["collection"] = (
                    f"collection {self.settings.collection!r} not found: run the indexer Job ({exc!r})"
                )

        if collection is not None and checks["collection"].startswith("ok"):
            checks["pin"] = self._check_pin(collection.metadata or {})

        checks["redis"] = self.cache.ping() if self.settings.cache_enabled else "disabled"
        ready = all(v == "ok" or v.startswith(("ok ", "disabled")) for v in checks.values())
        return ready, checks

    def _check_pin(self, meta: dict[str, Any]) -> str:
        try:
            if self._api_dimension is None:
                self._api_dimension = len(self.retriever.embedder.embed_query("dimension probe"))
        except Exception as exc:
            return f"cannot verify: the embedding service failed: {exc!r}"
        status = compare_pin(meta, self.settings.embed_model, self._api_dimension)
        if status.ok:
            return "ok"
        key = status.reason
        last_key, last_time = self._last_logged
        now = time.monotonic()
        if key != last_key or now - last_time > LOG_EVERY:
            # The exact conflict goes in the log: this line is what the demo shows.
            log.error("embedding pin mismatch: refusing to become ready",
                      extra={"reason": status.reason, **status.details})
            self._last_logged = (key, now)
        return status.reason
