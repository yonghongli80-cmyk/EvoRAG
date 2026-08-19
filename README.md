# RAGv3

面向团队知识管理的 RAG 系统，包含文档入库与版本管理、权限感知检索、回答可靠性分析、资料治理和 Excel 在线编辑。

## 快速开始

要求：Python 3.12、Node.js 22（Docker 部署只需 Docker Compose）。

### Docker

```bash
cp .env.example .env
# 编辑 .env，至少填写模型 API Key 和 REDIS_PASSWORD
docker compose up -d --build
```

访问 <http://localhost:8000>。完整说明见 [docs/deployment.md](docs/deployment.md)。

文档的当前/历史状态见 [docs/README.md](docs/README.md)；旧计划中的空复选框不直接代表功能尚未实现。

### 本地开发

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

cd frontend
npm ci
npm run build
cd ..

cp .env.example .env
python start.py
```

本地服务默认位于 <http://localhost:8080>。前端开发服务器可在 `frontend` 目录运行 `npm run dev`。

## 验证

```bash
pytest -q
ruff check . --select E9,F63,F7,F82

cd frontend
npm test
npm run build
```

## 依赖与目录

- `pyproject.toml`：Python 直接依赖和开发工具配置的唯一来源。
- `requirements.lock`：CI 与容器使用的可复现完整锁文件。
- `rag/`：后端与 RAG 核心逻辑。
- `frontend/src/`：Vue 3 前端源码。
- `static/`：由 Vite 生成、供 FastAPI 提供的生产前端。
- `prompts/`：提示词配置。
- `tests/`：后端测试。
- `data/`、`qdrant_data/`：运行时数据；不要提交用户数据或密钥。

## 安全提示

生产环境必须保持 `RAG_AUTH_ENABLED=true`，使用强随机 `REDIS_PASSWORD`，并通过反向代理启用 HTTPS。`.env`、数据库、索引和上传数据均已从版本控制及 Docker 构建上下文中排除。
