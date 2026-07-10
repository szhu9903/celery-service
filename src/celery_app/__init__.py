# Re-export the singleton so callers can do either:
#   from celery_app import celery_app
#   from celery_app.factory import celery_app
from celery_app.factory import celery_app, create_celery_app

__all__ = ["celery_app", "create_celery_app"]
