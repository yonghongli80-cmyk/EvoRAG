# Local Safety Followups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 完成本地可验证的 purge 文件保护、连接关闭竞态和 writer AST 证据纠错。
**Architecture:** 复用现有隔离工作树和真实临时 SQLite，三个独立范围并行实现、root 串行测试和独立复审。仅修复有 RED 证明的缺陷，未证明的怀疑写入报告，不扩大重构。
**Tech Stack:** Python 3.12、sqlite3、pytest、标准库 AST、现有依赖锁。
**Spec:** docs/superpowers/specs/2026-10-27-local-safety-followups-design.md

## Global Constraints
- 只在 C 隔离 worktree 改动，不切换 D 主目录、不改 .env、不连接真实模型/数据库/向量服务。
- 临时数据库和文件只在新 OS 临时目录；离线 pytest、低优先级、BLAS 单线程；pytest 由 root 串行调度。
- 产品不新增依赖，不做数据库迁移，不改变 transaction 嵌套语义，不批量格式化或为行数做无关拆分。
- 三实现代理使用 gpt-6.1-terra/high，互不修改对方文件；不自行创建子代理、提交、合并或推送。
- root 负责测试运行、独立评审和集成；保留失败证据，不把合成测试说成真实业务验收。
- 服务器矩阵/P99/SLO、真实迁移恢复、真实 fixture/供应商证据、PG/多实例/outbox 等继续列为未验。

## Review Focus
1. purge 计划中后一个目标无效时，前一个合法文件和向量也必须保留。
2. 根内非规范旧文件继续可清理，但其他文档/归档引用、链接与越界文件必须保留。
3. close 赢过 reader 发布或等待中的 writer 时，不泄漏连接或返回已关闭的 writer。
4. reader 初始化保持 WAL 非阻塞；异常初始化不遮蔽原异常。
5. AST visitor 的某条分支/类作用域不能污染其他路径，unknown 不得丢失 SQL sink。

## Task 1: Purge 目标计划与重试安全
**Files:** Modify rag/materials/recycle.py; optionally Create rag/materials/purge_paths.py; Create tests/test_purge_path_safety.py; Modify tests/test_review_purge_recovery.py for explicit temporary versions_root only.
**Interfaces:** 保持 purge_document(...) -> PurgeResult 及参数列表；新增辅助模块如有必要仅由 recycle 使用，错误为 RecycleError(reason_code)。生产 worker 的调用不变。
- [x] 阅读 purge、worker 调用、kb_document_cleanup 既有路径校验和 structured dataset 路径合同，确认最小复现。
- [x] 测试先行：根外路径、缺根、父级链接/junction、另一个文档版本引用/归档 source、后目标非法时前目标未删、合法旧文件成功、unlink/vector 失败重试、终态 commit 失败后可重试。平台无法创建链接时记录真实 skip。
- [x] 请求 root 运行 RED，确认失败原因属于实现缺口后再写代码。
- [x] 在任何 unlink/vector 之前收集并校验全部目标；保留已有终态事务/重试，避免无关 schema/接口调整。
- [x] 请求 root 运行 tests/test_purge_path_safety.py tests/test_material_recycle.py tests/test_review_purge_recovery.py tests/test_material_state_audit_atomicity.py，修复后交付报告。
- [x] 独立 SPEC/QUALITY 复审，关闭问题；root 集成。

## Task 2: ConnectionManager 关闭与连接发布
**Files:** Modify rag/db/connection.py; Create tests/test_db_connection_lifecycle.py.
**Interfaces:** ConnectionManager 的既有方法签名不变，关闭错误仍为 RuntimeError("ConnectionManager is closed")；不新增通用 UoW/嵌套事务行为。
- [x] 跟踪 get_read_connection、write_connection、transaction 与 close 锁序，制定 Event/barrier 控制的稳定竞态。
- [x] 测试先行：reader 配置暂停后 close；配置失败连接被关闭；等待中的 write_connection/transaction 在 close 后拒绝；正常 reader 不等待持有中的 writer；close 幂等。
- [x] 请求 root 运行 RED，不用 sleep 时间竞争作为核心断言。
- [x] 以最小锁内 recheck 和未发布连接清理修复；保留 WAL 并行读与既有 nested transaction 行为。
- [x] 请求 root 运行 tests/test_db_connection_lifecycle.py tests/test_db_connection.py tests/test_db_migrations.py tests/test_api_startup_boundary.py tests/test_startup_quiescence.py，提交事实报告。
- [x] 独立 SPEC/QUALITY 复审，关闭问题；root 集成。

## Task 3: Writer AST 保守作用域
**Files:** Modify scripts/audit_transaction_ownership.py; Create tests/test_transaction_ownership_control_flow.py. 不修改其他 scanner 或 Rag 运行时。
**Interfaces:** scan_tree(rag_root, scripts_root) -> dict、write_report(report, output_path)、CLI 保持兼容；writer_completeness 永远 not_proven。
- [x] 阅读现有 alias/hint/context visitor 和测试，构造不执行的微型源码 fixture。
- [x] 测试先行：if/else 不同连接、循环零次、try 异常路径、with 内重绑定后作用域、class body import/hint 泄漏；简单确定直线 alias/相同分支正例；扫描源码含 raise/副作用但不执行。
- [x] 请求 root 运行 RED，确认错误归因后最小修正。无法可靠解析的 receiver 降为 unknown，不能丢 sink。
- [x] 为分支/作用域做保守状态合并，避免假定某条分支必经；不构建推测性调用图。
- [x] 请求 root 运行 tests/test_transaction_ownership_control_flow.py tests/test_transaction_ownership_inventory.py tests/test_sql_portability_inventory.py 中存在的相关组。
- [x] 独立 SPEC/QUALITY 复审，关闭问题；root 集成。

## Task 4: Root 集成与本轮证据
**Files:** Update docs/local-readiness.md; docs/architecture/command-transaction-boundaries.md; docs/architecture/dynamic-sql-and-writer-review.md; Create docs/reviews/2026-10-27-local-safety-followups.md.
**Interfaces:** 仅 root 修改共享文档、运行证据脚本、冻结和测试；不得把旧补充包当新源码包。
- [x] 为本轮新建 artifacts/local-safety-followups-2026-10-27 与独立 SDD ledger；记录前后 main/状态/.env 哈希和8080基线，不读取配置正文/业务库。
- [x] 每任务 RED/GREEN 后独立复审；所有产品变动停止后跑 Ruff、变更模块格式检查和相关集成回归，按风险运行完整后端。
- [x] 重新生成 writer 候选清单，记录解析覆盖与 unknown，仍 not_proven；运行源码前后哈希检查，禁止复用上一轮冻结。
- [x] 若 Rag 源码改变，重新绑定受影响 SQL 审查来源；未变条目只有身份和全部证据哈希一致才保留，变更条目实际复核。
- [x] 完整分支独立复审并更新当前文档，列出本轮实测、真实 skip、服务器验收和长期架构待办。
- [x] 提交公司中文工作记录并检查成功回执。本轮默认保留可评审的本地改动，不自动更新既有交付包或推送。

