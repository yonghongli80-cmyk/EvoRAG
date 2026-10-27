# 项目复查整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 修复审查确认的审计/提交与恢复边界问题，并补齐旧命令故障矩阵的本地子集。

**Architecture:** 复用现有UserDB/ConnectionManager事务；成功审计加入各命令现有提交点，外部文件补偿保留。先准备恢复报告再发布隔离目录，context显式携带额外保护根。三路Luna并行处理不相交文件，materials内部子任务顺序执行。

**Tech Stack:** Python3.12、SQLite、FastAPI、pytest；已有开发/生产锁隔离环境。

**Spec:** `docs/superpowers/specs/2026-10-27-audit-and-restore-contracts-design.md`

## Global Constraints

- 基线8184463，隔离分支codex/review-remediation-2026-10-27。原Dmain99834ca、配置和业务文件不改；旧迁移包不变。
- 单pytest进程、BelowNormal、BLAS1、全新短OS临时目录、既有offline_network_guard和test isolation；root统一运行RED/GREEN并保留native exit和JUnit，不让三个pytest争资源。
- 生产代码先有能复现原问题的失败回归；只读/低影响文档无需镜像测试。所有操作使用合成数据。
- 不改权限/HTTP响应/非canonical identifier兼容，不扩成分布式事务或给异步索引套长事务。
- 不覆盖旧证据；不自动部署/触碰业务数据。集成与远端交付状态分开记录。

## Review Focus

- 已有owner/current版本的上传遇审计故障，应保存原状态与唯一可恢复文件（T3a）。
- archive helper内部直接commit不能穿透命令外层事务（T3a）。
- 删除终态提交失败不能保留成功审计，已删外部文件仍按deleting重试（T1）。
- conflict/replay/already_rolled_back不记假成功；审计失败不留下部分整理（T3b/T3c）。
- 外置Qdrant祖先/后代路径、已有恢复目标、报告写失败和重试均受保护（T2）。

## T1：KB删除终态与审计原子性（并行）

**Files:** 修改`rag/routers/knowledge.py`；新增`tests/test_kb_deletion_audit_atomicity.py`。只在需要时改`rag/kb_lifecycle.py`，先报root。

**Interfaces:** 沿用permit.finish_delete(cleanup_sql)；callback内先_audit_org_event再finish_kb_cleanup_sql，均由finish_delete拥有同一transaction，不让policy先消失。

- [x] 写组织KB合成回归：audit insert失败、terminal cleanup失败、真实deferred commit失败；断言无已提交success审计、lifecycle仍deleting、SQL未终态，外部清理状态独立保留。
- [x] root运行RED并记录真实失败后，最小调整callback。
- [x] 重试成功断言lifecycle deleted、终态SQL完成、审计恰好一次；非org路径保持。
- [x] root定向GREEN、独立复审、提交T1。

## T2：恢复发布与外置存储保护（并行）

**Files:** 修改`rag/backups/service.py`、`rag/backups/models.py`、`scripts/backup.py`；测试`tests/test_backup_restore.py`、`tests/test_backup_cli.py`、必要时`tests/test_backup_models.py`。

**Interfaces:** BackupContext末尾追加默认空的restore_protected_roots元组（保持旧构造兼容），context_from_settings填入本地qdrant_path；CLI合并已有保护根。restore_test签名不变。

- [x] 合成restore报告写OSError，断言正式目标/临时副本不残留、随后重试成功；已有目标不覆盖。
- [x] 从配置构造context，外置Qdrant目标/祖先/后代被CLI拒绝，外置目录内容保持；合法独立目录成功。
- [x] root RED后，改为报告在temporary写入/验证，再rename发布；额外保护根只控制目标，不纳入备份载荷。
- [x] root GREEN backup全组、独立复审、提交T2。

## T3a：普通/归档上传提交审计（与T1/T2并行）

**Files:** 修改`rag/materials/api.py`、`rag/materials/archives.py`、`rag/repositories/material_repo.py`和`tests/test_material_upload_cancellation.py`；新增`rag/materials/org_audit.py`与`tests/test_material_upload_audit_atomicity.py`，按需用独立archive测试模块。取消回归改为观察真实组织审计落库，保留worker/stage/线程断言，不能为旧API helper spy恢复后置审计。

**Interfaces:** `record_org_material_audit(db, actor, action, kb_id) -> None`复用原org policy判定与OrgAuditService.record合同，API兼容helper可委托它；不独立commit。普通上传在finalize回调内调用。archive upload inspection/hash后，短db transaction内聚合已有permission/version/manifest/entry与audit；相关manifest/entry helper改用cm.transaction而非直接commit。初始create_document_version已带quarantine路径，移除冗余set_version_file_path以免穿透事务。

- [x] 新/已有文档上传审计失败的HTTP与真实SQL/stage回归；成功只记一次且owner不变。
- [x] 归档entry/audit/外层commit失败的真实SQL回归：新记录回滚、旧container保留、源文件保持、无悬挂quarantine引用。
- [x] root RED后实施，检查所有参与helper无直接commit、文件inspection在写事务外、last_*在成功提交后更新。
- [x] root GREEN upload/archive相关组；独立复审后提交。T3b/T3c只能随后改同一api/archives接口。

## T3b：确认/回收/恢复审计（T3a之后）

**Files:** `rag/materials/api.py`、`rag/materials/archives.py`、`rag/materials/recycle.py`；新增`tests/test_material_state_audit_atomicity.py`。

**Interfaces:** 复用T3a helper，直接在service现有SQL提交点记录；API去掉后置mutation审计，preview审计不改变。幂等archive replay在mutation前返回，不新增事件。

- [x] 审计trigger失败回滚archive激活/manifest/result/jobs以及recycle/restore状态，已有文件补偿保持。
- [x] 同key archive确认重放不增加成功事件，非org保持；root RED后改提交点。
- [x] root GREEN archive/recycle/API生命周期组、独立复审、提交。

## T3c：整理结果与审计幂等（T3b之后）

**Files:** `rag/materials/api.py`、`rag/materials/organizer.py`；新增`tests/test_material_organization_audit_atomicity.py`。

**Interfaces:** 只在本次真实状态变化后、同一transaction内调用T3a helper。同action同KB集合去重；不增schema或新全局幂等键协议，返回结构保持。

- [x] 合成applied/conflict/partial、confirm same-key replay、rollback changed_since/未应用/重复回滚和多KB结果，断言metadata与实际成功审计对应。
- [x] 注入audit SQL失败，业务字段/batch结果一并回滚；root RED后实施。
- [x] root GREEN organizer/API组、独立复审、提交。

## T4：SQL信任合同、证据与交接（root，代码任务后）

**Files:** 新增`docs/architecture/sql-trust-boundaries.md`；更新command-transaction-boundaries、local-readiness、docs索引和本轮报告；动态SQL清单/source绑定按现有工具检查后更新。

- [x] 逐项说明10条SQL follow-up的可信调用方、artifact/catalog/snapshot来源、SQLite保证与不保证；保留quoted扩展兼容，不伪报漏洞或PG支持。
- [x] 对受影响SQL源重新扫描/绑定并独立复核；writer候选仍不是完整writer证明。
- [x] 冻结最终代码，完整后端/生产锁合成烟测、Ruff/格式、源码前后hash、整分支独立复审。新失败据实处理，不复用昨日3481/5。
- [x] 核对原服务状态/配置/main/跟踪树保持，记录所有真实缺口和后续架构依赖；实际submit当前根任务工作记录并检查成功。

执行状态、RED/GREEN证据与判断写入本计划专属SDD ledger，完成后将结果回填本页。本地代码完成不等于目标服务器或真实业务验收完成。

## T5：显式SELECT工具的文件只读打开（可与T1/T2/T3并行，T4冻结前完成）

**Files:** `rag/tools.py`；新增`tests/test_sql_tool_readonly.py`。

**Interfaces:** create_sql_tool(db_path)及返回callable不变。文件路径通过Path.resolve().as_uri()编码后加mode=ro，SQLite以uri=True打开；:memory:保持特例。保留SQL关键词/分号门禁、query_only和结果上限。

- [x] 先写行为RED：缺失db路径执行SELECT1不应成功或留下文件，查询错误也不创建空库；合法含空格/Unicode/#/%路径返回手工预置行，:memory: SELECT仍工作。
- [x] root RED确认文件创建缺陷后才改打开方式；不扩大授权DB范围，不改变允许SQL集合。
- [x] root GREEN新模块与既有test_tools组，独立复审、提交。T4说明此文件生命周期问题与SQL授权信任边界的区别。

## T6：旧版本表重建前拒绝未知列（与T3b/T3c文件独立，T4冻结前完成）

**Files:** `rag/repositories/schema.py`；新增`tests/test_document_versions_legacy_schema_guard.py`。

**Interfaces:** rebuild_document_versions_status(conn)签名与已知schema的DDL不变。仅需要重建的legacy表，先用固定PRAGMA table_xinfo核定已知12列且hidden=0；名字按SQLite ASCII大小写兼容，未知/生成列以不回显schema内容的RuntimeError拒绝。检查必须在foreign_keys/legacy_alter_table切换与BEGIN/DDL前；已经支持recycle_pending的表保留原早返回行为。

- [x] 合成旧表分别携带额外真实业务字段、virtual/stored生成列，确认旧代码可成功升级却丢列的行为RED；新合同应拒绝且原table SQL、值、index/trigger、FK开关与事务状态保持。
- [x] 已知12列旧库仍能升级并保留canonical字段数据和自定义index/trigger，新增recycle_pending状态可用。禁止把spreadsheet扩展schema误纳入此限制。
- [x] root RED后实施最小只读schema guard，不改UserDB schema/数据迁移内容，不在实际业务库运行。root GREEN本模块及已有版本迁移/FK回归，独立复审、提交。

本轮本地整改与验收已完成：受测5c501f1，完整3517/5、生产锁15场景、622文件前后核对、分项/整分支/证据复审通过。实际审查整改记录R-20261027-ACFC835E已submit成功(native0)；后续双推回执与补充包按独立交付事项记录，服务器/真实业务与长期架构缺口见最新交接。
