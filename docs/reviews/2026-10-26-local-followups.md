# 本地后续：启动排空、材料定位功能验收与 SQL 源审查

**本批本地代码与整体验证完成，交接记录及证据归档收尾中；尚未合并、推送或部署。** 本批基于上一批本地成果 `c74df90`，分支为 `codex/local-followups-2026-10-26`，受测源码 `e4b07ec6af6c9575f9f6c1e3f2792e6e033dcf23`。上一批 `codex/local-readiness-2026-10-26` 分支继续保留，D main 仍为 `99834ca`。本批完整回归为3462通过/5跳过，不复用上一批3423/5结果。

## 本批改动

1. **启动取消时先确认同步工作结束。** `StartupLifecycle` 用独立工作令牌区分取消等待与真实线程结束，保留默认 executor、ContextVars、原启动顺序和 API facade。排队任务取消后不会进入函数；运行中的任务最多等待默认 5 秒排空，重复取消不重置期限，超时保留资源和启动准入门闩，实际结束后只尝试一次清理。旧代回调不能清理新一轮资源，原事件循环关闭后仍有清理路径。
2. **保留主异常并补齐诊断。** 三个清理 hook 按原顺序均被尝试，原异常或取消优先；没有主异常才传播首个清理异常。取消超时后线程再抛错会被记录，asyncio 桥接异常被读取，避免未取异常警告。`cleanup_attempted` 表示尝试过，不等于全部资源关闭；material worker 停止失败时保留共享资源的保护不变。5 秒约束排空等待，不是进程退出或所有清理操作的硬上限，也不回滚已提交迁移。
3. **真实小型 SQLite 功能对照。** 独立离线子进程分别构造 two-KB、many-KB、mixed ACL、admin 四组，每组 8 份合成文档。比较旧新完整有序结果、model_input、top_k=1 的完整允许版本集合、只读前后全表哈希和真实调用期间的四方法计数及 SQL trace。未走的方法保留零；返回列表行数不代表 SQLite 总读取量。profile v2 将重叠构造属性与实际 ACL 可见/隐藏 ID 分开，包含 org 拒绝的 public/shared 文档。首次 diagnostic 注入失败另证 partial 保存和 DB 关闭；不触及预热、计时、ThreadPool 或资源采样。
4. **SQL 源审查与写归属候选。** 两个标准库只读工具分别验证审查清单与 AST 候选，不执行应用或 SQL。清单精确匹配 finding、inventory 和源文件哈希；evidence 必须为源根内真实路径和有效行号，并显式绑定每个引用文件的原始哈希，跨文件变化也使证据失效。AST 保留词法作用域、别名遮蔽/重绑、直接提交、私有/外部连接及未知项；维护脚本与 fixture 范围标签不是安全证明或完整调用图。

## 分项证据与复审

| 范围 | 最新分项验证 | 独立复审 |
|---|---|---|
| 启动治理 | 初轮 66 passed；late-error 修复后 29 passed / 21.85 秒，native 0 | 原 P2 已在 scoped review 关闭 |
| 材料定位功能与计数 | 计数来源修复后 3 passed / 31.40 秒，native 0 | 真实 old/new 调用期间计数，原 Important 已关闭 |
| SQL 工具 | evidence 来源修复后 39 passed / 23.28 秒，native 0 | 原 P2 已关闭；103 项源语义另经逐项复核 |

上表来自不同运行，不能相加当成一次完整回归。初始 RED、失败的 GREEN、fixture/测试合同修正、全部 native exit 和原始日志均保留。Task1 两次失败是事件条件及排程屏障问题；Task2 的 semantic 预期和人工 API 计数标签曾不符合真实调用，已修正；Task3 修复了赋值 RHS 漏发 constructor marker 以及 evidence 伪来源可通过的问题。两次根代理协调问题（review 包数组格式、已结束代理未被 send_message 唤醒）也已记入 ledger 并纠正，没有作为产品缺陷登记。

整分支复审为 SPEC/QUALITY PASS，无阻断发现；两项非阻断备注保留：worker 恰好在 cleanup intent 记录前失败、同时取消获胜时，次要错误诊断可能缺失，但资源已经静止、清理与主取消仍正确；SQL 工具短 docstring 未概述新增 provenance 检查。后续若要求覆盖每一种完成/取消次序的诊断，应新增确定性屏障用例，不能把当前验收表述为该更强保证已完成。

## 整体验收

| 检查 | 实际结果 |
|---|---|
| 最终受测源码 | `e4b07ec6af6c9575f9f6c1e3f2792e6e033dcf23`；整分支 SPEC/QUALITY PASS，无阻断发现 |
| 完整后端离线回归 | **3462 passed / 5 skipped，2089.65秒，`Popen.wait()` 实际退出0**；JUnit3467项、0失败、0错误；单pytest、BelowNormal、BLAS1、新OS短Temp |
| 生产依赖锁合成烟测 | 6 项通过、0 失败，`Popen.wait()` 实际退出 0；真实 API 门面和临时 DB，合成 hook 与真实取消线程，无 HTTP/provider/负载 |
| 全树 Ruff / 变更文件格式 | 全树 Ruff 退出 0；11 个变更 Python 文件 format-check 退出 0 |
| 源码前后 freeze/check | 612文件，回归前后均退出0；manifest SHA256 `e667d6b9ac6c98e10d3880cf91a056c8a492f361637453606a7c2cd8581e44f3`；受测HEAD与源文件均未变化 |
| 最终 SQL / writer 报告 | 新 inventory 覆盖 209 文件（205 Python/4 SQL）、850 静态 SQL、103 unresolved、349 事务标记、50 代码标记、0 错误；scanner 按不完整合同退出 1。writer AST 覆盖 231 Python 文件、1696 候选标记、0 错误，退出 0，完整 writer 仍未证明 |
| 服务保护 | 15:16:23最终复核：PID48512/启动时间未变、ready200、D.env哈希相同、Dmain99834ca/跟踪树无变化；C隔离树没有.env |

五项跳过原因保留：Windows缺fork、FIFO，以及terminate不能被忽略三项；另两项真实脱敏表格fixture缺失。未删断言或以合成数据冒充真实fixture。生产锁烟测stderr中的合成CleanupError和排空grace日志是预期注入，实际退出0；完整后端stderr为空。回归后仅更新结果、回执和交接文档，不把旧freeze复用为新HEAD或目标服务器验收。

## SQL 审查仍有的边界

103 项动态表达式均有源审查处置：**88 understood_shape、12 needs_followup、3 external_source、0 unreviewed**。12 项包括调用方提供的迁移 SQL、文件/SQLite schema 元数据 SQL、snapshot identifier、表格写入/API 的 metadata identifier 边界及运行时 SELECT 工具；它们不是已修复清单。三个 external_source 是独立数据源连接器，不能当作应用 UserDB 的 PostgreSQL 支持。

`review coverage.complete` 只表示记录和来源绑定完整。原 scanner coverage 因动态 SQL 仍不完整；`portability_readiness=not_assessed`、`writer_completeness=not_proven` 保持。最终报告须保留这三个不同口径，validator 不能证明 rationale 的语义、SQL 安全或 PG 方言等价。

最终 reviewed manifest 已由本批冻结 inventory 重建，103 identity 与 seed 精确对应，处置仍为 88/12/3；371 条 canonical evidence 绑定 31 个 Rag 源文件。root 重绑与 CLI check 均实际退出 0，独立生成物复核未发现不一致。旧 seed 中两个测试文件的 4 条辅助上下文引用位于 Rag 源根外，初次重绑因此拒绝；原 seed 和失败日志保留，这些引用另列 supplementary context，没有混入 canonical evidence、没有获得来源已绑定的声明，也没有改变迁移调用输入仍需 follow-up 的处置。文件位于本批 `artifacts/local-followups-2026-10-26/verification/`。

回归期间另由两路 Luna 只读追踪这些 follow-up 的应用入口、调用方及信任边界，没有确认当前应用路径可触发的新代码缺陷。表格 key 当前由 parser 生成，manifest 来自应用写入；迁移 iterable、legacy 目录与 runtime SQL tool 是调用方/操作者显式选择的扩展。旧库 schema-object replay、快照提供者信任和任意 dataclass/外部 dataset artifact 的兼容合同仍需决定；没有把“helper 可覆盖”或“SQL 是动态文本”直接写成已证明漏洞，也没有据此把12项 follow-up 改成已关闭。只读归因报告为本批 `followup-identifier-triage.md` 和 `followup-runtime-sql-triage.md`。

## 裁决与保留条件

| 裁决 | 原因与代价 |
|---|---|
| 默认 executor 加独立令牌，排空 grace 默认 5 秒 | 保留 ContextVars 和原调度语义；线程永远不结束时继续保留资源并拒绝重入，需要后续运维处理 |
| 不改 material supervisor 的忙 worker 关闭协议 | 避免在线程仍访问资源时关闭；不承诺所有 worker 已结束 |
| 每组最多 8 份，功能路径禁止性能组件 | 本地可证明契约，不能提供服务器性能、容量或 P99 结论 |
| 实际实现期间计数，不用人工 API 调用冒充旧新调用 | 未使用方法会为零；计数与返回行数不能当作延迟或总 SQL 读取量 |
| evidence 仅本地源码路径/行号和显式原始哈希 | 拒绝可机械识别的伪来源；跨文件或换行字节变化需要重新绑定，不能由 validator 自动补当前哈希 |
| 保留全部工作树、环境、SDD 和失败原件，只在本地分支提交 | 占用磁盘但保留复核与恢复依据；未自动合并推送或切换服务 |

当前计划与过程见[实施计划](../superpowers/plans/2026-10-26-local-followups.md)和[设计](../superpowers/specs/2026-10-26-local-followups-design.md)。源审查用法见[SQL 与 writer 说明](../architecture/dynamic-sql-and-writer-review.md)。正式 Linux/cgroup、完整压力矩阵、P50/P95/P99/吞吐/SLO、真实存量迁移/上线恢复、两个真实脱敏表格 fixture、供应商真实返回头、PG/UoW、共享一致性/对象存储/多实例和生产 shadow 仍未因此完成；服务器项目继续按用户要求暂缓。

工作记录及非 DB 证据归档：待整体验收和实际 submit 成功后填入回执。
