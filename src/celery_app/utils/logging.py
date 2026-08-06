# ==============================================================================
# src/celery_app/utils/logging.py
# 结构化日志配置（structlog）
#
# 设计要点：
#   1. add_logger_name 改为安全版本，兼容 Celery 内部日志没有 record 的情况
#   2. worker_hijack_root_logger=False（在 factory.py 里设置），
#      让 structlog 完全接管，Celery 不再覆盖 root logger
#   3. JSON 格式用于生产，console 彩色输出用于本地开发
# ==============================================================================
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from config.settings import get_settings


def _safe_add_logger_name(
    logger: Any,
    method: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    安全版的 add_logger_name。
    stdlib 的 add_logger_name 直接读 record.name，
    但 Celery 内部某些日志路径没有 record，会导致 AttributeError。
    这里先尝试从 _record 取，取不到就用 logger 对象的名字，再取不到就跳过。
    """
    record: logging.LogRecord | None = event_dict.get("_record")
    if record is not None:
        event_dict.setdefault("logger", record.name)
    elif hasattr(logger, "name") and logger.name:
        event_dict.setdefault("logger", logger.name)
    # 取不到就不加这个字段，不报错
    return event_dict


def _add_service_context(
    logger: Any,  # noqa: ARG001
    method: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """给每条日志注入服务级别的元信息。"""
    s = get_settings()
    event_dict.setdefault("service", s.service_name)
    event_dict.setdefault("version", s.service_version)
    event_dict.setdefault("env", s.environment)
    return event_dict


def configure_structlog() -> None:
    """
    配置 structlog + stdlib logging。
    在 worker 进程启动时调用一次（由 factory.py 的 setup_logging 信号触发）。
    """
    s = get_settings()

    # 共享 processors：structlog 自身日志和经由 ProcessorFormatter 的 stdlib 日志都走这里
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,      # 合并请求级上下文变量
        _safe_add_logger_name,                        # 安全取 logger 名（替换原 add_logger_name）
        structlog.stdlib.add_log_level,               # 注入 level 字段
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # ISO8601 时间戳
        structlog.processors.StackInfoRenderer(),     # 异常 stack info
        structlog.processors.UnicodeDecoder(),        # bytes → str
        _add_service_context,                         # service/version/env
    ]

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=True)
        if s.observability.log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(s.observability.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ProcessorFormatter 让经过 stdlib logging 的日志也走 structlog processors
    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain：处理来自 stdlib（包括 Celery）的日志
        # 这里不加 _safe_add_logger_name，因为 ProcessorFormatter 自己会从 record 提取
        foreign_pre_chain=[
            structlog.stdlib.ExtraAdder(),            # 把 extra={} 里的字段合并进来
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *shared_processors,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(s.observability.log_level)

    # 压制噪音较多的第三方 logger
    for noisy in (
        "kombu",
        "amqp",
        "celery.utils.functional",
        # "celery.app.trace",       # 每个任务都会打 succeeded/failed，生产可压制
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # 关键修改：将 celery.app.trace 设为 CRITICAL
    # 因为你已经在 BaseTask 的 on_success/on_failure/on_retry 中完全接管了日志
    # 所以不需要 Celery 默认的 "Task ... succeeded" 或 "Task ... raised unexpected"
    logging.getLogger("celery.app.trace").setLevel(logging.CRITICAL)
