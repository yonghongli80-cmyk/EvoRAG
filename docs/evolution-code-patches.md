# 自进化隔离代码进化运维说明（evolution-code-patches）

第 5 阶段（设计阶段表的最后一段）为**已确认根因层级**的失败生成**代码候选补丁**，在
**隔离 Git worktree** 中应用、跑目标测试与基准回放，结论写入门禁轨迹，最后由管理员审核。
**系统永不自动应用、永不提交、永不停服**：批准只导出 `.patch` 与应用命令，应用由人执行。
设计依据 `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md` 第 2 节
（分层开放与永久禁止项）、第 3 节（后台链路与 worktree）、第 13 节（门禁）；
实施计划 `docs/superpowers/plans/2026-09-27-assisted-self-evolution-code-patches.md`。

## 1. 允许改什么：层 → 路径白名单

| 层 | 允许 glob |
| --- | --- |
| chunking | `rag/chunk*/**`、`rag/chunker*/**` |
| reranking | `rag/rerank*/**`、`rag/rrf*/**`、`rag/fusion/**` |
| parser | `rag/parsers/**`、`rag/document_sources/**`、`rag/extract*/**` |
| tool_workflow | `rag/agent*.py`、`rag/tools/**`、`rag/coordinator*/**`、`rag/spreadsheets/**`、`rag/structured_query*/**` |
| code | `rag/**` 减黑名单 |

## 2. 永久禁止（黑名单，任何层都不可改）

`rag/permissions.py`、`rag/api_security.py`、`rag/org_auth/**`、`rag/auth.py`、`rag/user_db.py`、
**`rag/evolution/**`（禁止系统改自己的门禁与审计）**、`tests/**`（测试基准）、`**/conftest.py`、
`scripts/**`（验收器）、`config.py`（发布门禁）、`frontend/**`、`.github/**`、
`docs/superpowers/**`（设计文档）、`**/.env*`。
另有限额：单补丁 ≤5 文件、≤300 行变动、不得二进制、不得删文件、路径不得越界或含 `..`；
补丁必须真实通过 `git apply --check`（diff 经 stdin，显式 UTF-8）。

## 3. 根因层级的权威是管理员

生成补丁前，聚类上必须有**管理员确认的根因层级**（`confirmed_layer`）：
- 批准时带上：`POST /clusters/{id}/decision` body 加 `"confirmed_layer": "reranking"`；
- 或批准后单独确认：`{"action": "confirm_layer", "confirmed_layer": "...", "note": "<证据>"}`
  （仅 `awaiting_diagnosis_review` / `approved_for_candidate` 接受，写入审计备注）。
生成器规则：**确认层级存在 → 请求层必须等于确认层**（诊断器自己的建议不再阻塞）；
无确认 → 仍要求草案的 `suggested_layer` 与请求层一致。层级枚举与诊断草案同源。

## 4. 沙箱（层 2/3 的代码版）

`run_patch_sandbox(candidate, repo_root=..., tests_timeout=600, run_benchmark=True)`：
1. 主工作树必须干净（`git status --porcelain` 为空），否则拒绝运行；
2. `git worktree add <tmp> --detach <base_commit>`；
3. worktree 内 `git apply`（stdin + UTF-8）——失败即 `rejected`；
4. 目标测试 `pytest <test_paths>`（cwd/PYTHONPATH=worktree、`PYTHONHASHSEED=0`、不继承 `.env`）；
   失败 → `rejected`；超时 → `needs_more_evidence`（不误杀）；
5. 基准回放（子进程 `scripts/benchmark_jev_retrieval.py`）；回归 → `rejected`；
   **回放本身跑不起来（环境问题）不判死**，记 "replay unavailable" 仍可进入人工审核；
6. `finally` 强制 `git worktree remove --force` + `prune` + 删临时目录。
结论经 `sandbox_gate_outcome(report)` 映射为 (状态, gate_detail)，以 `gate="sandbox"` 落 gate_log。

## 5. 管理员审核与导出

- `GET /knowledge-operations/evolution/code-candidates`（列表：layer/状态/文件/沙箱摘要/置信度）
- `GET .../code-candidates/{id}`（+ diff 全文 + 五层 gate_log + 审计）
- `POST .../code-candidates/{id}/decision`：
  `approve_for_apply`（导出补丁 + 记录应用命令 + 推进 `approved_for_canary`，**不应用**）、
  `reject`、`needs_revision`。400/404/409 边界均不写审计、不导出。

导出文件 `artifacts/patches/candidate-<id>.patch` 头部含 apply 命令，例如：

```
# apply: git -C <repo> apply --3way artifacts/patches/candidate-<id>.patch && python -m pytest <test_paths>
```

应用后人工检查、跑全量测试、重启服务——全部由人执行，系统不留任何自动合入路径。

## 6. 端到端留痕（2026-09-28 首跑，真实模型调用）

输入：聚类 3（kb_accessories，已批准）由管理员确认 `confirmed_layer=reranking`，
备注写明证据来源（回放报告记录的分数区分度低/撞名淹没 + 全库核查证实副厂配件表存在却未被采用）。

| 轮次 | 生成 | 沙箱 | 结论 |
| --- | --- | --- | --- |
| obs 27（第一轮） | 91.9s，`rag/reranker.py`，53 行，confidence=medium | 应用成功，**目标测试失败**（补丁调用了不存在的 `_fingerprint()` → NameError） | `rejected`（门禁正确拦下看似合理实则坏的补丁） |
| obs 28（第二轮） | 31.1s，`rag/reranker.py`，30 行，confidence=low | 应用成功，**目标测试通过**；基准回放因 worktree 无知识库数据不可用 | `awaiting_verification` → 管理员 `approve_for_apply` → 导出 `artifacts/patches/candidate-3.patch`（2530B） |

验证：导出后 **`git status` 仍为空**（未应用任何改动）；审计链 `generate → advance(sandbox) → advance(review/export)`；
gate_log `scope(passed) → sandbox(passed) → review(passed)`；被拒候选（id=2）同样保留完整轨迹。

## 7. 已知限制（如实记录）

1. **沙箱基准回放不可用**：worktree 内没有知识库数据（`data/qdrant_data` 不在版本控制内），
   基准回放在沙箱里必然 `exit_1`；当前策略是“回放不可用不判死”，因此层 2 的**基准回归判定对代码补丁实际未生效**，
   只靠目标测试。补齐方式：把只读 KB 快照挂进沙箱，或在主树以隔离 Qdrant 实例回放——列为后续事项。
2. **目标测试由模型提议**：`test_paths` 必须真实存在（层 1 校验），但模型可能只挑弱测试；
   人工审核时应自行补充更严的验证命令。
3. **一次一轮、单候选**：不做多分支探索（DGM 式）与知识库级灰度，属设计第 15 节的后续增强。
4. **审计表沿用阶段 3 的白名单动作**：`confirm_layer` 的审计动作复用 `approve_for_candidate` +
   明确备注（表带 CHECK 约束，扩枚举需重建表）。

## 8. 关闭与回退

1. 关 `RAG_EVOLUTION_CANDIDATE_ENABLED`（代码补丁生成也受该开关与 `evolution_candidate_enabled` 语义管辖）；
   或直接不调用生成器（该能力默认不在 worker 轮次里自动跑代码补丁——第 5 阶段的生成由管理员/集成脚本触发）。
2. 删除候选：`evolution.db` 的 `candidates` 行（或整库）。
3. 已导出的补丁文件在 `artifacts/patches/`，删除即撤销导出；**代码回退**按普通 git 流程（`git revert`）。
