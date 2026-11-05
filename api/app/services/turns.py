"""Stores each conversation turn (section 3.2 step 8) so the next question can be recognised
as a follow-up. Turns live in Redis under the caller's session id and expire; losing them
only means the next question is treated as standalone."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("rahbar.turns")
MAX_TURNS = 20


class TurnStore:
    def __init__(self, redis_client: Any, ttl_seconds: int) -> None:
        self.redis = redis_client
        self.ttl = ttl_seconds

    @staticmethod
    def _key(session_id: str) -> str:
        return f"rahbar:turns:{session_id}"

    def last_question(self, session_id: str | None) -> str | None:
        if not session_id or self.redis is None:
            return None
        try:
            raw = self.redis.lindex(self._key(session_id), -1)
        except Exception as exc:
            log.warning("turn lookup failed", extra={"error": repr(exc)})
            return None
        if not raw:
            return None
        try:
            return str(json.loads(raw)["question"])
        except (ValueError, KeyError, TypeError):
            return None

    def add(self, session_id: str | None, question: str, answered: bool, cached: bool) -> None:
        if not session_id or self.redis is None:
            return
        turn = json.dumps({"question": question, "answered": answered, "cached": cached,
                           "at": int(time.time())})
        try:
            key = self._key(session_id)
            pipe = self.redis.pipeline()
            pipe.rpush(key, turn)
            pipe.ltrim(key, -MAX_TURNS, -1)
            pipe.expire(key, self.ttl)
            pipe.execute()
        except Exception as exc:
            log.warning("could not store the turn", extra={"error": repr(exc)})
