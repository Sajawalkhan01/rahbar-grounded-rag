"""Liveness and readiness (section 4.3). They answer different questions."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Can this pod take traffic? 200 only if Chroma is reachable, the collection exists and is
    non-empty, the index was built with this API's embedding model, and Redis is reachable.
    Otherwise 503 naming each failed dependency (app/readiness.py does the checking)."""
    ready, checks = request.app.state.readiness.check()
    failed = {name: reason for name, reason in checks.items() if not (
        reason == "ok" or reason.startswith(("ok ", "disabled", "stub")))}
    if not ready and len(failed) == 0:
        failed = checks
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "not ready", "checks": checks, "failed": failed}
