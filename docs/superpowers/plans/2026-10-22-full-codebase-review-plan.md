# 全库代码审查执行计划（2026-10-22）

> 配套 spec：`docs/superpowers/specs/2026-10-22-full-codebase-review.md`
> 基线：`HEAD=67da261`，2366 passed，ruff 全绿，服务 healthy。
> 原则：只读审查；不改产品代码、不写数据库、不停服务。

## 任务

- [x] **T0 基线**：Git 状态、测试、ruff、benchmark、locust、服务健康。
- [x] **T1 审查 spec**：范围、方法、证据标准、严重级别。
- [ ] **T2 域级并行审查**（6 个子代理，只读）：
  - [ ] A 检索/生成/可靠性
  - [ ] B API/认证/权限/组织
  - [ ] C 自演化/沙箱
  - [ ] D 素材/表格/Wiki/仓储/备份
  - [ ] E 前端
  - [ ] F 配置/部署/CI/测试
- [ ] **T3 跨域静态扫描**（主代理）：
  - 静默吞错、`eval/exec/subprocess/shell=True`、SQL 拼接、路径穿越、
    `is_admin`/鉴权入口、锁/线程/任务生命周期、`TODO/FIXME`、配置/依赖/测试隔离。
- [ ] **T4 历史差量**：10-14 / 10-16 / 10-17 审查项逐条标 `fixed/open/stale`。
- [ ] **T5 P0/P1 复核**：主代理读代码行验证子代理结论，剔除误报。
- [ ] **T6 汇总报告**：去重、排序、按“升级前必修 / 随升级做 / 只记录”分组。
- [ ] **T7 整改计划**：TDD 步骤、验收、回滚、文件边界；不实施。

## 并行分工与边界

| 代理 | 域 | 主要路径 | 禁止 |
| --- | --- | --- | --- |
| A | 检索/生成 | `rag/pipeline.py`、`retriever.py`、`reranker.py`、`generator.py`、`guard.py`、`agent.py`、`tools.py`、`reliability/`、`jev/`、`query_rewriter.py` | 改文件、跑全量、停服务 |
| B | API/权限 | `rag/api.py`、`routers/`、`api_security.py`、`auth.py`、`permissions.py`、`org_auth/`、`user_db.py`、`repositories/user_repo.py` | 同上 |
| C | 自演化 | `rag/evolution/` | 同上；不触发候选/沙箱 |
| D | 数据 | `rag/materials/`、`spreadsheets/`、`wiki/`、`repositories/`、`db/`、`document_versions*.py`、`backups/` | 同上；不写库 |
| E | 前端 | `frontend/src/` | 同上；不跑 npm build/test |
| F | 运维/测试 | `config.py`、`start.py`、Docker/Compose/CI、`scripts/`、`tests/`、依赖 | 同上；不装依赖、不构建镜像 |

## 交付物

1. `docs/superpowers/specs/2026-10-22-full-codebase-review.md`（已建）
2. `docs/superpowers/plans/2026-10-22-full-codebase-review-findings.md`（汇总报告）
3. `docs/superpowers/plans/2026-10-22-pre-upgrade-remediation-plan.md`（整改计划）

## 验收

- 六域各至少 5 条有证据的发现；无证据的猜测不入表。
- P0/P1 全部经主代理复核。
- 历史 open 项有现状结论。
- 审查结束 `git status` 只新增文档。
