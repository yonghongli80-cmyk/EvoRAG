# Staging Safety and Snapshot Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复材料upload取消/入队与KB cleanup的所有权缺口，并提供无写入的快照诊断。
**Architecture:** stage交接给不可被请求取消的同步worker；最终metadata/job短事务提交，文件补偿保留身份。删除前证明每个存储owner；独立纯模块以immutable SQLite快照及显式目录产生只读JSON。
**Tech Stack:** Python3.12、asyncio、SQLite、现有UserDB facade/portalocker/FastAPI，无新依赖。
**Spec:** `docs/superpowers/specs/2026-10-25-staging-safety-design.md`

## Global Constraints

- 全部写入仅在C隔离分支；不重启、停止、部署或压测原8080/PID48512，不打开业务DB/向量目录、不读真实.env；只允许事前/事后只读ready、进程和.env哈希核对。
- 验证使用既有锁定私有Python、offline_network_guard、系统Temp短basetemp；不启动未隔离lifespan，不调用模型/外部服务/Docker，不升级依赖。
- 不改schema、ACL、模型/canary/shadow、Redis可选和沙箱fail-closed。api.py<=830、pipeline.py<=1000、局部导入<=150，不放宽预算。
- 所有自动验证只用合成资料，最多4 worker；子任务定向串行测试，root稳定源码后统一一次完整后端；无前端修改不重复前端全套。
- 不删除/搬移现有工作树、环境、业务或Docker数据。旧global staging不自动回收；本轮没有任何实际rehome/cleanup执行。

## Review Focus

- executor尚未开跑就取消，以及已开跑后取消/失败；stage须由worker最终处置，不能留未观察异常（Task1）。
- 新版job INSERT失败时旧job cancellation必须回滚；补偿move-back再失败不能删owner（Task2）。
- archive confirmed与失败activation的合法source/final双表示不能被新owner检查误拒（Task3）。
- 外KB引用目标owner目录或同文件时，整次删除先拒绝，其他文件/SQL状态保持（Task3）。
- WAL快照、symlink/junction、缺schema和未知global stage报告不得隐式写/删除或推断可apply（Task4）。

## Execution Context

Workdir始终显式使用`C:/Users/Syf15/.codex/worktrees/post-handoff-remediation/RAGv3`，base `3f52490`，branch `codex/staging-recovery-2026-10-25`。PY为`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`；PYTHONPATH=repo根+`artifacts/kb-lifecycle-concurrency-2026-10-24/tools`，pytest加载`-p offline_network_guard`，basetemp用系统Temp中的短随机`s25xxxxxx`且事前不存在。定向日志写`artifacts/staging-recovery-2026-10-25/task-N/`。root统一Git和公司计时T-1875641B3C7F，子代理不另记、不commit/stage、不再委派。

### Task 1: Worker-owned stage handoff and cancellation

**Files:** Modify `rag/materials/api.py`; Create `rag/materials/upload_execution.py`, `tests/test_material_upload_cancellation.py`。
**Interfaces:** Produce `async run_upload_worker(worker: Callable[[], T]) -> T`（具体类型注解可用TypeVar）；先建立shielded task再await；外层取消只取消等待，worker结果须被观察。Task2沿用已完成后的route worker结构。

- [x] Step 1: 写真实Temp DB/文件route函数测试，用thread Event阻塞promotion/archive copy，取消caller后assert stage仍存在、gate仍忙；放行后真实worker成功并cleanup。另用单worker executor预先占用证明queued worker在caller取消后仍执行；failure分支不产生loop未处理task异常。
- [x] Step 2: 使用精确新测试运行并确认预期RED（当前async finally提前unlink/queuedfuture可取消）；记录exit与断言，不能以人为sleep替代barrier。
- [x] Step 3: 实现shielded交接，cleanup/preservation与组织audit移入实际同步worker；普通/归档都覆盖，保留API状态/响应、权限和原promotion失败保留字节契约。
- [x] Step 4: 新文件、`tests/test_material_backup_integration.py`、`tests/test_kb_lifecycle_materials.py`与相关material API定向GREEN；Ruff、diff检查，根选择性提交后fresh规格/质量复审。

### Task 2: Atomic finalization and job enqueue

**Files:** Modify `rag/db/connection.py`, `rag/document_versions.py`, `rag/repositories/material_repo.py`, `rag/user_db.py`, `rag/materials/archives.py`, Task1完成后的`rag/materials/api.py`; Create `tests/test_material_upload_commit.py`; extend `tests/test_archive_import.py`。
**Interfaces:**
- Add optional `finalize: Callable[[PromotedVersion], None] | None = None` to `promote_staged_version` and its private helper; existing PromotedVersion字段为version_id/version_no/file_path。finalize是纯SQL回调，在file move后由promotion helper拥有一个短locked_transaction调用；repo嵌套事务复用该连接，不允许回调内I/O或显式commit。default path behavior remains.
- Add `UserDB.finalize_material_upload(document_id,version_id,staged_path,final_path,content_hash,not_before)->int` and repository method. Same short transaction validates permission/lifecycle and staging version/CAS oldpath, updates finalpath, cancels oldactive material jobs and inserts newjob. Factor existing enqueue SQL into `_enqueue_latest_index_job_in_tx(conn,...)` so archive's existing transaction composes without commits.
- ConnectionManager.transaction保留nested reuse语义；body或commit失败均在write lock内对active transaction回滚，不能让补偿写入复用未清的失败事务。
- `VersionPromotionError(message, *, recovery_required: bool=False, version_id: int | None=None, file_path: Path | None=None)`；failed move-back或无法确认补偿完整时保留reservation row/owner，尽力记录实际finalpath，API不得cascade-delete新permission。

- [x] Step 1: 先加SQLite INSERT-trigger失败用例，覆盖新document与已存在current/oldjobs；再以真实DEFERRABLE外键+job trigger覆盖INSERT成功但COMMIT失败，assert in_transaction=False、旧job/新job/version状态回滚、stage保留且下一合法事务可成功；断言500 material_promotion_failed、保留字节、无新job/version且旧状态不变。另加finalize回调调用repo后抛错的事务回滚、真实move完成后再抛错的补偿、move-back失败身份保留用例；若stage/final两份同时存在且无法确认，保留两份与owner而不overwrite。archive child enqueue失败须无confirmed/idempotency且同key重试成功。
- [x] Step 2: 定向RED验证当前jobless版本/commit后enqueue缺陷；所有fixture仅临时文件，不改生产failpoint。
- [x] Step 3: 修正共享transaction的commit异常回滚边界，接入finalize callback与短事务，保留正常promotion兼容；错误补偿不删除唯一final字节/owner。archive新child job入队移入activation/manifest/result事务，commit后只做文件cleanup。
- [x] Step 4: 新测试、material jobs/backup/version/archive与Task1取消用例GREEN；Ruff/架构，根提交与fresh复审。

### Task 3: Owner-validated KB teardown

**Files:** Modify `rag/kb_document_cleanup.py`, `rag/kb_lifecycle_cleanup.py`; Create `tests/test_kb_cleanup_ownership.py`；必要已有`tests/test_kb_failed_mutations.py`/`tests/test_kb_lifecycle_api.py`仅本任务调整。
**Interfaces:** Produce pure `resolve_owned_version_path(root: Path, stored_path: str, *, document_id: int, kb_id: str) -> Path`，接受document final/doc staging/同KB hashed staging，拒绝lower symlink/junction/escape及别owner。`resolve_kb_document_paths(..., *, kb_wide: bool=False)`保持默认单文档严格owner契约，collect以kb_wide=True复用新whole-KB规则；不能放宽单文档删除。Task4可直接消费纯helper（不导入config）。

- [x] Step 1: 真实Temp DB/File构造“permission无kb_documents且version指向别KB文件”、错误archive container_version_id、外KB共享ref，路由DELETE应503有稳定机器码且manager无调用、文件和身份保持。先观察当前代码RED。
- [x] Step 2: 实现全version owner校验及manifest identity检查，存在legacy-global source保留既有archive_source_unowned；不要求confirmed/failed-activation的source等于合法container finalpath。外KB引用planned路径/目录时cleanup_path_shared；所有验证在collect完成前，不引入long SQL事务。
- [x] Step 3: 加正常pending/confirmed、失败confirm残留合法container_version.file_path final（manifest.source_path仍hash staging）、第二KB完整性、document-ID stage、missing target、link/junction拒绝兼容GREEN；source_global/source_final均拒绝且legacy paired global优先archive_source_unowned。root只控制自有合成路径。
- [x] Step 4: 新用例与KB cleanup/API/permissions相关GREEN；Ruff/架构，根提交与fresh复审。

### Task 4: Explicit snapshot read-only staging audit

**Files:** Create `rag/staging_audit.py`, `scripts/audit_staging.py`, `tests/test_staging_audit.py`。不改schema/UserDB/公共conftest。
**Interfaces:** Consume Task3 `resolve_owned_version_path`; produce `audit_staging(database: Path, versions_root: Path) -> dict`，`schema_version=1`、entries/findings/counts、每项action=inspect_only。CLI必填`--database`与`--versions-root`、报告及输入错误为JSON（--help显示标准帮助）、success0/inputerror2，未知--apply拒绝。

- [x] Step 1: 用关闭后的自包含Temp DB快照与合成目录写测试；missing input不建DB、WAL/SHM拒绝、DB/tree hashes及目录清单不变、symlink/junction不跟随；先确认新接口缺失RED。
- [x] Step 2: mode=ro&immutable=1与显式snapshot校验，无config/.env/UserDB/app导入；表结构optional-aware，缺核心表unsupported_schema。扫描staging/archive_work而不读取正文，root外ref不做fileIO。
- [x] Step 3: 报告referenced/pending和未注册stage、缺文件、owner/sharedrefs、unowned archive、staging版本缺material job的事实与其它引用上下文；不将缺job或文件名当可删除owner证明。覆盖legacy committedstage与confirmed source消失的合法状态，避免误报实际可清理。对declared path missing的staging row只检查其root/document_id/version_no目录，报告regular non-link owned_version_candidate，不猜文件名/内容hash且不更新DB；加move-back+repoint同时失败组合的synthetic snapshot用例。
- [x] Step 4: CLI在不同cwd/无.env场景真实subprocess验证JSON/exit，所有新用例GREEN、Ruff；根提交与fresh复审。

### Task 5: Integration, evidence and handoff

**Files:** Create `docs/reviews/2026-10-25-staging-safety.md`, `docs/staging-audit.md`; update本计划、`docs/README.md`、`docs/handoff-2026-10-24.md`。

- [x] Step 1: root核对所有任务的接口/文件无冲突，fresh任务复审与整分支复审，处理实际Critical/Important并冻结源码。
- [x] Step 2: 使用同一锁定环境/离线guard/系统Temp短基准目录完成稳定源码完整后端（最多4worker/loadfile、BelowNormal、BLAS1），全树Ruff/架构与必要runtime smoke；不重复未变frontend。
- [x] Step 3: 写只读CLI快照来源、报告code/限制、promotion补偿/无自动恢复的真实边界；保留失败日志，不用分项数拼全量结果。
- [x] Step 4: 只读核对原PID/ready/.env hash，根统一代码文档提交；真实清理/rehome/业务切换不在本任务执行范围。
- [x] Step 5: 提交当前计时T-1875641B3C7F的唯一公司记录并核对成功，保留工作树和证据。

## 整分支审查后的唯一修复波次

- [x] reviewed HEAD `8f45b88` 的补偿DELETE事务污染、default rag_docs global-stage候选漏报、NUL路径JSON错误契约三项一并修复并完成一次fresh增量复审。
- Files: `rag/repositories/doc_repo.py`（仅两个DELETE helper）、`rag/staging_audit.py`、new `tests/test_staging_recovery_integration.py`、`tests/test_staging_audit.py`。
- 真实deferred DELETE commit、实际rag_docs route→snapshot、含NUL SQLite路径先RED再GREEN；修复提交`192aa8e`，28项新回归与58项相关回归通过，fresh增量复审三项均ADDRESSED、无新问题。源码冻结后由root统一全量，具体证据见本轮最终报告。

## 最终验收

代码/测试冻结`192aa8e`。首轮宿主工具异常停在约96%，无pytest退出码/JUnit，不计为完整通过；确认旧测试进程全部退出后，以2 worker/BelowNormal/BLAS1、短系统Temp与离线guard重新完整运行：**3207通过/4跳过**，830.29秒、退出0；JUnit3211项、0失败、0错误。全树Ruff、生产锁烟测及架构预算通过，原PID48512/ready200/启动时间/.env哈希保持。详情与运行/部署边界见[最终报告](../../reviews/2026-10-25-staging-safety.md)。本轮新分支尚未合并main或双推。

根实现事项已实际执行ASCII JSON提交，`submit --timer-id T-1875641B3C7F`退出0，记录`R-20261025-326EBF8F`已创建，状态`pending_review`；不是仅计时或准备JSON。原始返回保留于`verification/work-record-submit-attempt-1.log`。
