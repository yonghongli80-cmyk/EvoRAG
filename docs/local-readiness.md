# 本地准备与当前剩余工作

更新：2026-10-26。当前已交付 main 为 `99834ca`，材料定位两阶段成果已合并并双推，见[交付记录](reviews/2026-10-26-material-locator-delivery.md)。本页作为文档状态索引的当前摘要；历史报告中的“仍本地”“Docker不可用”等须结合日期，不直接作为新待办。

用户要求继续并发 Luna、先做本地工作，服务器上线近期暂缓。当前隔离分支为 `codex/local-followups-2026-10-26`，基于上一批 `c74df90`；旧 `codex/local-readiness-2026-10-26` 分支继续保留。当前[后续计划](superpowers/plans/2026-10-26-local-followups.md)覆盖启动排空、真实小型 SQLite 功能对照、SQL 源证据及写归属候选。本轮不切换原服务、不读业务数据库或资料，不运行正式压力矩阵，不自动合并推送。

上一批完整后端 **3423通过/5跳过，1966.16秒/退出0**、生产锁5项烟测及605文件前后核对是[上一批验收](reviews/2026-10-26-local-readiness.md)的历史基线，不作为本批新结果。本批冻结 `e4b07ec`，完整后端 **3462通过/5跳过，2089.65秒/退出0**、生产锁6项合成烟测、全树Ruff、612文件前后核对及独立分项/整分支复审通过。详见[本批验收与裁决](reviews/2026-10-26-local-followups.md)。源码仍本地，服务器正式验证与下列未来产品架构保留，不能把本批结项称全项目完成。

## 本轮本地交付范围

| 子项目 | 本地工作 | 当前状态 |
|---|---|---|
| 可移植验收工具 | tracked CLI 分开 freeze/check/run；profile v2 分开构造属性与 effective ACL；四组8文档真实SQLite功能对照，计数和SQL trace来自实际旧新调用 | 本批计数修复后定向3项、独立复审、612文件前后核对、生产锁6项烟测和完整后端通过；真实Linux和正式run未执行 |
| API 启动治理 | 纯导入实现与动态 facade 保留；owned work区分取消等待与线程结束，5秒grace、超时保留资源、随后一次清理、原错误优先与late-error诊断 | 初轮66项、修复后29项分别通过，独立复审关闭P2，完整后端通过；不相加分项数字 |
| SQL/事务迁移准备 | 字面量清单、源绑定动态审查manifest和AST writer候选；引用路径/行号/跨文件hash有效；保持PG/UoW实现边界 | 修复后39项fixture通过；209文件/850静态SQL/103unresolved/0错误，103源审查处置及最终生成物独立复核通过；1696语法候选不代表完整writer |
| 文档状态治理 | 统一当前交付、历史快照和真正未完成项，保留旧证据数字 | 当前链接与最终状态已核对，历史数值保留 |

上述准备不能替代服务器运行验收。工具拥有 Linux 分支或 cgroup fixture 单测通过，也不能证明在目标 Linux/容器中实际采样成功；查询数减少不能证明延迟或长尾改善。

工具入口为 `python -m scripts.paired_material_locator_acceptance {freeze,check,run}`，仅从源Git checkout执行。`freeze`/`check`不导入RAG、不建数据库或运行定位；输出只允许新建在 `artifacts/material-locator-acceptance/` 下。`run`才生成负载，必须提供服务器选定的正数资源门槛、完整counts/workers，且本轮未调用。后续每次运行新建输出目录，不能复用旧freeze或覆盖历史报告。

SQL准备入口见[静态清单说明](architecture/sql-portability-inventory.md)、[源审查与writer候选](architecture/dynamic-sql-and-writer-review.md)和[目标UoW合同](architecture/unit-of-work-contract.md)。Python SQL标记使用真实参数表达式起始行作源码锚点，另列SQL相对行/语句序号；这不等于token级源码映射。CLI遇动态SQL或解析空缺会输出报告并返回1，不能把该非零状态改报全覆盖成功。

本批在 `e4b07ec` 新建只读扫描：205个Python和4个SQL文件、349个事务位置、50个代码可移植性标记。103个unresolved经源审查为88个形状已理解、12个需跟进、3个外部数据源，仍不等于103个已证实bug或PG兼容。371条canonical evidence绑定31个Rag源文件；review coverage完整与scanner coverage不完整分开，portability仍not_assessed。AST候选另覆盖231个Python文件、1696个语法标记、0错误，writer_completeness仍not_proven。新freeze/check回归前后612文件均退出0；之后仅结果与回执文档提交，目标服务器必须新建freeze/run，不复用本次Windows或旧manifest。

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

API 前期索引/迁移现有owned-work排空与一次清理；线程未结束则保留资源，material worker stop=False的保护不变。5秒grace不是进程退出或所有清理hook的硬上限，也不承诺撤销已提交迁移。极窄的完成早于cleanup intent且取消获胜时，次要诊断可能缺失，完整取消次序诊断仍可后续加强。

后续可在本地继续逐项核定12项动态SQL输入/identifier边界及真实调用链，再设计完整writer/UoW迁移；它们不是本次manifest校验成功便已修复的事项。

单机上线不要求先实现全部扩容架构；扩大实例数前则须通过相关 ADR 的一致性与恢复门槛。SQL 静态清单只标记候选和未解析项，不是全部 SQL 已证明可移植的证书。

入口：[服务器验收清单](material-locator-server-acceptance.md)、[容量与候选SLO](capacity-and-slo.md)、[DAL/PG ADR](architecture/data-access-postgresql.md)、[共享状态/存储 ADR](architecture/shared-state-storage-workers.md)。后续实际实现和验收更新本页及索引，历史报告保留其当时事实。
