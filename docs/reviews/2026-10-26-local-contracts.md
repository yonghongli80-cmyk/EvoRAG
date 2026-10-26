# 本地合同加固验收：启动诊断、SQLite identifier 与命令事务边界

**本批本地实现、独立复审和整体验证完成，交接归档收尾中；未合并、双推或部署。** 分支 `codex/local-contracts-2026-10-26` 基于上一批 `0b48872`，受测源码 `cdf039a86364fa0806a9f246bcf16086023658a0`。本批完整回归3481通过/5跳过、1892.99秒、实际退出0，不复用上一批3462/5。

## 已实现的本地工作

1. **失败诊断与资源回调分离。** 独立 token done callback 对每个非cancelled失败工作记录一次诊断，不再依赖 cleanup intent 或当前资源 generation/active 身份。普通 await 仍抛原错误对象，取消获胜仍保留原取消。覆盖 callback 先清 active、finish先于资源 callback以及新generation已经进入的排序；资源 guard、默认executor/ContextVars、5秒排空和一次清理均保持。
2. **grace日志故障不覆盖主取消。** 真实单项RED确认 grace-expiry 的logger抛KeyboardInterrupt会覆盖原取消；该诊断现在与失败callback一样隔离BaseException。没有改worker停止协议，stop=False仍保留资源，5秒不等于所有关闭操作或进程退出的硬上限。
3. **统一SQLite单identifier引用。** 轻量helper仅接受str、拒绝NUL，不回显输入；双引号转义，空名/Unicode/空格/标点保留，不新增canonical白名单。store建表/插入、rows manifest表列、只读staging snapshot查询使用同一helper，值继续参数绑定。保留ACL、路径/归档身份、只读/immutable/query_only、额外schema列、排序分页、公式和HTTP错误合同。真实RED显示旧路径无法处理合法含引号名称；这不是“已确认应用可达SQL注入”的证明，也不意味着任意外部dataset获支持。
4. **操作级事务清单。** [命令边界文档](../architecture/command-transaction-boundaries.md)追踪KB删除、上传/索引发布、受控恢复的SQL owner、提交/回滚、文件/vector/private-store效果、补偿与unknown。ACL helper reader不假称writer同连接快照，独立组织审计不假称与终态提交原子；故障矩阵是后续合成测试建议，不是本批已执行测试。没有新增空壳UoW或改业务事务协议。

## 分项与独立复审

| 范围 | 最新分项证据 | 审查结果 |
|---|---|---|
| 启动诊断 | 真实竞态RED缺少诊断；grace单项RED证实主取消被日志覆盖；focused 34 passed / 14.20秒，native0 | SPEC/QUALITY PASS |
| identifier helper/consumer | 真实SQLite quoted-name RED为3失败/1通过；初步GREEN71 passed / 31秒；覆盖增强后focused21 passed / 14.43秒，native0 | SPEC PASS；已有target保留和多行分页两项证据补齐后scoped QUALITY PASS |
| 操作级事务清单 | 纯文档/docstring，Ruff/格式检查退出0；不为低影响文档新增测试 | 纠正reader/writer与独立audit两项P2后scoped PASS |

各行是不同运行，不能相加当成完整后端。首次混合logger运行触及真实grace故障，后来单项RED独立确认，原件均保留；不把真实缺陷写成纯工具错误。测试证据补强没有改生产逻辑，也没有人为伪造RED。六个identifier变更文件在最后格式化后仍低于1000行；legacy源文件原本也需format，最终统一后store300/API379/staging929行，未放宽预算。

## 根级集成验证

| 检查 | 实际结果 |
|---|---|
| 源码版本 | 冻结 `cdf039a`；整分支静态SPEC/QUALITY PASS，无需final-fix wave |
| 完整后端 | **3481 passed / 5 skipped，1892.99秒，实际退出0**；JUnit3486项、0失败0错误，单pytest/BelowNormal/BLAS1/新OS短Temp |
| 生产依赖锁烟测 | 8项通过、0失败、实际退出0；包括API合成hook/真实取消线程、普通工作失败对象与一次诊断、真实SQLite含引号对象/空列名/参数值 |
| Ruff与格式 | 全源树Ruff退出0，9个变更Python文件format-check退出0；最大源文件929行 |
| source freeze/check | 615文件回归前后均退出0，manifest SHA256 `b75e53693155104ebcf97bb95b505c6a13fa80e1b8f2e9fb65fd22214d5ada79`，受测HEAD与源文件保持 |
| 原服务保护 | 17:11:21最终核对PID48512/start/ready200、D.env哈希及Dmain99834ca/跟踪树保持，C隔离树无.env |

烟测中的合成CleanupError、grace保护日志和正常工作失败traceback属于预期注入，实际退出0；完整后端stderr为空。五项skip为Windows fork/FIFO/terminate不能忽略三项和缺失真实脱敏表格fixture两项，未删断言或用合成数据冒充真实覆盖。本批没有正式run、预热/计时矩阵、模型外呼或业务数据操作。回归后仅补结果与回执文档，不把旧freeze冒作新HEAD或服务器证明。

## 更新后的SQL审查

新scanner覆盖210文件（206 Python/4 SQL）、850静态条目、103unresolved、0错误，按覆盖不完整合同退出1。旧审查只在finding identity、源文件和全部引用文件原始哈希一致时保留，98项满足条件；改动的5项按新源代码独立重审，没有给旧行号/hash盲目打补丁。最终manifest合并及CLI check均退出0：**90 understood_shape、10 needs_followup、3 external_source**，401条canonical evidence、32个Rag源文件。独立生成物核对确认集合、来源及处置一致。

两条staging snapshot读取形状因安全引用与固定表调用边界得到理解；store/API三项仍保留metadata/输入合同跟进。处置变化不是确认或修复漏洞的计数。scanner coverage仍不完整，review coverage表示记录完整，PG仍`not_assessed`。新writer AST扫描232个Python文件、1696个语法候选、0错误/退出0；`writer_completeness=not_proven`不变，不能把候选当可达写操作或完整事务图。

## 裁决与仍缺事项

- 采用引用转义而非canonical名称收紧，保留合法历史schema和扩展输入；代价是metadata信任、允许对象范围及非canonical数据合同仍需后续明确。
- 失败诊断独立于资源身份，并隔离grace日志异常；普通失败可能也被上层框架记录，但每个owned token只产一条本模块诊断。不扩张成一般worker/分布式fencing协议。
- 操作图明确SQL之外的文件、向量与私有store不随SQL rollback；后续须先做故障切点验证和幂等/审计合同，再设计UoW、outbox和迁移，不盲目套外层事务。
- 只在隔离分支与合成数据中工作，保留旧分支、工作树、环境、SDD和全部失败原件；不自动合并推送或部署。

本地后续可推进10项动态SQL输入/旧库和快照信任合同、命令级故障矩阵与事务/UoW设计；不代表它们是10个已证实bug。两个真实脱敏表格fixture、供应商真实request-ID与业务答案质量仍缺证据。目标Linux/cgroup、完整材料定位矩阵/P99/SLO、真实业务迁移/备份恢复/切换回滚按用户要求继续暂缓。PG主库、共享一致性、对象存储、多实例和生产shadow仍需实际实现及环境验收。

入口：[本地当前状态](../local-readiness.md)、[设计](../superpowers/specs/2026-10-26-local-contracts-design.md)、[计划](../superpowers/plans/2026-10-26-local-contracts.md)。本批原始证据在 `artifacts/local-contracts-2026-10-26/`。

非DB归档实际退出0：161份、3,128,233字节已保存至D `artifacts/local-contracts-2026-10-26/accepted-evidence`，逐文件SHA256全部一致；未复制业务/fixture数据库、依赖或.env。源工作树、环境、SDD及全部失败原件继续保留，AGENTS.md和公司记录目录未加入产品提交。最终文档与记录回执另存closure快照，不覆盖初始证据。

公司工作记录待实际submit成功后补回执。
