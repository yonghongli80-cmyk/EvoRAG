# 容量组件基线与架构门禁（2026-10-25）

状态：三项实施与分项/增量复审通过，正式组件诊断完成；整专项最终复查PASS，独立从manifest/raw重算全部9档结果一致，无材料性发现。基线3c001ee，分支codex/capacity-architecture-2026-10-25。生产源、依赖和部署配置未变，D main仍1d413ed；没有新模型调用、业务数据访问或部署。

## 建议与结果

用户不知道业务规模并委托建议。先以单机1000份资料、查询并发2试点；5000/10000和1/2/4是工程实验档。[容量与候选SLO](../capacity-and-slo.md)给出全部9行分位数、实际n、最大值、统计边界及正式验收条件。

本轮真实UserDB/MaterialLocator/ACL/FTS，共27组、2520测量样本，实际exit0、无正确性错误或资源事件。1000/C2材料定位P50 28.51ms、P95 39.56ms、P99 75.72ms（n240）；10000/C4 P50 1419.94ms、P95 1670.46ms、P99 2108.08ms（n480）。5000/C2有7752.65ms单点最大值；不能只展示分位数掩盖尖峰。

输入为两普通legacy用户交替private、每10项tombstone、每文档历史/当前两版本和两FTS行，无正文、HTTP、向量或模型。按固定顺序在共享宿主BelowNormal运行，检查点采样峰值约235.9MiB，资源门槛未降低；这不是OS硬内存限额或生产SLO。三个fixture的schema和users/kb_metadata/document_permissions/document_versions/material_search_fts五类核心表hash保持，当前writer total_changes增量0，不泛称整个DB所有表的dump均不变。

分位数从各档3轮原始样本合并后重新计算，不平均P99；n120/240/480的尾部仍稀疏。C1只使用A，C2/C4混合A/B，tombstone分布导致A可见50%、B可见40%，不作精确并发伸缩推断。调用分位数只包Locator；组墙钟/RPS含预热、保护与正确性检查，不当服务吞吐。未测cold、OCR、摄取或完整/query。

结果支持先做小试点；大规模并发前需单独规划材料定位的全量元数据、授权集合/版本映射和诊断字符串成本优化，保留完整ACL/current/tombstone/semantic集合等价。共享宿主的调度尖峰还需独立时段分段定位，不将所有尾延迟武断归因于SQL或模型。建议问答P95<=10s/P99<=20s/error<=1%只是root候选目标，未正式接受或验收；权限/owner错误零容忍。旧200条真实模型基线保持原freeze，不能替本轮或真实业务SLO背书。

## 源码与验证证据

- 正式执行PID41256，驱动SHA256 `7f10ee9d6dc5df12e48566a98843ce2f77f8944a42bacf3107f19b870e7f9fff`，执行前后相等；不可变同层副本benchmark_material_capacity.executed-7f10ee9d.py和manifest/raw样本保留于ignored artifacts/capacity-architecture-2026-10-25。
- 当前便捷helper仅lint清理，SHA `51f2026b0c53dba3705fdc7ce3c1af7a88506e7a914c408f2974f948b6da5c05`，Ruff/compile/help及N20/C1/2样本自检实际exit0。独立增量复审确认正向测量语义保持；正式矩阵未重跑，也未改标为新SHA。
- 初版选择性needle自检不符合固定capacity查询，已修正后重新小自检并独立复审；旧自检仅作准备历史。Win32 priority/resource API类型问题、wrapper误写数字文件及旧脚本10项lint诊断均保留。数字文件经内容/路径确认移动到自己的ignored证据，没有删除用户文件。
- 自检真实阻断自建合成.env打开与fixture外合成SQLite连接（未创建文件）；网络只用模拟audit事件验证拒绝，不向8080或外网发连接。低内存模拟触发共享stop/no request。正式运行仅创建自己的fixture，保护设置保持。
- 现有MaterialLocator回归4通过、13.58秒、实际exit0；生产源与已验收3c001ee无差异，复用3.2的160/1与生产锁4项证据，不将其重报为本轮新跑。19:47:11最终只读核对原PID48512/start/ready200、D.env哈希及D main/跟踪状态保持。

## 架构交付

[数据访问/PostgreSQL ADR](../architecture/data-access-postgresql.md)与[共享状态/对象/worker ADR](../architecture/shared-state-storage-workers.md)只完成源码盘点、设计与运行门禁，没有适配器、server或迁移成功声明。

复审提出的两处设计缺口已修复并增量通过：journal ID不能充当commit水位，首版采用全writer fence/freeze/drain、旧代失权、封存一致快照及完整状态reconcile；ACL/data epoch必须与权威SQL相关变更同事务提交/回滚，outbox仅通知，cache以当前SQL授权/epoch决定可见性。现有BM25 SQLite持久快照与进程内loaded cache已区分；恢复不得回滚当前ACL，未知staging保留。

顺序为DAL/PG行为等价与备份恢复，再共享epoch/持久fencing、对象存储，最后双API/独立worker。缺环境、驱动/secret、业务问题集、维护窗及RPO/RTO等列为外部门禁；真实迁移、故障矩阵、正式业务SLO仍未运行。不能用Qdrant server既有合成结果称整个应用多副本已支持。

## Root裁决及代价

1. 单机1000/C2试点、3规模/3并发为工程假设；代价是实际业务分布仍需新验收。
2. 只测MaterialLocator/ACL/FTS，不重跑大批模型；代价是不覆盖HTTP/模型/向量/摄取或端到端SLO。
3. 1GiB剩余内存/256MiB采样WorkingSet软门禁；代价是资源不足会保留partial/延后档位，不为了矩阵完整挤压服务。
4. 无经验证环境不加生产adapter/driver或切换；代价是长期架构仅设计完成，运行能力仍待逐项实施。
5. P95 10s/P99 20s/error1%为root候选建议；代价是目标需业务确认及真实负载复测，旧基线不能证明达标。
6. 第一版迁移/回滚坚持全writer fence/drain/seal，ID不等于提交顺序；代价是较长维护窗，online CDC另立验证项目。
7. SQL原子推进epoch、outbox只通知、明确响应授权点；代价是cache hit也需SQL权威核验，共享故障fail-closed且必须覆盖所有写入口。
8. 保留正式执行版与lint便捷版两SHA、不重跑矩阵；代价是审阅/复现需选对源码版本，不能混用自检和正式数据。
9. 内存只报采样、hash只报受测五表与schema、RPS注明检查开销；代价是不具备OS全程峰值、所有表baseline或服务吞吐证明。

## 接续与运行边界

本专项是总顺序的容量建议/设计交付收尾，timerT-A669C8D55180最终复查与校验已通过，实际submit回执收尾补入。源码/设计、合成组件诊断与生产切换分别记录；业务SLO、PG/对象/共享fencing实现、维护恢复/数据迁移和部署仍需相应门禁。所有新成果仍在本地隔离分支，不重启原服务。
