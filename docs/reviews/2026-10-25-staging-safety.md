# 材料暂存安全与快照诊断整改（2026-10-25）

状态：实现、分项及整分支复审、唯一修复波次和最终统一代码验收已完成。完整后端第2轮 **3207通过/4跳过**，830.29秒、退出0；JUnit3211项、0失败、0错误。全树Ruff、生产锁烟测及原服务只读核对通过。受测源码与测试冻结在`192aa8ec7ff6ff7c5ce4751a1ace547d9715b3e7`；后续仅文档收尾。本轮新分支尚未合并或部署。

## 交付上下文

用户要求先合并并双推已验收KB整改，再继续由主代理思考、Luna并发推进。前一阶段已把main从`9f0574b`快进到`d5ff13e`，交付说明提交后本地main/Gitee/GitHub核对为`3f524904e16b19119fe76009c41763b89bbdae8d`，全部tag refs一致、无强推；合并后22项隔离冒烟通过。该阶段记录为`R-20261025-2AD3EF2D`，与本次实现事项分开。

本次新分支`codex/staging-recovery-2026-10-25`基于`3f52490`，继续复用C隔离worktree和已有锁定环境。设计、计划见[规格](../superpowers/specs/2026-10-25-staging-safety-design.md)、[计划](../superpowers/plans/2026-10-25-staging-safety.md)；工具操作见[只读审计说明](../staging-audit.md)。这轮选择由主代理根据用户委托作出；没有替业务实例执行停写、部署或恢复操作。

## 实现行为

| 范围 | 结果与保证 |
| --- | --- |
| 上传取消/所有权 | fully staged请求交给shielded同步worker后，取消HTTP等待不会取消queued/running工作或提前unlink；worker持续持有KB gate，完成audit和cleanup，并观察取消后的异常。流式接收期间失败仍清理partial文件。 |
| 普通版本与job | 文件move后，version final-path CAS、旧job取消与新job插入共享一个短SQL事务；INSERT或COMMIT失败回滚。根据stage/final实际状态补偿，不能确认移回时保留字节与version/permission身份。 |
| 归档确认 | 新child jobs与container/child activation、manifest confirmed及idempotency结果同事务，不再先confirmed/删源后单独enqueue。 |
| 事务退出 | ConnectionManager将COMMIT纳入异常回滚边界，补偿用version/permission DELETE复用该事务上下文；真实延迟外键验证COMMIT失败后身份回滚、连接可继续合法写入。 |
| 删除owner | target全部version、manifest/container关系、外KB共享路径引用和文件/目录类型在collect阶段校验，失败在manager/filesystem teardown前返回503；missing targets保持可重试。source hash staging与version final表示分开验证。 |
| 快照审计 | explicit数据库与目录输入，immutable SQLite、自包含sidecar检查、元数据和目录只读扫描、无config/.env/UserDB/app初始化。所有结果inspect_only，candidate不推断内容hash或自动修复；无apply/rehome参数。 |

不新增产品schema、receipt表、恢复API、依赖、ACL或模型行为。历史global stage不能从文件名证明owner；工具提供事实与引用上下文，保留人工核验边界。

## 审查与定向证据

Luna三路先只读审计，主代理设计并对依赖、事务与路径角色作裁决，再由Luna并行实现Task1/3/4；共享上传入口的Task2在Task1批准后接入。Git由主代理选择性提交，未将其他任务施工文件混入单项提交。

| 任务 | RED/GREEN与复审 |
| --- | --- |
| Task1 | 4项取消RED；70项相关GREEN。提交`de4920b`，fresh规格/质量PASS。 |
| Task2 | 真实job INSERT trigger 3项RED；真实deferred COMMIT留下open transaction的RED。最终相关83项GREEN，含Task1取消与架构；提交`6688f75`，fresh规格/质量PASS。 |
| Task3 | 越owner版本DELETE原返回200的RED；source/global错误优先级2项RED。类型角色补充6项RED后11项目标GREEN，相关131项GREEN；`5d4fdf6` + `b36282f`，增量复审PASS。 |
| Task4 | 新接口缺失RED明确不称既有产品bug；可选表/非整数metadata复审RED后18项GREEN，含真实Windows junction。`8200d35` + `a1670ff`，增量复审PASS。 |

整分支审查以`8f45b88`执行，发现两个Important和一个被root提升为Important的NUL路径错误契约问题：补偿DELETE显式commit未回滚；真实rag_docs global-stage在move-back/repoint双失败时漏报final候选；SQLite路径含NUL可逃出JSON出口。

唯一修复波次提交`192aa8e`：两个DELETE helper改用具备COMMIT失败回滚的事务上下文；对确认缺失的root内staging声明检查identity目录，候选仍为`candidate_only/inspect_only`，root外路径和链接不触发扫描；version、KB document和manifest路径中的NUL在文件操作前返回`unsupported_schema`。真实延迟DELETE约束及guarded CLI首次定向为6失败/2通过，实际rag_docs route→SQLite backup候选回归另为1失败；修复后新integration/audit共28通过，上传/版本/归档/权限相关58通过。首次RED夹具的不存在方法名已纠正，实际路由RED另行保留，不把夹具错误计作产品缺陷。

fresh Luna对`8f45b88..192aa8e`只做一次增量复查：三项均ADDRESSED、无修复引入的新问题；没有重复做第二轮整分支审查。每项包含实现与独立复审证据，root统一运行补足复查明确未执行测试的边界。

## 最终统一验证

稳定源码完整后端第2轮通过，使用锁定开发Python、offline_network_guard、每个pytest进程的BelowNormal priority plugin、系统Temp短basetemp、2 worker/loadfile、BLAS1。单次结果为 **3207 passed / 4 skipped in 830.29s**，进程退出0；JUnit3211项、0 failures、0 errors，stderr为空。没有拼接首跑或分项通过数。验收后再次检查源码和测试与冻结提交`192aa8e`一致，无未跟踪源码；之后只补文档。

四项跳过：`test_start_signal_forwarding.py`的Windows terminate不可忽略；`test_kb_lifecycle_integration.py`的fork启动方法不可用；`test_spreadsheet_schema_mirror_e2e.py`两项缺少真实脱敏数据夹具。没有伪造业务夹具或削弱断言消除跳过。

已独立执行全树`python -m ruff check .`，退出0。生产锁离线smoke退出0，实际检查普通upload/job提交、延迟COMMIT与两类补偿DELETE回滚后可继续写入、immutable audit、config-free导入、global-stage候选和NUL metadata错误契约。所有数据位于系统Temp，SQLite backup和更新连接均关闭后才做immutable检查。frontend无变化，不重复其历史326项或构建。

本轮证据根目录为`artifacts/staging-recovery-2026-10-25/`，完整验证集中于`verification/`：`backend-final-attempt-2.*`、`run-backend-final-attempt-2.ps1`、`verification-summary.json`（含日志SHA256）、`preflight-results.json`、`ruff-final.log`、`runtime-smoke.py`与`runtime-smoke-attempt-1.log`。最终JUnit SHA256为`8e752b68f89d07a545f8f513768ec2d93dbab90519566b530fc1599069ce1e65`。分项、整分支与唯一增量复审原始报告保留于本计划SDD目录；这些本机忽略文件需随跨机器交接另行提供。

首轮基础设施异常完整保留于`interruption.json`及`backend-final-attempt-1.*`：新PowerShell/cmd进程返回`0xC0000142`，Node返回`0xC000026B`，原生命令连接关闭，wrapper最终退出`1073807364`。未确立底层原因，不把它写成产品断言失败或已证明的资源不足。用户确认本机恢复后，先检查旧日志、全部记录PID与原服务，再重跑；没有停止原服务、重启宿主或安装/修复系统组件。第二轮单独使用`backend-final-attempt-2.*`和`run-backend-final-attempt-2.ps1`。

## 过程与边界

- Task4纯模块测试初次正常conftest收集碰到并行Task2施工中的API缩进错误，已协调并由Task2修正/compile；Task4用`--noconftest`验证纯模块，root最终全量仍将使用正常conftest。该收集错误没有被包装成产品GREEN或部署问题。
- 两位实施报告曾把materials/api的非空行数写成god-module api计数，root纠正为物理行数：rag/api.py823、pipeline.py615、materials/api.py681；最终budget以自动化架构门禁为准。
- actual Windows junction测试只在自有Temp创建，清理junction自身并检查外部目标保持；不对业务junction或历史文件执行动作。
- 旧global stage、未知archive source不自动删除或重写；既有备份排除暂存目录，普通备份payload不等于暂存文件全量镜像。
- CLI不自动重定位快照内绝对路径；与输入副本根不匹配时仅报告root外引用，不回到原业务路径查找。副本路径对应关系由已有快照/恢复流程保证。
- 外部actor在校验后替换文件的TOCTOU不属于本协议保证；全局metadata扫描的生产规模延迟未压测，无SLO承诺。

## 主代理决策及代价

1. 复用C隔离worktree并从交付main新开分支；若判断错，只在该分支返工。
2. 按文件所有权并发Task1/3/4、Task2等待共享入口review；代价是根代理必须整合接口与统一验证。
3. 采用现有安全修复+只读诊断，不增加持久receipt/写恢复API；代价是历史无主stage仍需已授权人工处置。
4. immutable只接受无WAL/SHM的自包含快照；代价是操作者必须先导出一致快照。
5. 不把archive source与container version final路径强行要求相等；source仍须hash staging。代价是未支持的legacy布局继续fail-closed。
6. 基线沿用已核对的3141/4历史全量及22项exact-merge smoke，不重复未变源码基线；环境差异由本轮定向与最终全量发现。
7. missing声明只按document/version identity目录报告候选，不猜文件名/hash；代价是仍需人工确认原始内容。
8. CLI放在独立rag顶层模块避开materials包的runtime导入；代价是新增一个维护模块，不重构现有公共包。
9. 共享transaction修复COMMIT失败回滚，保持nested reuse；代价是需验证公共事务使用面，所以保留真实延迟约束与完整后端门禁。
10. --help为标准人读帮助，执行报告和输入错误为JSON；解析help的机器调用方需遵守实际数据协议。
11. NUL路径异常按同类malformed snapshot影响提升为必须修复；代价是一项有界验证与回归，无新业务行为。
12. 不扩展到外部文件系统actor的TOCTOU保证；代价是部署/维护仍要控制所有写入者和目录权限。
13. O(N)版本/manifest引用证明保留，未作生产性能承诺；代价是大型库删除延迟需要隔离测量后再优化。
14. 宿主中断的首跑保留为未完成，确认无遗留测试进程后降至2 worker完整重跑；代价是验收耗时更长，不用局部结果替代完整证据，也不据此推断宿主故障原因。

## 运行状态与交付

2026-10-25 11:39:30只读核对：原8080服务仍为PID48512，启动时间`2026-10-23T14:09:17.6870660+08:00`保持；`/ready`200，SQLite/Qdrant local为OK，Redis仍为可选degraded。原`.env` SHA256保持`6128C4BD1F1BC0E43AB5189BAD25FA160CB8567B555B1B0CC5921E8771B0E1BD`；C隔离树没有`.env`。证据为`verification/service-after.json`；未重启、切换实例、部署或操作业务存储。

D主目录main和两端只读ls-remote仍一致为`3f524904e16b19119fe76009c41763b89bbdae8d`，标签一致、D tracked状态干净。前批KB交付已完成合并双推；本轮代码仍在`codex/staging-recovery-2026-10-25`，尚未合并或推送。验收通过不表示已经上线。C工作树、环境、SDD和artifact证据继续保留。

当前实现事项的公司记录在代码文档提交后由root统一提交，不能用前一阶段交付记录或子代理审查记录代替。

## 后续事项的范围

| 事项 | 当前状态与下一步边界 |
| --- | --- |
| 历史无主暂存和legacy archive源 | 本轮提供保留、拒绝不明删除和只读诊断；显式恢复/rehome仍需owner证据、ACL、幂等及过期策略的独立设计，不直接操作业务旧文件。 |
| 运行实例切换 | 代码合并不等于部署。继续遵守上一轮KB协议要求：备份、停写、排空旧写者，再统一升级所有写进程和规范化DB路径；本轮不执行停服或迁移。 |
| 真实规模与性能 | 既有真实模型合成基线仍有效，但业务分布、容量/SLO和大库元数据扫描延迟需要各自隔离测量；本轮没有新增模型或Docker运行演练。 |
| 长期架构及其它交接条件 | 数据访问/PostgreSQL、共享Redis/锁、对象存储、多副本/独立worker，以及会话/Compose拓扑、脱敏夹具和供应商request-ID等沿用当前交接的专项边界，没有被本轮安全整改自动完成。 |

上述事项不与已经关闭的本轮代码发现混算，也不能据本轮验证声称整个项目及业务上线已全面验收。更早Docker已完成的受控运行验收仍按原交接保留，不重复记为本轮成果。
