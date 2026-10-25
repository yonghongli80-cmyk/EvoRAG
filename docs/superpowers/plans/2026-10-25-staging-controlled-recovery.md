# Historical Staging Controlled Recovery Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. User explicitly requests parallel Luna on disjoint scopes; root owns design, Git, timer and final verification.

**Goal:** 实现两个有限、可验证且可重试的历史暂存维护恢复流程。
**Architecture:** Task1纯文件保护、Task2持久回执与原子CAS并行；Task3依赖两者接口实现preview/apply。保留audit只读，不接入日常HTTP或自动恢复。
**Tech Stack:** 当前Python3.12/SQLite/KB lifecycle/ACL/pytest/Ruff，无依赖更新。
**Spec:** `docs/superpowers/specs/2026-10-25-staging-controlled-recovery-design.md`。

## Global Constraints

- C工作树分支codex/staging-controlled-recovery-2026-10-25，基线380813f；D main仍1d413ed，不merge/push/deploy/restart，不读真实.env/业务库或恢复真实文件。
- 仅version_pointer staging missing声明+显式相同version目录候选，以及非rag_docs pending_confirmation/staging且未过期archive global stage copy-rehome；hash必须与DB可信64hex匹配。原字节永久保留，无激活/重试job/confirm/删除。
- preview无文件/metadata/receipt写；apply需maintenance_confirmed=True、当前actor重新授权、同步KB permit、顶层BEGIN IMMEDIATE、全局引用检查、重新hash与CAS，成功metadata和回执同事务；失败不删字节/身份。
- 子代理不Git/timer/submit/派生代理；只改指定文件、不读其他SDD。root timerT-A9195D72ED29。Python=`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`；runtime同env/runtime-python；不使用默认Python。
- 测试PYTHONPATH=repo;artifacts/kb-lifecycle-concurrency-2026-10-24/tools;artifacts/staging-recovery-2026-10-25/verification/tools，pytest `-p offline_network_guard -p staging_test_priority`，BLAS1，新短basetemp且不重设TEMP/TMP。日志artifacts/staging-controlled-recovery-2026-10-25/，最多2workers、BelowNormal。

## Review Focus

- 路径只按字符串归属不足：Task1覆盖root/ancestor symlink/junction、..、hardlink、特殊文件、读期间变化；支持Windows能力skip要明确。
- 旧计划在ACL/owner/job/共享引用变化后失效：Task3 preview/apply间变更及重放撤权回归，不信任plan字段。
- copy成功但SQLite commit失败或进程崩溃：Task2原子回滚与Task3保留copy、同请求可重试，绝不先删原件。
- 回执重放泄露他人操作/跨请求借key：Task3 actor重载+ACL先于回执，key/hash不符拒绝；Task2唯一key测试。
- 新维护功能污染只读CLI/正常启动：Task2真实app迁移v6、已有迁移回归；Task3固定audit依然inspect_only和无新route。

### Task 1: Confined hashed file validation and preserved copy

**Files:** Create `rag/materials/recovery_files.py`, `tests/test_staging_recovery_files.py`。
**Interfaces:** `RecoveryFileError(code: str)`；frozen `RecoveryFile(path: Path, sha256: str, byte_size: int)`；`confined_recovery_path(root: Path, path: str | Path) -> Path`（允许缺失，已有file只regular/nlink1；root已存在绝对普通目录、root及祖先无links）；`verify_recovery_file(root: Path, path: str | Path, expected_sha256: str) -> RecoveryFile`；`copy_recovery_file(root: Path, source: str | Path, destination: str | Path, expected_sha256: str) -> RecoveryFile`。copy安全创建parents、xb/fsync/分块、已有同hash目的地可重用、从不覆盖/删原件/失败copy。

- [ ] 写真实Temp测试并运行RED：错误hash、逃逸/..、root/parent/file link、hardlink、特殊类型、读取变化拒绝；copy不覆盖/源保留/失败copy保留、已有匹配copy重试。
- [ ] 实现纯文件模块，不依赖config/UserDB；同一handle前后fstat及路径身份比较；read/copy块最多64KiB，异常稳定code，不吞未知错误。
- [ ] GREEN+Ruff/format，自审并写本SDD task-1-report.md（实际命令/结果/限制）。通知root和Task3接口READY。

### Task 2: Application receipt migration and transaction-only repository

**Files:** Modify `rag/db_migrations.py`；Create `rag/repositories/staging_recovery_repo.py`, `tests/test_staging_recovery_receipts.py`。仅必要时更新已有migration版本断言，并报告。
**Interfaces:** app migration6表material_recovery_receipts字段见Spec；`get_recovery_receipt(conn: sqlite3.Connection, operation_key: str) -> dict | None`；`insert_recovery_receipt(conn, *, operation_key: str, request_hash: str, actor_user_id: int, document_id: int, version_id: int, receipt: dict) -> None`（不commit）；`cas_recovery_paths(conn, *, version_id: int, expected_version_path: str, destination_path: str, manifest_id: int | None = None, expected_manifest_source: str | None = None, update_version: bool = True) -> None`（version须staging，manifest须pending_confirmation；CAS0抛`RecoveryReceiptConflict`，不commit、不激活/改状态）。`RecoveryReceiptConflict`为此repo异常。

- [ ] RED测试真实UserDB新建与v5升级安装v6、migration重跑不改变数据；同key唯一性、CAS失败、deferred FK commit失败metadata+receipt同回滚，表不随doc删除cascade。
- [ ] 实现migration及小repo，helper仅使用调用者conn且要求conn.in_transaction，禁止内部commit/新事务或reader。业务身份/ACL由Task3负责。
- [ ] GREEN含既有tests/test_db_migrations.py、test_startup_migrations.py；写task-2-report.md并通知Task3接口READY。

### Task 3: Maintenance preview/apply orchestration and operations guide

**Files:** Create `rag/materials/recovery.py`, `tests/test_staging_controlled_recovery.py`, `docs/staging-recovery.md`；Modify `docs/staging-audit.md`仅链接新流程、保持只读边界。
**Interfaces:** Spec中RecoveryRequest/RecoveryPlan/RecoveryError和preview/apply签名逐字使用；消费Task1/2以上接口。回执dict固定schema_version=1, action='recovered', operation_key, kind, actor_user_id, document_id, version_id, manifest_id, source_path, destination_path, content_hash, byte_size, source_preserved=True；重复返回完全相同dict。metadata_fingerprint规范JSON包含目标行+完整相关引用快照，request_hash同时绑定request/root/source/dest/期望路径/hash/大小/fingerprint，排除request_hash自身，目标copy名可先由request规范hash计算，禁止自引用哈希。

- [ ] 先写RED（缺接口可收集失败），等Task1/2 READY后运行合成场景。happy paths、preview不改树/DB、原current不动、两路径/receipt原子、幂等、异参冲突、撤权/停用actor、错误KB/owner/hash、共享路径/identity/current-pointer、active job、过期manifest/tombstone、篡改/陈旧plan、嵌套事务、提交失败保留copy及重试必须覆盖。
- [ ] 实现按spec同步流程和稳定错误；每次权限重查先于回执；apply不能直接相信plan内路径/hash/actor，重新preview并比较所有字段（成功重放单独核验最终身份/ACL/路径/hash）。全局目标路径引用包含source与destination，不只文件名。v6回执表由真实UserDB初始化，preview不DDL。
- [ ] 写维护手册：独立授权、停所有写者、备份含stage、显式映射与内容证明、preview复核再apply、回执核验、失败保留与重试、恢复不等于重建索引；没有可写CLI/HTTP。本轮只合成验收、不提供真实数据命令代入。
- [ ] GREEN+Ruff/format，报告task-3-report.md。root逐任务提交/独立复审，Task3工作依赖1/2接口但文件不重叠。

### Task 4: Root integration, independent review and continuation

- [ ] 独立分项规格/质量复查，真实发现按原implementer修复和增量review；最后一次整分支复查，冻结源。
- [ ] 统一新三文件+既有audit/recovery/archive/lifecycle/迁移相关回归，Ruff及生产锁实际API烟测；所有数据合成Temp，记录实际JUnit/exits，不重复无关全量。
- [ ] 原服务identity/ready/env hash/Dmain只读核对；更新交接/总计划、实际submit工作记录，保留所有源码/证据环境；本地不部署。
- [ ] 接续容量/SLO及长期存储架构：以单机1000/5000/10000文档、并发1/2/4作明确工程实验假设，不能冒充业务生产目标。
