# 文档状态索引

> 更新日期：2026-10-25

项目功能、依赖和启动方式以根目录 `README.md`、当前代码、自动化测试和 Git 提交为准。历史设计文档保留决策背景，不等同于尚未完成的待办。

## 当前有效入口

- `reviews/2026-10-25-material-locator-overhead.md`：本地材料定位 SQL 授权投影与分配优化、完整等价、84 项回归及生产锁 4 项烟测；正式配对性能矩阵因资源门禁待补，尚未再次交付 main。
- `reviews/2026-10-25-local-results-delivery.md`：KB清理/会话与Compose/恢复/容量架构四专项新增成果的集成、快进双推、3341/5+330及运行保护核对。
- `capacity-and-slo.md`：单机试点建议、27组材料定位组件结果、候选SLO及正式业务验收条件。
- `architecture/data-access-postgresql.md`、`architecture/shared-state-storage-workers.md`：源码盘点、全writer迁移水位、SQL原子epoch及后续共享存储/worker门禁；提案不等于已实现。
- `reviews/2026-10-25-capacity-architecture.md`：正式2520样本、两版驱动SHA、统计/资源边界、独立复审与外部条件。
- `reviews/2026-10-25-staging-controlled-recovery.md`：历史暂存维护恢复、重放后置条件、独立复审及统一160/1和生产锁4项验收。
- `staging-recovery.md`：显式维护preview/apply、支持范围、备份/停写、字节保留和失败重试门禁。
- `reviews/2026-10-25-auth-compose-contracts.md`：当前401身份清除、403数据重置、Compose语义对照、独立复审及330前端/42后端验收。
- `auth-session-contract.md`、`runtime-topology-contract.md`：会话代际和运行拓扑的有效行为边界。
- `reviews/2026-10-25-kb-cleanup-index.md`：KB清理调用内版本快照、目录祖先集合优化、旧新完整计划/拒绝矩阵、真实SQLite合成对照及统一验收。
- `superpowers/plans/2026-10-25-kb-cleanup-index.md`：顺序产品任务、并行兼容/基准准备、独立复审及根验收记录。
- `reviews/2026-10-25-three-phase-delivery.md`：暂存安全、上游观测、jobs索引三轮成果的完整集成、合并双推、原始证据归档及运行边界。
- `superpowers/plans/2026-10-25-sequential-followups.md`：用户授权的交付→KB清理优化→策略/架构逐项执行顺序和进展。
- `reviews/2026-10-25-staging-audit-jobs-index.md`：暂存审计jobs预索引、报告兼容/复杂度回归、旧新合成测量及统一验收。
- `superpowers/plans/2026-10-25-staging-audit-jobs-index.md`：本轮先计划再并行Luna的实施、复审及收尾状态。
- `reviews/2026-10-25-upstream-observability.md`：供应商HTTP响应ID摘要与内部日志关联、SDK真实构造点接入、独立复审及最终验收状态。
- `superpowers/plans/2026-10-25-upstream-observability.md`：上游观测实施与分项/统一验证记录。
- `upstream-observability.md`：事件schema、JSON日志读取、ID摘要和供应商追溯边界及SDK升级检查。
- `reviews/2026-10-25-staging-safety.md`：材料暂存所有权、上传取消与提交一致性、KB删除前置校验和只读快照审计；定向证据与最终验收状态分开记录。
- `superpowers/specs/2026-10-25-staging-safety-design.md`：本轮安全边界、事务/owner设计与只读审计契约。
- `superpowers/plans/2026-10-25-staging-safety.md`：本轮实现任务、审查和统一验证记录；最终证据以交接和审查报告为准。
- `staging-audit.md`：显式快照输入的只读CLI使用说明、报告码及候选/自动恢复边界。
- `handoff-2026-10-24.md`：当前阶段交接，顶部区分本地验收与已交付状态；保留各轮验收及部署/Docker历史边界，main以最新实际远端核对为准。
- `reviews/2026-10-24-kb-lifecycle-concurrency.md`：KB删除/上传生命周期并发协议、崩溃重试、失败归档所有权、最终完整回归与部署边界。
- `superpowers/plans/2026-10-24-kb-lifecycle-concurrency.md`：KB生命周期并发整改的TDD计划及完成状态。
- `reviews/2026-10-24-deep-audit.md`：上一轮深度审查、增量缺陷修复、实际验证、过程偏差与剩余专项设计/运行条件。
- `superpowers/plans/2026-10-24-deep-audit-remediation.md`：本次先计划再并行Luna的逐项实施与验收状态。
- `reviews/2026-10-24-real-model-p99.md`：真实模型200条合成资料基线、阶段/外呼与metrics对齐、少量参数/连接对照及适用边界。
- `superpowers/plans/2026-10-24-real-model-p99.md`：本次隔离运行验收计划与执行状态。
- `reviews/2026-10-24-handoff.md`：凌晨Gitee交付的原始交接及两轮修复证据口径。
- `handoff-2026-10-23.md`：前次交接，保留SSH/双远端、关键行为和pipeline整改历史。
- `superpowers/plans/2026-10-24-review-backlog.md`：本轮逐项执行计划，当前状态以新交接和实际验证为准。
- `superpowers/plans/2026-10-23-post-handoff-remediation.md`：交接后定向整改与集成验证完成记录，已并入 `main`。
- `superpowers/plans/2026-10-23-pipeline-observability.md`：本轮 pipeline 职责拆分、直接计时与日志上下文实施记录，已并入 `main`；集成和验证状态以最新交接为准。
- `superpowers/plans/2026-10-21-unified-remediation.md`：上一轮统一修复计划（交接遗留 + 全库审查）。
- `PROJECT_INTRODUCTION.md`：架构白皮书（描述当前 `main`；历史对照数字以文中标注的提交为准）。
- `development-log.md`：按“问题→证据→决策→结果”记录的开发日志。
- `deployment.md`：生产部署的权威入口（本地开发与 Docker 部署以本文为准）。
- `superpowers/specs/`：功能设计边界和安全约束。
- `superpowers/plans/`：TDD 实施步骤记录；文件中的未勾选复选框是执行模板，不代表当前代码缺少该功能。
- Git 历史：判断功能是否已经落地的最终变更记录。

## 历史快照

以下文档保留用于追溯，不再作为当前待办来源：

- `project-review.md`：2026-06-21 项目状态快照。
- `code-review-2026-06-26.md`：修复前的审查问题清单。
- `tech-debt-plan.md`：四项技术债均已完成，计划已归档。
- `engineering-infra-plan.md`：核心工程基础设施已经落地，计划已归档。
- `handoff-2026-08-06*` 至 `handoff-2026-10-21*`：历次会话交接，保留过程；当前交接见上一节。
- `plans/`：按**文件**而非按目录区分——`plans/2026-10-19-refusal-answer-position.md` 与 `plans/dev-log.md` 仍被当前交接引用，不属归档；`plans/2026-05-29` 至 `plans/2026-06-28` 各计划与 `plans/rag-system-plan.md` 为历史快照。

## 当前维护原则

1. 新工作只从当前代码、测试失败或新的明确需求建立待办。
2. 不根据历史计划中的空复选框重复开发已经存在的功能。
3. 已实现计划保留原文，不批量改写历史过程；归档文档在顶部标注归档状态（`docs/plans/` 下的早期计划尚未逐篇补齐，属待办）。
4. 性能优化和依赖弃用升级单独实施、单独验证，不与业务功能混改。
