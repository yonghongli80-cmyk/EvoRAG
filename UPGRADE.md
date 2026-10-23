# RAGv3 生产化升级方案（多实例）

> **状态：Phase 0 分段计时与日志上下文代码补齐（已并入 main），新负载验收待执行；Phase 1 配置面落地，运行时迁移未执行。** 日期：2026-10-23。
> 本文只回答“单机跑通之后，怎样安全地变成可上线的多实例系统”；
> 单机缺陷的修复见 [`docs/superpowers/plans/2026-10-21-unified-remediation.md`](docs/superpowers/plans/2026-10-21-unified-remediation.md)。
> 原则：**不 big-bang**，每个阶段独立可回滚，上一阶段验收不过不进下一阶段。
> 2026-10-22 的实施细节、压测命令与基线档案见
> [`docs/phase0-observability-and-baseline.md`](docs/phase0-observability-and-baseline.md)。

---

## 0. 一页结论

| 项 | 现在 | 目标 |
| --- | --- | --- |
| API | 单 Uvicorn 进程（`start.py`） | 无状态多副本，前面挂 TLS/负载均衡 |
| 向量库 | Qdrant **本地嵌入式**（`QdrantClient(path=...)`） | Qdrant server（gRPC/HTTP），所有副本共享 |
| 权威库 | SQLite（单写连接 + 线程本地读） | PostgreSQL（或同等级关系库） |
| 缓存/锁 | Redis 只用于 LLM 池并发门 | Redis 承担共享缓存、限流、分布式锁 |
| 文件 | 本地上传/版本/结构化数据集目录 | 对象存储（S3/MinIO），DB 只存 key |
| 后台任务 | 素材/演化/备份 worker 在 API 进程内 | 独立 worker 部署，租约 + 幂等 |
| 可观测 | `/ready`、管理员 `/metrics`、执行轨迹与直接分段已实现；有历史 P50/P95/P99 压测档案 | 持续采样、告警、SLO 与部署验收 |
| 安全 | JWT/Cookie、组织 ACL；TLS 靠外部 | TLS、密钥管理、限流、审计、PITR |

**顺序**：Phase 0 观测基线 → Phase 1 Qdrant server → Phase 2 PostgreSQL → Phase 3 Redis 共享缓存/锁 →
Phase 4 对象存储 → Phase 5 多副本与独立 worker → Phase 6 安全与容灾。

**先做 0+1**：Qdrant 本地模式的独占目录锁是当前多进程的硬阻塞，不解决它，Redis、多副本都无从谈起。

---

## 1. 现状与阻塞（代码证据）

| 组件 | 现状 | 阻塞点 | 证据 |
| --- | --- | --- | --- |
| API | 单进程 | 无副本、无优雅摘流 | `start.py` 只 `Popen` 一个 uvicorn |
| Qdrant | `QdrantClient(path=settings.qdrant_path)`，或 `url=settings.qdrant_url`（Phase 1 已加，默认空） | **进程级独占目录**，第二个进程打不开；基准/沙箱必须停服务 | `rag/vector_store.py::_get_client` |
| SQLite | `ConnectionManager`：单写连接 + 线程本地只读连接 | 多副本写会锁；WAL 放共享盘不安全 | `rag/db/connection.py` |
| SQL 方言 | 56 张表、`INSERT OR IGNORE`、`lastrowid`、`PRAGMA`、FTS5 | 不能直接换 Postgres | `rag/repositories/schema.py`、`rag/db/migrations/` |
| Redis | 只给 LLM 池并发门用 | 答案缓存 `cached=None`；改写缓存是进程内 | `rag/llm_pool/router.py`、`rag/pipeline_prepare.py` |
| 文件 | `data/upload`、`data/document_versions`、`data/structured_datasets` | 本机盘；Docker `COPY . .` 有带入风险 | `rag/context.py:27`、`.dockerignore` |
| Worker | 素材/演化/备份在 API 进程内启动 | 多副本会重复跑 | `rag/api.py` lifespan |
| 观测 | `/ready`、`/metrics` 与执行轨迹；rerank/generation 直接测量；日志统一请求/KB/逻辑路由 | 新负载采样与部署验收继续跟踪 | `rag/stage_timing.py`、`rag/logging_config.py`、`docs/phase0-observability-and-baseline.md` |

---

## 2. 目标架构

```
Client ──TLS──> LB / Reverse Proxy
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   API replica  API replica  API replica      (stateless)
        │           │           │
        ├───────────┼───────────┤
        ▼           ▼           ▼
   PostgreSQL     Redis      Qdrant server
   (权威数据)   (缓存/锁/限流)   (向量)
        │
        ▼
   对象存储 (S3/MinIO)  ← 上传 / 文档版本 / 结构化数据集

   独立部署：material-worker / evolution-worker / backup-worker
   观测：metrics + logs + traces + alerts
```

---

## 3. 分阶段实施

### Phase 0：观测基线（1–2 天）

- 2026-10-21 单进程基线（用于后续对照）：RAG `/query` 20 样本 / 5 并发，P50 **10.8s**、
  P95 **17.0s**、P99 **17.9s**（含生成）；轻接口 `/health` P99 928ms、`/knowledge-bases`
  P99 318ms（20 并发 × 200 次）。尾延迟主要来自生成，不是检索准备。
- 2026-10-22 完整 locust 套件（隔离实例，8 用户 / 60s）：73 请求、0 失败，P50 530ms、
  P95 13s、P99 19s、QPS 1.7。混合负载受 LLM 生成主导。
- **[x] 压测档案**：`docs/phase0-observability-and-baseline.md` 第 3 节给出 locust/benchmark
  的固定命令、CSV 百分位列、记录模板与上面两条已记录基线，并明确 benchmark 只输出
  P50/P95、不反推 P99。本次未重跑压测，不补数字。
- **[x] `/ready`**（只读依赖就绪）：SQLite `SELECT 1`、Qdrant 集合可列举、Redis 已配置时 ping；
  未就绪 503 + 组件状态，不泄露异常文本；`/health` 契约保持不变（容器 healthcheck 继续用它）。
- **[x] `/metrics`**（只读，管理员鉴权）：Prometheus 文本格式，输出请求量/延迟（P50/P95/P99）、
  检索阶段耗时、执行轨迹分段、依赖健康、LLM 池并发；不含问题文本/文档名/原始路径/凭据。
  HTTP 计数由 `rag/api.py` 中间件按路由模板采集，检索耗时在 `rag/vector_store.py` 采集。
- **[x] rerank/generation 独立分段（已并入 main）**：在准备与查询执行模块包围真实调用，
  经原 trace JSON 落盘并由 `/metrics` 展示。双重排累计，流式排除消费暂停，arena 计合并流等待。
  保留 `preparation`/`jev_*` 旧分段；preparation 含 rerank，不相加，不用差值冒充 generation。
- **[x] 结构化日志补 `request_id`、`kb_id`、`route`（已并入 main）**：JSON/文本统一三个字段，
  route 为有界逻辑路由；请求、流式逐次推进、线程与 shadow worker 作用域隔离，结束后恢复。
  shadow 只传日志标识，保留业务 ContextVar 隔离；不增加 Prometheus 高基数标签。
- **验收**：同一压测脚本在单机产出稳定 P99（**部分达成**：命令与历史档案已就绪，本轮未重跑压测）；
  检索、准备、重排与最终 RAG 生成可分别观测，新计时代码用离线确定性回归验证。
- **回滚**：只增观测，无行为变化（`/health` 不变，新增端点只读）。

### Phase 1：Qdrant server（2–3 天）

- **[x] 配置**：新增 `RAG_QDRANT_URL`、`RAG_QDRANT_API_KEY`；为空时保持本地 `path` 模式
  （向后兼容）。`config.Settings` 默认空，`.env.example` 已写明两种模式与回滚方式。
- **[x] 代码**：`rag/vector_store.py::_get_client()` 按 URL/Path 二选一构造客户端
  （URL 非空 → `QdrantClient(url=..., api_key=...)`；空 → `QdrantClient(path=...)`），
  所有 `collection` 名称保持不变；本地/远程分支有单测（mock client，不真连）。
- **[x] 部署**：Compose 增加 `qdrant` 服务（`qdrant/qdrant:v1.18.3`、持久卷
  `qdrant_server_data`、健康检查、`expose: 6333` 不发布宿主端口；API key 认证默认关闭，
  启用方式与"空字符串不要注入"的注意事项写在 `.env.example`/Compose 注释里）；
  应用通过 `RAG_QDRANT_URL` 切换，默认空仍连本地目录；旧 `qdrant_data` 卷保留用于回滚。
- **[ ] 迁移**：对每个集合做 snapshot/restore（或 scroll + upsert），迁移后比对点数与抽样召回。
  **未执行——Docker daemon 当前不可用**（无法拉镜像/起容器）。
- **验收**（全部待 Docker 可用后验证）：两个 API 进程可同时检索/写入，不再有目录锁；
  同一问题 top-5 与迁移前一致；`scripts/verify_structured_schema_mirror.py` 与基准可不停服务运行。
- **回滚**：清空 `RAG_QDRANT_URL` 回到本地目录（迁移期间保留旧目录只读副本）。
- **阻塞事实（2026-10-22）**：`docker` daemon 不可用；`docker-compose.yml` 通过了 YAML 解析、
  静态断言与 `docker compose config`（用占位 `REDIS_PASSWORD`/`QDRANT_API_KEY` 做插值校验，
  `config` 不接触 daemon），但镜像 tag 未 pull、容器未启动、健康检查与 API key 未实测、
  迁移与双进程验收均未做。详见 `docs/phase0-observability-and-baseline.md` 第 5 节。

### Phase 2：PostgreSQL（1–2 周，最大的一块）

- 先做**数据访问抽象**：仓储层已存在，但 SQL 仍是 SQLite 方言；逐条盘点
  `INSERT OR IGNORE`、`lastrowid`、`PRAGMA`、`AUTOINCREMENT`、`json_extract`、FTS5。
- Schema：把 `rag/db/migrations/*.sql` 转成可移植 DDL；为 Postgres 建独立迁移目录；
  时间戳/布尔/JSON 类型统一。
- 全文检索：`material_search_fts`（FTS5）改为 Postgres `tsvector` + GIN，或 `pg_trgm`；
  中文分词需要 `zhparser`/`pg_jieba` 或应用层分词（先用应用层，降低运维依赖）。
- 连接层：`ConnectionManager` 抽成协议，新增 Postgres 实现（连接池、事务、只读副本）。
- 迁移路径：双写 → 回填 → 影子读比对 → 切读 → 切写 → 保留 SQLite 只读回滚窗口。
- **验收**：全量 2313 用例在 Postgres 下通过；并发压测无锁等待；备份/恢复演练通过；
  `kb/documents/versions/structured/wiki/evolution` 计数与 SQLite 一致。
- **回滚**：保留 SQLite 快照；切写开关可回退；不接受“迁移脚本单向不可逆”。

### Phase 3：Redis 共享缓存、限流与锁（3–5 天）

- 改写缓存后端接口化：`memory`（默认，单进程）/ `redis`（多副本）；
  key `rag:rewrite:v1:{model}:{sha256(question+history)}`，TTL 1h，Redis 故障降级内存。
- 答案缓存**不能**直接打开：先做 ACL 版本化（用户/知识库/文档版本变化即失效），
  否则会跨权限返回旧答案。
- 限流：登录、上传、查询按用户/IP 限流；LLM 池并发门已有，补全局限流指标。
- 分布式锁：备份、演化 worker、素材清理用 Redis 租约锁，替代进程内单例。
- **验收**：两个副本对同一问题得到同一改写；Redis 停掉后功能降级不报错；
  压测下无跨用户缓存命中。
- **回滚**：`RAG_QUERY_REWRITE_CACHE_BACKEND=memory`，答案缓存保持关闭。

### Phase 4：对象存储（3–5 天）

- 上传、文档版本、结构化数据集改存 S3/MinIO；DB 存 `storage_key`，下载走短时签名 URL。
- 大文件分片/流式上传；删除走标记 + 异步清理；备份改为对象存储快照 + DB dump。
- **验收**：100MB 上传/下载、断点重试、删除回收、跨副本读取一致；
  `data/upload` 等本地目录不再被 `COPY . .` 带进镜像。
- **回滚**：保留本地目录读写适配器，按配置切换。

### Phase 5：多副本与独立 worker（3–5 天）

- API 无状态化：会话、限流、缓存全部外置；`/health`（存活）与 `/ready`（依赖就绪）分离；
  优雅停机（停止接新请求、等待在途、释放连接）。
- worker 独立 Deployment：素材、演化、备份各自副本数为 1，用 Redis 租约锁防重；
  任务幂等（素材 job 已有 lease/attempt 机制）。
- 滚动发布：先起新副本 → 健康 → 摘旧副本；数据库迁移作为独立 Job 先跑。
- **验收**：滚动发布零 5xx；worker 不重复执行；单副本故障不影响查询。
- **回滚**：副本数回 1；worker 回到 API 进程内（保留开关）。

### Phase 6：安全与容灾（持续）

- TLS 终止、Secure/HttpOnly/SameSite Cookie、HSTS；如需跨域再显式加 CORS。
- 密钥管理：`.env` → 密钥管理服务；JWT secret、Redis 密码、模型 key 定期轮换。
- 备份：Postgres PITR + 对象存储版本化；每季度一次隔离恢复演练（现有 `restore-test` 扩展）。
- SLO：查询 P95/P99、错误率、检索命中、拒答率；告警到人。
- **验收**：恢复演练 RTO/RPO 达标；渗透测试/依赖审计通过。

---

## 4. 数据迁移与回滚原则

1. 每次迁移前做**可验证备份**（现有 `scripts/backup.py create/verify`），迁移后跑 `restore-test`。
2. 先双写、再回填、再影子读；影子读差异超过阈值就停，不切。
3. 所有切换用**环境变量开关**，默认保持旧路径；开关回退不需要重新发版。
4. 不接受“只能前进”的迁移脚本；每个阶段保留旧存储的只读副本一个发布周期。
5. 迁移脚本必须幂等、可重跑，并在 CI 的临时库上跑一遍。

---

## 5. 验收矩阵

| 阶段 | 硬指标 | 测试 | 回滚 |
| --- | --- | --- | --- |
| 0 观测 | 直接分段已落地；新负载 P99 与部署采样待验收 | 历史压测档案 + `/ready` `/metrics` + 离线计时回归 | 无 |
| 1 Qdrant | 双进程并发；top-5 一致（**配置面已就绪，运行时未验证：Docker 阻塞**） | 检索回归 + 基准 | 清空 URL |
| 2 Postgres | 2313 用例通过；并发无锁；恢复演练 | 全量 + 迁移比对 | 切回 SQLite |
| 3 Redis | 跨副本改写一致；故障降级 | 多副本集成 + 断网 | backend=memory |
| 4 对象存储 | 大文件/断点/删除 | 集成 + 恢复 | 本地适配器 |
| 5 多副本 | 滚动发布零 5xx；worker 不重复 | 发布演练 | 副本数回 1 |
| 6 安全 | RTO/RPO、审计、TLS | 恢复 + 扫描 | 配置回退 |

---

## 6. 主要风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| Postgres 方言迁移 | 最大工作量，易漏 SQL | 先抽象连接层；逐仓储迁移；影子读比对 |
| FTS5 → Postgres 全文 | 中文召回变化 | 应用层分词 + `tsvector`；保留 BM25 缓存 |
| Qdrant 迁移体积 | 停机窗口 | snapshot/restore；先只读副本；增量补点 |
| 重嵌入成本 | 时间与费用 | 复用现有向量；只补缺失点；分批限速 |
| Redis 一致性 | 缓存串权限 | ACL 版本化后再开答案缓存；改写缓存不含权限数据 |
| 对象存储延迟 | 上传/下载变慢 | 分片、并行、就近区域；本地缓存热点 |
| 多副本后台任务 | 重复执行 | 租约锁 + 幂等 + 单副本 worker |
| 配置膨胀 | 误配 | 每阶段只加必要开关；默认旧行为；启动时校验 |

---

## 7. 明确不做

1. 不把 SQLite 放共享盘跑多副本（WAL 语义不保证）。
2. 不在 Qdrant 仍是本地模式时扩 API 副本（目录锁直接失败）。
3. 不在 ACL 未版本化时打开答案缓存（会跨权限返回旧答案）。
4. 不 big-bang 重写；不一次性替换全部存储。
5. 不为“上线”放宽证据门、拒答口径或层 2 门禁阈值。

---

## 8. 下一步

1. Phase 0 直接 rerank/generation 分段已补齐；日志上下文与最终集成结果见本轮
   `docs/superpowers/plans/2026-10-23-pipeline-observability.md`，仍需部署采样及新的 P99 验收。
2. **在有 Docker daemon 的环境**按 `docs/phase0-observability-and-baseline.md` 4.3 完成 Qdrant
   迁移与双进程验收；验收不过不进 Phase 2。
3. 单机修复（统一修复计划）全部验收通过后，再开 Phase 2；每个阶段单独分支/提交，
   验收记录写入 `docs/development-log.md`。

> 本文是路线图，不是承诺工期；实际排期取决于 Postgres 迁移的盘点结果与压测数据。

---

## 9. 实施记录（2026-10-22，S7：Phase 0 + Phase 1 配置面）

| 项 | 内容 |
| --- | --- |
| 代码 | `rag/routers/system.py`：只读 `/ready`、管理员 `/metrics`（请求量/延迟、检索耗时、执行轨迹分段、依赖健康、LLM 池并发）；`rag/api.py`：按路由模板采集 HTTP 指标（排除 `/metrics` 自身，不落原始路径）；`rag/vector_store.py`：`_get_client()` 的本地/远程分支 + 检索耗时/命中计数；`config.py`：`qdrant_url` / `qdrant_api_key`（默认空） |
| 部署 | `docker-compose.yml`：新增 `qdrant` 服务（`qdrant/qdrant:v1.18.3`、持久卷 `qdrant_server_data`、bash `/dev/tcp` 健康检查、`expose` 不发布宿主端口、应用 `depends_on: service_healthy`）；`.env.example`：两种 Qdrant 模式、API key 与回滚说明 |
| 文档 | 新增 `docs/phase0-observability-and-baseline.md`（端点契约、指标清单/缺口、可复现压测命令、基线与记录模板、Phase 1 切换/回滚/阻塞）；更新本文件 Phase 0/1、验收矩阵与阻塞说明 |
| 测试 | 新增 `tests/test_system_ready_metrics.py`、`tests/test_vector_store_remote.py`、`tests/test_qdrant_deployment.py`；`tests/test_router_parity.py` 基线加入 `/ready`、`/metrics` |
| 验证 | 新增用例 30 passed；`test_api` / 部署与 Compose / `test_vector_store*` / `test_stream_eventloop_nonblocking` / router parity 等回归通过；本次改动文件 `ruff check` 全绿（全树当时仍有其它并行工作流修改中的文件报错） |
| 未做/阻塞 | **Docker daemon 不可用**：未 pull 镜像、未启动容器、未验证健康检查/API key/持久卷、未做 snapshot 迁移、未做双进程验收；Compose 仅静态校验。rerank/generation 独立分段需 `pipeline.py`/`reranker.py`/`generator.py` 钩子（超出 S7 边界）。未提交 Git |


## 10. 2026-10-23：pipeline 与观测代码补齐（已集成 main）

`codex/pipeline-observability` 将入口按准备/Jev/查询/诊断拆分，保留签名、QueryResult、
实时 facade 替换接缝和 ACL 证据门禁；入口 615 行（上限1000）、api 822 行、局部导入150。
直接 rerank/generation、取消部分轨迹与终态防重、三字段日志及 shadow 日志隔离均有离线回归。
首次全量暴露独立准备日志 route 残留，已定位生产边界并修复，保留原失败断言与失败证据。
修复后源码 `af32f43` 全后端 **2879 passed / 3 skipped / 1 既有警告**，退出码0；
JUnit 2882项、0失败/0错误，全树Ruff通过，分项与整分支代码审查通过。
交付提交 `6dcca5c` 已由 `8294753` 快进合并到 main，并按交接四条命令推送 Gitee 和 GitHub。
合并后 104 项定向回归及全树 Ruff 通过；相对全量验收源码仅有文档变更。原服务未重启。
Docker/Qdrant 运行、迁移与双进程、新负载/P99、
PostgreSQL/共享Redis/对象存储/多副本和自动 shadow 采样仍按既有计划待办。
完整口径、结果与本机证据路径见最新交接和 `docs/phase0-observability-and-baseline.md`。
