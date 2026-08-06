# ==============================================================================
# src/celery_app/tasks/beat_schedule.py
# Celery Beat periodic task registry.
# ==============================================================================
from __future__ import annotations

from celery.schedules import crontab

BEAT_SCHEDULE: dict = {
    # ── Housekeeping ──────────────────────────────────────────────────────────
    "health-check-every-minute": {
        "task": "celery_app.tasks.example.health_check",
        "schedule": 60.0,
        "options": {"queue": "default", "expires": 55},
    },
    # ── Business tasks ────────────────────────────────────────────────────────
    "daily-report-midnight-utc": {
        "task": "celery_app.tasks.example.generate_report",
        "schedule": crontab(hour=0, minute=0),
        "kwargs": {"report_id": "daily-summary", "filters": {}, "output_format": "csv"},
        "options": {"queue": "low", "expires": 3600},
    },
    # Add more here...
}


def register_beat_schedule(app) -> None:  # type: ignore[type-arg]
    """Merge BEAT_SCHEDULE into running Celery app config."""
    app.conf.beat_schedule.update(BEAT_SCHEDULE)
    app.conf.timezone = "UTC"
