# src/celery_app/integrations/flask_adapter.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from celery import Task

from celery_app.factory import celery_app

if TYPE_CHECKING:
    from flask import Flask


class _FlaskTask(Task):
    """Pushes Flask application context for every task execution."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with _app.app_context():
            return super().__call__(*args, **kwargs)


_app: "Flask"


def init_celery(flask_app: "Flask") -> Any:
    """
    Bind a Flask app so all tasks run inside its application context.

    Usage:
        from flask import Flask
        from celery_app.integrations.flask_adapter import init_celery

        app = Flask(__name__)
        celery = init_celery(app)
        # Tasks now have access to current_app, g, db.session, etc.
    """
    global _app
    _app = flask_app
    celery_app.Task = _FlaskTask
    celery_app.config_from_object(flask_app.config.get("CELERY", {}), silent=True)
    return celery_app
