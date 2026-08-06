# ==============================================================================
# Dockerfile  (src 布局)
# 多阶段构建 — 最小化、非 root、生产就绪。
#
# 阶段:
#   base    — Python 运行时 + 系统依赖
#   builder — pip install 到 /install
#   worker  — Celery worker  (默认目标)
#   beat    — Celery Beat 调度器
#   flower  — Flower 监控 UI
#
# 构建示例:
#   docker build --target worker -t celery-service:worker .
#   docker build --target beat   -t celery-service:beat   .
#   docker build --target flower -t celery-service:flower .
# ==============================================================================

# ── 阶段 1: base ─────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS base

ENV
    # 禁止 Python 自动生成 .pyc 字节码缓存文件
    PYTHONDONTWRITEBYTECODE=1 \
    # 禁止 Python 缓冲 stdout/stderr，确保日志实时输出
    PYTHONUNBUFFERED=1 \
    # 启用 Python 错误处理器，打印堆栈跟踪
    PYTHONFAULTHANDLER=1 \
    # 禁止 pip 缓存和版本检查，减少镜像体积 Docker 构建必备，必须保留
    PIP_NO_CACHE_DIR=1 \
    # pip 安装时不再弹窗提示
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # src 布局: 将 src/ 加入 PYTHONPATH，使 `import celery_app` 可解析
    PYTHONPATH=/app/src

# 仅安装运行时必需的底层库（如需要连接 PostgreSQL 则保留 libpq-dev）
#RUN apt-get update && apt-get install -y --no-install-recommends \
#        libpq-dev \
#    && rm -rf /var/lib/apt/lists/*

# 非 root 用户 (uid/gid 1001)
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home --shell /sbin/nologin appuser

WORKDIR /app


# ── 阶段 2: 依赖构建 ───────────────────────────────────────────────────────
FROM base AS builder

COPY pyproject.toml ./
# 将项目 + flower 扩展安装到独立前缀目录（保持最终镜像干净）
RUN pip install --prefix=/install ".[flower]"


# ── 阶段 3: worker (默认) ──────────────────────────────────────────────────
FROM base AS worker

# 复制已安装的依赖包
COPY --from=builder /install /usr/local
# 复制源码
COPY src/  ./src/
COPY config/ ./config/
# .env 在运行时注入 — 绝不烘焙进镜像
COPY pyproject.toml ./

USER appuser

HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=40s \
    --retries=3 \
    # celery inspect：Celery 官方自带运维探测命令
    CMD celery -A celery_app.workers.main inspect ping -d "celery@$HOSTNAME" || exit 1

CMD ["celery", \
     "--app", "celery_app.workers.main", \
     "worker", \
     "--loglevel", "info", \
     "--queues", "default,high,low,critical", \
     "--hostname", "worker@%h", \
     "--without-gossip", \
     "--without-mingle", \
     "--without-heartbeat"]


# ── 阶段 4: beat 调度器 ────────────────────────────────────────────────────
FROM worker AS beat

# Beat 必须作为单例运行 — 与 worker 的唯一区别是 CMD
CMD ["celery", \
     "--app", "celery_app.workers.main", \
     "beat", \
     "--loglevel", "info", \
     "--scheduler", "celery.beat:PersistentScheduler"]


# ── 阶段 5: Flower UI ──────────────────────────────────────────────────────
FROM worker AS flower

EXPOSE 5555

CMD ["celery", \
     "--app", "celery_app.workers.main", \
     "flower", \
     "--port=5555", \
     "--persistent=true", \
     "--db=/tmp/flower"]
