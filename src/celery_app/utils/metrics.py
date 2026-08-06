# src/celery_app/utils/metrics.py
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from config.settings import get_settings

_NS = "celery_service"

WORKER_UP = Gauge("worker_up", "1 if worker healthy", namespace=_NS)

TASKS_STARTED = Counter(
    "tasks_started_total", "Tasks started", ["task_name"], namespace=_NS
)
TASKS_COMPLETED = Counter(
    "tasks_completed_total", "Tasks completed", ["task_name", "state"], namespace=_NS
)
TASKS_FAILED = Counter(
    "tasks_failed_total", "Tasks failed", ["task_name", "exception"], namespace=_NS
)
TASKS_RETRIED = Counter(
    "tasks_retried_total", "Tasks retried", ["task_name"], namespace=_NS
)
TASK_DURATION = Histogram(
    "task_duration_seconds",
    "Task execution duration",
    ["task_name"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0),
    namespace=_NS,
)
QUEUE_DEPTH = Gauge("queue_depth", "Queue message count", ["queue_name"], namespace=_NS)


def start_metrics_server() -> None:
    s = get_settings()
    if s.observability.prometheus_enabled:
        start_http_server(s.observability.prometheus_port)
