# RAGv3 部署指南

本文是 RAGv3 **生产部署的权威入口**（本地开发与 Docker 部署的完整步骤以本文为准），生产环境推荐使用 Docker Compose。

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
- 可选设置 `INIT_ADMIN_USERNAME`（仅提升已有账号，空库不能创建，见下）。

空库没有任何可用账号：自注册默认关闭，而 `INIT_ADMIN_USERNAME` 只会把**已存在**的用户提升为管理员，不会创建账号。首次部署应先构建镜像，在启动前创建首个管理员：

```bash
docker compose build rag-app
docker compose run --rm rag-app python scripts/bootstrap_admin.py --username admin
docker compose up -d
```

脚本会提示输入密码且不回显，并按现有口令强度规则拒绝短于 8 位、缺少大写/小写/数字或 `123456` 这类弱口令；用户名已存在时直接失败，不会提权或覆盖。成功创建后该账号 `is_admin=1`，并带有 active 的 `org_people` 行。首次登录后请立即修改密码。

非 Docker 本地运行时直接执行：

```bash
python scripts/bootstrap_admin.py --username admin
```

自动化环境可传 `--password`，但它会进入 shell 历史和进程列表，只应在受控的一次性环境使用，并在首次登录后立刻改密。`INIT_ADMIN_USERNAME` 只能提权既有账号，不能替代本脚本。

启动并检查状态：

```bash
docker compose ps
docker compose logs -f rag-app
```

服务默认暴露在 <http://服务器地址:8000>，健康检查地址为 `/health`。

## 演示组织种子与远程启动护栏（重要）

`rag.org_auth.seed` 会创建 `demo001`–`demo280` 共 280 个演示账号，初始密码统一为 `123456`（其中 `demo001`、`demo002` 是公司管理员）。`start.py` 在绑定非回环地址时，只要发现任一 **active** 组织账号仍使用 `123456`，就会拒绝启动。Docker 通过 `DOCKER_CONTAINER=1` 固定绑定 `0.0.0.0`，因此：

- **生产环境不要运行 seed**；用上面的 `bootstrap_admin.py` 创建唯一管理员。
- 空库首次部署后若又执行 seed，容器**下次重启**会因上述检查退出并进入重启循环；只有改掉所有演示账号口令，或显式设置 `RAG_ALLOW_INITIAL_PASSWORDS_REMOTE=true`，才能远程启动。
- `RAG_ALLOW_INITIAL_PASSWORDS_REMOTE=true` 会关闭唯一的口令护栏。280 个已知口令（含两个管理员）可被网络上任何人直接登录，读取或修改业务数据。**不要在生产环境打开。** 仅限隔离演示环境临时使用；演示结束或改为生产前，必须修改/删除全部演示账号并把该变量恢复为 `false` 后重启。

若确实需要在隔离环境初始化演示组织，可先执行（Docker 内路径为 `/app/data/users.db`）：

```bash
docker compose run --rm rag-app python -m rag.org_auth.seed --db-path /app/data/users.db --metadata-only
```

更多组织权限说明见 `docs/organization-authorization-operations.md`。

## 更新

根目录的 `deploy.sh` 会拉取当前分支、重建应用镜像、做一次到期备份、启动服务，并**在最后验证健康**：

```bash
bash deploy.sh
```

流程与失败边界（F-06，2026-10-22）：

1. 预检：`docker compose version`、`.env` 存在（缺失则退出 1）、`.env` 权限收紧为 `600`、
   端口绑定提示（发布到全部网卡时告警，公网部署应前置 TLS 反代）；
2. `git pull --ff-only` → `docker compose build rag-app`；
3. `python scripts/backup.py create-if-due --trigger deploy`（失败即中止，旧容器继续跑）；
4. `docker compose up -d`；
5. 健康门禁：先探测 `/ready`（S7 落地后存在；503 = 依赖未就绪，继续等），不存在（404）
   时退回 `/health`；要求**一次 200**，否则打印 `docker compose ps`、最近 100 行日志并
   **退出 1**（不再出现"容器起不来也算部署成功"）。

可用环境变量调整门禁：`RAG_PUBLISHED_PORT`（默认 8000）、`RAG_HEALTH_BASE_URL`、
`RAG_HEALTH_RETRIES`（默认 24）、`RAG_HEALTH_INTERVAL_SECONDS`（默认 5）、
`RAG_HEALTH_PATH`（强制指定路径）、`RAG_HEALTH_PROBE_TIMEOUT_SECONDS`、`RAG_COMPOSE_SERVICE`。

也可手动执行：

```bash
git pull --ff-only
docker compose build rag-app
docker compose up -d
```

### 优雅停止（F-04）

容器里 `python -u start.py` 是 **PID 1**（`Dockerfile` 的 exec-form `CMD`），它负责：

- 把 `SIGTERM`/`SIGINT` 转发给 uvicorn 子进程，让 FastAPI lifespan 清理真正跑完；
- 宽限期（`RAG_SHUTDOWN_TIMEOUT`，默认 10 秒）内没退出就 `kill()`，避免容器长期卡在 `stopping`；
- 第二次信号（运维连按 Ctrl+C / compose 再发一次）立即强杀。

对运维的含义：`docker stop` 默认等 10 秒后 SIGKILL，与上面的默认宽限期相同。**如果把宽限期调大，
要同步调大 `docker stop --time` / compose 的 `stop_grace_period`，否则清理仍会被 SIGKILL 打断。**
`/health` 探测超时（`RAG_START_HEALTH_ATTEMPTS`，默认 60 次 × 0.5 秒）时只打印警告、不打印
"服务已就绪"，也不再打开浏览器。

## CI 与代码托管事实（F-08）

- **本仓库采用双远端**：`origin` 是 Gitee（`git@gitee.com:li-yonghong111/ragv3.git`），
  `github` 是 GitHub 镜像（`git@github.com:yonghongli80-cmyk/EvoRAG.git`）。
  2026-10-23 只读核对时，两边 `main` 均为 `55862fb`；后续以真实远端查询为准。
- **仅推送到 Gitee 不会触发 GitHub Actions**。同步推送到 GitHub 后，
  `.github/workflows/ci.yml` 按 GitHub 的 push/pull_request 事件触发；
  `deploy.yml` 监听 `main` 上 CI 完成的 workflow_run 事件，
  部署 job 再通过 `conclusion == 'success'` 条件决定是否执行。
  仓库已同步不等于 CI 或部署已验收：Actions 权限、部署 secrets 和实际运行结果仍需查看 GitHub 的配置及日志。
- 后续按交接文档分别推送两个远端的 `main`，新增标签也同步两边。
  Gitee 侧若需要独立自动门禁，可接入 Gitee Go 或自建 runner，命令与下面"本地等价命令"一致。
  服务器手动部署入口仍为 `bash deploy.sh`，包含同一套备份门禁与健康门禁。

本地等价命令（与 CI 各 job 一一对应）：

```bash
# backend / runtime-lock：锁文件一致性 + 依赖自检 + 导入冒烟
python -m venv .venv-clean
.venv-clean/bin/pip install -r requirements.lock      # Windows: .venv-clean\Scripts\pip.exe
.venv-clean/bin/pip check
.venv-clean/bin/python -c "import rag.api"
.venv-clean/bin/python -m pytest tests/test_dependency_lock.py -q

# backend：开发锁 + 全量门禁
pip install -r requirements-dev.lock
pip check
ruff check .
pytest tests/ -q

# docker-build：镜像冒烟（Docker daemon 不可用时跳过）
docker build --tag ragv3:ci .
docker run --rm ragv3:ci python -c "import rag.api"
```

CI 固定的行为（`tests/test_ci_quality_gates.py` 会因此失败）：

- 所有 action 用 **commit SHA** 固定，且同一行要有版本注释（缺注释测试会失败）；
- 每个 job 必须有 `timeout-minutes`，workflow 顶层 `permissions` 只有 `contents: read`；
- 必须有一条"只装 `requirements.lock`"的 job，并在其中 `pip check` + `import rag.api`；
- `docker build` 冒烟存在；runner 没有 Docker daemon 时打印 `::warning` 后跳过（可用仓库变量
  `RUN_DOCKER_BUILD=false` 关掉整个 job）。

## Render 模板已移除（F-09）

`render.yaml` 已删除。原因（不是"暂时不用"，而是它和新拓扑不兼容）：

1. 旧模板是单 web 服务 + 1 GB 磁盘，**没有挂载 `qdrant_data`**，每次部署都会丢失向量索引
   （2026-06-26 审查 I48 已记录）；
2. 生产拓扑是 **Compose 单机**：`rag-app` + `redis`（必填 `REDIS_PASSWORD`）+ 本地 Qdrant
   目录卷，Render 的免费层磁盘/私有网络无法等价表达，保留模板只会误导部署者；
3. 若将来改用托管平台，应按 `docker-compose.yml` 的卷与密钥清单重写模板，并同步
   `tests/test_redis_deployment_security.py` 的断言（它现在只校验 Compose/.env 这一条路径）。

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
