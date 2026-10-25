# Sequential Delivery, Cleanup and Architecture Follow-ups

> **For agentic workers:** Use superpowers:subagent-driven-development for independent implementation tasks. Root coordinates order, Git, reviews and final verification. User explicitly authorized these phases in order; do not pause between authorized tasks.

**Goal:** 先交付三轮既有整改，再完成KB清理性能优化，最后逐项处理策略和架构待办。
**Architecture:** 每阶段使用独立设计/证据/工作记录，阶段顺序保持；阶段内仅对互不重叠的任务并行Luna。已有服务不重启、不迁移业务数据，部署与源码交付单独记录。
**Tech Stack:** 当前锁定Python3.12、pytest、Ruff、既有Git双远端；复用C隔离工作树及私有测试环境。
**Spec:** 用户本轮三阶段顺序及`docs/handoff-2026-10-24.md`最新交接。阶段2/3在开始各专项前另写其精确设计和实施计划。

## Global Constraints

- 当前本地候选`2d9102cde11e98902fd087e8206f716141fa607d`含暂存安全、上游观测、jobs索引三轮成果。D main与两个远端在本轮fetch后均为`3f52490`；这是15:27核对事实，推送前再次检查。
- Stage1明确授权合并main并按交接双推；不强推、不删除标签/分支/工作树、不改用户AGENTS或recorder数据。
- 所有测试在C无.env隔离树及显式合成Temp运行，offline guard、BelowNormal、BLAS1、短且新basetemp，至多2worker；不把Temp环境变量设成pytest basetemp。
- 主服务PID/start/ready及D.env哈希只读核对，不执行restart/reload、live cleanup/rehome或存储迁移。保留所有既有环境和成功/失败证据。
- 各独立事项各自timer和实际submit中文工作记录。Stage1 timer`T-97FD5AF93589`，不复用前轮记录。

## Review Focus

- 远端在检查后前进：推送前再fetch/比较；非快进停止该推送并重新集成，绝不force。
- 交付源码与受测树不同：完整记录候选/merge tree及git对象，docs-only回执必须核对源等价。
- 运行目录自动重载或Git hook副作用：合并前核对reload与有效hook，源码合并不宣称运行部署。
- KB清理提速遗漏外部引用或改变错误优先级：专项计划必须保留全局安全校验与旧新等价用例。
- 策略/容量缺少业务事实：先核对既有契约和证据，明确设计假设；真实业务SLO与实际迁移不得靠合成基线或默认值冒充已验收。

## Phase 1: Merge and dual push existing accepted work

- [x] 核对C/D工作树、分支、两个远端及SSH fetch，确认两端main一致、候选仅ahead无behind；检查跟踪范围不含.env/artifacts，前端及锁无改动。
- [x] 在最终候选冻结树运行原生完整后端2worker及Ruff，保留exit/stdout/stderr/JUnit；Luna只读核对三轮验收及交付证据关联，不重复全分支源码复查。
- [x] 更新本轮交付文档，核对D无跟踪改动及hooks、远端未前进，再将main快进到候选；在相同无.env代码树做合并后定向烟测并确认tree等价。
- [x] 按`git push origin main`、`git push github main`、`git push origin --tags`、`git push github --tags`双推，实际ls-remote核对main和所有标签一致；原服务PID/start/ready/env hash核对保持。
- [x] 归档本轮及三阶段原始证据，hash核对；提交Stage1唯一工作记录并记录真实回执；继续Phase2。

## Phase 2: KB cleanup snapshot and directory lookup optimization

- [x] 读`collect_kb_cleanup`及路径解析调用，写独立规格/计划、测试计数和错误优先级兼容矩阵，再分派Luna。
- [x] 先复用一次目标版本快照，消除同一document重复SQL；保留全局版本/manifest外部引用、owner及归档身份校验。
- [x] 再优化计划目录包含/重复目录判定；以确定性访问次数和完整计划/错误对照验收，不省略链接/目录类型安全检查。
- [x] 独立复审、隔离回归/合成测量、交接及实际工作记录；保留可审阅分支与证据。

## Phase 3: Policy, recovery workflow and architecture

- [ ] 403与401前端数据/身份处理：核对现有契约，写清重置范围和并发请求行为，以合成前端回归固定策略；不凭名称擅改现有会话语义。
- [ ] Compose可选组件与local/server拓扑：核对应用设置、启动依赖和文档，形成可验证矩阵与兼容方案；静态/隔离验收，不切换现有业务实例。
- [ ] 历史暂存恢复写流程：先设计owner/ACL复核、幂等、回执与补偿保留，再在合成存储实现/验证受控流程；不根据只读audit候选自动操作真实文件或DB。
- [ ] 业务容量/SLO：盘点已有真实模型及合成基线，形成明确负载和验收口径；缺少业务事实时请求必要信息，独立低并发/隔离运行，避免把合成结果当生产承诺。
- [ ] 长期数据架构：按DAL/PostgreSQL、共享Redis、对象存储、多副本/worker依赖顺序逐项写设计与迁移/回滚门禁；仅执行已具备验证条件的隔离工作，实际业务迁移保留维护窗口与备份条件。
- [ ] 汇总各专项完成证据、未完成条件和工作记录；不将设计、源码验收与运行切换混作同一完成状态。

Phase1收尾：最终main/Gitee/GitHub同为1d413ed，完整3263/4、合并后6项及生产锁SDK9/CLI4通过；Stage1记录R-20261025-CD2FF224已实际创建。最终本轮117个artifact文件及3阶段历史证据复制D归档、hash匹配。Phase2基于此main新建隔离分支继续，不表示部署。

Phase2完成：隔离分支基于main1d413ed，冻结7c22af9统一86/1、生产4项、真实SQL18轮对照通过；N300计划收集中位19.181→3.581s，非生产SLO。记录R-20261025-884467AF已实际创建，源码尚未交付main或部署。接续Phase3契约与恢复工作，再推进容量和长期架构。
