# ==============================================================================
# Makefile  (uv 版本)
# 所有命令通过 uv run 执行，自动使用 .venv 和 .env，无需手动 activate。
#
# 前提：安装 uv
#   curl -LsSf https://astral.sh/uv/install.sh | sh
#   # 或 brew install uv
# ==============================================================================

.PHONY: help init sync sync-dev sync-all lint fmt type-check \
        test test-cov test-integration \
        worker beat flower \
        docker-build docker-up docker-up-tools docker-down docker-logs \
        lock upgrade clean secret

# ── Config ────────────────────────────────────────────────────────────────────
IMAGE    := celery-service
TAG      := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)

# uv run 自动处理 PYTHONPATH（src layout 通过 pyproject.toml 声明）
# 但某些工具（如直接调 celery 而非 uv run celery）需要显式设置
export PYTHONPATH := $(PWD)/src

# ── Help ──────────────────────────────────────────────────────────────────────
help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-28s\033[0m %s\n", $$1, $$2}'

# ==============================================================================
# 环境管理
# ==============================================================================

init:  ## 首次初始化：复制 .env、同步所有依赖、安装 pre-commit hooks
	@test -f .env || (cp .env.example .env && echo "✅  .env created from .env.example")
	uv sync --extra dev,flower
	uv run pre-commit install
	@echo "✅  Done. Run 'make worker' to start."

sync:  ## 安装/同步生产依赖（按 uv.lock 精确复现）
	uv sync

sync-dev:  ## 同步开发依赖（dev + flower）
	uv sync --extra dev,flower

sync-otel:  ## 同步 OTel 链路追踪依赖（需要时才装）
	uv sync --extra dev,flower,otel

sync-all:  ## 同步全部依赖
	uv sync --extra all

lock:  ## 重新生成 uv.lock（依赖有变动时执行）
	uv lock

upgrade:  ## 升级所有依赖到最新兼容版本
	uv lock --upgrade
	uv sync --extra all

# ==============================================================================
# 代码质量
# ==============================================================================
lint:  ## Ruff 静态检查
	uv run ruff check src/ config/ tests/

fmt:  ## Ruff 自动格式化
	uv run ruff format src/ config/ tests/

type-check:  ## Mypy 类型检查
	uv run mypy src/ config/

# ==============================================================================
# 测试
# ==============================================================================
test:  ## 单元测试（不需要 Redis）
	uv run pytest tests/unit -v

test-cov:  ## 单元测试 + HTML 覆盖率报告
	uv run pytest tests/unit -v \
		--cov=celery_app \
		--cov=config \
		--cov-report=term-missing \
		--cov-report=html:htmlcov

test-integration:  ## 集成测试（需要 Redis 在 localhost:6379）
	uv run pytest tests/integration -v -m integration --timeout=30

# ==============================================================================
# 本地启动
# uv run 会自动：
#   1. 激活 .venv
#   2. 读取 .env（通过 pyproject.toml [tool.uv] env-file = ".env"）
#   3. 设置 PYTHONPATH（src layout 通过 hatchling 声明）
# ==============================================================================

worker:  ## 启动本地 worker（消费所有队列，并发 2）
	uv run celery --app celery_app.workers.main worker \
		--loglevel info \
		--queues default,high,low,critical \
		--concurrency 2 \
		--hostname local@%h

worker-high:  ## 仅消费 high + critical 队列
	uv run celery --app celery_app.workers.main worker \
		--loglevel info \
		--queues high,critical \
		--concurrency 4 \
		--hostname high@%h

beat:  ## 启动 Beat 定时调度器（单例！不要同时开两个）
	uv run celery --app celery_app.workers.main beat --loglevel info

flower:  ## 启动 Flower 监控 UI → http://localhost:5555
	uv run celery --app celery_app.workers.main flower --port 5555

# ==============================================================================
# Docker
# ==============================================================================

docker-build:  ## 构建 worker 镜像
	docker build --target worker -t $(IMAGE):$(TAG) .

docker-up:  ## 启动完整本地栈
	docker compose up --build -d

docker-up-tools:  ## 启动本地栈 + Flower
	docker compose --profile tools up --build -d
	@echo "Flower UI → http://localhost:5555"

docker-down:  ## 停止所有容器
	docker compose down

docker-logs:  ## 跟踪 worker + beat 日志
	docker compose logs -f worker-default worker-high worker-low beat

# ==============================================================================
# 工具
# ==============================================================================

secret:  ## 生成 SECRET_KEY（粘到 .env 里）
	@uv run python -c "import secrets; print(secrets.token_hex(32))"

clean:  ## 清理构建产物和缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf .coverage htmlcov .mypy_cache .ruff_cache dist build *.egg-info
	@echo "✅  Clean."