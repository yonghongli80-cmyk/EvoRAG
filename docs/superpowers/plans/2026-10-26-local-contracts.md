# Local Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Root owns decisions, Git, test slots and work record; Luna implementers use disjoint files and independent review gates.

**Goal:** 本地补齐启动诊断竞态、SQLite identifier引用和关键命令事务清单。
**Architecture:** 不改变生命周期所有权协议，失败token均诊断；轻量单identifier helper供三个consumer使用；操作级文档明确事务和外部副作用。
**Tech Stack:** Python3.12/stdlib/既有pytest和依赖锁；真实TempSQLite，无新服务或依赖。
**Spec:** `docs/superpowers/specs/2026-10-26-local-contracts-design.md`。

## Global Constraints

- BASE0b488725，新分支codex/local-contracts-2026-10-26，复用C linked worktree；Dmain99834ca/PID48512保护，禁止重启/部署/业务资料和DB/真实.env内容读取/正式run/压测/模型外呼。无自动merge/push。
- root timerT-10BFAFC56574；唯一SDD `.superpowers/sdd/2026-10-26-local-contracts/`，artifact `artifacts/local-contracts-2026-10-26/`。不读旧SDD；子代理禁止任何Git写、timer/submit/recorder、派子代理；unit自建TempGit可写。
- 私有Python `artifacts/review-backlog-2026-10-24/env/{developer-python,runtime-python}/Scripts/python.exe`；PYTHONPATH repo+`artifacts/kb-lifecycle-concurrency-2026-10-24/tools`+`artifacts/staging-recovery-2026-10-25/verification/tools`。pytest plugins offline_network_guard/staging_test_priority，BLAS/OMP/MKL/NUMEXPR=1、BelowNormal、root串行许可、短新OS Temp basetemp，不覆盖pytest TEMP/TMP。
- 每文件<1000行，不放宽架构预算/不改旧golden、helper或证据。A/B先真实RED再实现，C仅文档和docstring按低影响变更不写测试。原环境/工作树/SDD/失败原件保留。

## Review Focus

- callback在intent之前完成、取消获胜：日志一次、primary身份/清理仍正确；logger抛BaseException不得逸出。
- SQLite DQS可把缺列当字面量：用真实已建对象及与名字不同的存储值验证，不以“不报错”为通过。
- 标识符含双引号/空格/Unicode/空名时不改名；值仍参数绑定，不引入canonical白名单或API错误映射。
- snapshot额外列、read-only/immutable/query_only、路径/归档身份与schema准入保持；NUL不可存schema不要伪造运行证据。
- SQL source/evidence因B变更必须重新审查/绑定，旧hash和marker计数不是当前或完整writer/PG证明。

### Task 1: Startup failure diagnostic race

**Files:** `rag/startup_lifecycle.py`；`tests/test_startup_quiescence.py`（若加后超限，新建`tests/test_startup_failure_diagnostics.py`）。
**Interface:** 保留StartupLifecycle公开方法；内部失败诊断不依赖cleanup_requested，保留原late消息语义。

- [ ] 真实Thread/Event测试RED：原callback先清active/观察无intent，再屏障暂停，取消caller并finish；断言日志恰一次、原取消身份、清理一次，finally放行全部线程。另测普通异常对象和logger KeyboardInterrupt防泄漏。
- [ ] 最小修复读取/记录所有非cancelled失败token，日志outside lock且BaseException隔离；不改grace/gate/finalizer/worker协议。
- [ ] GREEN新/原quiescence+startup migrations+pipeline lifecycle，Ruff/format；报告实际PID/nativeexit/log与边界，root提交和独立SPEC/QUALITY。

### Task 2: SQLite identifier contract

**Files:** 新`rag/sqlite_identifiers.py`；改`rag/spreadsheets/store.py`、`rag/spreadsheets/api.py`、`rag/staging_audit.py`；新`tests/test_sqlite_identifiers.py`、`tests/test_sqlite_identifier_consumers.py`（可再按消费者拆focused新文件，先告知root，不改原超长测试）。
**Produces:** `quote_sqlite_identifier(value: str) -> str`，str-only/TypeError、NUL/ValueError、double quote escape、空名保留，消息不含输入。固定SQL值仍?参数。

- [ ] helper与真实consumer测试先RED：含引号表/列实际DDL/DML、store原子stage、rows真实响应、snapshot额外列读取；普通parser键/分页/公式语义保持。非法值/NUL仅可表达边界测试。
- [ ] 实现小helper并仅替换spec列出的动态identifier；不改ACL/路径/归档身份/只读guard/错误HTTP状态/DQS开关，不过滤额外列。
- [ ] GREEN新focused及既有spreadsheet store/API、staging audit/jobs兼容相关；Ruff/format/预算。报告实际命令、nativeexit、SQL值/对象hash与未覆盖实际fixture，root提交独立SPEC/QUALITY。

### Task 3: Command transaction boundaries

**Files:** 新`docs/architecture/command-transaction-boundaries.md`；`scripts/review_dynamic_sql.py`仅模块docstring。
**Consumes:** 0b48872的KB删除/上传/索引发布/恢复真实调用链；B仅quoting不改变事务协议，文档以函数名及审查版本作锚点。

- [ ] 根据recon补完整source-linked操作表，三命令各列SQL commit ownership、跨repo连接、外部副作用、补偿/unknown与最小合成故障矩阵。
- [ ] 明确当前不保证全局/分布式原子，给后续UoW设计和可本地验证顺序；不新建运行时UoW、CM或新事务。
- [ ] 更新docstring已有证据来源检查摘要，Ruff/format；不为纯文档写测试。root独立规格/事实复审，必要只读查源。

### Task 4: Root integration

- [ ] 各task独立SPEC/QUALITY、原作者fix-loop；一次整分支review、最多一波final修复与scoped review。
- [ ] 最终源冻结、生产锁合成启动/identifier烟测、一次完整后端回归、Ruff/格式、前后freeze/check；不相加分项或复用上一批3462/5数字。
- [ ] 新SQL inventory；重新审查B影响的finding/evidence，其余仅匹配identity/hash后保留；新manifest严格Rag source refs/raw hash，独立生成物核对；新writer候选，PG/writer未证明标记保留。
- [ ] 更新当前入口/交接/本批报告和剩余待办，核对原服务PID/start/ready/env hash/Dmain与跟踪树；实际submit当前唯一记录、非DB归档，保留local branch，不merge/push/deploy。

## Preflight

| Pair / task | Shared interface or file | Ruling |
|---|---|---|
| A/B/C | 无共同编辑源码；C只docstring/new doc | 可并行写码，pytest严格串行；root独占Git/记录 |
| B/root proof | B改三个SQL源码的行号/hash与引用依据 | 最终freeze后重建并重审affected，禁止盲目重绑旧seed |
| A | 确定性屏障与普通错误对象 | 不用短sleep猜race，不让callback/logger改变primary |
| B | helper由同一个owner产出并接入全部consumer | 不拆共享helper并行编辑，保留API错误与公式合同 |
| C | 操作级清单不是实现 | 不把marker/词法with当完整命令图或原子性证明 |
