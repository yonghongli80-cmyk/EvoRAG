# 2026-10-27 本地安全后续：清理、连接与 writer 证据

## 范围与当前状态

本轮用户要求先计划、再并发 6.1 Terra，root 统筹，服务器验收保留且不影响业务数据。基线为双远端已交付的 `8eac1fa`；当前仅在 C 隔离工作区分支 `codex/local-safety-2026-10-27` 开发。此前补充包保持其冻结版本，不自动覆盖。本轮未切换 D 工作区、不读业务库或配置正文、不启动服务。

本轮受测源码冻结为 `f047a739a33ab477087a5f09673574d78e465d9b`。完整后端 **3572通过/5跳过、2151.06秒/native0**，JUnit3577项0失败0错误；625受测文件前后核对一致。三项独立SPEC/QUALITY、最终整批修复复核、生产锁5场景及SQL来源绑定均通过。随后仅更新文档，本批保持本地隔离分支，不合并双推或覆盖旧补充包。设计与计划见[设计](../superpowers/specs/2026-10-27-local-safety-followups-design.md)和[计划](../superpowers/plans/2026-10-27-local-safety-followups.md)。

## 三项本地合同

1. 到期 purge 在任何 unlink/vector 操作前，先验证所有版本和结构化数据集目标、版本归属、跨文档版本/归档源引用、目录链接及显式根目录。现存根内非规范旧文件名保持支持；有版本记录缺 versions_root、有数据集记录缺 datasets_root 均失败闭合。最终 SQL 仍在外部效果成功后提交，局部文件已删后的失败通过原记录继续重试，不称文件 rollback 或跨存储事务。
2. ConnectionManager 用 reader 注册锁线性化关闭判定和发布，失败时关闭未发布 reader；等待 writer 锁的 write_connection/transaction 获锁后重新检查 closed。读初始化不请求业务 writer 锁，嵌套 transaction 语义未改；已经交给调用者的 reader 仍可被 close 关闭，不新增 lease/drain。
3. writer AST 工具保守处理分支、循环、异常、with 和词法作用域。未知身份保留 unknown 和 sink，确定直线正例保留。工具只解析源码，仍不能证明可达性、运行时对象身份、writer 完整性或 PG 兼容。

## 已有实际证据（阶段结果，不相加）

- 有效基线43通过/50.93秒/native0。最初沙箱临时目录权限错误和并发测试编辑错误不计作行为 RED，日志保留；正常临时目录运行始终保留网络拦截、低优先级和单线程BLAS。
- 初始 RED 17失败/5通过/22.32秒/native1，分别确认7个purge、4个连接和6个AST行为缺口。
- 补充 RED 6失败/28通过/23.18秒/native1，确认缺根相对路径、外来链接别名和四类同名绑定。
- 首轮联合96通过/45.89秒/native0；独立复审发现 dataset 缺根、暂存相对路径及 AST 提前退出/绑定缺口，未据绿色单测直接结项。
- 审查修正后的联合111通过/65.70秒/native0；后续 AST with 确定直线归因回归经RED 1失败/1通过，再由scanner全组及第二轮定向复审确认关闭。
- 整体集成124通过/80.71秒/native0；整批独立复审再发现重叠根的外来数据集持有者遗漏。唯一最终修复波先做RED 3失败/1通过/9.10秒，再做purge相关44通过/27.86秒/native0；最终单次限定复核关闭原P1，未发现修复差异新回归。上述分项结果不相加作完整后端计数。

## 复审事实更正

正常 `final_version_path` 返回绝对路径；最初评审将其当作带 root 前缀的相对路径来源，已撤回并更正。真实来源是 `stage_version` 及 ArchiveService 预览 quarantine 的直接相对持久化。修复测试分别覆盖最终路径正例、真实暂存相对路径、双候选歧义拒绝和跨文档引用，而不根据错误推断扩大接口。

## 冻结与独立验证

代码提交分别为 `b469d63`（purge目标/引用）、`a95cec8`（连接关闭）、`1a68944`（AST作用域），随后 `f047a73` 保存设计/计划/阶段报告。三组独立任务审查均关闭重要问题；整批唯一修复波覆盖重叠根的外来数据集持有者，最终限定复核关闭P1且未发现修复差异的新回归。

生产依赖锁5个合成场景通过，9.532秒/native0、SQLite3.49.1：reader配置期间关闭、purge越根零副作用、缺根相对路径失败闭合、合法旧相对路径清理与replay、AST分支unknown及不执行源码。SQLite路径护栏记录5次打开均在私有fixture中；网络护栏保持。此烟测不覆盖真实模型、业务数据、服务部署或性能。

冻结后重建SQL清单：211文件（207Python/4SQL）、857静态SQL、103动态项、0错误；scanner按覆盖未完成返回1，未改报成功。writer清单233文件、1707语法候选、0错误、parse_complete=true/CLI0，writer_completeness仍not_proven。

仅102条身份、自身源和全部evidence哈希未变化的记录保留；另1条ConnectionManager的SQLite PRAGMA经过独立Terra重审。最终103条来源处置完整，90understood_shape/10needs_followup/3external_source，409条引用绑定32个Rag源文件；API/CLI校验均0，portability_readiness仍not_assessed，human_reviewed=false。构造器整数类型注解不提供运行时输入验证，已在重审论据和后续建议中明示。

原始证据位于 `artifacts/local-safety-followups-2026-10-27/`；625源清单位于 `artifacts/material-locator-acceptance/local-safety-followups-2026-10-27/source-freeze.json`。内部审查/失败迭代保留在本计划独立SDD目录，既有补充包未覆盖。

## 最终完整后端与原目录保护

完整后端于13:48:38开始、14:24:35原生退出0，父进程2157.093秒；pytest3572通过/5跳过、2151.06秒。5跳过分别是Windows不支持fork、FIFO、忽略terminate三项，以及缺真实脱敏表格fixture两项；未通过删测试或合成样例伪装闭合。

完整回归之后，官方freeze/check再次校验625文件并退出0，manifest摘要 `8307616a3c5ab982557d60881a99ffbb5a5a01c85b277ce80eb5bbeb87e79bb7`。只读核对原D目录main仍99834ca、跟踪树干净、配置哈希不变；8080开始和结束均未监听，没有启动/停止原服务或读取业务库。当前C无.env。生产环境127项、开发环境133项锁版本重新核对均零偏差，无安装或更新依赖。

`verification-summary.json` 已交叉核对原生结果、JUnit、source-check、运行锁、SQL来源及前后环境记录。Ruff全树检查和三个新测试文件格式检查通过；没有宣称存量全树格式统一。服务器/真实业务/长期架构的未验门槛仍按下节保留。

## 保留的待办

服务器资源门禁、正式矩阵/P99/SLO、真实业务恢复/对账/切换、真实脱敏fixture及供应商证据继续未验。writer admission、授权一致快照、未知commit结果、进程崩溃与TOCTOU/持久fencing、PG/UoW、共享状态/BlobRef/outbox/多实例继续按既有依赖推进。本轮不提供这些完成证明。
