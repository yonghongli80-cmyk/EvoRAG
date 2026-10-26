# Local Readiness and Startup Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Root coordinates design, Git, test scheduling, reviews and actual recorder submission. Independent Luna file scopes run in parallel; pytest execution is serialized.

**当前结果：** 受测冻结d837501，完整后端3423通过/5跳过（1966.16秒、实际退出0），生产锁启动烟测5项、Ruff、605文件回归前后核对及独立复审通过。源码仍本地；服务器压力/上线按用户要求留空。见[验收与裁决](../../reviews/2026-10-26-local-readiness.md)。

**Goal:** 完成本地可闭环的服务器工具准备、启动治理、SQL迁移清单及状态整理；上线和正式压测留空。
**Architecture:** 三个独立代码子项目和一个消费其接口/证据的文档子项目；API只保持facade拆分并最小修复后期清理，不启用PG或线上shadow。
**Tech Stack:** Python3.12/stdlib/pytest/现有依赖锁，无新依赖。
**Spec:** `docs/superpowers/specs/2026-10-26-local-readiness-design.md` 中 A/B/C/D 各自为独立可拒绝的子项目设计。

## Global Constraints

- BASE/main/两远端99834ca，C现有隔离工作树分支codex/local-readiness-2026-10-26；D main/原PID48512服务保持，不部署/重启/迁移/压力负载/真实模型，不读真实.env/业务资料或DB。未经新指令不合并推送本轮分支。
- root timerT-016DB524EDC8；子代理不得操作共享Git/branch/index/commit，不得调用任何timer/submit/recorder、不得派代理。单元测试允许创建自己Temp Git仓库。已有勘查咨询记录单独保留，不重复登记该咨询为实现成果。
- 唯一SDD：.superpowers/sdd/2026-10-26-local-readiness；不读写其它计划SDD。所有exec显式C workdir。原环境/历史helper/样本/source/golden保留。
- 私有developer-python/runtime-python沿用artifacts/review-backlog-2026-10-24/env；PYTHONPATH repo+artifacts/kb-lifecycle-concurrency-2026-10-24/tools+artifacts/staging-recovery-2026-10-25/verification/tools。pytest加载offline_network_guard/staging_test_priority，BLAS/OMP/MKL1、BelowNormal、新OS短Temp，不改TEMP。**同一时刻最多一个测试进程，由root授予许可**，实现者先写测试等待许可RED，再实现，GREEN也先申请。
- 不放宽api/pipeline/局部导入预算；新工具每文件<1000行。CLI freeze/check无RAG/DB/locator负载，run本轮禁止；Linuxfixture单测不等同真实Linux运行验收。

## Review Focus

- 工具是否在资源/冻结/隔离前导入RAG、继承密钥/真实HOME、连接loopback生产服务，或把不可测cgroup当无限制。
- 小case跨requested_N目录重用、core/ACL扩展样本混算、漏新batch方法、截断model_input/semantic集合或门禁后丢round证据。
- API facade/cache依赖/兼容setter或原延迟边界改变，及迁移/索引线程未完成时提前关存储。
- 后期startup原错误/取消被清理错误覆盖，或第一个stop失败跳过其它清理；正常次序/worker未停不关共享资源的保护必须保持。
- SQL静态动态表达式被假当可解析完整SQL、eval/连接数据库、漏错误仍报100%完成，文档把准备/设计称已部署或性能通过。

### Task 1: Portable acceptance tooling

**Owner files:** scripts/material_locator_resources.py、scripts/material_locator_acceptance_support.py、scripts/paired_material_locator_acceptance.py；tests/test_material_locator_resources.py、tests/test_material_locator_acceptance_tool.py。

- [x] 先写纯解析/拒绝/fixture用例，申请单进程许可运行真实RED（模块缺失或行为失败），保存真实命令/PID/退出/输出。
- [x] 实现spec A：Win/Linux/cgroup祖先/路径边界/优先级、完整freeze、干净环境和网络/SQLiteguard、核心case路径唯一、完整对照/semantic探针/hash/四方法分阶段计数、先落盘后gate和partial非零、freeze/check/run显式CLI。
- [x] 单测覆盖v1/v2有限/无限/ancestor/missing/bad/escape，fakeLinux不可算实机；manifest dirty/untracked/missing/hash，dotenv/连接拒绝、旧fixture LF、counter phase、AB/BA、18/810+ACL扩展2/90路径唯一、existing-output拒绝。可以测试纯调度和拒绝/报告，不运行实际压力case。
- [x] 定向GREEN/Ruff后停止，给root接口READY和task-1-report。共享CPU许可释放。不Git或记录器。

### Task 2: API startup extraction and late cleanup

**Owner files:** rag/api.py、rag/api_startup.py；tests/test_startup_migrations.py、tests/test_api_startup_boundary.py。现有index/lifecycle/router等测试读取但不弱化。

- [x] 先写真实RED：restore抛错漏清理、清理失败仍尝试余项且保留primary；新增无I/O startup模块边界；申请串行测试许可。
- [x] 保持七个API facade/动态module上下文和延迟imports，移动实现；保持_ApiModule/router/public签名、正常调用/线程顺序与既有预算。
- [x] 单独最小修复三个后期启动hook的try/finally边界及best-effort清理错误策略；不修改前期to_thread取消/线程排空语义，不去掉material-worker stop保护。
- [x] 申请定向GREEN：新测试+startup_migrations/index_recovery/material_migration/pipeline_lifecycle/org_legacy/router_parity/architecture；Ruff/format。报告分开记录纯拆分和bugfix RED/GREEN，完成后停止。

### Task 3: Static SQL/transaction readiness

**Owner files:** scripts/audit_sql_portability.py、tests/test_sql_portability_inventory.py；docs/architecture/sql-portability-inventory.md、docs/architecture/unit-of-work-contract.md。

- [x] 新纯fixture测试RED：静态/拼接/动态fstring、unresolved、SQL特性、事务owner、语法错误/稳定排序/输出不覆盖，证明不导入app、不执行SQL和无业务路径访问。
- [x] 实现标准库read-only scanner和明确coverage/error契约，不自动改写SQL或安装driver。
- [x] 定向GREEN/Ruff；在当前源码静态生成真实清单到artifact，文档实际统计以root冻结后的扫描为准。UoW仅合同/RFC，不冒充adapter实现。

### Task 4: Current-state docs and remaining roadmap

**Owner files:** docs/README.md、docs/handoff-2026-10-24.md、UPGRADE.md、docs/local-readiness.md、docs/material-locator-server-acceptance.md。

- [x] 统一当前交付99834ca与本轮local分支、历史快照/旧Docker故障、已完成隔离验收；不改旧测试数字/时间。
- [x] 消费Task1接口READY后更新命令/工具状态及真实未验门禁；消费Task3实际清单/合同而非重复原ADR结项。
- [x] 路线图区分本轮本地准备与后续PG/多实例/自动shadow实现，保留合法真实fixture与服务器条件。链接可解析、CLI help与文档一致，文档低影响不写镜像测试。

### Task 5: Root verification and closure

- [x] 各代码任务独立SPEC/QUALITY审查，原实现者修复/增量复审；新任务与旧已接受历史不重复审查。
- [x] 全部代码冻结后，合适的定向/架构静态检查与一次完整后端离线回归（API启动公共路径变更有跨套件风险，串行低优先级），不重复前端/旧性能矩阵。
- [x] 生产依赖锁合成API smoke、tracked工具freeze/check（只预检无RAG负载），静态SQL真实扫描；核对源码/helper与门禁范围、原服务前后PID/start/ready/env hash、Dmain保持。
- [x] 一次最终整分支复查及必要一波修复；更新计划/交接与公开验收。服务器压测/SLO/上线/业务迁移继续空缺。
- [ ] **待实际提交及归档回执。** 实际提交本轮实现/整合工作记录并检查成功、保留勘查咨询原记录、不重复登记；归档非DB证据，保留可审阅分支、环境与历史证据，不自动合并部署。

**Preflight:** Task1/2/3源码、测试互不共享；只共享解释器/测试CPU，以root许可串行运行。Task4独立文案可并行，精确CLI/状态依赖1/3 READY；不由docs改代码。Task2两个行为阶段同一owner顺序实施。root独占Git/recorder，review只读可并行。新需求/设计裁决写本SDD ledger，避免未记录扩大范围。
