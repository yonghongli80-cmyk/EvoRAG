# ADR：UserDB 数据访问层与 PostgreSQL 迁移准备

- **状态：** 提案；未实现、未连接 PostgreSQL、未运行迁移或验证。
- **日期：** 2026-10-25
- **决策范围：** `UserDB`、SQLite 连接/事务、应用 schema、repositories、材料 FTS，以及 SQL 与文件/索引跨存储的切换和恢复。
- **前置顺序：** 本 ADR 是 Task 3 shared state / 多 API 副本的关系库前置条件。完成 PostgreSQL 等价和迁移门禁前，不把共享缓存、独立 worker 或多 API 副本作为可部署架构。

## 现状与直接证据

### SQLite 专属入口

| 事实 | 源码位置 |
|---|---|
| `UserDB` 默认读取 `settings.users_db_path` 并构造 `ConnectionManager`；随后创建核心/材料 schema，运行 `rag.db_migrations`、知识任务类型迁移和 org schema，再组装七类 repository。它是 SQLite facade，不是通用数据库接口。 | [`rag/user_db.py`](../../rag/user_db.py#L47)、[`rag/user_db.py`](../../rag/user_db.py#L52)、[`rag/user_db.py`](../../rag/user_db.py#L77)、[`rag/user_db.py`](../../rag/user_db.py#L85)、[`rag/user_db.py`](../../rag/user_db.py#L120) |
| `ConnectionManager` 持有一个共享写连接及 `RLock`；读连接按线程缓存并设 `query_only=ON`；所有连接设 WAL、busy timeout、foreign keys。 | [`rag/db/connection.py`](../../rag/db/connection.py#L14)、[`rag/db/connection.py`](../../rag/db/connection.py#L24)、[`rag/db/connection.py`](../../rag/db/connection.py#L44)、[`rag/db/connection.py`](../../rag/db/connection.py#L58)、[`rag/db/connection.py`](../../rag/db/connection.py#L73) |
| 管理事务在嵌套时复用现有连接而不建 savepoint；否则可选 `BEGIN IMMEDIATE`，成功 commit，异常 rollback。直接 `write_connection()` 使用者可自行 `commit()`。 | [`rag/db/connection.py`](../../rag/db/connection.py#L115)、[`rag/db/connection.py`](../../rag/db/connection.py#L128)、[`rag/db/connection.py`](../../rag/db/connection.py#L132)、[`rag/db/connection.py`](../../rag/db/connection.py#L138) |
| 当前运行时 migration 由 `user_version` 整数推进，并对版本脚本逐个 `executescript` 后设版本并 commit；核心 baseline 由 `create_tables` 提供。`rag/db/migrator.py` 明确标记为 legacy、非 runtime。 | [`rag/db_migrations.py`](../../rag/db_migrations.py#L9)、[`rag/db_migrations.py`](../../rag/db_migrations.py#L113)、[`rag/db_migrations.py`](../../rag/db_migrations.py#L120)、[`rag/db/migrator.py`](../../rag/db/migrator.py#L1)、[`rag/db/migrator.py`](../../rag/db/migrator.py#L25) |
| schema 使用 SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`、SQLite 时间默认值、`PRAGMA`、`executescript`、部分索引及 SQLite SQL。示例：文档权限/版本/外键定义在核心 schema，org schema 用部分唯一索引。 | [`rag/db/migrations/001_initial_schema.sql`](../../rag/db/migrations/001_initial_schema.sql#L100)、[`rag/db/migrations/001_initial_schema.sql`](../../rag/db/migrations/001_initial_schema.sql#L163)、[`rag/db/migrations/002_org_permissions.sql`](../../rag/db/migrations/002_org_permissions.sql#L23)、[`rag/db_migrations.py`](../../rag/db_migrations.py#L124) |

### Repository 与事务边界

- `UserDB` facade 转发到 `UserRepository`、`DocRepository`、`ChatRepository`、`SpreadsheetRepository`、`MaterialRepository`、`AnalysisRepository` 和 `KBLifecycleRepository`；各仓储接收同一个 connection manager。[`rag/user_db.py`](../../rag/user_db.py#L85)
- 部分写路径已把状态检查、CAS、队列/幂等记录放在一个 `cm.transaction(immediate=True)` 中。例如上传 finalize 检查 staging 路径和 hash，再写新路径并 enqueue；归档确认在单一事务里激活父子版本、写 FTS、manifest、幂等结果并 enqueue。[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L140)、[`rag/materials/archives.py`](../../rag/materials/archives.py#L675)、[`rag/materials/archives.py`](../../rag/materials/archives.py#L727)
- 其他仓储路径直接 `write_connection()` 后 `conn.commit()`；`lastrowid` 被广泛作为新行 ID 返回。须在迁移前收敛所有写方法事务所有权，避免 adapter 在 repository 内部 commit 或一个 UnitOfWork 被提前提交。[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L292)、[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L307)、[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L427)、[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L439)、[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L147)
- 版本激活通过同事务更新旧/新版本状态、`document_permissions.current_version_id`、兼容 `kb_documents` 指针；但当前该指针更新只按主键匹配，没有 `expected current_version_id` 条件，因而 PG 改造需增加明确的 expected-version CAS 并把冲突作为领域结果，而不能声称所有当前激活已经具备 CAS。[`rag/repositories/doc_repo.py`](../../rag/repositories/doc_repo.py#L522)、[`rag/repositories/doc_repo.py`](../../rag/repositories/doc_repo.py#L532)、[`rag/repositories/doc_repo.py`](../../rag/repositories/doc_repo.py#L540)

### FTS、ACL 与外部数据

- 材料搜索表是普通 FTS5 virtual table，字段为未索引的 `document_id`、`version_id` 与 `filename/logical_path/document_type/tags/extracted_text`；schema 未指定 tokenizer，SQLite FTS5 默认 tokenizer 行为即现状。不要把 FTS5 内部 shadow tables 当应用表或拿旧的“56表”口径当本次盘点。[`rag/repositories/schema.py`](../../rag/repositories/schema.py#L849)
- 查询先用带引号的 token 构造 FTS 表达式，再筛当前版本及未删除权限记录；`MaterialLocator` 先计算 authorized visible docs（组织授权、legacy owner/public/shared 和 admin），再把 FTS 命中与可见集合相交，semantic retriever 也只收到授权版本 ID。迁移后 ACL-first 行为必须保持，不得以 FTS 命中替代授权检查。[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L36)、[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L446)、[`rag/materials/locator.py`](../../rag/materials/locator.py#L43)、[`rag/materials/locator.py`](../../rag/materials/locator.py#L166)、[`rag/materials/locator.py`](../../rag/materials/locator.py#L176)
- ACL 约束包括显式 `platform admin` 判断、legacy KB owner/admin 规则、组织 KB 授权，以及无组织 policy 时对非 owner fail closed。[`rag/api_security.py`](../../rag/api_security.py#L155)、[`rag/api_security.py`](../../rag/api_security.py#L161)、[`rag/api_security.py`](../../rag/api_security.py#L172)、[`rag/api_security.py`](../../rag/api_security.py#L192)
- 版本正文以 stage 文件转成最终文件路径；SQLite 同时记录版本路径、hash、当前指针，并与 FTS/队列/Qdrant 生命周期协作。这不是一个 SQL 事务可原子覆盖的写入。[`rag/document_version_store.py`](../../rag/document_version_store.py#L25)、[`rag/document_version_store.py`](../../rag/document_version_store.py#L52)、[`rag/repositories/material_repo.py`](../../rag/repositories/material_repo.py#L140)、[`rag/materials/worker.py`](../../rag/materials/worker.py#L850)
- 现有 backup 会对 SQLite 数据库调用 online backup，复制 manifest 要求的文件，写 `manifest.json` 和 SHA256 sidecar，并校验后从 `.incomplete-*` 原子改名；transient `staging`、`quarantine`、`archive_work` 默认排除。数据库复制和文件复制不是跨存储同一时点事务，未来迁移清单必须把这些 transient 目录里被正式记录引用的内容纳入完整性核对。[`rag/backups/service.py`](../../rag/backups/service.py#L39)、[`rag/backups/service.py`](../../rag/backups/service.py#L72)、[`rag/backups/service.py`](../../rag/backups/service.py#L127)、[`rag/backups/service.py`](../../rag/backups/service.py#L503)、[`rag/backups/service.py`](../../rag/backups/service.py#L541)、[`rag/backups/service.py`](../../rag/backups/service.py#L573)
- 当前没有应用 UserDB PostgreSQL adapter 或其 dependency。仓库中 `rag/data_sources/db_source.py` 的可选 psycopg2 只用于用户定义的外部 PostgreSQL 数据源，不等价于应用主数据库支持。[`rag/data_sources/db_source.py`](../../rag/data_sources/db_source.py#L229)、[`rag/data_sources/db_source.py`](../../rag/data_sources/db_source.py#L251)、[`pyproject.toml`](../../pyproject.toml#L6)

## 决策与备选

**推荐：保留 UserDB 领域 facade，建立显式、可注入的数据库端口和 PostgreSQL adapter，逐域迁移到明确拥有事务的 UnitOfWork；先完成离线 schema/query 盘点，再用隔离环境做双库行为等价、迁移和恢复演练，全部通过后才考虑切换。** 迁移期间不让 SQLite 与 PostgreSQL 同时接受业务写入，不做无可靠 outbox 的双写。

备选：

1. **只把连接 URL 改为 PostgreSQL**：拒绝。连接、placeholder、事务、schema、ID 获取、migration、FTS、SQLite PRAGMA 等均不同，当前结构会直接失败或产生语义偏差。
2. **保留 SQLite，不迁移**：作为当前可运行路径继续有效；短期成本最低，但 SQLite WAL 同一时间仍只有一个 writer，不能以此证明多实例共享写入已满足容量或并发需求。官方 WAL 文档明确写事务仍只能有一个 writer。[SQLite WAL](https://www.sqlite.org/wal.html)
3. **部署双库 shadow / 同步双写**：只可用于隔离、只读比较或明确设计的可重放 CDC/outbox。未经幂等事件、顺序与补偿验证，不做线上双写。shadow 的结果必须逐 ACL 范围和行为对齐。

## 目标契约（提案，尚未实现）

### UnitOfWork 与事务

1. 一个请求/命令显式创建 UnitOfWork：`read` 或 `write` 事务、同一连接/session、退出时唯一 commit/rollback 所有者；repository 不得暗中 `commit()`。嵌套只通过 savepoint 或禁止，不复用但无隔离的 SQLite nesting 语义。
2. 将 `BEGIN IMMEDIATE` 的意图翻译成 PG 明确语义：对具体竞争行使用 `SELECT ... FOR UPDATE`/条件更新及唯一约束；必要时采用 `SERIALIZABLE`，对 serialization failure/deadlock 整个事务重放。不得把全表/无关行锁当成替代。
3. 条件更新必须以 expected version/状态谓词和 `rowcount == 1` 作为成功条件。唯 current version pointer 权威保持在 SQL `document_permissions.current_version_id`；FTS、向量和文件不可成为 ACL 或 current pointer 的权威。
4. 建立 `UnitOfWork.run(command, retry_policy)`：仅对识别的可重试 SQLSTATE（至少 serialization failure `40001`、deadlock `40P01`）在事务外退避重试**整个命令**；重试上限/总 deadline 有界；唯一键冲突、ACL deny、CAS 失败、校验错误、非瞬态网络错误不重试。command 内外部副作用必须先写 outbox 或具备幂等键，在重试期间不得重复发送、写文件或调向量服务。写入提交结果不明时须通过幂等键查询 reconcile，不能盲目重复写。PostgreSQL `SERIALIZABLE` 会因并发冲突中止事务且要求应用重试整个事务，这是官方文档支持的引擎事实；上述接口/分类是本 ADR 的工程推论。[PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)、[PostgreSQL explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html)

### Dialect、schema、migration 和 ID

- Adapter 不暴露原生 connection/cursor 到业务层；通过绑定参数、typed row、统一分页/时间/布尔/JSON/冲突接口消除 SQL 方言泄漏。审计 `?` placeholder、`INSERT OR IGNORE/REPLACE`、`lastrowid`、`datetime/strftime`、`PRAGMA`、FTS `MATCH`、SQLite `executescript` 与 SQLite 类型亲和性；逐处制定 PG 等价 SQL，不用文本替换批量改写。
- 建 PostgreSQL schema migration 表，存稳定版本号、校验和与应用时间；每版 migration 明确事务性/不可逆性。在线 schema expand → backfill → validate → contract；重复执行要幂等，回滚脚本不能假定数据可逆。启动自动变更生产 schema 的现有行为须在切换前重定为受控、可观测的 release migration。
- `INTEGER AUTOINCREMENT` 映射为 `BIGINT GENERATED BY DEFAULT AS IDENTITY`（必要时统一 64-bit domain）。应用不得依赖连续/无间断 ID；写语句用 `INSERT ... RETURNING id` 获得 ID。跨数据库复制时保留已有 ID，并在导入后校正 sequence 高水位；FK/unique 检查后才开放写入。
- 显式映射 SQLite `REAL` epoch、文本时间、布尔/JSON、nullable/default/check/FK、partial index 和 collation；存量数字时间与字符串时间需按列定义转换，禁止猜测/自动按值形态推断。比较唯一约束、NULL、大小写与排序语义。

### CAS、并发与 FTS

- 保持 document version 状态迁移、`current_version_id`、组织 policy/grant/audit、legacy share、admin 数据源特殊授权、tombstone/restore、index job lease 与幂等事件的一致性。PG 锁定粒度只针对相同 KB/document/queue 行；不同文档并发不得串行化成全局写锁。使用明确 CAS/唯一约束，所有冲突返回可识别领域错误。
- PostgreSQL FTS 方案先做中文语料 PoC，再在 PG `tsvector`/`tsquery`、可配置 tokenizer 扩展（如经运维批准的中文 parser）与单独中文检索引擎之间评估。PG 原生默认词法分析不是 FTS5 等价中文分词，不能未经实验宣称同义。目标等价用例：中文连续词/混合英文数字、标点和双引号转义、多个 token 隐式 AND、空白、同义/边界词、大小写、路径/标签字段、重排/稳定 tie-break、最新 current version 筛选、tombstone、limit 与权限过滤。记录 recall/precision、排序一致性和 p95/p99；语义不兼容须通过接受的业务阈值或保留专门索引方案才能切换。
- FTS 结果始终与 `MaterialLocator` 授权集合相交；管理员、legacy owner/public/shared 和 org ACL 测试分别覆盖；隐藏文件名不得进入输出或模型输入。Fts 索引不可查询出跨 tenant 内容。

## 迁移与回退方案（设计推论，未执行）

### 切换前：可恢复快照

1. 选定维护窗、责任人、冻结范围、允许的停机时长和最大数据丢失/恢复时间；提前演练写停止、备份和恢复。先登记运行中 app/worker/request/lease 与所有文件根目录。
2. 阻止新请求、索引 worker、后台调度和人工写入；等待活跃 UnitOfWork、index lease、`staging` upload、`quarantine`、`archive_work` 确认/解包，以及 backup/restore 完成。停止写后导出最终 source watermark/change journal。
3. 生成一个**应用一致性**备份包：所有 SQLite DB 的一致性副本；每个被数据库引用的 immutable version 文件；upload/quarantine/staging/archive_work 临时材料（含 hash、owner KB/document/version、状态、能否恢复）；Qdrant collections/points 快照或可验证重建材料；embedding cache/index state 的显式 include/exclude；一份跨存储 manifest。现有 backup manifest 不包含 Qdrant，并排除了 transient dirs，因此必须另扩建/包装；不能将单独 SQLite online backup 宣称完整。[`rag/backups/service.py`](../../rag/backups/service.py#L39)、[`rag/backups/service.py`](../../rag/backups/service.py#L45)、[`rag/backups/service.py`](../../rag/backups/service.py#L231)
4. Manifest 对每个对象记录稳定逻辑 ID、存储位置、字节长度、SHA-256、数据库版本/表行数和最大事件序号；保存签名/校验和并异地只读留存。使用恢复副本验证 manifest、外键、每个引用文件 hash、当前版本唯一性、FTS rebuild 与向量对齐；本地 WAL/SHM 文件不得作为未 checkpoint 的独立备份替代。

### 导入、核对与切换

1. 在隔离的 PG 主从/备份/驱动环境恢复 schema 到空目标；预计算 deterministic mapping，保留主键；以一致性快照 bulk load 后按明细处理 delta/change journal。验证每表行数及 checksum（按排序业务键规范化）、FK/unique/check、org/legacy ACL 与 tombstone、每文档 current-version 状态、migration 高水位、索引/FTS rebuild、每个文件和向量引用。
2. 对合成数据和受批准的脱敏只读快照执行 shadow read 比对；禁止 shadow endpoint 读取未经批准的真实内容。比较 repository 返回值、权限拒绝、冲突、排序/FTS、计数和错误类别；逐项差异需归因并签字关闭。
3. 维护窗开始后再次全停写及排空；导出并应用 journal 到最终 watermark；复核总量/抽样hash/ACL/FTS/query smoke/backups。切换为 PG connection config，验证应用只使用 PG 主库、schema版本和 readiness；随后仅恢复一个 writer，并观察审计、数据库错误、权限拒绝、索引队列/文件引用完整性。恢复前置门槛不满足则不打开写入。

### 切换后失败：先保写入，再恢复服务

- 切换后 PG 已接受的新写入可能尚未存在 SQLite。严禁仅把 URL 改回旧 SQLite，否则会丢失这些写入并可能让 Qdrant/文件引用倒退。
- 每个 PG 业务变更在同一 PG 事务写 append-only change journal/outbox：有单调 commit sequence、operation ID/idempotency key、实体/版本 ID、actor/KB、必要的 before/after 或可重放 payload、hash。切换前持续或维护窗最终按 journal reverse-apply 到隔离 SQLite 副本并校验。未验证可逆的变更（schema destructive migration、外部 side effect、ACL/版本冲突）进入人工 reconcile，不能自动覆盖。
- 如果 journal 不完整、落后 watermark 或 reconcile 有冲突：保持写停、留存 PG 与所有跨存储对象、收敛事件清单并逐项裁决，修复目标副本后再经门禁切回；可以继续在 PG 修复并恢复 PG 服务，不能损害 PG 已提交的新数据。
- 演练需模拟切换前中断、切换后部分成功、PG 提交结果超时不明、文件 promote 成功而 DB rollback、DB commit 后 FTS/outbox 消费失败、旧 writer/worker 仍提交、ACL revoke 与回滚同时发生。所有结果均校验新写的保留、幂等、不重复索引、current pointer、owner 和 tombstone。

## 隔离验证矩阵（计划，不表示已验证）

| 测试项 | 断言 | 状态 |
|---|---|---|
| SQLite → PG schema/schema history | DDL 版本与校验和、约束、索引、sequence 高水位可重建；新增迁移幂等 | 未运行；待无业务隔离 PG 环境 |
| Repository golden behavior | 每域 CRUD、分页、NULL/排序/时间/布尔/JSON/唯一冲突及 ID 返回等价 | 未运行；需双 adapter fixture |
| Transaction/UoW | commit、异常 rollback、嵌套/savepoint、读隔离、多 repository 共享事务 | 未运行；当前 SQLite nesting 复用无 savepoint |
| CAS 与锁竞争 | 同版本双激活只成功一个；相同文档串行、不同文档并发；deadlock/40001 整事务重放且无重复副作用 | 未运行；需可控并发 fault injection |
| ACL / org / admin / legacy | owner/public/share、org read/manage/deny、unassigned fail-closed、admin 特权及撤权竞态全等价 | 未运行；只准合成身份与KB |
| FTS 中文等价 | 目标中文切词和上述标点/混合语言用例、current/tombstone、排序、命中数、ACL 无泄露 | 未运行；依赖中文 PoC/接受阈值 |
| Cross-store consistency | 文件 promote、SQL commit/rollback、Qdrant 更新、FTS/outbox 失败恢复与幂等 | 未运行；仅本地隔离对象/向量实例 |
| Snapshot/import/reconcile | DB、stage、archive_work、版本文件、manifest、索引可验证恢复；journal watermark 无 gap | 未运行；须备份演练及停写窗口 |
| Cutover & reverse migration | 停写门禁、最终 delta、单 writer、故障后新写反向 reconcile；URL 回旧库被流程阻止 | 未运行；生产切换须单独审批和可审计 runbook |

## 前提与后续任务

缺少 PostgreSQL 服务器与版本/HA 设计、网络与 TLS、secret 管理、备份/PITR/恢复环境、确认的驱动和依赖审批、脱敏业务 representative dataset、中文 FTS 接受阈值、维护窗、RPO/RTO 和负责签字人。下一个可执行事项是建立纯隔离 PG 测试环境和脱敏/合成 fixture，编制 SQL 特性清单、UoW API 与 migration 工具 RFC；在无环境时仅可继续源码审计。

本 ADR 引用的官方页面只支持 SQLite WAL 写者与 PostgreSQL 隔离/锁语义事实；目标接口、迁移次序、回滚与验证项均为本 ADR 设计推论。当前只完成源码盘点与方案，不声称 PostgreSQL 支持、迁移成功或多实例就绪。
