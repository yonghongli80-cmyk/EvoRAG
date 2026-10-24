# KB 删除与写入并发一致性设计

## 目标与已确认事实

用户要求先写计划，再由 Luna 并行执行，主代理统筹；原8080及业务数据不可参与演练。基线 `9f0574b`，已有隔离工作树的新分支 `codex/kb-lifecycle-concurrency-2026-10-24`。不再次要求执行方式确认。

本轮已用真实ASGI、临时SQLite/文件/Qdrant复现：上传被阻塞在真实 `finish_kb_document` 前；删除清理其pending目录、权限、版本与文件并返回200；随后上传的UPDATE匹配零行却仍返回200 added。证据为 `artifacts/kb-lifecycle-concurrency-2026-10-24/audit/repro-report.md` 和该目录 `repro/pytest-second-run.log`。入口审计另确认materials、archive、spreadsheet、Wiki和后台重试也是自定义KB写入者，不能只锁两条HTTP路由。

## 方案取舍

实现方案比较：
- 仅用进程内锁实现简单，但多worker/多进程无法互斥，与本项目已有portalocker能力及删除恢复要求不符，排除。
- 把SQLite事务覆盖向量和文件I/O能串行化单库写入，但会长时间占有全局写锁，故障恢复困难，排除。
- 采用每KB稳定OS文件准入加独立SQLite持久墓碑：Redis保持可选，短事务存状态，外部清理可按deleting意图重试；这是本轮执行方案，因为它直接覆盖进程崩溃与同库多进程竞态。


采用**同KB跨进程文件准入 + SQLite持久化删除墓碑**。文件锁只在支持可靠OS文件锁的同机/共享文件系统拓扑保证互斥，不宣称任意多主机分布式一致性。不同KB各用独立门禁，数据库事务仅做短状态/CAS操作。Redis当前可选，不能成为唯一门禁；把模型/向量/文件I/O放进长SQLite事务会阻塞所有写入，均不采用。

墓碑无FK到kb_metadata，无记录表示尚未进入删除流程，既有授权仍是必需条件。状态为 `deleting -> deleted`，失败保持deleting并可按同一删除身份重试。没有TTL自动解除、没有上传把墓碑改回active、没有复用已删除KB id。

## 线性化与锁生命周期

- 同一KB所有发布/删除操作争用同一稳定文件锁；忙时快速409，无副作用，不声称等待中的操作已完成。不同KB可以并行。
- 准入覆盖权限复核、预留、staging、向量、版本/数据集激活、目录完成、任务状态与回滚。任何会继续产生写副作用的后台线程都必须自己持有门禁直到实际退出；HTTP取消不能提前释放它。
- 同线程、同逻辑任务的嵌套准入可重入；不同async任务即使共用线程也不能借用准入。ContextVar凭据须核对PID、线程和async任务身份，不能被子任务/进程继承后伪装为锁owner。所有准入均在同步工作线程中取得；持有准入期间不允许再启动任何会写KB的asyncio.to_thread，上传请求可先写入有界请求临时文件，后续pending/版本/文件/向量/激活全部由同一个持锁同步worker完成。取消HTTP coroutine不会释放仍在运行的worker门禁。
- 锁文件以规范化绝对SQLite数据库路径旁的专用目录及KB id摘要命名，不在释放时unlink；所有受支持应用进程必须用相同规范路径与锁目录。不同挂载别名/硬链接不在保证内，除非显式配置同一共享锁目录。:memory: 用每个ConnectionManager的进程内门禁，不把独立内存库混为一库。
- 只读查询/流不持有生命周期准入；删除不强行关闭已在途查询。只有可能改变KB状态的请求或worker进入同步准入。

## 删除协议

1. 快速授权，进入删除门禁后再复核现有owner/admin或组织ADMINISTER权限；`rag_docs`保持禁止删除。未知KB不创建墓碑假报成功；orphan edit仍遵循现有409，不能借此扩权。
2. 读取全部目标KB文档身份，而不只kb_documents；枚举immutable版本、可证明归属的staging、legacy KB文件、结构化数据集、Wiki页面/修订、后台任务。先校验所有路径的根和owner归属，再进行任何删除。
3. 短事务写入deleting和删除token/generation。取消可归属该KB的队列/在途任务，再执行外部清理；Wiki任务必须在Wiki身份清除前识别并取消，避免崩溃后丢失关联。保留足够身份供重试。
4. 在事务外幂等移除该collection和所有已验证归属的文件/数据集/Wiki内容；仅在已有删除意图的恢复路径中把资源已不存在当成已清理。任何失败不得返回deleted，保持可恢复deleting。
5. 全部外部清理成功后，同一短事务删除剩余目标关系数据并把墓碑置deleted。提交失败整体回滚关系数据/状态。以后读写均拒绝墓碑KB；不让缓存pipeline或Wiki重试重建它。

不自动扫除归属不明的历史staging或业务孤儿文件，不改变聊天历史保留策略，不用删除其他KB的数据换取通过。已注册的新staging应带document owner目录，崩溃后能被该文档/KB的删除恢复处理。

普通版本导入使用 `staging/<document_id>/` 归属；材料流在doc id尚未知时使用 `staging/kb/<sha256(kb_id)>/`，所以进程在预留身份前退出仍可只回收该KB的临时字节。系统rag_docs的既有staging形态保持，旧全局staging没有可信归属时仍保留并报清理边界。

## 写入边界

- knowledge upload/link/remove：整个实际写入在一个同步worker的门禁内，上传/link复用同一导入编排；链接先保护源KB读取快照，释放源准入后再进入目标，避免跨KB嵌套死锁。保留路由patch surface与现有响应字段。
- common version ingest、BatchImporter、spreadsheet编辑/恢复：同KB准入覆盖文件到发布，版本预留/激活、目录完成另有事务内墓碑检查，零行UPDATE不能返回成功。
- materials上传/归档确认、索引/修复/purge worker：所有自定义KB使用现有KB编辑授权。busy要保留/安全重排任务，deleting/deleted任务取消，不重新建collection；任务结束状态也在持有准入期间处理。
- Wiki create/update/rollback/delete与reindex：用户DB准入必须覆盖Wiki DB修改及向量写；Indexer显式注入用户DB或接受当前合法准入，不能为自定义KB静默绕过。保持精确Wiki点删除边界。删除先按KB枚举Wiki page ids，取消UserDB中job_scope=wiki且document_id属于这些page ids的活动重试；再清理全部Wiki向量，全部成功后才在一个Wiki事务中删除页面/修订/链接身份。此接口由WikiIndexer.remove_kb_content(kb_id, *, db)提供，调用方需已持有delete permit；UserDB.cancel_wiki_reindex_jobs(page_ids)负责重试任务取消。
- KB详情/查询等授权入口追加生命周期可用性检查；删除重试只能显式允许deleting，同时继续做原删除权限检查。KB metadata/目录更新有事务内检查；新KB创建在物理创建前拒绝墓碑ID。

## 错误与兼容

忙/已有未完成删除：409；已删除/不存在：404（保留授权层原有隐藏/拒绝语义）；实际删除失败：503 `knowledge_base_delete_incomplete`，可重试。生命周期HTTP错误使用可读detail字符串，稳定机器码放 `X-KB-Lifecycle-Error` 头，不让前端把内部对象当通知正文。原duplicate、admin、legacy owner、org角色、rag_docs策略不放宽。

## 全局约束与发布边界

- 所有受支持进程必须使用同一个规范化绝对SQLite路径并共享其相邻锁目录；不同挂载别名或硬链接不在互斥保证内，除非显式配置同一共享锁目录。
- rag.org_auth.seed.seed_organization只允许在应用未服务请求时运行或位于统一停写维护窗口；它的KB metadata/policy事务仍做生命周期fence，遇墓碑时整体回滚，不能复活旧ID。


- 不重启、停止、部署或压测原127.0.0.1:8080/PID48512，不打开或迁移业务DB/向量目录，不读写真实.env。只允许事前/事后只读ready、进程与.env哈希核对。
- 所有运行验证只用系统Temp合成数据、已锁私有Python和本轮offline_network_guard；禁止独立探针启动源相对路径app lifespan。正规conftest完全隔离的受控lifespan可用。
- 不升级依赖、不添加必需Redis，不改变模型、ACL、canary/shadow或沙箱fail-closed；api.py<=830、pipeline.py<=1000、局部导入<=150，不放宽门禁。
- 旧版写入者不遵循新门禁，真实启用必须在停写/统一升级窗口完成。本轮只在临时库验证添加schema和恢复，不执行业务切换；源码交付按既有约定处理，不把源代码合并称为协议已在原实例运行。
- 不重试上轮被自动审核拒绝的SDD目录清理，不删除/搬移任何旧工作树、环境、Docker数据或业务文件。

## 验收

确定性API交错、同KB/不同KB跨进程门禁、async取消时真实写线程仍持锁、真实子进程死亡后OS锁释放及删除意图恢复、向量/文件/最终事务各断点重试、材料/Wiki/表格/批量入口、权限矩阵和其他KB不受损。每个修复先保留预期失败，再跑定向与一次稳定源码完整后端回归，fresh任务/整分支复审；前端源码不变时不重复整套前端构建。
