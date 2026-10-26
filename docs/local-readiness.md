# 本地准备与当前剩余工作

更新：2026-10-26。当前已交付 main 为 `99834ca`，材料定位两阶段成果已合并并双推，见[交付记录](reviews/2026-10-26-material-locator-delivery.md)。本页作为文档状态索引的当前摘要；历史报告中的“仍本地”“Docker不可用”等须结合日期，不直接作为新待办。

本轮用户要求先做本地工作，服务器上线近期暂缓。当前隔离分支为 `codex/local-readiness-2026-10-26`，实施计划见[本地准备计划](superpowers/plans/2026-10-26-local-readiness.md)。本轮不切换原服务、不读业务数据库或资料，不运行正式压力矩阵，不自动合并推送。

## 本轮本地交付范围

| 子项目 | 本地工作 | 当前状态 |
|---|---|---|
| 可移植验收工具 | tracked CLI 分开 freeze/check/run，具备 Windows/Linux 资源门禁、冻结、隔离、完整对照和失败 partial 证据；非法run参数先于应用加载拒绝 | 27项fixture单测通过，分项和partial修复复审通过；最终参数顺序修正的增量复审待关闭；真实Linux和正式run未执行 |
| API 启动治理 | 拆出纯导入启动实现，保留动态 facade/延迟依赖；修复后期启动 hook 失败时漏清理及清理错误覆盖原错误 | 定向37项及额外API用例1项、独立复审通过；生产锁合成启动烟测5项通过，完整后端待验证 |
| SQL/事务迁移准备 | 只读 AST/SQL 清单工具，显式 unresolved/coverage；定义目标 UoW 合同，不修改 SQLite 运行行为 | 10项fixture单测及两轮修复复审通过；源树扫描208文件、850条静态SQL、103项unresolved，0读取/语法错误 |
| 文档状态治理 | 统一当前交付、历史快照和真正未完成项，保留旧证据数字 | 链接与CLI help核对通过；最终整体验收结果另行回填 |

上述准备不能替代服务器运行验收。工具拥有 Linux 分支或 cgroup fixture 单测通过，也不能证明在目标 Linux/容器中实际采样成功；查询数减少不能证明延迟或长尾改善。

工具入口为 `python -m scripts.paired_material_locator_acceptance {freeze,check,run}`，仅从源Git checkout执行。`freeze`/`check`不导入RAG、不建数据库或运行定位；输出只允许新建在 `artifacts/material-locator-acceptance/` 下。`run`才生成负载，必须提供服务器选定的正数资源门槛、完整counts/workers，且本轮未调用。后续每次运行新建输出目录，不能复用旧freeze或覆盖历史报告。

SQL准备入口见[静态清单说明](architecture/sql-portability-inventory.md)与[目标UoW合同](architecture/unit-of-work-contract.md)。Python SQL标记使用真实参数表达式起始行作源码锚点，另列SQL相对行/语句序号；这不等于token级源码映射。CLI遇动态SQL或解析空缺会输出报告并返回1，不能把该非零状态改报全覆盖成功。

本轮只读扫描覆盖204个Python和4个SQL文件，另记录349个事务位置、50个代码可移植性标记；103个unresolved是无法从AST静态还原的表达式，不等于103个已证实bug。源清单绑定283887e的rag/scanner内容，最终候选仅新增验收工具参数校验时可核对这些文件未变再复用该清单；未来代码变化应另建报告。首次freeze/check已核验605个冻结文件且退出0，最终候选仍需独立生成新的清单，不将旧manifest复用为目标服务器验收。

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

API 前期 `to_thread` 索引/迁移的取消与排空协议保持原边界，本轮不在线程可能仍使用存储时提前关闭它；后期资源清理修复不能表述为所有启动取消路径均已治理完。

单机上线不要求先实现全部扩容架构；扩大实例数前则须通过相关 ADR 的一致性与恢复门槛。SQL 静态清单只标记候选和未解析项，不是全部 SQL 已证明可移植的证书。

入口：[服务器验收清单](material-locator-server-acceptance.md)、[容量与候选SLO](capacity-and-slo.md)、[DAL/PG ADR](architecture/data-access-postgresql.md)、[共享状态/存储 ADR](architecture/shared-state-storage-workers.md)。后续实际实现和验收更新本页及索引，历史报告保留其当时事实。
