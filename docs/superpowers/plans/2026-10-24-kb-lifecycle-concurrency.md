# KB Lifecycle Concurrency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除KB删除与上传及后台写入的交错成功、数据丢失和collection复活，提供可恢复删除屏障。

**Architecture:** 稳定的同KB OS文件门禁覆盖真实写入生命周期，SQLite短事务保存deleting/deleted和删除token。API、公共导入、materials/spreadsheets与Wiki共用准入，短事务额外防止墓碑后的发布。外部I/O不放进长用户数据库事务。

**Tech Stack:** Python3.12/FastAPI/SQLite WAL/portalocker/pytest；临时Qdrant真实本地存储+离线embedding。

**Spec:** `docs/superpowers/specs/2026-10-24-kb-lifecycle-concurrency-design.md`

## Global Constraints

- 不重启、停止、部署或压测原127.0.0.1:8080/PID48512，不打开或迁移业务DB/向量目录，不读写真实.env。只允许事前/事后只读ready、进程与.env哈希核对。
- 所有运行验证只用系统Temp合成数据、已锁私有Python和本轮offline_network_guard；禁止独立探针启动源相对路径app lifespan。正规conftest完全隔离的受控lifespan可用。
- 不升级依赖、不添加必需Redis，不改变模型、ACL、canary/shadow或沙箱fail-closed；api.py<=830、pipeline.py<=1000、局部导入<=150，不放宽门禁。
- 旧版写入者不遵循新门禁，真实启用必须在停写/统一升级窗口完成。本轮只在临时库验证添加schema和恢复，不执行业务切换；源码交付按既有约定处理，不把源代码合并称为协议已在原实例运行。
- 所有受支持进程必须用同一个规范化绝对SQLite路径并共享其相邻锁目录；通过不同挂载别名打开同一SQLite文件不在互斥保证内，除非部署显式配置同一共享锁目录。
- rag.org_auth.seed.seed_organization 是离线引导写入者，只允许在应用未服务请求时运行或位于统一停写维护窗口；仍须用事务内生命周期fence拒绝墓碑KB复活。
- 不重试上轮被自动审核拒绝的SDD目录清理，不删除/搬移任何旧工作树、环境、Docker数据或业务文件。

## Review Focus

- HTTP取消后后台线程仍写：实际线程退出前，门禁必须继续阻止删除/新写入。
- 删除后或删除失败重启：永久墓碑与可恢复意图不能随metadata级联消失，也不能按TTL自动放开。
- 同线程不同async任务、跨进程、嵌套准入：互斥与重入都必须对应真正owner。
- materials/archives/spreadsheet/Wiki/retry旁路：不得仅修两个HTTP入口，任务被取消后不能重新发布。
- 文件和最终事务失败：其他KB、原身份和重试线索不能因部分清理而丢失；没有匹配行的UPDATE不能假报成功。

## Environment

工作树 C:/Users/Syf15/.codex/worktrees/post-handoff-remediation/RAGv3，基线9f0574b。PY=artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe；每条pytest设置PYTHONPATH为repo根和 artifacts/kb-lifecycle-concurrency-2026-10-24/tools，加载 -p offline_network_guard，系统Temp，不能用仓库内basetemp。日志各用 artifacts/kb-lifecycle-concurrency-2026-10-24/task-N/。根统一Git写入和公司记录 T-28247353A251；子代理不得Git写入、开启子代理或另记工作记录。

既有复现见audit/repro-report.md；它断言坏行为存在而passed，不是已修复证据。正式回归应反过来断言“不允许双成功却资源消失”。

### Task 1: Durable lifecycle store and owned cross-process admission

**Files:** Create `rag/kb_lifecycle.py`, `rag/kb_lifecycle_http.py`, `rag/repositories/kb_lifecycle_repo.py`; Modify `rag/repositories/schema.py`, `rag/user_db.py`; Create `tests/test_kb_lifecycle_core.py`。

**Interfaces produced:**
- `db.kb_lifecycle_repo` 与 `db.get_kb_lifecycle(kb_id)->dict|None`。
- schema `kb_lifecycle(kb_id PRIMARY KEY,state CHECK deleting/deleted,delete_token TEXT,generation INTEGER,requested_at REAL,deleted_at REAL,last_error_code TEXT)`，独立于metadata，无级联FK；无行不替代现有授权。
- `kb_mutation(db,kb_id,*,mode='write')` 同步context manager，yield `KBMutationPermit`；mode为write/delete。rag_docs写入保持既有系统策略，delete拒绝。
- Permit `begin_delete()->str`（授权由调用者在门禁内完成，未知metadata不写意图）；`fail_delete(code)->None`；`finish_delete(cleanup_sql:Callable[[sqlite3.Connection],None])->None`（纯SQL回调与deleted转换同事务）；token不匹配拒绝。
- `assert_kb_available(db,kb_id,*,allow_deleting=False)->None` 和 `assert_kb_available_in_tx(conn,kb_id,*,allow_deleting=False)->None`，只检查生命周期，不授予权限。
- domain errors `KBLifecycleError(code)`, `KBBusy`, `KBDeleting`, `KBDeleted`, `KBUnknown`, `KBDeleteIncomplete`；`lifecycle_http_error(error)->HTTPException`：409 busy/deleting、404 deleted/unknown、503 incomplete，detail可读字符串，机器码头 `X-KB-Lifecycle-Error`。

- [x] Step 1: 写新接口的状态/迁移/回滚与互斥测试，保留未实现接口RED（如属于缺失新接口要如实标识，不伪称既有产品复现）。临时旧schema实例化后旧数据不变；墓碑不会级联删除。
- [x] Step 2: 实现短事务state/token CAS与真实portalocker非阻塞文件锁。锁文件在规范化绝对数据库路径旁的专用目录，以KB id摘要命名；所有进程必须打开相同规范路径并共享该目录。不同挂载别名或硬链接不自动互斥；不unlink锁文件。
- [x] Step 3: 进程内使用非重入互斥锁+ContextVar合法重入，校验PID/线程/async任务身份；异步同线程的不同任务和继承context的子任务不能借锁。准入只在同步worker中取得，持有准入时不得再派发会写入KB的asyncio.to_thread；先在请求协程读入有界临时文件可行，但所有pending/版本/向量/发布副作用必须在实际持锁worker内完成。asyncio.CancelledError不得释放仍运行worker的门禁。内存库按ConnectionManager隔离，只用进程内门禁，状态读写使用正确连接。
- [x] Step 4: 运行 `PY -m pytest -p offline_network_guard tests/test_kb_lifecycle_core.py -q` GREEN、Ruff与架构门禁，不添加局部导入突破150；报告精确命令/退出。
- [x] Step 5: 根审阅并提交/复审本任务，接口稳定后并行派发Task2/3/4/5。

### Task 2: KB HTTP operations and resumable complete teardown

**Files:** Modify `rag/routers/knowledge.py`, `rag/api_security.py`, `rag/knowledge_base.py`, `rag/kb_document_cleanup.py`; Create `rag/kb_lifecycle_cleanup.py`, `tests/test_kb_lifecycle_api.py`；必要既有测试仅 `tests/test_kb_failed_mutations.py`, `tests/test_kb_file_lifecycle.py`, `tests/test_kb_permissions.py`。

**Interfaces consumed:** Task1准入、permit、可用性与HTTP映射；Task4的UserDB.cancel_wiki_reindex_jobs(page_ids: Sequence[int]) -> int；Task5的get_wiki_indexer(user_db=db).remove_kb_content(kb_id, db=db) -> None。**Produced:** knowledge upload/link复用同步导入编排 _ingest_kb_document_sync(source, db, manager, actor, roots)；删除由一个实际同步worker持有delete permit完成，先调用Wiki清理接口，再执行其余外部清理。

- [ ] Step 1: 将audit的真实ASGI/SQLite/临时Qdrant交错改成正式安全断言并观察RED；覆盖上传先入门禁、删除先入门禁、finish UPDATE丢行、链接跨库源快照。禁止两个成功响应却资源缺失。
- [ ] Step 2: 整段pending→ingest→文件/数据集→目录→audit/rollback纳入同一worker准入，保留现有路由可patch的loader/toc/summary/ingest函数和响应字段。源链接先在源准入中读取快照，释放后才进入目标，避免双KB锁环。
- [ ] Step 3: 删除枚举目标KB全部document_permissions、版本、目录、dataset和Wiki page ids，不只kb_documents；借现有storage primitives校验根和owner，先取消这些page ids对应的Wiki retry，再清理Wiki向量与页面，随后处理其余资源。预验证在任何删除前，I/O在用户DB事务外，最后通过permit.finish_delete原子清关系与墓碑。失败503且可重试；已有意图时资源缺失幂等，未知ID不假成功；真实legacy文件与其他KB保持。
- [ ] Step 4: central require_kb_view/edit/administer追加生命周期检查，删除重试显式allow_deleting但原删除授权仍必需；旧orphan edit409和org隐藏语义保留。metadata/目录写入事务内复核状态；新create在物理collection创建前检查墓碑（Manager可新增可选before_create回调，不破坏旧默认调用）。避免缓存绕过墓碑，不强关在途query对象。
- [ ] Step 5: API权限矩阵/故障重试/其他KB与既有KB、版本回归GREEN，根提交并fresh规格/质量复审。

### Task 3: Shared version, batch and spreadsheet writers

**Files:** Modify rag/document_versions.py, rag/document_version_store.py, rag/batch_importer.py, rag/repositories/doc_repo.py, rag/spreadsheets/edit_service.py, rag/spreadsheets/api.py, rag/spreadsheets/migration.py, rag/org_auth/seed.py, rag/org_auth/migration.py; Create tests/test_kb_lifecycle_writers.py；相关既有version/batch/spreadsheet测试按实际调用面补充，不能改公共conftest。

**Interfaces consumed:** Task1 kb_mutation、事务内可用性检查、HTTP映射；Task2不依赖本任务新签名。既有ingest/promote/batch返回契约保持。

- [ ] Step 1: RED证明common ingest、custom BatchImporter、表格编辑/restore及启动中的旧版本/表格迁移遇删除意图仍不能创建或激活；另证明组织seed/legacy-policy迁移遇墓碑ID时不会重新插入metadata/policy；用两连接真实SQLite与合成文件，不以mock存在性作为断言。
- [ ] Step 2: 同步公共编排获得同KB准入，嵌套同owner不重锁；所有外部写及补偿在scope内。migrate_existing_document_versions与migrate_staged_document_versions按每个权限记录的kb_id取得准入后再读写行/文件，保留api.py现有启动调用顺序；新增doc owner staging目录支持（stage_version(...,document_id=None)可选参数，原默认兼容），新公共导入传doc id，让崩溃待发布文件可安全归属。归属不明旧staging不擅删。
- [ ] Step 3: document permission/version预留、激活、create_kb_metadata、finish_kb_document在短事务内加墓碑fence；finish匹配零行抛冲突不假成功。低层repo只SQL检查，不在持DB事务时反向拿文件锁。组织seed保持离线维护入口；seed和migrate_legacy_knowledge_bases在插入KB metadata/policy的事务中调用assert_kb_available_in_tx(conn,kb_id)，遇墓碑整笔事务回滚或安全跳过。
- [ ] Step 4: 表格service入口和migrate_existing_spreadsheets逐KB覆盖全部实际文件/registry/mirror写入，保留edit session/base-version CAS、rollback和rag_docs/文档ACL；API映射错误正确。
- [ ] Step 5: 新用例及version/batch/spreadsheet既有定向GREEN，根提交并fresh审查。

### Task 4: Materials, archives and background jobs

**Files:** Modify rag/materials/api.py, rag/materials/archives.py, rag/materials/worker.py, rag/materials/recycle.py, rag/materials/staging.py, rag/materials/jobs.py, rag/materials/migration.py, rag/repositories/material_repo.py, rag/user_db.py; Create tests/test_kb_lifecycle_materials.py；相关既有material/job/archive测试可调整。

**Interfaces consumed:** Task1 gate/fence/errors；Task3既有promote函数签名保持；Task5 WikiIndexer生产实例显式 user_db=db（同接口契约并行开发，组合测试待Task5实现落盘后运行）。**Produced:** UserDB.cancel_wiki_reindex_jobs(page_ids: Sequence[int]) -> int，只将匹配page IDs且job_scope=wiki的active任务置cancelled并清lease。

- [ ] Step 1: RED覆盖材料custom KB无policy跳过编辑授权、deleting/deleted上传/归档确认、在途index/repair/purge与删除交错、busy后job不丢失或反复复活。
- [ ] Step 2: 自定义KB全部走现有require_kb_edit，rag_docs保留特例。保持分块/大小限制；不在async coroutine中持生命周期permit。所有KB外部写由实际同步worker在同一准入内完成，不能为了锁无界buffer文件。migrate_material_metadata也按document permission的kb_id逐条取得准入，在短事务内fence后再补FTS/逻辑metadata。自定义KB未取得doc id的材料staging放在 versions_root/staging/kb/<sha256(kb_id)>/，保护尚未建身份时的崩溃文件归属；原staging函数新增可选kb_id参数，默认系统路径不变。
- [ ] Step 3: Archive upload/confirm、worker完整执行/补偿/状态完成覆盖scope。busy任务以CAS安全重排，不能把已取消job改回queued；deleting/deleted任务取消且不触向量/激活。permit在真正worker退出前一直有效，保留最新版本/权限/session CAS。Wiki retry worker由UserDB读取page所属KB，在一个同步worker准入下重新读取page并索引；墓碑后不得写向量或settle成重试。
- [ ] Step 4: 生产Wiki retry实例传user_db并与Task5接口协作；取消Wiki job在清Wiki身份前可完成且不误取消别库任务。
- [ ] Step 5: 材料/归档/worker与启动迁移定向GREEN；两份真实脱敏缺失fixture保持原跳过原因，根提交并fresh审查。

### Task 5: Wiki mutation and retry admission

**Files:** Modify `rag/wiki/api.py`, `rag/wiki/indexer.py`, `rag/wiki/store.py`（仅确有必要的定向清理/枚举）；必要 `scripts/verify_wiki_knowledge_loop.py` 生产组合点；Create `tests/test_kb_lifecycle_wiki.py`，既有Wiki测试同步合法新依赖。

**Interfaces consumed:** Task1和Task4的UserDB.cancel_wiki_reindex_jobs(page_ids)。**Produced:** get_wiki_indexer(user_db: UserDB | None = None) -> WikiIndexer；constructor兼容可选user_db，custom KB写不能静默绕过（无db时必须有当前合法permit或明确拒绝）；WikiIndexer.remove_kb_content(kb_id: str, *, db: UserDB) -> None，调用方必须已持有该KB delete permit。该方法按KB枚举page ids及revision titles，先取消这些page ids的Wiki retry、幂等清理全部向量，只有所有向量清理成功后才在一个Wiki事务中删除该KB全部page rows及其revisions/links；失败保留Wiki身份供delete retry。参数名固定user_db，接口ready后通知根与Task2/4。

- [ ] Step 1: RED真实临时Wiki SQLite/Qdrant，KB删后sync/retry能重建collection的旧行为；create/update/rollback/delete与整KB删除交错，其他KB普通doc/wiki点均保留。
- [ ] Step 2: 用户DB KB gate先于Wiki writer transaction，覆盖Wiki DB修改与向量写。保持精确page/revision点删除、既有失败重试收敛，不把每次页删除变全库清理。
- [ ] Step 3: Wiki API/所有生产组合点接入；create/update/rollback/delete与reindex都持同KB同步准入至SQLite和向量写结束；retry enqueue在墓碑时拒绝，重试通过Task4 worker持有permit。remove_kb_content在KB删除器开始其他Wiki关系终结前调用；Task4 owns worker.py，本任务不得并行修改该文件。
- [ ] Step 4: 新/旧Wiki API/indexer/loop回归GREEN，验证锁顺序不造成跨DB死锁，根提交并fresh审查。

### Task 6: Cross-process, cancellation and crash integration

**Files:** Create `tests/test_kb_lifecycle_integration.py` 与必要 `tests/kb_lifecycle_fixtures.py`；owned subprocess helper放当前artifacts或测试utility，不向产品加测试专用方法。

**Interfaces consumed:** Task1-5最终公开契约。可以在前序任务施工期间先设计/准备自己的测试文件，依赖未就绪不伪报GREEN。

- [ ] Step 1: 两个独立Python进程同一合成DB/锁目录：同KB互斥，不同KB并行；可重入/async同线程不同task/fork继承凭据均不能绕过。
- [ ] Step 2: 实际异步请求取消时同步索引/文件线程仍阻塞，竞争删除仍409且无副作用；放行真实工作后重试删除成功且无迟到发布。
- [ ] Step 3: owned child在deleting提交后、向量删后、部分文件删后、最终关系事务前退出；OS锁释放，重启retry收敛。只控制自己spawn且核实身份的进程，不杀已知或未知现存PID。
- [ ] Step 4: 覆盖unlink/向量失败、最终SQL触发器失败的503与保留身份；所有目标资源清完才deleted、墓碑不级联、新旧调用不能复活、其他KB不损坏。真实临时Qdrant，只stub embedding。
- [ ] Step 5: 独立集成矩阵GREEN，所有未适用平台条件单独说明，根fresh审查；发现源码问题只报告根，不越权改其他任务文件。

### Task 7: Unified verification, handoff and source delivery

**Files:** Create `docs/reviews/2026-10-24-kb-lifecycle-concurrency.md`; update本计划、`docs/handoff-2026-10-24.md`、`docs/README.md`。

- [ ] Step 1: 根统一审阅/提交Task1-6、每任务fresh规格/质量复核，完成唯一整分支审查及必要的一轮fix，源码/测试冻结后再统一回归。
- [ ] Step 2: 同一私有开发锁、网络guard、最多4worker/loadfile低优先级，完整后端一次；Ruff/架构及必要生产锁离线核心烟测。前端无变化则保留此前326通过历史口径，不冒充本轮重测。
- [ ] Step 3: 明确Windows/共享文件系统锁保证、故障恢复、403/权限兼容、旧版写入者不能混跑及真实停写部署步骤。全部实际验证只在临时库，不向业务schema加表。
- [ ] Step 4: 由主代理在隔离分支统一整合代码并归档证据，不触碰原服务；合并到共享main和双远端推送作为后续独立交付动作。
- [ ] Step 5: 复核原PID/ready/.env哈希，归档文本证据并提交当前计时T-28247353A251唯一公司记录；保留工作树/环境和所有旧SDD，不重试历史policy拒绝。
