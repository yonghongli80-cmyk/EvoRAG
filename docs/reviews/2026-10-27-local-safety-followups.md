# 2026-10-27 本地安全后续：清理、连接与 writer 证据

## 范围与当前状态

本轮用户要求先计划、再并发 6.1 Terra，root 统筹，服务器验收保留且不影响业务数据。基线为双远端已交付的 `8eac1fa`；当前仅在 C 隔离工作区分支 `codex/local-safety-2026-10-27` 开发。此前补充包保持其冻结版本，不自动覆盖。本轮未切换 D 工作区、不读业务库或配置正文、不启动服务。

本页先记录实施/复审过程；完整后端、源码冻结、生产锁和最终独立复审尚待本轮收尾后填入，不能拿前一轮3517/5替代新证据。设计与计划见 `../superpowers/specs/2026-10-27-local-safety-followups-design.md` 和 `../superpowers/plans/2026-10-27-local-safety-followups.md`。

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
- 整体集成124通过/80.71秒/native0；整批独立复审再发现重叠根的外来数据集持有者遗漏。唯一最终修复波先做RED 3失败/1通过/9.10秒，再做purge相关44通过/27.86秒/native0；最终单次限定复核关闭原P1，未发现修复差异新回归。完整后端仍待冻结后执行。

## 复审事实更正

正常 `final_version_path` 返回绝对路径；最初评审将其当作带 root 前缀的相对路径来源，已撤回并更正。真实来源是 `stage_version` 及 ArchiveService 预览 quarantine 的直接相对持久化。修复测试分别覆盖最终路径正例、真实暂存相对路径、双候选歧义拒绝和跨文档引用，而不根据错误推断扩大接口。

## 保留的待办

服务器资源门禁、正式矩阵/P99/SLO、真实业务恢复/对账/切换、真实脱敏fixture及供应商证据继续未验。writer admission、授权一致快照、未知commit结果、进程崩溃与TOCTOU/持久fencing、PG/UoW、共享状态/BlobRef/outbox/多实例继续按既有依赖推进。本轮不提供这些完成证明。
