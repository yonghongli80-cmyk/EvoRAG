# 本地后续：启动排空、合成功能验证与SQL所有权

用户要求继续并发Luna。基线为本地已验收 `c74df90`（上一批source d837501完整3423通过/5跳过），D main仍99834ca。本批只在C新分支 `codex/local-followups-2026-10-26`；服务器上线、正式压力run/性能指标、真实业务迁移与数据继续暂缓。新工作不自动合并推送。

## A. 早期启动取消与安全排空

当前lifespan两个 `asyncio.to_thread` 在清理try之外。取消等待不等于同步函数结束，索引仍可能用DB/vector或晚启动material worker。因此不能只扩大finally或用cancelled asyncio Task.done判断资源静止。

### 选定边界

新增纯标准库 `rag/startup_lifecycle.py`，API保持原同步legacy迁移→后台同步index→后台同步staged迁移→backup/evolution/restore→yield顺序、公共facade、ContextVars和默认executor行为。可用 `asyncio.to_thread` 的owned wrapper，或等价的 `loop.run_in_executor(None, copy_context().run, wrapper)`，不强制保留一个在loop关闭后无法完成的asyncio Task；内部用自有工作令牌跟踪“实际fn是否仍可能运行”：可由Executor桥接内部管理 concurrent Future 状态，queued cancel成功必须保证fn不会再进入，running任务只有fn退出后才完成。不得从已取消的asyncio等待推断工作结束，不引入新专用服务或默认新线程池。

每个app有启动owner/门闩；前一次abandoned工作仍在运行或其finalizer正在清理时，新启动在开始迁移/索引前拒绝，防止关闭新旧共享资源。正常启动完成可释放启动准入；不把该本地门闩称作多实例/数据库持久fencing。旧完成信号重复触发或晚到，不能清理新一轮资源。

早期异常已确认同步工作静止时，进入同一清理顺序。收到取消先保存原CancelledError，最多等待默认5秒的排空grace；重复cancel不得重置deadline或取消底层正在运行的工作。超时则原取消传播、资源保持、持有工作/任务/owner强引用；工作真实完成后无论原事件循环是否仍活跃，都只尝试一次清理。令牌完成回调本身不能向Future setter抛BaseException。未开始的queued工作取消成功后不得再触发fn。

清理顺序仍backup stop→evolution stop→close_pipeline_processors，原错误/取消优先，无primary时传播首cleanup错误；deferred路径已无caller时只记录错误及诊断状态。不得无限等待，不取消真实Python线程，不更改material supervisor stopFalse保留共享资源的保护，不声称忙worker也已全部关闭；正常关闭已有join时限保持。新增grace限制的是等待同步工作，不宣称给所有同步关闭操作提供OS硬deadline。

一次性finalizer状态由锁保护，检查工作静止与开始清理原子协调；不得持锁执行任意业务hook或Future.cancel/add_done_callback造成同步重入死锁。循环关闭后不能仅依赖call_soon_threadsafe做唯一清理；真实线程完成/令牌才是依据。未结束与保留资源状态需可诊断，不能假报“closed”。启动取消不承诺撤销已经提交的迁移，仍沿用原幂等恢复语义。

单测用真实Thread/Event/Barrier且所有阻塞在finally释放，短可注入grace；覆盖快完成、超时保留/随后一次清理、repeat cancel、queued cancel、工作异常竞争、loop关闭后的完成、新startup被挡及旧callback不关新资源、ContextVars和原thread/调用顺序、cleanup异常和现worker busy保护。只使用fake资源或Temp数据库，不启真实worker/模型/服务。

依据：[Python3.12 shield/to_thread](https://docs.python.org/3.12/library/asyncio-task.html)、[concurrent Future状态](https://docs.python.org/3.12/library/concurrent.futures.html)。这是应用的owned-work桥接，不是任意用户调用Future状态方法或保证线程可被取消。

## B. 非计时的小型真实SQLite功能验证

旧tracked工具只做过纯fixture/parser单测和freeze/check，正式run未执行。本批在隔离子进程创建每分布最多8份文档、两KB/manyKB/mixed ACL/admin四类合成UserDB，直接比较实际旧/新快照，不调用CLI run、不预热/计时/采样、不输出延迟吞吐。所有上下文/SQLite路径在新Temp、禁dotenv/secret、移除继承凭据并阻断外联/生产端口，任何业务路径失败关闭。

新增functional测试证明fixture schema/版本/FTS/org数据真实可用，完整返回字段/顺序/model_input相同；单独top_k1 semantic探针收到完整可见版本ID与原参数，且隐藏文档/拒绝org不进入模型输入。hash在所有只读探针前后相同；四方法counter和SQL trace分implementation/functional phase可复核，不把返回行数称SQLite总读取量。

修复fixture报告的计数歧义：原 `hidden` 只是owner构造模式，org-denied的public/shared也实际不可见。新fixture profile必须有版本，并把重叠的构造属性与真实ACL有效可见/隐藏ID/count分栏；禁止将属性计数求和当互斥总数。可保留旧字段为明确legacy alias，或以新版本清晰改名，但有效ACL口径必须是独立明确字段。mixed ACL的8文档profile应含public及shared的拒绝org样本，真实iterator/旧baseline和独立预期共同验证。profile变化不拼接历史运行。

错误关闭验证可以进入 `run_case` 的第一个diagnostic失败点，但必须patch timed worker/ThreadPoolExecutor/perf_counter_ns为禁止调用，证明未触及预热/计时或资源采样；原异常/partial/DB.close皆验证。它是正确性测试，不是正式run或性能样本，不更改18/810与ACL2/90矩阵合同。旧source/golden和ignored历史helper不改。

## C. 动态SQL审查清单与写所有权候选

继续已有字面量scanner，不把103项unresolved自动当不安全或直接兼容PG。本批新增纯标准库 `scripts/review_dynamic_sql.py`：从冻结inventory构建逐finding记录并验证审查manifest，精确匹配path/line/owner/sink/classification、唯一finding_id、输入inventory哈希与所涉源码文件哈希，缺项/重复/过期/伪来源均拒绝。

初始build必须unreviewed。人工/AI逐项源代码审查可标 `understood_shape`、`needs_followup`、`external_source` 等明确disposition；有证据指针/rationale，有限identifier需要列closed-set来源，绑定值形状与语法片段分开，opaque SQL不能捏造模板，needs_followup要有下一步。validator仅证明完整性和绑定，不自动证明rationale正确、SQL安全或PG可移植。输出review coverage与portability readiness分开，后者保持未验。

生成并独立复核本次103项的源审查register到专属artifact；可按group共享rationale但每finding有自己的hash/指针/disposition。不强行把全部未知“resolved”，不执行/评估SQL，也不自动重写业务代码。

新增 `scripts/audit_transaction_ownership.py`，AST只读发现read/write/locked_transaction/transaction/write_cursor/get_write_connection、raw execute/commit/rollback、sqlite3.connect构造与词法with上下文。记录路径/行/owner、connection hint、嵌套/直接commit/可能caller-owned、private store与外部datasource边界；未知类型/别名/动态分派明确unknown，不能把grep或AST hit当可达操作数/完整writer证明。Rag和scripts明确范围，诊断fixture writer单独分域不混作业务入口。

配套tests验证manifest exact/hash/duplicate/stale/disposition约束、稳定JSON/独占新输出、AST aliases/with/nested/direct/private/external/unknown/error覆盖。配套文档列实际计数、若干跨repository/维护调用链、UoW gap register；不新增空壳UnitOfWork，不改ConnectionManager/SQLite业务语义、不装PG驱动或声明PG支持。真实全writer fence、双adapter/中文FTS/迁移/外部副作用等仍需后续环境和实现。

## 共同验收

先写计划，再真实RED后实施，三任务独立Luna文件归属、独立规格/质量复审与原作者修复。root串行测试，低优先级/BLAS1/离线guard、新短Temp，不改pytest TEMP。API公共启动路径变更需冻结后一次完整后端回归，前端/旧压力矩阵不重跑。所有后续CLI source freeze/check只读不负载；实际小fixture数据和用例不代表Linux/cgroup或业务性能验收。

保留前批分支/环境/原始证据，新结果和所有裁决写本批ledger/public review；root唯一Git与工作记录操作者，子代理不得timer/submit。完成后保留可审阅local分支，不自动合并推送/部署。
