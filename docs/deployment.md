# RAGv3 部署指南

本文是项目唯一的部署入口。生产环境推荐使用 Docker Compose。

## 前置条件

- Docker Engine 24+ 与 Docker Compose v2
- 至少 2 核 CPU、4 GB 内存和 20 GB 可用磁盘
- 可用的生成模型与嵌入模型 API Key

## 首次部署

```bash
git clone <仓库地址> RAGv3
cd RAGv3
cp .env.example .env
```

编辑 `.env`：

- 填写 `RAG_DEEPSEEK_API_KEY` 和 `RAG_BAILIAN_API_KEY`；
- 保持 `RAG_AUTH_ENABLED=true`；
- 为 `REDIS_PASSWORD` 设置 URL-safe 的强随机值；
- 可选设置 `INIT_ADMIN_USERNAME`。

启动并检查状态：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f rag-app
```

服务默认暴露在 <http://服务器地址:8000>，健康检查地址为 `/health`。

## 更新

根目录的 `deploy.sh` 会拉取当前分支、重建应用镜像并重启服务：

```bash
bash deploy.sh
```

也可手动执行：

```bash
git pull --ff-only
docker compose build rag-app
docker compose up -d
```

## 数据与备份

Compose 使用四个命名卷：

- `rag_data`：用户、文档、审计与结构化数据；
- `qdrant_data`：向量索引；
- `redis_data`：Redis 持久化数据。
- `backup_data`：经过完整性验证的权威数据备份。

应用默认每 7 天创建一次备份并保留最近 8 份。非 Docker 运行时，默认写入项目同级的 `RAGv3-backups`；Docker 使用独立的 `backup_data` 卷。备份未加密，请像保护生产数据库一样限制目录或卷的访问权限。

备份包含 `users.db`，并在存在时包含 `analysis.db`、`memory.db`，以及 `upload`、`kb_files`、文档版本和结构化数据文件。它不包含 `.env`、API 密钥、缓存、BM25、嵌入缓存，也不包含 Qdrant。恢复演练完成后仍需重新生成向量索引。

常用命令（输出均为不含绝对路径的 JSON）：

```bash
python scripts/backup.py create --trigger manual
python scripts/backup.py create-if-due --trigger scheduled
python scripts/backup.py list
python scripts/backup.py verify <backup-id>
python scripts/backup.py restore-test <backup-id> <隔离目录>
```

`restore-test` 只把备份复制到指定隔离目录并再次校验，绝不会替换生产数据库或生产文件。当前项目刻意不提供“一键覆盖生产”的恢复命令。

`deploy.sh` 和 GitHub 部署流程会在新镜像构建完成、旧容器停止之前运行 `create-if-due --trigger deploy`。备份失败会终止部署，因此旧容器继续运行。不要把 `.env`、运行时数据库或备份负载提交到 Git，也不要放入 Docker 构建上下文。

删除 Compose 容器不会删除命名卷；执行 `docker compose down -v` 会永久删除业务数据和备份，生产环境不要使用。备份卷仍应纳入宿主机级容灾策略。

## HTTPS 与公网访问

公网部署应在应用前配置 Nginx、Caddy 或云负载均衡器，终止 TLS 后转发到 `127.0.0.1:8000`。防火墙只开放 SSH、HTTP 和 HTTPS；不要直接暴露 Redis。

## 非 Docker 开发运行

```bash
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
pip install -e ".[dev]"

cd frontend
npm ci
npm run build
cd ..

cp .env.example .env
python start.py
```

本地默认端口为 8080。可用 `PORT` 环境变量覆盖端口。

## 常用排查

```bash
docker compose ps
docker compose logs --tail=200 rag-app
docker compose logs --tail=100 redis
curl -f http://localhost:8000/health
```

如果 Redis 未启动，先确认 `.env` 中 `REDIS_PASSWORD` 非空且不含需要 URL 转义的字符。如果应用容器不健康，检查模型密钥、卷权限和端口占用。
