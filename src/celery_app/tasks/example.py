# ==============================================================================
# src/celery_app/tasks/example.py
# Canonical examples of every task pattern.
# Replace with your actual domain tasks.
# ==============================================================================
from __future__ import annotations

import time
from typing import Any

import structlog
from celery import chain, chord, group
from celery.exceptions import SoftTimeLimitExceeded
from pydantic import BaseModel, EmailStr, Field

from celery_app.factory import celery_app
from celery_app.tasks.base import BaseTask, CriticalTask, LongRunningTask

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic input schemas
# ---------------------------------------------------------------------------

class NotificationPayload(BaseModel):
    user_id: int = Field(gt=0)
    email: EmailStr
    message: str = Field(min_length=1, max_length=2000)
    priority: int = Field(default=0, ge=0, le=9)


class ReportPayload(BaseModel):
    report_id: str
    filters: dict[str, Any] = Field(default_factory=dict)
    output_format: str = Field(default="pdf", pattern="^(pdf|csv|xlsx)$")


# ---------------------------------------------------------------------------
# 1. Standard task  (default queue, 3 retries, exponential back-off)
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=BaseTask,
    name="celery_app.tasks.example.send_notification",
    queue="default",
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,       # 30 s → 60 s → 120 s
    retry_backoff_max=300,
    retry_jitter=True,
    soft_time_limit=60,
    time_limit=90,
    input_schema=NotificationPayload,   # Pydantic validates kwargs before run
)
def send_notification(
    self: BaseTask,
    *,
    user_id: int,
    email: str,
    message: str,
    priority: int = 0,
) -> dict[str, Any]:
    """
    Send a notification.

    Calling patterns
    ----------------
    Fire-and-forget:
        send_notification.delay(user_id=1, email="a@b.com", message="hi")

    With apply_async options:
        send_notification.apply_async(
            kwargs={"user_id": 1, "email": "a@b.com", "message": "hi"},
            queue="high",           # override queue at call site
            countdown=10,           # delay execution 10 s
            expires=3600,           # discard if unconsumed within 1 h
            task_id="my-idem-key",  # deterministic ID for dedup
        )

    In a chain (output of prev task piped as first positional arg):
        chain(fetch_user.s(user_id=1), send_notification.s(message="hi")).delay()
    """
    log = logger.bind(task_id=self.request.id, user_id=user_id)
    log.info("Sending notification", email=email, priority=priority)

    try:
        time.sleep(0.05)   # ← replace with real send logic
        return {"status": "sent", "user_id": user_id, "email": email}
    except Exception as exc:
        log.warning("Send failed, scheduling retry", error=str(exc))
        self.retry_task(exc)
        return {}  # unreachable; silences type checker


# ---------------------------------------------------------------------------
# 2. High-priority + idempotent  (payment, order fulfilment, etc.)
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=BaseTask,
    name="celery_app.tasks.example.process_payment",
    queue="high",
    max_retries=5,
    default_retry_delay=5,
    retry_backoff=True,
    retry_backoff_max=120,
    soft_time_limit=120,
    time_limit=150,
    idempotent=True,          # Redis dedup — same task_id won't re-execute
    idempotency_ttl=86_400,   # 24 h
)
def process_payment(
    self: BaseTask,
    *,
    order_id: str,
    amount_cents: int,
    currency: str = "USD",
) -> dict[str, Any]:
    """Process payment. Safe to retry — idempotent."""
    logger.info("Processing payment", order_id=order_id, amount_cents=amount_cents)
    try:
        return {"order_id": order_id, "status": "processed", "currency": currency}
    except Exception as exc:
        self.retry_task(exc)
        return {}


# ---------------------------------------------------------------------------
# 3. Critical task  (no retries, fast hard limit)
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=CriticalTask,
    name="celery_app.tasks.example.send_security_alert",
    # CriticalTask defaults: max_retries=0, queue='critical', time_limit=90
)
def send_security_alert(
    self: CriticalTask,  # noqa: ARG001
    *,
    user_id: int,
    event: str,
    ip: str,
) -> None:
    """Security event — must not fail silently."""
    logger.warning("Security event", user_id=user_id, event=event, ip=ip)
    # ← PagerDuty / Opsgenie / webhook


# ---------------------------------------------------------------------------
# 4. Long-running task with soft-limit cleanup
#
# 重写 on_soft_time_limit_exceeded 必须用显式子类，不能在函数体内定义方法。
# 原因：@celery_app.task(bind=True) 创建的是 LongRunningTask 的实例，不是子类。
# 函数体内的 def 只是局部变量，不会被 Python MRO 找到，永远不会被调用。
# ---------------------------------------------------------------------------

class _GenerateReportTask(LongRunningTask):
    """
    generate_report 专用 Task 子类。
    通过继承 LongRunningTask 来重写 on_soft_time_limit_exceeded。
    只有需要重写钩子时才需要这个子类；普通任务直接用 base=LongRunningTask 即可。
    """

    def on_soft_time_limit_exceeded(self) -> None:
        """
        Worker 即将超时时由 BaseTask.__call__ 调用。
        此时任务函数已经收到 SoftTimeLimitExceeded 异常，
        可以在这里做最后的清理工作。
        注意：此方法执行完后异常会继续向上传播，任务最终标记为 FAILURE。
        """
        report_id = self.request.kwargs.get("report_id", "unknown")
        logger.warning("generate_report.soft_limit", report_id=report_id)
        # 真实场景里做这些：
        #   - 删除 /tmp 下的部分生成文件
        #   - 更新数据库中该报告的状态为 "timeout"
        #   - 发送告警通知


@celery_app.task(
    bind=True,
    base=_GenerateReportTask,          # ← 用子类，不用 LongRunningTask
    name="celery_app.tasks.example.generate_report",
    input_schema=ReportPayload,
    # 继承自 LongRunningTask: queue='low', soft_time_limit=3600, time_limit=3660
)
def generate_report(
    self: _GenerateReportTask,
    *,
    report_id: str,
    filters: dict[str, Any],
    output_format: str = "pdf",
) -> dict[str, Any]:
    """
    长耗时报告生成任务。
    SoftTimeLimitExceeded 不需要在函数体里 catch——BaseTask.__call__ 会捕获它，
    然后调用 on_soft_time_limit_exceeded() 做清理，之后重新抛出。
    函数体只需要关注业务逻辑。
    """
    logger.info("Generating report", report_id=report_id, fmt=output_format)

    try:
        time.sleep(0.1)  # ← actual heavy computation here
        return {
            "report_id": report_id,
            "url": f"s3://reports/{report_id}.{output_format}",
        }
    except Exception as exc:
        self.retry_task(exc)
        return {}


# ---------------------------------------------------------------------------
# 5. Canvas primitives for use in group / chord / chain
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=BaseTask,
    name="celery_app.tasks.example.fetch_user_data",
    queue="default",
    max_retries=2,
    soft_time_limit=30,
)
def fetch_user_data(self: BaseTask, *, user_id: int) -> dict[str, Any]:  # noqa: ARG001
    return {"user_id": user_id, "name": "Alice", "email": "alice@example.com"}


@celery_app.task(
    bind=True,
    base=BaseTask,
    name="celery_app.tasks.example.aggregate_results",
    queue="default",
    soft_time_limit=30,
)
def aggregate_results(self: BaseTask, results: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: ARG001
    """Chord callback — receives list of results from all parallel group tasks."""
    logger.info("Aggregating", count=len(results))
    return {"aggregated": results, "total": len(results)}


def build_parallel_pipeline(user_ids: list[int]) -> Any:
    """
    chord: run N tasks in parallel, then feed all results to a single callback.

        chord(group(t1, t2, t3), callback).apply_async()
    """
    return chord(
        group(fetch_user_data.s(user_id=uid) for uid in user_ids),
        aggregate_results.s(),
    )


def build_sequential_pipeline(user_id: int, message: str) -> Any:
    """
    chain: task B receives the return value of task A as its first arg.

        chain(A.s(x=1), B.s(extra=2)).apply_async()
    """
    return chain(
        fetch_user_data.s(user_id=user_id),
        send_notification.s(message=message),
    )


# ---------------------------------------------------------------------------
# 6. Periodic / Beat task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=BaseTask,
    name="celery_app.tasks.example.health_check",
    queue="default",
    max_retries=0,
    soft_time_limit=10,
    time_limit=15,
)
def health_check(self: BaseTask) -> dict[str, str]:
    """Heartbeat task — scheduled by Celery Beat (see tasks/beat_schedule.py)."""
    return {"status": "ok", "task_id": self.request.id or "eager"}