# ==============================================================================
# config/settings.py
# Multi-environment configuration via Pydantic Settings v2.
#
# 核心设计原则：
#   子配置类（BrokerSettings 等）全部继承 BaseModel（不是 BaseSettings）。
#   只有顶层 AppSettings 继承 BaseSettings，由它统一负责从环境变量和 .env 读取。
#
# 这样做的原因：
#   当子类继承 BaseSettings 并设置 env_prefix 时，嵌套进 AppSettings 后
#   子类的 model_config（包括 env_prefix）会被完全忽略。
#   Pydantic Settings v2 处理嵌套时，只认 env_nested_delimiter 规则：
#     字段名 + __ + 子字段名 → BROKER__URL
#   而 .env.example 里写的是 BROKER_URL（单下划线），两者对不上，读不到值。
#
# 解决方案：
#   子类改为 BaseModel，完全不参与环境变量读取。
#   AppSettings 用 model_config 里的 env_nested_delimiter="__" 来映射：
#     BROKER__URL            → settings.broker.url
#     WORKER__CONCURRENCY    → settings.worker.concurrency
#     OBS__LOG_LEVEL         → settings.observability.log_level
#   同时保留单下划线的直觉写法（通过字段别名 alias），见下方说明。
#
# .env 变量命名规则（最终确定版）：
#   BROKER__URL=amqp://...          双下划线分隔嵌套
#   WORKER__CONCURRENCY=4
#   OBS__LOG_LEVEL=DEBUG
#   ENVIRONMENT=production          顶层字段，无前缀
#   SERVICE_NAME=celery-service
#
# 优先级（高 → 低）：
#   1. 环境变量（export FOO=bar）
#   2. .env 文件
#   3. Field 默认值
# ==============================================================================
from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ==============================================================================
# 子配置类：继承 BaseModel（纯数据结构 + 校验，不读环境变量）
# ==============================================================================

class BrokerSettings(BaseModel):
    url: str = Field(
        default="redis://localhost:6379/0",
        description="redis:// | amqp:// | sentinel://",
    )
    pool_max_connections: int = Field(default=20, ge=1, le=200)
    socket_timeout: float = Field(default=5.0)
    socket_connect_timeout: float = Field(default=5.0)
    sentinel_master: str | None = Field(default=None)
    sentinel_urls: list[str] = Field(default_factory=list)


class BackendSettings(BaseModel):
    url: str = Field(default="redis://localhost:6379/1")
    result_expires: int = Field(default=3600, ge=60)
    result_compression: Literal["gzip", "bzip2", "zlib"] | None = Field(default="gzip")


class WorkerSettings(BaseModel):
    concurrency: int = Field(default=4, ge=1)
    pool: str = Field(default="prefork")
    max_tasks_per_child: int = Field(default=500, ge=10)
    max_memory_per_child: int = Field(default=200_000)
    task_acks_late: bool = Field(default=True)
    task_reject_on_worker_lost: bool = Field(default=True)
    prefetch_multiplier: int = Field(default=1, ge=1)
    heartbeat_interval: float = Field(default=10.0)
    shutdown_timeout: int = Field(default=60, ge=10)


class QueueSettings(BaseModel):
    queues: list[str] = Field(default_factory=lambda: ["default", "high", "low", "critical"])
    default_queue: str = Field(default="default")
    dead_letter_queue: str = Field(default="dlq")


class ObservabilitySettings(BaseModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    log_format: Literal["json", "console"] = Field(default="json")
    prometheus_enabled: bool = Field(default=True)
    prometheus_port: int = Field(default=9090, ge=1024, le=65535)
    otel_enabled: bool = Field(default=False)
    otel_exporter_otlp_endpoint: str = Field(default="http://localhost:4317")
    otel_service_name: str = Field(default="celery-service")
    sentry_dsn: str | None = Field(default=None)
    sentry_environment: str = Field(default="production")
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)


class SecuritySettings(BaseModel):
    task_serializer: Literal["json", "msgpack"] = Field(default="json")
    accept_content: list[str] = Field(default_factory=lambda: ["json"])
    task_always_eager: bool = Field(default=False)
    secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))


# ==============================================================================
# 顶层配置：只有这一个类继承 BaseSettings，统一负责读取环境变量
# ==============================================================================

class AppSettings(BaseSettings):
    """
    唯一的 BaseSettings 子类。
    通过 env_nested_delimiter="__" 把扁平的环境变量映射到嵌套结构：

        .env 写法               映射到
        ─────────────────────   ──────────────────────────────
        BROKER__URL             settings.broker.url
        BROKER__POOL_MAX_CONNECTIONS  settings.broker.pool_max_connections
        BACKEND__URL            settings.backend.url
        BACKEND__RESULT_EXPIRES settings.backend.result_expires
        WORKER__CONCURRENCY     settings.worker.concurrency
        WORKER__POOL            settings.worker.pool
        QUEUE__DEFAULT_QUEUE    settings.queues.default_queue
        OBS__LOG_LEVEL          settings.observability.log_level
        OBS__LOG_FORMAT         settings.observability.log_format
        OBS__PROMETHEUS_PORT    settings.observability.prometheus_port
        SECURITY__TASK_SERIALIZER  settings.security.task_serializer
        SECRET_KEY              settings.security.secret_key  ← 顶层别名，见下
        ENVIRONMENT             settings.environment
        SERVICE_NAME            settings.service_name
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # 双下划线分隔嵌套层级，例如 BROKER__URL → broker.url
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 顶层字段（直接用字段名作为环境变量名）────────────────────────────────
    environment: Literal["development", "staging", "production", "test"] = Field(
        default="production"
    )
    service_name: str = Field(default="celery-service")
    service_version: str = Field(default="1.0.0")

    # SECRET_KEY 作为顶层环境变量（常见惯例），注入到 security.secret_key
    # 用 model_validator 处理，见下方
    secret_key: str | None = Field(default=None, exclude=True)

    # ── 嵌套配置（通过 env_nested_delimiter 填充）────────────────────────────
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    backend: BackendSettings = Field(default_factory=BackendSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    queues: QueueSettings = Field(default_factory=QueueSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, v: str) -> str:
        return v.lower().strip()

    @model_validator(mode="after")
    def apply_overrides(self) -> "AppSettings":
        # SECRET_KEY 顶层变量 → 覆盖 security.secret_key
        # 这样同时支持两种写法：
        #   SECRET_KEY=xxx                 （常见的平铺写法）
        #   SECURITY__SECRET_KEY=xxx       （嵌套写法）
        if self.secret_key is not None:
            self.security.secret_key = self.secret_key

        # 环境相关的默认覆盖
        if self.environment in ("development", "test"):
            self.observability.log_format = "console"
            self.observability.log_level = "DEBUG"
        if self.environment == "test":
            self.security.task_always_eager = True

        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """
    进程级单例。只解析一次，之后全部走缓存。
    测试中重置：get_settings.cache_clear()
    """
    return AppSettings()