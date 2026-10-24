# 文档状态索引

> 更新日期：2026-10-24

项目功能、依赖和启动方式以根目录 `README.md`、当前代码、自动化测试和 Git 提交为准。历史设计文档保留决策背景，不等同于尚未完成的待办。

## 当前有效入口

- `handoff-2026-10-24.md`：当前交接，包含复查待办闭环、锁环境、候选竞态修复、宿主Docker修复与实际验收状态。
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
