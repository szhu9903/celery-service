# Celery Service — Enterprise Template (`src` layout)

独立部署的企业级 Celery 服务。采用 `src/` 布局，可无缝接入 Flask、FastAPI、Twisted 等框架。
支持 Docker Compose 本地开发和 Kubernetes/Helm 生产部署。

---

## 目录结构

```
celery-service/
├── src/
│   └── celery_app/                  ← 可安装的 Python 包
│       ├── __init__.py              # 重导出 celery_app 单例
│       ├── factory.py               # Celery 工厂 + 信号处理（核心）
│       ├── tasks/
│       │   ├── base.py              # BaseTask / CriticalTask / LongRunningTask
│       │   ├── example.py           # 所有装饰器参数的完整演示
│       │   └── beat_schedule.py     # 定时任务注册
│       ├── workers/
│       │   └── main.py              # Worker 进程入口（-A 指向这里）
│       ├── middlewares/
│       │   ├── idempotency.py       # Redis 幂等去重
│       │   └── rate_limit.py        # 滑动窗口限流
│       ├── utils/
│       │   ├── logging.py           # structlog JSON 结构化日志
│       │   ├── metrics.py           # Prometheus 指标
│       │   └── tracing.py           # OpenTelemetry 链路追踪
│       └── integrations/
│           ├── flask_adapter.py     # Flask app context 注入
│           └── fastapi_adapter.py   # FastAPI 异步结果轮询
├── config/
│   └── settings.py                  # Pydantic Settings v2 统一配置
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_tasks.py
│   │   └── test_settings.py
│   └── integration/
│       └── test_tasks_integration.py
├── deploy/
│   ├── docker/prometheus.yml
│   ├── k8s/deployment.yaml          # Worker + Beat + Flower + KEDA + PDB
│   └── helm/values.yaml
├── Dockerfile                       # 多阶段：worker / beat / flower
├── docker-compose.yml
├── pyproject.toml                   # src layout package discovery
├── Makefile
└── .env.example
```

---

## 为什么用 `src/` 布局？

| 问题 | `app/` 布局 | `src/` 布局 |
|------|------------|------------|
| 意外导入本地目录 | 有可能 | 不可能（包在 `src/` 内） |
| 可安装为 wheel | 需额外配置 | 开箱即用 |
| IDE 跳转 | 有时混乱 | 清晰 |
| 与 Celery 包名冲突 | `celery_app.py` vs `celery` 包 | 包名 `celery_app`，无冲突 |

**核心规则**：`Celery` 实例所在文件是 `factory.py`（而非 `celery_app.py`），避免包内部循环导入。

---

## 快速开始

```bash
# 前提：安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. 一键初始化（复制 .env、创建 .venv、装依赖、装 pre-commit）
make init

# 2. 编辑 .env，改 BROKER_URL 和 BACKEND_URL（见下方配置说明）
vim .env

# 3. 启动 worker（另一个终端）
make worker

# 4. 启动 Beat 定时调度器（另一个终端）
make beat

# 5. Flower 监控 UI → http://localhost:5555
make flower
```

> `uv run` 会自动激活 `.venv`、读取 `.env`，无需手动 `source .venv/bin/activate`。

### Docker Compose（推荐本地联调）

```bash
docker compose up -d                          # 核心服务
docker compose --profile monitoring up -d    # + Prometheus + Grafana
docker compose --profile debug up -d         # + Redis Commander
```

| 服务 | 地址 |
|------|------|
| Flower | http://localhost:5555 |
| Grafana | http://localhost:3000 (`admin/changeme`) |
| Prometheus | http://localhost:9090 |
| Redis Commander | http://localhost:8081 |

---

## 创建新任务

### 标准任务模板

```python
# src/celery_app/tasks/my_domain.py
from pydantic import BaseModel, Field
from celery_app.factory import celery_app
from celery_app.tasks.base import BaseTask


class MyPayload(BaseModel):
    user_id: int = Field(gt=0)
    action: str


@celery_app.task(
    bind=True,
    base=BaseTask,
    name="celery_app.tasks.my_domain.do_work",  # 必须显式命名
    queue="default",         # default | high | low | critical
    max_retries=3,
    default_retry_delay=30,
    retry_backoff=True,      # 30s → 60s → 120s
    retry_backoff_max=300,
    retry_jitter=True,       # 加随机抖动
    soft_time_limit=60,      # raises SoftTimeLimitExceeded → on_soft_time_limit_exceeded()
    time_limit=90,           # SIGKILL
    input_schema=MyPayload,  # Pydantic 校验，无效参数直接拒绝
    idempotent=True,         # 相同 task_id 只执行一次
)
def do_work(self: BaseTask, *, user_id: int, action: str) -> dict:
    try:
        result = heavy_operation(user_id, action)
        return {"user_id": user_id, "result": result}
    except TransientError as exc:
        self.retry_task(exc)          # 指数退避重试
        return {}
    except PermanentError:
        raise                         # 不重试，直接失败
```

### 三种 base class 对比

```python
# 普通任务
@celery_app.task(bind=True, base=BaseTask, queue="default")

# 关键任务（绝不重试，60s 超时）
@celery_app.task(bind=True, base=CriticalTask)

# 长任务（1h 超时，low 队列，5次重试）
@celery_app.task(bind=True, base=LongRunningTask)
```

### 调用方式

```python
# 异步触发
do_work.delay(user_id=1, action="process")

# 完整控制
do_work.apply_async(
    kwargs={"user_id": 1, "action": "process"},
    queue="high",          # 覆盖队列
    countdown=10,          # 延迟 10s
    expires=3600,          # 1h 内未消费则丢弃
    task_id="order-123",   # 固定 ID 用于幂等
)

# chain: A → B → C（顺序，前一个的返回值作为下一个的第一个参数）
chain(fetch.s(id=1), process.s(), notify.s()).apply_async()

# chord: [A1, A2, A3] → 全部完成后 → B（并行+聚合）
chord(group(fetch.s(id=i) for i in ids), aggregate.s()).apply_async()
```

---

## 接入外部框架

### Flask

```python
from flask import Flask
from celery_app.integrations.flask_adapter import init_celery

app = Flask(__name__)
celery = init_celery(app)
# tasks 现在可访问 current_app、g、db.session
```

### FastAPI

```python
from fastapi import FastAPI
from celery_app.integrations.fastapi_adapter import init_celery, get_task_result
from celery_app.tasks.example import generate_report

api = FastAPI()

@api.post("/reports")
async def create(payload: ReportIn):
    task = generate_report.apply_async(kwargs=payload.model_dump())
    return {"task_id": task.id}

@api.get("/reports/{task_id}")
async def poll(task_id: str):
    return await get_task_result(task_id, timeout=5.0)
```

---

## Kubernetes 部署

```bash
# 直接 apply
kubectl apply -f deploy/k8s/deployment.yaml

# 或 Helm（推荐）
helm upgrade --install celery-service ./deploy/helm \
  --namespace celery-service --create-namespace \
  --set global.image.tag=$(git describe --tags)
```

### 关键规则

- Beat 永远是 **1 个副本**，`strategy: Recreate`（防双写）
- `terminationGracePeriodSeconds: 90` > `WORKER_SHUTDOWN_TIMEOUT: 60`
- KEDA ScaledObject 按队列深度自动扩缩容，无需 CPU HPA
- PodDisruptionBudget 保证滚动更新期间最多 1 个 pod 不可用

---

## 测试

```bash
make test              # 单元测试（内存 broker，不需要 Redis）
make test-cov          # 带覆盖率
make test-integration  # 集成测试（需要 Redis）
```

---

## 配置速查

所有配置通过环境变量注入，见 `.env.example`。

| 变量 | 默认 | 说明 |
|------|------|------|
| `BROKER_URL` | `redis://localhost:6379/0` | Redis / RabbitMQ / Sentinel |
| `BACKEND_URL` | `redis://localhost:6379/1` | 结果存储 |
| `WORKER_CONCURRENCY` | `4` | 并发进程数 |
| `WORKER_POOL` | `prefork` | prefork/eventlet/gevent/solo |
| `WORKER_PREFETCH_MULTIPLIER` | `1` | 保持 1，公平调度 |
| `WORKER_MAX_TASKS_PER_CHILD` | `500` | 防内存泄漏 |
| `OBS_LOG_FORMAT` | `json` | 生产 json，本地 console |
| `OBS_PROMETHEUS_PORT` | `9090` | Prometheus 暴露端口 |
| `SECRET_KEY` | 随机 | 生产必须显式设置（`make secret`） |