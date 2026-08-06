# src/celery_app/middlewares/idempotency.py
from __future__ import annotations

import hashlib
import json
from typing import Any

import redis
import structlog

from config.settings import get_settings

logger = structlog.get_logger(__name__)
_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(get_settings().broker.url, decode_responses=True, socket_timeout=3)
    return _redis


def _key(task_name: str, raw: str) -> str:
    return f"idem:{task_name}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


class IdempotencyMixin:
    """
    Mixin that skips re-execution when a task with the same idempotency key
    has already completed successfully.

    Usage:
        class MyTask(IdempotencyMixin, BaseTask):
            idempotent = True
            idempotency_ttl = 86_400   # seconds
    """

    idempotent: bool = False
    idempotency_ttl: int = 86_400

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self.idempotent:
            return super().__call__(*args, **kwargs)  # type: ignore[misc]

        raw_key = (
            (self.request.headers or {}).get("idempotency_key")
            or self.request.id
            or "unknown"
        )
        rkey = _key(self.name, raw_key)  # type: ignore[attr-defined]
        r = _get_redis()

        cached = r.get(rkey)
        if cached is not None:
            logger.info("idempotency.hit", task=self.name, key=raw_key)  # type: ignore[attr-defined]
            return json.loads(cached)

        result = super().__call__(*args, **kwargs)  # type: ignore[misc]

        try:
            r.setex(rkey, self.idempotency_ttl, json.dumps(result))
        except (TypeError, ValueError):
            logger.warning("idempotency.result_not_serialisable", task=self.name)  # type: ignore[attr-defined]

        return result
