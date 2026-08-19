# RAGv3 工程基础设施优化方案

> 生成日期：2026-06-03
> 状态：核心内容已完成并归档（2026-08-19 复核）
>
> `pyproject.toml`、集中日志与请求 ID、Ruff/pre-commit、非 root Docker、Docker Compose、CI/CD 和依赖锁定均已落地。本文保留原始方案用于追溯；全异步改造和更深层性能优化属于可选的独立工作，不是当前上线阻塞项。

---

## 目录

1. [pyproject.toml](#一pyprojecttoml)
2. [日志系统](#二日志系统)
3. [Lint / Format](#三lint--format)
4. [Docker](#四docker)
5. [CI/CD](#五cicd)
6. [异步并发优化](#六异步并发优化)
7. [落地顺序](#落地顺序)
8. [文件变更清单](#文件变更清单)

---

## 一、pyproject.toml

### 为什么

当前项目只有 `requirements.txt`，没有项目元数据、没有开发依赖分离、没有工具配置入口。`pyproject.toml` 是 Python 项目的"身份证"，所有现代工具（ruff、mypy、pytest、build）都从这里读配置。

### 怎么做

创建 `pyproject.toml`，内容分四块：

**① 项目元数据**

```toml
[project]
name = "rag-knowledge-base"
version = "3.0.0"
description = "基于检索增强生成的智能知识库系统"
requires-python = ">=3.12"
```

**② 依赖管理**（替代 requirements.txt）

- 运行依赖放在 `[project.dependencies]`（从现有 requirements.txt 迁移）
- 开发依赖放在 `[project.optional-dependencies].dev`：pytest、ruff、mypy、pre-commit
- `pip install -e .` 装运行依赖，`pip install -e .[dev]` 装开发依赖

**③ 工具配置**（统一入口）

```toml
[tool.ruff]                  # Lint + Format
[tool.pytest.ini_options]    # 测试配置
[tool.mypy]                  # 类型检查
```

**④ 构建配置**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.backends"
```

### 注意事项

- `requirements.txt` 保留一段时间作为兼容，但标注"已迁移至 pyproject.toml"
- `opendataloader-pdf` 依赖 Java 17，单独建一个 `pdf` extra，不需要 PDF 功能的人不用装 Java
- 版本约束从 `requirements.txt` 原样搬过来即可

---

## 二、日志系统

### 为什么

当前状态：3 个文件用 `logging.getLogger()`，5 个文件用 `print()`，没有根 logger 配置，没有日志格式、级别控制、请求 ID 关联。生产环境出问题时 `print()` 输出无法被 ELK/Loki/Grafana 采集。

### 怎么做

**① 新建 `rag/logging_config.py`**

```
rag/logging_config.py
├── setup_logging(level="INFO", json_format=False)
│   ├── 创建 StreamHandler（输出到 stderr）
│   ├── 格式：%(asctime)s | %(levelname)-7s | %(name)s | %(message)s
│   ├── json_format=True 时输出 JSON（给 Docker/生产用）
│   └── 设置根 logger 级别
└── get_request_id() —— 从 contextvars 获取当前请求 ID
```

**② 在 `rag/api.py` 启动时调用**

在 FastAPI 的 `startup` 事件中调用 `setup_logging()`：

- 开发环境：`json_format=False`（人类可读输出）
- 生产环境：通过环境变量 `RAG_LOG_JSON=1` 切换为 JSON 格式
- 日志级别：通过环境变量 `RAG_LOG_LEVEL` 控制（默认 INFO）

**③ 添加请求 ID 中间件**

1. 从请求头读取 `X-Request-ID`（如有），否则生成 UUID
2. 存入 `contextvars.ContextVar`
3. 响应头中返回 `X-Request-ID`
4. 自定义 log formatter 从 contextvars 读取 request_id 并注入日志

**④ 全局 print → logger 替换**

| 文件 | 当前 | 改为 |
|---|---|---|
| `rag/api.py` | `print()` 3 处 | `logger.info()` |
| `rag/folder_indexer.py` | `print()` 1 处 | `logger.warning()` |
| `start.py` | `print()` 多处 | **保留**（CLI 启动脚本，print 合理） |

**⑤ 日志级别规划**

| 场景 | 级别 |
|---|---|
| 启动索引、查询路由、缓存命中 | INFO |
| Prompt injection 检测、reranker 降级 | WARNING |
| Pipeline 刷新失败、外部 API 异常 | ERROR |
| 详细的 LLM 调用参数、retrieval 结果 | DEBUG |

### 注意事项

- `start.py` 是 CLI 启动脚本，`print()` 面向用户终端输出，不需要改 logging
- 不要引入 `structlog` 等第三方库，stdlib `logging` 完全够用
- `logging_config.py` 要在所有模块 `import` 之前调用

---

## 三、Lint / Format

### 为什么

当前没有任何 lint/format 配置。多人协作时代码风格会逐渐不一致。

### 怎么做

**① 选择 Ruff**

Ruff 是目前 Python 社区标准：速度极快（Rust 写的），兼容 flake8 + isort + black，一个工具替代三个。

**② 在 `pyproject.toml` 中配置**

```toml
[tool.ruff]
target-version = "py312"
line-length = 120

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort（import 排序）
    "B",    # flake8-bugbear（常见 bug）
    "UP",   # pyupgrade（现代化语法）
    "SIM",  # flake8-simplify
]
ignore = [
    "E501",  # 行太长（交给 formatter 处理）
]

[tool.ruff.lint.isort]
known-first-party = ["rag", "config"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
```

**③ pre-commit hook**

创建 `.pre-commit-config.yaml`：

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

**④ 首次全量格式化**

```bash
ruff check --fix .
ruff format .
```

### 注意事项

- 首次格式化会产生大量 diff，建议单独一个 commit：`style: ruff format`
- `E501` ignore 掉，让 formatter 自动处理
- 不需要配 black、isort、flake8——ruff 全部替代了
- mypy 可以后续再加

---

## 四、Docker

### 为什么

当前项目的运行依赖：Python 3.12 + Java 17（PDF 解析）+ Qdrant 数据目录 + SQLite + .env 配置。新人 clone 下来要装 Python、装 Java、配 .env、建目录，步骤多且容易出错。

### 怎么做

**① Dockerfile（多阶段构建）**

```dockerfile
# ── 基础镜像 ──
FROM python:3.12-slim AS base
  # 安装 OpenJDK 17 JRE（opendataloader-pdf 需要）
  # 创建非 root 用户 rag
  # 设置工作目录 /app

# ── 依赖安装层（利用 Docker 缓存）──
FROM base AS deps
  # 复制 pyproject.toml
  # pip install --no-cache-dir .
  # 这一层只在依赖变化时重建

# ── 生产镜像 ──
FROM deps AS production
  # 复制整个项目
  # 创建 /app/data/upload, /app/qdrant_data, /app/history 目录
  # 暴露 8000 端口
  # HEALTHCHECK curl http://localhost:8000/health
  # CMD ["uvicorn", "rag.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

**② docker-compose.yml（开发环境）**

```yaml
services:
  rag:
    build:
      context: .
      target: deps
    volumes:
      - .:/app
      - ./data:/app/data
      - ./qdrant_data:/app/qdrant_data
    ports:
      - "8000:8000"
    env_file: .env
    environment:
      - RAG_LOG_LEVEL=DEBUG
    command: uvicorn rag.api:app --host 0.0.0.0 --port 8000 --reload
```

**③ docker-compose.prod.yml（生产环境）**

```yaml
services:
  rag:
    build:
      context: .
      target: production
    volumes:
      - ./data:/app/data
      - ./qdrant_data:/app/qdrant_data
      - ./history:/app/history
    ports:
      - "8000:8000"
    env_file: .env
    environment:
      - RAG_LOG_JSON=1
      - RAG_LOG_LEVEL=INFO
    restart: unless-stopped
```

**④ .dockerignore**

```
.git
__pycache__
*.pyc
.env
qdrant_data/
history/
data/upload/
*.db
.vscode/
.pytest_cache/
```

### 注意事项

- `.env` 不要 COPY 进镜像，用 `env_file` 或运行时注入
- Qdrant 数据目录必须挂载 volume，否则容器重建数据丢失
- Java JRE 只需要 runtime，不需要 JDK（节省约 200MB）
- `--reload` 只在开发环境用，生产环境去掉

---

## 五、CI/CD

### 为什么

当前没有自动化：提交代码不跑测试，不检查风格，不自动部署。

### 怎么做

**① CI 流水线：`.github/workflows/ci.yml`**

每次 push 或 PR 到 main 时触发：

```yaml
Jobs:
  lint:
    - checkout
    - setup python 3.12
    - pip install ruff
    - ruff check .
    - ruff format --check .

  test:
    - checkout
    - setup python 3.12
    - pip install -e .[dev]
    - pytest tests/ -v --tb=short
```

两个 job 并行跑，任一失败则 CI 红。

**② CD 流水线：`.github/workflows/deploy.yml`**

push 到 main 且 CI 通过时触发：

```yaml
Jobs:
  build:
    - checkout
    - docker build -t rag-kb:${{ github.sha }} .
    - docker tag ... latest

  deploy:（根据部署目标选一个）
    - 方案 A：推送到 Docker Hub / 阿里云 ACR
    - 方案 B：SSH 到服务器执行 docker pull + restart
    - 方案 C：Kubernetes kubectl apply
```

**③ Dependabot（可选）**

创建 `.github/dependabot.yml`，自动检查依赖更新并提 PR。

### 注意事项

- CI 中的 pytest **不需要真实 API key**——测试已 mock
- CI 中**不需要 Java**——PDF 加载测试用 mock
- Secrets 放在 GitHub repo 的 Settings → Secrets 中
- 测试慢（>2 分钟）可加 `pytest-xdist` 并行

---

## 六、异步并发优化

### 当前状态

上一轮修复后 `/query` 已经是 `async def` + `asyncio.to_thread()`，并发基本解决。还可以进一步优化：

**① 读写锁替换全局 pipeline 锁**

`api.py` 中的 `_pipeline_lock` 当前是 `threading.Lock()`，但你已经有了 `ReadWriteLock`。改为：

- 查询路径用 `read()` — 多个查询并发
- 索引路径用 `write()` — 独占更新 pipeline 引用

```python
from rag.concurrency import ReadWriteLock
_pipeline_lock = ReadWriteLock()

# 查询时
with _pipeline_lock.read():
    current_pipeline = pipeline

# 索引时
with _pipeline_lock.write():
    pipeline = RAGPipeline(...)
```

**② Embedding 缓存**

`retriever.py` 每次查询都调用 `embed([query])`，同一个问题问两遍会重复调用。可以在 `embed()` 内部加 LRU 缓存：

```python
from functools import lru_cache

@lru_cache(maxsize=256)
def _embed_single(text: str) -> list[float]:
    ...
```

注意：参数必须是 hashable，需要处理 list → tuple 转换。

**③ BM25 增量更新**（可选，文档量大时才需要）

当前 BM25 全量构建。文档量 >10000 chunks 时可以考虑 pickle 持久化 + 增量添加。

---

## 落地顺序

| 阶段 | 内容 | 工作量 | 依赖 |
|---|---|---|---|
| **阶段 1** | pyproject.toml + ruff 配置 + 首次格式化 | 半天 | 无 |
| **阶段 2** | 日志系统（logging_config.py + 请求 ID + print 替换） | 1 天 | 无 |
| **阶段 3** | Docker（Dockerfile + docker-compose） | 1 天 | 阶段 1 |
| **阶段 4** | CI/CD（GitHub Actions） | 半天 | 阶段 1 + 3 |
| **阶段 5** | 异步优化（读写锁替换 + embedding 缓存） | 半天 | 无 |

阶段 1-2 可以并行做，阶段 3-4 有依赖，阶段 5 独立。

---

## 文件变更清单

| 操作 | 文件 | 说明 |
|---|---|---|
| 新建 | `pyproject.toml` | 项目元数据 + 依赖 + 工具配置 |
| 新建 | `rag/logging_config.py` | 集中式日志配置 |
| 新建 | `Dockerfile` | 多阶段构建 |
| 新建 | `docker-compose.yml` | 开发环境 |
| 新建 | `docker-compose.prod.yml` | 生产环境 |
| 新建 | `.dockerignore` | Docker 构建排除 |
| 新建 | `.pre-commit-config.yaml` | pre-commit hooks |
| 新建 | `.github/workflows/ci.yml` | CI 流水线 |
| 新建 | `.github/workflows/deploy.yml` | CD 流水线 |
| 修改 | `rag/api.py` | startup 调用 setup_logging + pipeline 锁改读写锁 |
| 修改 | `rag/folder_indexer.py` | print → logger |
| 修改 | `rag/retriever.py` | embedding 缓存（可选） |
| 删除 | `requirements.txt` | 迁移后可删除或保留兼容 |
