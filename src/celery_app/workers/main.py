# ==============================================================================
# src/celery_app/workers/main.py
# Worker process entrypoint.
#
# Start commands:
#   celery -A celery_app.workers.main worker --queues default,high -c 4
#   celery -A celery_app.workers.main beat
#   celery -A celery_app.workers.main flower
# ==============================================================================
from __future__ import annotations

# The import of factory triggers:
#   1. Celery instance creation
#   2. Signal handler registration
#   3. Task autodiscovery
from celery_app.factory import celery_app  # noqa: F401
from celery_app.tasks.beat_schedule import register_beat_schedule
from celery_app.utils.logging import configure_structlog
from celery_app.utils.metrics import start_metrics_server
from celery_app.utils.tracing import setup_tracing

configure_structlog()
setup_tracing()
start_metrics_server()
register_beat_schedule(celery_app)
