# Local Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Root owns design/Git/test permits/recorder; independent Luna tasks work in disjoint files and request serialized RED/GREEN permits.

**Goal:** 本地推进真实启动排空安全、非计时合成功能验证和源绑定SQL/写所有权审查。
**Architecture:** owned-work令牌+一次性启动finalizer；现验收工具fixture有效权限统计及独立子进程功能验证；两个静态SQL审查/所有权工具及证据合同，不换数据库。
**Tech Stack:** Python3.12/stdlib/现有依赖锁/pytest，无新包、数据库服务或前端变更。
**Spec:** `docs/superpowers/specs/2026-10-26-local-followups-design.md`。

## Global Constraints

- BASEc74df90，C linked worktree复用，新branch codex/local-followups-2026-10-26；旧local-readiness分支保留。Dmain99834ca、原PID48512，禁止重启/部署/业务资料或DB/真实.env读取、CLI正式run/压力矩阵/模型外呼，服务器和SLO仍留空。
- root timerT-A247604DEFB9；唯一SDD .superpowers/sdd/2026-10-26-local-followups，不读其它SDD；artifact artifacts/local-followups-2026-10-26。禁止子代理共享Git写、任何timer/submit/recorder、派代理。仅unit自建TempGit允许写。
- 私有developer/runtime-python沿用旧review-backlog env；PYTHONPATH repo+kb-lifecycle-concurrency-2026-10-24/tools+staging-recovery-2026-10-25/verification/tools；offline_network_guard和staging_test_priority，BLAS/OMP/MKL1、BelowNormal、一个pytest进程、新OS短basetemp，root许可串行，真实Thread测试必须finally释放屏障。
- 不放宽api/pipeline/局部import预算，新单源文件<1000；旧helper/source/golden/证据不改，不安装驱动或启数据库，不把准备称PG/多实例实现。

## Review Focus

- async等待cancel与真实工作结束区分，queued/running信号、重复cancel、loop关闭和callback异常不可误关资源/丢primary。
- finalizer latch/门闩重入与锁下callback，旧回调不得关新startup资源；worker stopFalse保护和正常startup顺序/ContextVars不能变。
- 小fixture只做正确性，必须禁止timer/workerPool/正式run，并真实覆盖public/shared org deny、完整model_input/semantic/hash/counters/关闭。
- 构造属性与有效ACL计数不可混用；所有profile更改版本绑定，不能拼旧性能数据。
- SQL审查coverage、语法理解与PG readiness不能混为一谈；manifest源hash/精确finding、unknown writer/opaque外部SQL要真实保留，静态工具不得执行源代码或SQL。

### Task 1: Owned startup quiescence

**Files:** rag/startup_lifecycle.py、rag/api.py；tests/test_startup_quiescence.py、tests/test_startup_migrations.py（必要兼容更新，不削弱既有断言）。Material supervisor默认只读，真有必要改需先root裁决。

- [x] 真实Thread/Event测试先RED：cancel时线程仍用资源却无随后清理；超时保留、完成后仅一次；queued cancel、重复cancel、不误关新owner、loop关闭后finalizer、原始错误/contextvars/正常顺序。
- [x] 实现owned-work追踪与app gate，保留default executor/ContextVars与to_thread等价后台同步语义，5秒grace(测试可短注入)，按spec处理primary/cleanup与busy worker边界。
- [x] 串行GREEN新/改startup+原index/lifecycle/legacy/architecture/API相关用例、Ruff/format；task1报告exactPID/exit/命令/输出与保留边界。

### Task 2: Tiny SQLite fixture functional validation

**Files:** scripts/paired_material_locator_acceptance.py、scripts/material_locator_acceptance_support.py（仅需时）；tests/test_material_locator_acceptance_functional.py；docs/material-locator-server-acceptance.md（本功能边界段）。

- [x] 新隔离子进程测试先RED：最多8doc/profile有效ACL与旧/新完整快照/semantic/hash/counters、denied org公开共享样本、计数语义不混淆。
- [x] 修fixture元数据版本与构造/有效ACL分栏，必要最小功能缺陷按实际RED修；不运行run/预热/计时或正式矩阵。
- [x] 首diagnostic注入异常验证partial与DB关闭，禁止ThreadPool/timer/resource采样调用；GREEN+旧工具两测试模块/等价用例、Ruff；task2报告说明这些不是压测。

### Task 3: Dynamic review and writer candidate map

**Files:** scripts/review_dynamic_sql.py、scripts/audit_transaction_ownership.py；tests/test_dynamic_sql_review.py、tests/test_transaction_ownership_inventory.py；docs/architecture/dynamic-sql-and-writer-review.md。

- [x] 纯fixture真实RED：exactfinding/dup/missing/hash过期/disposition-evidence、未知alias/词法事务/direct commit/private/external/error/稳定输出/禁止覆盖与无执行副作用。
- [x] 实现标准库只读工具，不修改原scanner合同/数据库；完整源绑定和review/PG状态分开。
- [x] 对103项逐项源审查，保留理解/待跟进/外部数据源区分、源码证据与hash；输出artifact reviewed manifest及writer候选与UoW gap，独立复审不能用validator代替语义审查。
- [x] 定向GREEN/Ruff；正式candidate扫描/manifest check在root最终源码冻结后生成。task3报告actualcommands/PID/exit和scope limits。

### Task 4: Root integration and closure

- [x] 各task独立SPEC/QUALITY、原作者修复/增量复审、一次final整分支及最多一波合并修复。
- [x] 源码冻结后一次完整后端离线回归(启动公共路径风险)，生产锁小型功能/生命周期烟测与源码前后check，实际skip理由保留，不重复前端/压力。
- [x] 验证当前SQL103源形状与新writer范围，root真实生成新inventory/manifest/proof，未解析或未知不伪报完成。
- [x] 更新当前路线图/交接/本批结果裁决，确认原服务PID/start/ready/env hash、Dmain/跟踪树不变；旧批事实不改成新结果。
- [x] root实际submit本独立事项记录并核对成功、归档非DB证据及hash，保留分支/env/历史，无自动merge/push/deploy。

**Preflight:** Task1仅Rag startup+对应tests，Task2仅scripts fixture/新functional test+server文档，Task3两个新静态工具/测试/架构doc，互不编辑。Task4 root文档消费1/2/3 READY后更新，不抢子任务文件。共享测试解释器和CPU以root许可串行；review只读可并发。Task2元数据改变不改变核心18/810/扩展2/90样本合同。Task3源bindings最终需候选同hash，不能将早期snapshot硬标当前。
