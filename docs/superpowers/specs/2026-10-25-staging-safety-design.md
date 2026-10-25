# 材料暂存所有权、提交一致性与只读诊断设计

## 意图与依据

用户要求合并双推后继续，由主代理思考、Luna并行推进；延续既有“不影响运行服务和业务数据”的约束。已交付main及两端为`3f524904e16b19119fe76009c41763b89bbdae8d`。新工作使用C隔离worktree分支`codex/staging-recovery-2026-10-25`。本轮执行方案由主代理根据用户委托选择，不把这些实现取舍写成用户逐项选择。

三路只读审计保留在`artifacts/staging-recovery-2026-10-25/audit/`。源码确认：materials upload在await to_thread外层finally删除源stage，request取消时可能先删仍被worker使用的文件；普通上传promotion和job入队分开，入队失败可留下无job的staging版本；archive confirm在activation/idempotency提交后才入队，失败后同key重放会提前返回；KB teardown对没有kb_documents目录行的version只做全局root约束，缺少document/KB owner证明和archive container/version交叉引用校验。正常promotion失败保留原stage字节、回滚新permission的现有契约继续保留。

## 方案取舍

1. 仅修异常处理，成本低，但仍缺少可用的保留文件诊断。
2. 新增持久upload receipt和恢复API，能提供端到端用户重试，但需新schema、ACL、幂等和过期策略，超出本轮最小安全整改。
3. 修正worker/事务/删除owner边界，增加显式快照输入的只读审计CLI。本轮采用此方案；保留字节和既有API，先让状态可判读，后续恢复写操作需另行设计。

## 功能与不变量

### 1. 请求取消后的stage所有权

- 流式接收仍有界；接收失败/取消仍由stream_upload_to_stage清理自身partial文件。
- stage交给同步worker后，cleanup和保留决定只由该worker作出；HTTP coroutine的finally不得删除worker仍可消费的stage。
- 调度后即使coroutine在executor尚未开跑时取消，也不取消已转交的工作；屏蔽外层取消对工作future的传播，取消立即向请求传播，worker继续持有/取得KB gate并执行自己的cleanup。消费取消后worker的异常，避免未观察task异常；日志只含固定事件和异常类型。
- 普通文件、archive分支和组织审计副作用都由实际同步worker完成。KB gate取得后持续到写入/补偿/cleanup结束；gate拒绝时只回收本请求独有stage。已有HTTP响应、权限复核、rag_docs特例保持。

### 2. 文件promotion与入队的提交边界

- 普通上传先保留version reservation身份，进行文件move；随后在一个短SQLite事务中CAS更新file_path、取消旧active material jobs、插入新material_index job。失败时旧job取消和新job插入一并回滚。
- 为现有promote_staged_version增加可选纯SQL finalize回调，默认调用方式保持。file move后由helper持有短locked_transaction再调用回调，material repo复用该连接；path更新与job提交共享边界，取消原来的独立enqueue调用。回调不显式commit、不做I/O，IO不放进长SQL事务。
- 可补偿的promotion/finalize失败把本次文件移回stage、删本次version，正常新document permission回滚；响应继续`500 material_promotion_failed`并保留字节。不能只凭move函数是否返回判断副作用：stage缺失且final存在时按实际文件状态补偿。若移回失败，或两份同时存在无法确认，应保留字节与version/permission的owner身份，尽力记录实际final路径，异常携带recovery_required，不能再级联删除唯一恢复身份或overwrite另一份。
- archive confirmation的所有新child jobs移入已有activation/manifest/idempotency同一事务；不在commit后另行入队。插入job失败则整个activation回滚，源archive保留，重试同key不会因假confirmed而跳过工作。
- 共享ConnectionManager事务context中，SQL body或COMMIT失败均须在writer lock释放前回滚仍active的事务，nested reuse不另开/提交事务。SQLite延迟约束导致的commit失败不能污染后续补偿和其它KB写入。
- 本轮不宣称跨进程崩溃时自动重试所有upload断点；现有reservation/final owner文件保留，由只读诊断报告组合状态。无新schema、无恢复/清理API。

### 3. KB删除的owner证明

- 所有目标version路径必须属于该document的final目录、document-ID staging目录，或同KB的hashed staging目录。全局root confinement不足以证明owner；未知global staging保持fail-closed。
- manifest.container_version_id必须存在并属于container_document_id。正常pending source路径、confirm成功或失败后位于container的final路径均是合法表示，不能一律要求source_path等于已finalized的version.file_path。
- 存在的archive source仍必须位于目标KB hashed staging root；旧global源继续`archive_source_unowned`，不自动rehome。
- 对将被删路径/owner目录检查其它KB的version或manifest引用；共享或交叉owner引用拒绝整次删除。所有验证在collect阶段、任何manager/filesystem teardown之前完成。
- 稳定错误码：version_path_unowned、archive_identity_mismatch、cleanup_path_shared；均用KBDeleteIncomplete映射503并保留所有身份。现有archive_source_unowned兼容。
- 复用纯路径owner helper，保持symlink/junction和根越界拒绝；不扩大对外部文件系统actor的TOCTOU保证。

### 4. 只读快照审计

- 新CLI `scripts/audit_staging.py --database PATH --versions-root PATH`，必填显式输入，不读config/.env、不启动app、不默认访问业务路径、无apply/删除/移动选项。默认输出JSON，正常报告退出0；输入或schema错误退出2并用稳定error code。
- 输入必须是无-wal/-shm sidecar的自包含SQLite快照；以mode=ro&immutable=1打开，防止读操作创建旁文件。不构造UserDB、不建schema。存在sidecar时拒绝，要求先由操作者生成一致快照。
- 报告schema_version=1，包含entries/findings/counts。核对versions、permissions、archive manifests、material jobs与磁盘stage/archive_work，报告缺文件、未注册暂存、owner不匹配、共享引用、unowned archive、链接路径，以及staging版本缺material job这一事实和其它引用上下文；不把缺job自动等同于可删除垃圾。
- 当staging version的declared file_path缺失时，仅检查其确定的`root/<document_id>/<version_no>/`目录，报告无链接regular文件为owned_version_candidate，附declared_path和identity；不猜original_filename、不声称content hash匹配、不更新metadata。目录不存在或有链接则报告missing/unsafe。这覆盖move-back及best-effort repoint同时失败而reservation身份仍在的组合。
- 不跟随symlink/junction，不读取文件正文/计算全库内容hash，不对root外引用做文件IO。路径名只能作为namespace线索，不能据文件名推造actor/owner或恢复metadata。报告action固定inspect_only。
- 只读DB快照与目录须由操作者在维护流程取得；本轮只用合成Temp数据验证，不读取任何真实业务副本。

## 文件/接口边界

- Task1拥有materials/api与新的upload_execution异步交接helper。
- Task2拥有db/connection的commit异常回滚边界、document_versions、material_repo、UserDB、archives及Task1完成后的materials/api窄接入。
- Task3拥有kb_document_cleanup、kb_lifecycle_cleanup，产出resolve_owned_version_path供Task4读取。
- Task4仅新增audit模块/CLI/测试，消费Task3纯路径helper，不改schema或公共conftest。
- root统一Git、计划/报告/交接和最终验收；子代理不能写D主目录或另开代理。

## 全局约束

- 全部写入仅在C隔离分支；不重启、停止、部署或压测原8080/PID48512，不打开业务DB/向量目录、不读真实.env；只允许事前/事后只读ready、进程和.env哈希核对。
- 验证使用既有锁定私有Python、offline_network_guard、系统Temp短basetemp；不启动未隔离lifespan，不调用模型/外部服务/Docker，不升级依赖。
- 不改schema、ACL、模型/canary/shadow、Redis可选和沙箱fail-closed。api.py<=830、pipeline.py<=1000、局部导入<=150，不放宽预算。
- 所有自动验证只用合成资料，最多4 worker；子任务定向串行测试，root稳定源码后统一一次完整后端；无前端修改不重复前端全套。
- 不删除/搬移现有工作树、环境、业务或Docker数据。旧global staging不自动回收；本轮没有任何实际rehome/cleanup执行。

## 验收

先用真实Temp SQLite/文件、实际asyncio executor和deterministic barrier观察取消/入队失败/错owner删除的RED。GREEN必须覆盖：worker未开跑与已开跑取消、archive复制、取消后后台异常被观察；新/旧document的SQL trigger入队失败与旧job不变、补偿失败owner身份保留；archive原子activation/jobs与重试；cross-KB版本/manifest引用拒绝和正常confirmed/failed-confirm兼容；CLI无sidecar/无文件写入、链接不跟随、错误输入不创建DB。每任务独立复审，root最终集成复审及适当完整后端验收，全部文档标注真实运行边界。
