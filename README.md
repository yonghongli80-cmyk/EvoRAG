# RAGv3

面向团队知识管理的 RAG 系统，包含文档入库与版本管理、权限感知检索、回答可靠性分析、资料治理和 Excel 在线编辑。

## 快速开始

要求：Python 3.12、Node.js 22（CI 与本地验证使用 22；`Dockerfile` 前端构建阶段使用 `node:20-alpine`。Docker 部署只需 Docker Compose）。

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
ruff check .          # 与 CI 一致；不要只跑 E9/F63/F7/F82 子集

cd frontend
npm ci
npm run test
npm run build
npm audit --omit=dev --audit-level=high
```

## 依赖与目录

- `pyproject.toml`：Python 直接依赖和开发工具配置的唯一来源。
- `requirements.lock`：CI 与容器使用的可复现完整锁文件。
- `rag/`：后端与 RAG 核心逻辑。
- `frontend/src/`：Vue 3 前端源码。
- `static/`：由 Vite 生成、供 FastAPI 提供的生产前端。
- `prompts/`：提示词配置。
- `tests/`：后端测试。
- [UPGRADE.md](UPGRADE.md)：生产多实例升级路线（Qdrant server / PostgreSQL / Redis / 对象存储 / 可观测性）。
- `data/`、`qdrant_data/`：运行时数据；不要提交用户数据或密钥。

自动备份默认每 7 天执行并保留最近 8 份；命令、排除项和隔离恢复演练见[部署指南的“数据与备份”章节](docs/deployment.md#数据与备份)。

## 组织知识库权限

知识库按公司、组织和敏感领域归属，文档继承所在知识库权限；文件页面仍保留个人私密、共享、公开三级权限。固定组织数据初始化、旧知识库迁移、账号停用和回滚方法见 [组织知识库权限运维手册](docs/organization-authorization-operations.md)。

## 安全提示

生产环境必须保持 `RAG_AUTH_ENABLED=true`，使用强随机 `REDIS_PASSWORD`，并通过反向代理启用 HTTPS。`.env`、SQLite 数据库与 Qdrant 索引已从版本控制和 Docker 构建上下文中排除；用户上传目录 `data/upload/` 未被 `.gitignore` 或 `.dockerignore` 排除，`data/document_versions/`、`data/kb_files/` 仅从版本控制排除、未从 Docker 构建上下文排除——打镜像前请自行清理本机上传件。
