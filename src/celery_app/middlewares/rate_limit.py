# src/celery_app/middlewares/rate_limit.py
from __future__ import annotations

import time
from typing import Any

import redis
import structlog

from config.settings import get_settings

logger = structlog.get_logger(__name__)
_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(get_settings().broker.url, decode_responses=True)
    return _redis


class RateLimitExceeded(Exception):
    def __init__(self, key: str, limit: int, window: int, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limit {limit}/{window}s exceeded for '{key}'. Retry after {retry_after:.1f}s.")


def check_rate_limit(key: str, limit: int, window: int, *, raise_on_exceed: bool = True) -> bool:
    """
    Sliding window rate limiter backed by Redis sorted sets.

    Args:
        key:             Unique bucket key, e.g. "email:user:42"
        limit:           Max calls in window
        window:          Window size in seconds
        raise_on_exceed: Raise RateLimitExceeded or return False

    Returns True if allowed, False if exceeded (when raise_on_exceed=False).
    """
    r = _get_redis()
    now = time.time()
    rkey = f"rl:{key}"

    pipe = r.pipeline()
    pipe.zremrangebyscore(rkey, 0, now - window)
    pipe.zadd(rkey, {str(now): now})
    pipe.zcard(rkey)
    pipe.expire(rkey, window + 1)
    _, _, count, _ = pipe.execute()

    if count <= limit:
        return True

    oldest = r.zrange(rkey, 0, 0, withscores=True)
    retry_after = (float(oldest[0][1]) + window - now) if oldest else float(window)
    logger.warning("rate_limit.exceeded", key=key, count=count, limit=limit, retry_after=retry_after)

    if raise_on_exceed:
        raise RateLimitExceeded(key, limit, window, retry_after)
    return False


def rate_limited(key_fn: Any, limit: int, window: int) -> Any:
    """
    Task decorator factory for per-entity rate limiting.

    Args:
        key_fn:  lambda receiving task kwargs → returns bucket key string
                 e.g.: lambda kw: f"sms:{kw['user_id']}"
        limit:   Max executions per window
        window:  Window in seconds

    Example:
        @celery_app.task(bind=True, base=BaseTask)
        @rate_limited(key_fn=lambda kw: f"sms:{kw['user_id']}", limit=3, window=60)
        def send_sms(self, *, user_id: int, text: str) -> dict:
            ...
    """
    def decorator(fn: Any) -> Any:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                check_rate_limit(key_fn(kwargs), limit, window)
            except RateLimitExceeded as exc:
                if hasattr(self, "retry"):
                    raise self.retry(exc=exc, countdown=int(exc.retry_after) + 1)
                raise
            return fn(self, *args, **kwargs)
        return wrapper
    return decorator
