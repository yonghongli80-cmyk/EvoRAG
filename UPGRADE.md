# RAGv3 生产化升级方案（多实例）

> **状态：规划稿，未实施。** 日期：2026-10-21。
> 本文只回答“单机跑通之后，怎样安全地变成可上线的多实例系统”；
> 单机缺陷的修复见 [`docs/superpowers/plans/2026-10-21-unified-remediation.md`](docs/superpowers/plans/2026-10-21-unified-remediation.md)。
> 原则：**不 big-bang**，每个阶段独立可回滚，上一阶段验收不过不进下一阶段。

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
| 可观测 | 基准有 P50/P95，无 P99/指标/追踪 | P50/P95/P99、指标、追踪、告警、SLO |
| 安全 | JWT/Cookie、组织 ACL；TLS 靠外部 | TLS、密钥管理、限流、审计、PITR |

**顺序**：Phase 0 观测基线 → Phase 1 Qdrant server → Phase 2 PostgreSQL → Phase 3 Redis 共享缓存/锁 →
Phase 4 对象存储 → Phase 5 多副本与独立 worker → Phase 6 安全与容灾。

**先做 0+1**：Qdrant 本地模式的独占目录锁是当前多进程的硬阻塞，不解决它，Redis、多副本都无从谈起。

---

## 1. 现状与阻塞（代码证据）

| 组件 | 现状 | 阻塞点 | 证据 |
| --- | --- | --- | --- |
| API | 单进程 | 无副本、无优雅摘流 | `start.py` 只 `Popen` 一个 uvicorn |
| Qdrant | `QdrantClient(path=settings.qdrant_path)` | **进程级独占目录**，第二个进程打不开；基准/沙箱必须停服务 | `rag/vector_store.py:37-41` |
| SQLite | `ConnectionManager`：单写连接 + 线程本地只读连接 | 多副本写会锁；WAL 放共享盘不安全 | `rag/db/connection.py` |
| SQL 方言 | 56 张表、`INSERT OR IGNORE`、`lastrowid`、`PRAGMA`、FTS5 | 不能直接换 Postgres | `rag/repositories/schema.py`、`rag/db/migrations/` |
| Redis | 只给 LLM 池并发门用 | 答案缓存 `cached=None`；改写缓存是进程内 | `rag/llm_pool/router.py:192`、`pipeline.py:501` |
| 文件 | `data/upload`、`data/document_versions`、`data/structured_datasets` | 本机盘；Docker `COPY . .` 有带入风险 | `rag/context.py:27`、`.dockerignore` |
| Worker | 素材/演化/备份在 API 进程内启动 | 多副本会重复跑 | `rag/api.py` lifespan |
| 观测 | 基准算 P50/P95；`locustfile.py` 有 P99 口径 | 无重构后报告、无指标端点 | `scripts/benchmark_jev_retrieval.py:357` |

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

- 给 `scripts/locustfile.py` 补一次可复现的压测档案：固定用户数、思考时间、目标 KB、`--user-id`；
  产出 P50/P95/P99、错误率、RPS。
- 增加只读 `/metrics`（或 OpenTelemetry exporter）：请求量/延迟、检索/重排/生成分段耗时、
  Qdrant/SQLite/Redis 健康、LLM 池排队。
- 结构化日志补 `request_id`、`kb_id`、`route`、`rewrite_dual_rerank`（已有）。
- **验收**：同一压测脚本在单机产出稳定 P99；能看到“慢在检索、重排还是生成”。
- **回滚**：只增观测，无行为变化。

### Phase 1：Qdrant server（2–3 天）

- 配置：新增 `RAG_QDRANT_URL`、`RAG_QDRANT_API_KEY`；为空时保持本地 `path` 模式（向后兼容）。
- 代码：`rag/vector_store.py` 按 URL/Path 二选一构造客户端；所有 `collection` 名称保持不变。
- 部署：Compose 增加 `qdrant` 服务（持久卷、健康检查、只对内网暴露），应用改连 `http://qdrant:6333`。
- 迁移：对每个集合做 snapshot/restore（或 scroll + upsert），迁移后比对点数与抽样召回。
- **验收**：两个 API 进程可同时检索/写入，不再有目录锁；同一问题 top-5 与迁移前一致；
  `scripts/verify_structured_schema_mirror.py` 与基准可不停服务运行。
- **回滚**：清空 `RAG_QDRANT_URL` 回到本地目录（迁移期间保留旧目录只读副本）。

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
| 0 观测 | P99 可复现；分段耗时可见 | 压测档案 | 无 |
| 1 Qdrant | 双进程并发；top-5 一致 | 检索回归 + 基准 | 清空 URL |
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

1. **先做 Phase 0 + 1**：补 P99 压测档案、加 `/metrics`、把 Qdrant 切到 server 模式并在双进程下验证。
2. 单机修复（统一修复计划）全部验收通过后，再开 Phase 2。
3. 每个阶段单独分支/提交，验收记录写入 `docs/development-log.md`。

> 本文是路线图，不是承诺工期；实际排期取决于 Postgres 迁移的盘点结果与压测数据。
