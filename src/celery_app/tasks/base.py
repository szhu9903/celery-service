# ==============================================================================
# src/celery_app/tasks/base.py
# Enterprise base task classes.
#
# Hierarchy:
#   celery.Task
#     └── BaseTask          — default for all tasks (3 retries, exp back-off)
#           ├── CriticalTask  — no retries, 'critical' queue, 60 s hard limit
#           └── LongRunningTask — 5 retries, 1 h limit, 'low' queue
# ==============================================================================
from __future__ import annotations

import time
import traceback
from typing import Any
from uuid import uuid4

import structlog
from celery import Task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class BaseTask(Task):
    """
    Abstract base task. Inherit to gain:
      - Structured logging on every lifecycle event
      - Prometheus metrics (wired via signals in factory.py)
      - Optional Pydantic input validation (set input_schema)
      - Optional idempotency (set idempotent=True)
      - Exponential back-off retry helper (self.retry_task(exc))
      - Soft time limit cleanup hook (override on_soft_time_limit_exceeded)

    Decorator reference:
        @celery_app.task(
            bind=True,
            base=BaseTask,
            name="celery_app.tasks.<module>.<fn>",  # always explicit
            queue="default",          # default | high | low | critical
            max_retries=3,
            default_retry_delay=60,   # base seconds for exp back-off
            retry_backoff=True,       # double delay each attempt
            retry_backoff_max=600,    # cap at 10 min
            retry_jitter=True,        # add ±random to avoid stampede
            soft_time_limit=300,      # raises SoftTimeLimitExceeded → cleanup
            time_limit=360,           # SIGKILL — must be > soft_time_limit
            acks_late=True,           # ack AFTER execution (safer)
            reject_on_worker_lost=True,
            track_started=True,
            idempotent=False,         # set True to enable Redis dedup
            input_schema=None,        # set to a Pydantic model for validation
        )
    """

    abstract = True

    # ── Retry policy ──────────────────────────────────────────────────────────
    max_retries: int = 3
    default_retry_delay: int = 60
    retry_backoff: bool = True
    retry_backoff_max: int = 600
    retry_jitter: bool = True

    # ── Acknowledgment ────────────────────────────────────────────────────────
    acks_late: bool = True
    reject_on_worker_lost: bool = True
    track_started: bool = True

    # ── Extension flags ───────────────────────────────────────────────────────
    idempotent: bool = False
    input_schema: type[BaseModel] | None = None
    idempotency_ttl: int = 86_400  # 24 h

    # ── Internals ─────────────────────────────────────────────────────────────

    def _log(self) -> Any:
        return logger.bind(
            task_name=self.name,
            task_id=self.request.id or str(uuid4()),
            attempt=self.request.retries + 1,
            max_retries=self.max_retries,
        )

    def _validate_input(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.input_schema is None:
            return kwargs
        return self.input_schema(**kwargs).model_dump()

    # ── Celery lifecycle hooks ────────────────────────────────────────────────

    def before_start(self, task_id: str, args: tuple, kwargs: dict) -> None:  # noqa: ARG002
        self._start_time = time.monotonic()
        self._log().info("task.start", task_id=task_id)

    def on_success(self, retval: Any, task_id: str, args: tuple, kwargs: dict) -> None:  # noqa: ARG002
        elapsed = time.monotonic() - getattr(self, "_start_time", time.monotonic())
        self._log().info("task.success", task_id=task_id, duration_s=round(elapsed, 3))

    def on_failure(
        self, exc: Exception, task_id: str, args: tuple, kwargs: dict, einfo: Any  # noqa: ARG002
    ) -> None:
        elapsed = time.monotonic() - getattr(self, "_start_time", time.monotonic())
        self._log().error(
            "task.failure",
            task_id=task_id,
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
            duration_s=round(elapsed, 3),
            # tb=einfo.traceback if einfo else traceback.format_exc(),
            exc_info=exc,
        )

    def on_retry(
        self, exc: Exception, task_id: str, args: tuple, kwargs: dict, einfo: Any  # noqa: ARG002
    ) -> None:
        self._log().warning(
            "task.retry",
            task_id=task_id,
            attempt=self.request.retries,
            reason=str(exc),
        )

    # ── __call__: validation + soft-limit guard ───────────────────────────────

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        log = self._log()

        if self.input_schema and kwargs:
            # 校验失败直接 raise，不在这里打日志。
            # Celery 会调用 on_failure 钩子，统一在那里记录，避免重复日志。
            kwargs = self._validate_input(kwargs)

        try:
            return super().__call__(*args, **kwargs)
        except SoftTimeLimitExceeded:
            log.warning("task.soft_time_limit")
            self.on_soft_time_limit_exceeded()
            raise

    def on_soft_time_limit_exceeded(self) -> None:
        """Override to clean up resources when approaching the time limit."""

    # ── Retry helper ──────────────────────────────────────────────────────────

    def retry_task(
        self,
        exc: Exception,
        countdown: int | None = None,
        *,
        max_retries: int | None = None,
    ) -> None:
        """
        Retry with exponential back-off.

        Args:
            exc:         Triggering exception — logged + attached to retry.
            countdown:   Override back-off delay (seconds).
            max_retries: Override max retries for this call.

        Raises:
            MaxRetriesExceededError when exhausted.
        """
        effective_max = max_retries if max_retries is not None else self.max_retries

        if self.request.retries >= effective_max:
            raise MaxRetriesExceededError(
                f"{self.name} exhausted {effective_max} retries"
            ) from exc

        raise self.retry(exc=exc, countdown=countdown, max_retries=effective_max)


# ---------------------------------------------------------------------------
# Specialised base classes
# ---------------------------------------------------------------------------

class CriticalTask(BaseTask):
    """
    For high-severity work that must not be silently swallowed.
    - max_retries = 0  (failures escalate immediately)
    - queue = 'critical'
    - Hard time limit: 90 s
    """
    abstract = True
    max_retries: int = 0
    queue = "critical"
    soft_time_limit = 60
    time_limit = 90


class LongRunningTask(BaseTask):
    """
    For slow, resource-intensive jobs.
    - Routed to 'low' queue (isolated from fast tasks)
    - 1-hour soft limit with cleanup hook
    - Up to 5 retries with back-off capped at 1 hour
    """
    abstract = True
    max_retries: int = 5
    default_retry_delay: int = 120
    retry_backoff_max: int = 3_600
    queue = "low"
    soft_time_limit = 3_600
    time_limit = 3_660