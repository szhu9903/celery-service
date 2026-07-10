# src/celery_app/integrations/fastapi_adapter.py
from __future__ import annotations

import asyncio
from typing import Any

from celery_app.factory import celery_app


def init_celery() -> Any:
    """Return the Celery singleton. FastAPI needs no context wrapping."""
    return celery_app


async def get_task_result(
    task_id: str,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """
    Non-blocking poll for a task result.

    Usage in a FastAPI route:
        @app.get("/jobs/{task_id}")
        async def poll(task_id: str):
            return await get_task_result(task_id, timeout=5.0)
    """
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        r = celery_app.AsyncResult(task_id)
        state = r.state

        if state == "SUCCESS":
            return {"task_id": task_id, "status": "success", "result": r.result}
        if state == "FAILURE":
            return {"task_id": task_id, "status": "failure", "error": str(r.result), "traceback": r.traceback}
        if state == "REVOKED":
            return {"task_id": task_id, "status": "revoked"}

        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError(f"Task {task_id} did not complete within {timeout}s")

        await asyncio.sleep(poll_interval)


def revoke_task(task_id: str, *, terminate: bool = False) -> None:
    celery_app.control.revoke(task_id, terminate=terminate)
