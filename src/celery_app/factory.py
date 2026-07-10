# ==============================================================================
# src/celery_app/factory.py
# Celery application factory + signal wiring.
#
# In src layout the package is `celery_app` (lives at src/celery_app/).
# The module that holds the Celery instance is `celery_app.factory` to avoid
# the name collision that happens when the file is also called celery_app.py.
#
# External services (Flask, FastAPI, Twisted…) import the singleton:
#
#   from celery_app.factory import celery_app
#
# Workers are started with:
#   celery -A celery_app.workers.main worker ...
# ==============================================================================
from __future__ import annotations

import logging
from typing import Any

from celery import Celery
from celery.signals import (
    celeryd_after_setup,
    celeryd_init,
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    worker_ready,
    worker_shutdown,
    worker_shutting_down,
)
from kombu import Exchange, Queue

from config.settings import AppSettings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Queue / Exchange topology
# ---------------------------------------------------------------------------

def _build_queues(settings: AppSettings) -> tuple[list[Queue], dict[str, Any]]:
    """
    Build Kombu Queues and task_routes from settings.

    Exchange topology:
      ┌──────────┐   direct   ┌──────────────┐
      │ producer │ ─────────► │ exchange:{q} │ ──► queue:{q}
      └──────────┘            └──────────────┘
                                    │ on nack / expiry
                                    ▼
                              ┌───────────┐
                              │ dlq exch  │ ──► queue:dlq
                              └───────────┘
    """
    dlq_exchange = Exchange("dlq", type="direct", durable=True)

    exchanges = {
        q: Exchange(q, type="direct", durable=True)
        for q in settings.queues.queues
    }

    queues = [
        Queue(
            name,
            exchange=exchanges[name],
            routing_key=name,
            # Native dead-lettering (RabbitMQ honours; Redis ignores gracefully)
            queue_arguments={
                "x-dead-letter-exchange": "dlq",
                "x-dead-letter-routing-key": settings.queues.dead_letter_queue,
            },
            durable=True,
        )
        for name in settings.queues.queues
    ]

    # DLQ
    queues.append(
        Queue(
            settings.queues.dead_letter_queue,
            exchange=dlq_exchange,
            routing_key=settings.queues.dead_letter_queue,
            durable=True,
        )
    )

    # Convention: tasks in celery_app.tasks.<queue_name>.* → routed to <queue_name>
    task_routes: dict[str, dict[str, str]] = {
        f"celery_app.tasks.{q}.*": {"queue": q}
        for q in settings.queues.queues
    }

    return queues, task_routes


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_celery_app(settings: AppSettings | None = None) -> Celery:
    """
    Create and configure a Celery application instance.

    Args:
        settings: Optional AppSettings. Defaults to get_settings() singleton.

    Returns:
        Fully configured Celery instance.

    Example — Flask integration:
        from celery_app.factory import create_celery_app
        from celery_app.integrations.flask_adapter import init_celery

        celery = create_celery_app()
        init_celery(flask_app, celery)

    Example — share with FastAPI:
        from celery_app.factory import celery_app  # pre-built singleton
    """
    if settings is None:
        settings = get_settings()

    app = Celery(settings.service_name)
    queues, task_routes = _build_queues(settings)

    broker_transport_options: dict[str, Any] = {
        "socket_timeout": settings.broker.socket_timeout,
        "socket_connect_timeout": settings.broker.socket_connect_timeout,
        "max_connections": settings.broker.pool_max_connections,
        "visibility_timeout": 3600,
        "retry_policy": {"timeout": 30},
    }
    if settings.broker.sentinel_master:
        broker_transport_options["master_name"] = settings.broker.sentinel_master

    app.config_from_object(
        {
            # ── Broker ────────────────────────────────────────────────
            "broker_url": settings.broker.url,
            "broker_transport_options": broker_transport_options,
            "broker_connection_retry_on_startup": True,
            "broker_connection_max_retries": 10,
            "broker_heartbeat": settings.worker.heartbeat_interval,
            "broker_pool_limit": settings.broker.pool_max_connections,
            # ── Result backend ────────────────────────────────────────
            "result_backend": settings.backend.url,
            "result_expires": settings.backend.result_expires,
            "result_compression": settings.backend.result_compression,
            "result_extended": True,      # store task name + args in result
            "result_persistent": True,
            # ── Serialization ─────────────────────────────────────────
            "task_serializer": settings.security.task_serializer,
            "result_serializer": settings.security.task_serializer,
            "accept_content": settings.security.accept_content,
            # ── Queues & routing ──────────────────────────────────────
            "task_queues": queues,
            "task_default_queue": settings.queues.default_queue,
            "task_default_exchange": settings.queues.default_queue,
            "task_default_routing_key": settings.queues.default_queue,
            "task_routes": task_routes,
            # ── Worker behaviour ──────────────────────────────────────
            "worker_concurrency": settings.worker.concurrency,
            "worker_pool": settings.worker.pool,
            "worker_max_tasks_per_child": settings.worker.max_tasks_per_child,
            "worker_max_memory_per_child": settings.worker.max_memory_per_child,
            "worker_prefetch_multiplier": settings.worker.prefetch_multiplier,
            "worker_lost_wait": 10.0,
            # ── Task execution ────────────────────────────────────────
            "task_acks_late": settings.worker.task_acks_late,
            "task_reject_on_worker_lost": settings.worker.task_reject_on_worker_lost,
            "task_track_started": True,
            "task_send_sent_event": True,
            "task_always_eager": settings.security.task_always_eager,
            "task_soft_time_limit": 300,
            "task_time_limit": 360,
            # ── Beat ──────────────────────────────────────────────────
            "beat_scheduler": "celery.beat:PersistentScheduler",
            "beat_schedule_filename": "/tmp/celerybeat-schedule",  # noqa: S108
            # ── Misc ──────────────────────────────────────────────────
            "timezone": "UTC",
            "enable_utc": True,
            "worker_hijack_root_logger": False,
            "worker_redirect_stdouts": False,
        }
    )

    # app.autodiscover_tasks(
    #     packages=["celery_app.tasks.example"],
    #     related_name="",
    #     force=True,
    # )
    # 直接列出所有任务模块，Celery 逐个 import 触发 @task 装饰器注册。
    #
    # 不用 autodiscover_tasks(packages=["celery_app.tasks"], related_name="") 的原因：
    #   related_name="" 只 import tasks/__init__.py 本身。
    #   如果在 __init__.py 里 import 子模块，子模块又 import factory.py 里的
    #   celery_app 变量，而此时 factory.py 还没执行完，导致循环导入。
    #
    # 用 include 直接指定模块路径，Celery 在 worker 进程完全启动后才 import，
    # 此时 celery_app 变量已经存在，不会循环。
    #
    # 新增任务模块时在这里加一行即可。
    app.conf.update(
        include=[
            "celery_app.tasks.example",
            # "celery_app.tasks.your_new_module",  ← 新增任务模块在这里加
        ]
    )

    return app


# ---------------------------------------------------------------------------
# Module-level singleton — the only Celery instance in the process
# ---------------------------------------------------------------------------
celery_app: Celery = create_celery_app()


# ---------------------------------------------------------------------------
# 应用级信号
# ---------------------------------------------------------------------------

@setup_logging.connect
def configure_logging(**kwargs: Any) -> None:  # noqa: ARG001
    from celery_app.utils.logging import configure_structlog
    configure_structlog()


# Worker进程初始化时触发。记录Worker进程开始初始化
@celeryd_init.connect
def on_celeryd_init(sender: str, **kwargs: Any) -> None:  # noqa: ARG001
    logger.info("Worker进程初始化", extra={"sender": sender})


# Worker队列设置完成后触发。记录队列就绪状态，包括队列名称列表。
@celeryd_after_setup.connect
def on_celeryd_after_setup(sender: str, instance: Any, **kwargs: Any) -> None:  # noqa: ARG001
    logger.info("Worker队列设置完成", extra={
        "sender": sender,
        "queues": [q.name for q in instance.app.amqp.queues.values()],
    })


# Worker准备好接受任务时触发。设置Prometheus指标 WORKER_UP 为1，并记录Worker上线。
@worker_ready.connect
def on_worker_ready(**kwargs: Any) -> None:  # noqa: ARG001
    from celery_app.utils.metrics import WORKER_UP
    WORKER_UP.set(1)
    logger.info("Worker准备好接受任务")


# Worker开始关闭时触发。设置 WORKER_UP 为0，并记录正在排空任务。
@worker_shutting_down.connect
def on_worker_shutting_down(**kwargs: Any) -> None:  # noqa: ARG001
    from celery_app.utils.metrics import WORKER_UP
    WORKER_UP.set(0)
    logger.info("Worker开始关闭 — 正在排空任务")


# Worker完全关闭时触发。记录关闭完成。
@worker_shutdown.connect
def on_worker_shutdown(**kwargs: Any) -> None:  # noqa: ARG001
    logger.info("Worker完全关闭")


# ---------------------------------------------------------------------------
# 任务级信号：只负责 Prometheus metrics（无状态，全局广播）
#
# 分工原则：
#   信号    → metrics（counter/gauge 更新）。无状态，不需要 self，信号天然适合。
#   BaseTask 钩子 → 结构化日志 + 耗时计算。需要 self._start_time，只有钩子能做。
#
# 不在信号里打日志的原因：
#   BaseTask.on_failure / on_success / on_retry 已经打了带 task_id、duration、
#   traceback 的完整结构化日志。如果信号里再打一次，同一个事件会出现两条日志。
#   task_failure 信号有一个例外：它能捕获到不继承 BaseTask 的任务（如第三方库任务），
#   对这些任务做一次兜底日志是合理的，见下方注释。
# ---------------------------------------------------------------------------

# 任务执行前触发。增加 TASKS_STARTED 计数器（按任务名）。
@task_prerun.connect
def on_task_prerun(task_id: str, task: Any, **kwargs: Any) -> None:  # noqa: ARG001
    # 仅更新 counter，日志由 BaseTask.before_start 负责
    from celery_app.utils.metrics import TASKS_STARTED
    TASKS_STARTED.labels(task_name=task.name).inc()


# 任务执行后触发。增加 TASKS_COMPLETED 计数器（按任务名和状态，如SUCCESS/FAILURE）。
@task_postrun.connect
def on_task_postrun(task_id: str, task: Any, state: str, **kwargs: Any) -> None:  # noqa: ARG001
    # 仅更新 counter，日志由 BaseTask.on_success / on_failure 负责
    from celery_app.utils.metrics import TASKS_COMPLETED
    TASKS_COMPLETED.labels(task_name=task.name, state=state).inc()


# 任务失败时触发。增加 TASKS_FAILED 计数器（按任务名和异常类型）。对不继承 BaseTask 的任务（第三方库）做兜底错误日志。
@task_failure.connect
def on_task_failure(task_id: str, exception: Exception, sender: Any, **kwargs: Any) -> None:  # noqa: ARG001
    from celery_app.utils.metrics import TASKS_FAILED
    TASKS_FAILED.labels(task_name=sender.name, exception=type(exception).__name__).inc()

    # 兜底日志：只对不继承 BaseTask 的任务打日志（第三方库任务等）
    # BaseTask 子类已经在 on_failure 钩子里打了完整日志，这里不重复
    from celery_app.tasks.base import BaseTask
    if not isinstance(sender, BaseTask):
        logger.error(
            "task.failure.unhandled",
            extra={"task_id": task_id, "task_name": sender.name, "exception": str(exception)},
            exc_info=exception,
        )


# 任务重试时触发。增加 TASKS_RETRIED 计数器（按任务名）。
@task_retry.connect
def on_task_retry(request: Any, reason: Exception, **kwargs: Any) -> None:  # noqa: ARG001
    # 仅更新 counter，日志由 BaseTask.on_retry 负责
    from celery_app.utils.metrics import TASKS_RETRIED
    TASKS_RETRIED.labels(task_name=request.task).inc()