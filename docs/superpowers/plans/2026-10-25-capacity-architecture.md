# Capacity Baseline and Architecture Gates Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. User requests parallel Luna; root owns planning, integration, controlled execution, reviews and recorder.

**Goal:** 给出单机试点建议、受资源约束的组件容量证据和长期架构实施门禁。
**Architecture:** 三任务分别负责artifact诊断helper、数据访问/PG方案、共享状态/存储/worker方案；没有生产代码修改。root串行执行profile、汇总SLO口径并核对方案依赖，保持阶段顺序。
**Tech Stack:** 当前锁定Python3.12/SQLite/MaterialLocator，既有私有runtime/developer环境，官方文档；不新增依赖。
**Spec:** `docs/superpowers/specs/2026-10-25-capacity-architecture-design.md`。

## Global Constraints

- C隔离分支codex/capacity-architecture-2026-10-25基线3c001ee；D main仍1d413ed，不merge/push/deploy/restart，不读真实.env/业务DB，不变更生产源/测试/依赖/容器。
- 1000/5000/10000资料、并发1/2/4及候选SLO是root工程建议，不是业务事实/生产承诺。本轮只SQLite+ACL+FTS材料定位；无模型/HTTP/vector/原文处理，历史200模型基线不能充当本轮SLO验收。
- 当前内存约1.4GiB，profile单进程BelowNormal/BLAS1、每请求free>=1GiB/WorkingSet<=256MiB，越界stop event+部分证据+非零exit；不强跑后续档、不停止用户程序。最多4profile threads，与pytest不并行。
- Python=`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`，runtime同env/runtime-python；offline工具=`artifacts/kb-lifecycle-concurrency-2026-10-24/tools`，priority=`artifacts/staging-recovery-2026-10-25/verification/tools`，显式PYTHONPATH。不设TEMP为pytest basetemp。
- 子代理不Git/timer/submit/派生代理，只写各自指定文件/本SDD；root timerT-A669C8D55180。artifact为artifacts/capacity-architecture-2026-10-25，所有旧环境/证据/SDD保留。

## Review Focus

- 组件结果冒称整/query容量或已支持PG/多实例：Task1每组scope和实际n；Task2/3明确implemented/proposal/not-run；root报告分开。
- helper误读真实DB或耗尽宿主内存：Task1自建fixture/audit/network/resource gates真实自检，root只一次受限正式执行。
- 缓存/锁变更漏ACL或旧lease写入：Task3给出fencing/cache epoch/故障矩阵及可验证门禁，不靠RedisRequired名字宣称已安全。
- 迁移回滚丢切换后的写入：Task2明确停写/完整备份/变更日志与恢复新写入方案，没有可验证逆迁移不能简单改URL回退。
- 分位数/失败样本偏差：Task1保存raw样本和错误、每线程闭环/预热分离；root正式SLO条件包含时段/分布/流式/失败，不平均分位数。

### Task 1: Resource-bounded synthetic material capacity preparation

**Files:** Create ignored `artifacts/capacity-architecture-2026-10-25/benchmark_material_capacity.py`, preparation/selfcheck logs/manifest；report本SDD task-1-report.md。
**Interfaces:** CLI `--output-dir <new artifact subdirectory>`、`--scales 1000 5000 10000`、`--concurrency 1 2 4`、`--repeats 3`、`--requests-per-worker 40`、`--warmup 5`，另`--selfcheck`可用N20/并发1/测量2验证完整seed/query/hash/gates；root正式执行前保存help/compile/selfcheck。不接受已有DB输入；output-dir已存在拒绝覆盖。正式运行仅root；子代理最多selfcheck。

- [ ] 阅读Spec和真实UserDB/MaterialLocator/MaterialLocationQuery/FTS schema，准备单文件artifact helper；合成seed可批量SQL单事务，seed时间单列，不宣称验证上传写能力。查询必须走真实Locator，不mock visibility/FTS。
- [ ] 严格资源/网络/环境/DB输入边界，两个普通用户交替private、每10一tombstone、每文档历史/当前两版本与两FTS行。验证所有结果当前/可见/未删除且model_input无hidden。每thread独立Locator，closed-loop；检查逻辑dump+writer changes不变。保存selfcreated fixtures、raw samples/group summaries/source/interpreter/hash/resource provenance；连接显式close，输出失败/资源延后可审计。
- [ ] 用私有Python/guard/priority先小selfcheck，验证正确性+门禁触发能停止（可安全模拟阈值，不作正式性能数据）；Ruff/compile。不要跑正式1000/5000/10000矩阵。报告READY精确root命令/路径/exits/限制。

### Task 2: Data-access and PostgreSQL migration readiness

**Files:** Create `docs/architecture/data-access-postgresql.md`；artifact `data-access-inventory.json`（只源码盘点/必要自建Temp app schema），report本SDD task-2-report.md。
**Interfaces:** 与Task3按关系库先于shared-state/多副本，公共依赖按Spec；root唯一修改UPGRADE/README/handoff。

- [ ] 定向盘点UserDB/ConnectionManager/app migrator/repositories/FTS和跨存储事务；每类给实际文件行号，不沿用旧56表数字。若报表数，区分sqlite内部/FTS shadow，数据只新建Temp，不读业务库。
- [ ] 写中文ADR：现状/备选与推荐，UnitOfWork/driver/schema/返回ID/SQL兼容与整事务重试契约、中文FTS等价及PG验证矩阵；保持既有ACL/admin/legacy/canary/sandbox边界。没有服务器/驱动不加假实现。
- [ ] 细化备份含stage/跨存储manifest、停写迁移、校验、切换、切换后写入的逆迁移/reconcile/冻结门禁；列环境/凭据/数据集等外部前提与下一可执行任务。引用官方SQLite WAL与PG isolation/locking页面，标明推论/尚未实施。自审链接/代码事实，不重复pytest。

### Task 3: Shared state, object storage and independent worker readiness

**Files:** Create `docs/architecture/shared-state-storage-workers.md`；artifact `shared-state-inventory.json`（源码证据），report本SDD task-3-report.md。
**Interfaces:** DAL/PG方案文件由Task2写，勿修改；本文件记录Redis->object storage->multi API/worker依赖，全部为设计门禁。

- [ ] 定向核对Redis/答案缓存/LLM pool、kb_mutation/worker leases、local file/version/backup和API lifespan。按当前实际code标implemented/proposal/not-run，勿将claim_material_jobs支持wiki的既有分派误报bug。
- [ ] 写中文ADR与故障矩阵：ACL/data epoch缓存、shared-required/failclosed、持久fencing及SQL/blob/vector旧worker写保护；不可变BlobRef/条件写/checksum/配额/outbox，保留全局refs/tombstone/补偿身份。
- [ ] 细化API/worker未来角色/lease/重复投递/崩溃/撤权/删除并发、跨存储备份/回滚/双API隔离验收；未来flag不能写成当前命令。引用官方Redis锁、S3 conditional/versioning及Qdrant deployment，缺环境列条件，不跑容器/服务/真实数据；自审链接和代码事实。

### Task 4: Root experiment, review and closure

- [ ] 任务独立规格/质量复查（artifact helper亦审边界），一次整专项复查/必要单次修复波次；不重复已通过定向检查。
- [ ] 原服务/内存读前检查，受限单进程运行正式profile一次，资源gate停止保留partial不降低门槛；解析实际raw samples/正确性/hash/exit，不以主观预期填表。
- [ ] 形成`docs/capacity-and-slo.md`及`docs/reviews/2026-10-25-capacity-architecture.md`，链接所有工程建议、正式验收条件及ADR，按实际结果决定是否需另立优化；更新UPGRADE/索引/handoff/总计划。
- [ ] 若生产源保持，沿用3.2受测冻结并核对无源差异；仅对profile所调用MaterialLocator既有回归做一次合成定向检查，Ruff/文档/原服务核对。实际submit唯一工作记录、保留证据，最后报告整个顺序任务的完成与外部运行门禁。
