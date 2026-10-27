# 本地准备与当前剩余工作

## 当前：2026-10-27 复查整改

用户授权先审查再修复、继续并发Luna。隔离分支`codex/review-remediation-2026-10-27`从8184463开始；受测源码5c501f1已快进交付两远端main，之后仅追加交接回执，见[最新交接](handoff-2026-10-27.md)。D磁盘main保留99834ca；8080开始即未监听，结束验收保持该现状，不启动服务或迁移业务数据。用户确认旧20261026业务迁移包已经拷走，本轮只生成代码/测试补充包。

本批修复组织审计与上传/归档/删除/回收恢复/整理的提交边界、假成功与replay重复，隔离恢复目录发布/外置Qdrant保护、只读SQL工具缺失文件建库，以及旧版本表未知列的静默丢失保护。完整后端3517通过/5跳过、1988.53秒/native0，生产锁15场景、622文件前后核对及独立复审通过；不能把下方历史3481/5当本批新结果。SQLite输入信任合同见[SQL边界](architecture/sql-trust-boundaries.md)，当前事务与已验故障子集见[命令边界](architecture/command-transaction-boundaries.md)，完整证据见[本轮复查](reviews/2026-10-27-project-review.md)。

这完成的是本轮本地合同整改。PG主库/UoW、完整writer admission与授权/共享快照、持久幂等/outbox/fencing、共享存储/多实例、真实fixture/供应商证据与业务质量仍有后续工作；目标服务器矩阵/P99/SLO、真实存量迁移/切换继续保留验收空缺。

## 历史：2026-10-26 本地准备与交付

更新：2026-10-26。按用户停用旧机的交付要求，Gitee/GitHub main已从 `99834ca` 快进到 `e3c842a`，交付此前本地33个提交；之后仅追加交接文档，最终refs见[旧机交付与迁移说明](reviews/2026-10-26-machine-handoff.md)。D工作区磁盘main暂留旧版本以保护在线服务，迁移包Git main指向交付版本。本页作为文档状态索引的当前摘要；历史报告中的“仍本地”“Docker不可用”等须结合日期，不直接作为新待办。

用户要求继续并发Luna，由root主导本地工作，服务器上线近期暂缓。当前隔离分支 `codex/local-contracts-2026-10-26` 基于上一批 `0b48872`；旧 local-readiness/local-followups 分支继续保留。当前[合同加固计划](superpowers/plans/2026-10-26-local-contracts.md)推进启动诊断竞态、SQLite identifier引用和操作级事务清单。本轮不切换原服务、不读业务数据库或资料，不运行正式压力矩阵，不自动合并推送。

此前3423/5、605文件，以及上一批 `e4b07ec` 的 **3462通过/5跳过、2089.65秒/退出0**、生产锁6项、612文件前后核对，分别是[local-readiness](reviews/2026-10-26-local-readiness.md)和[local-followups](reviews/2026-10-26-local-followups.md)历史结果。本批冻结 `cdf039a`，完整后端 **3481通过/5跳过、1892.99秒/退出0**，分项/整分支复审、生产锁8项、全树Ruff/9文件格式、615文件前后核对通过，不复用旧数字。详见[本批合同加固验收](reviews/2026-10-26-local-contracts.md)。源码现已双推，仍不能把阶段结项称全项目完成。

## 本轮本地交付范围

| 子项目 | 本地工作 | 当前状态 |
|---|---|---|
| 可移植验收工具 | tracked CLI 分开 freeze/check/run；profile v2 分开构造属性与 effective ACL；四组8文档真实SQLite功能对照，计数和SQL trace来自实际旧新调用 | 本批计数修复后定向3项、独立复审、612文件前后核对、生产锁6项烟测和完整后端通过；真实Linux和正式run未执行 |
| API 启动治理 | owned work/5秒grace与一次清理保留；失败诊断独立于资源callback身份，grace日志故障不覆盖主取消 | 本批focused34项、独立复审、生产锁8项与完整后端通过 |
| SQLite与SQL/事务准备 | 单identifier helper保留合法含引号名称及额外schema列，值仍绑定；动态manifest精确来源；三命令操作级事务图 | 本批初GREEN71项、补证据后21项分别通过；新210文件/850静态SQL/103unresolved/0错误，90/10/3处置及401引用/32文件独立复核通过；232文件/1696语法候选不是完整writer |
| 文档状态治理 | 统一当前交付、历史快照和真正未完成项，保留旧证据数字 | 当前链接与最终状态已核对，历史数值保留 |

上述准备不能替代服务器运行验收。工具拥有 Linux 分支或 cgroup fixture 单测通过，也不能证明在目标 Linux/容器中实际采样成功；查询数减少不能证明延迟或长尾改善。

工具入口为 `python -m scripts.paired_material_locator_acceptance {freeze,check,run}`，仅从源Git checkout执行。`freeze`/`check`不导入RAG、不建数据库或运行定位；输出只允许新建在 `artifacts/material-locator-acceptance/` 下。`run`才生成负载，必须提供服务器选定的正数资源门槛、完整counts/workers，且本轮未调用。后续每次运行新建输出目录，不能复用旧freeze或覆盖历史报告。

SQL准备入口见[静态清单说明](architecture/sql-portability-inventory.md)、[源审查与writer候选](architecture/dynamic-sql-and-writer-review.md)和[目标UoW合同](architecture/unit-of-work-contract.md)。Python SQL标记使用真实参数表达式起始行作源码锚点，另列SQL相对行/语句序号；这不等于token级源码映射。CLI遇动态SQL或解析空缺会输出报告并返回1，不能把该非零状态改报全覆盖成功。

本批 `cdf039a` 新扫描206个Python和4个SQL文件，850静态条目与103个unresolved、0读取/语法错误；旧身份及全部来源hash一致的98项才保留，改动的5项另经源审查。最终处置90形状已理解、10需跟进、3外部源；401条canonical evidence绑定32个Rag文件，review完整与scanner不完整分开，PG仍not_assessed。AST候选覆盖232个Python文件、1696个语法标记、0错误，writer_completeness仍not_proven。615文件回归前后均匹配并退出0；之后仅文档提交，目标服务器必须新建freeze/run。

## 已完成，不重复开发

- 材料定位默认分配与 SQL 授权投影、多 KB 有序分批、完整旧新等价已交付 main；当前性能数值仍未验收。
- KB删除/上传生命周期、暂存安全与受控恢复、清理快照/目录索引、jobs索引、401/403契约、供应商ID日志采集代码等已分阶段完成。真实业务恢复、供应商真实返回头和业务验收与代码完成分开。
- Docker宿主修复、Qdrant server鉴权/合成迁移/持久化和隔离双容器验收此前完成；这不代表真实业务资料已迁移，也不证明 SQLite 应用可安全多副本部署。
- 原始测试、部署/故障演练的固定版本与边界见各专题报告；旧空复选框、旧失败日志或旧索引文案不覆盖较新的验收证据。

## 后续依赖与未验门槛

| 顺序 | 后续事项 | 尚缺内容 |
|---|---|---|
| 1 | 服务器组件/业务验收 | 目标平台资源门禁实测、冻结版本和负载、完整新旧矩阵、真实问题分布与正式 SLO；按用户要求暂缓，数值留空 |
| 2 | 真正上线和存量迁移 | 服务器部署、维护窗口、备份恢复、真实存量 SQL/文件/向量对账和切换回滚；本轮未执行 |
| 3 | PostgreSQL 主库实现 | 在本地 SQL/事务清单基础上建立隔离 PG 原型、选择驱动、schema/迁移、repository 双 adapter、事务/CAS/中文 FTS 等价；当前没有 PG 主库支持 |
| 4 | 共享状态/存储/多实例 | PG/DAL 验收后实现原子权限/data epoch、缓存失效、持久 fencing、BlobRef 条件写、outbox/reconcile、独立 worker 与故障矩阵；当前仍为设计 |
| 5 | 生产自动 shadow | 明确内容范围、脱敏、保留/访问权限、预算/并发与隔离后再接入；现有执行工具不代表自动采样已启用 |
| 6 | 数据与覆盖空缺 | 两项真实脱敏表格 fixture、供应商真实 request-ID 证据、平台特定回归和业务答案质量，不能用合成数据伪装完成 |

API前期owned-work排空与一次清理保持，线程未结束则保留资源，material worker stop=False保护不变。本批独立token诊断已覆盖此前cleanup intent前或finish先赢时的诊断缺口，也隔离grace日志异常。5秒grace不是进程或全部清理hook的硬上限，不回滚已提交迁移；不宣称全部worker已停或分布式fencing已实现。

本地仍可逐项核定10项SQL输入/metadata/旧库信任合同，按[操作级事务图](architecture/command-transaction-boundaries.md)补故障矩阵与幂等/审计合同，再设计完整writer/UoW迁移。授权helper读取不假称共享writer snapshot，独立组织审计不假称与删除终态提交原子；文档中提出的故障测试未伪称已运行。这些follow-up并非10个已证实bug。

单机上线不要求先实现全部扩容架构；扩大实例数前则须通过相关 ADR 的一致性与恢复门槛。SQL 静态清单只标记候选和未解析项，不是全部 SQL 已证明可移植的证书。

入口：[服务器验收清单](material-locator-server-acceptance.md)、[容量与候选SLO](capacity-and-slo.md)、[DAL/PG ADR](architecture/data-access-postgresql.md)、[共享状态/存储 ADR](architecture/shared-state-storage-workers.md)。后续实际实现和验收更新本页及索引，历史报告保留其当时事实。
