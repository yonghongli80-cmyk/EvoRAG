# 残留项审计与实施计划（2026-10-22）

> 状态：审计完成，待并行实施。
> 基线：`HEAD=f7e2f99`，工作树干净，2357 passed / 0 failed，`ruff` 全绿，服务 8080 healthy。
> 本文只覆盖 2026-10-22 列出的 5 个残留项；单机修复与基准闭环见
> `docs/superpowers/plans/2026-10-21-unified-remediation.md`。

---

## 1. 审计结论

| ID | 项 | 现状（证据） | 决定 |
| --- | --- | --- | --- |
| R-1 | 非企业空检索直接生成（D-2） | legacy 路径 `_prepare_context_standard` 用 `build_bundle=enterprise_request`（`rag/pipeline.py:722`）；空上下文且非企业时 `build_bundle=False`，`_finalize_rag_context` 不建证据包、不判定，直接 `generate()`。Jev 路径已用 `enterprise_request or not context`（`:998`），所以两条链路行为不一致 | **修**：legacy 对齐 Jev，空上下文一律过证据门；非空非企业行为不变 |
| R-2 | 层 2 门禁零净变化（D-1） | `passed = not reasons`；`route_delta_pp == 0` 时即使逐例有进有退也 pass；`per_case` 已记录 `route_regressed/route_improved`，但 `stats` 没有“净零但逐例变动”的显式信号 | **不改语义**；只加非阻塞统计 `net_zero_churn`，让审计一眼看到 |
| R-3 | `structured_query_kind` 无 `failed` 真机样本 | DB 分布 `[(None,198),(not_applicable,6),(unsupported,3)]`；生产者在 `pipeline._attempt_structured_query`，单测覆盖 record/detail，但没有 DB 落库用例 | **补 DB 级测试**：用临时 `evolution.db` + 真实 `FailureRecorder`，确认 `failed` 写进 `failure_observations.details_structured_query_kind`；不改产品代码 |
| R-4 | 候选 #30/#31、alice 账号、canary 观察窗 | `candidates`：30 条；#30 `canary`（percent=0）、#31 `candidate_testing`（code_patch，只有 scope 门禁）。`users`：4 个 `alice_*` + 4 个伴生 `admin_*`；#30 0% 导致 canary 窗无样本 | **运维/产品动作**：写运行手册与决策表，不自动删账号、不自动推进候选 |
| R-5 | 架构升级 | Qdrant 本地独占、SQLite 单写、Redis 只用于 LLM 门、文件在本地、单进程 worker | **不实施**；在 `UPGRADE.md` 基础上补一份“当前阻塞/下一步”审计，Phase 0/1 才动手 |

---

## 2. 实施拆分（文件不重叠，可并行）

### 工作流 A：R-1 空检索证据门

- 文件：`rag/pipeline.py`、`tests/test_pipeline_empty_context.py`（新建）。
- 改动：`_prepare_context_standard` 的 `build_bundle=enterprise_request` → `build_bundle=enterprise_request or not context`。
- 行为：非企业 + 空上下文 → `insufficient_evidence` 固定拒答，不调用 `generate()`；非空非企业路径不变。
- 测试：
  1. 非企业、检索为空：`route=fixed`、`decision.kind=insufficient_evidence`、`generate` 未被调用；
  2. 非企业、有上下文：仍走生成（不误伤）；
  3. 企业模式回归不变。
- 验收：`pytest tests/test_pipeline.py tests/test_pipeline_empty_context.py -q`。

### 工作流 B：R-3 `failed` 落库

- 文件：`tests/test_structured_kind_persistence.py`（新建）。
- 做法：临时 `evolution.db` + 真实 `FailureRecorder`；构造 `ExecutionTrace` 与
  `DiagnosticDetail(structured_query_kind="failed")`，`submit` 后轮询/等待队列落库，
  断言 `failure_observations.details_structured_query_kind == "failed"`。
- 不改产品代码。
- 验收：`pytest tests/test_structured_kind_persistence.py -q`。

### 工作流 C：R-2 门禁可见性

- 文件：`rag/evolution/candidate_gates.py`、`tests/test_evolution_candidate_gates.py`。
- 改动：在 `stats` 增加 `net_zero_churn: bool`（`route_delta_pp == 0` 且同时存在
  `route_regressions` 与 `improvements`）；**不改** `passed`、`verdict`、阈值。
- 测试：零净变化且逐例有进有退 → `passed` 仍 True，`net_zero_churn` True；
  无变动 → False。
- 验收：`pytest tests/test_evolution_candidate_gates.py -q`。

### 工作流 D：R-4/R-5 运维与架构审计

- 文件：`docs/superpowers/plans/2026-10-22-residual-operations.md`（新建）。
- 内容：
  - 候选 #30/#31 的建议动作（#30 保持追溯；#31 人工看 payload 后推进或退回）；
  - 4 个 alice + 4 个 admin 的去留与删除前置条件（conversation/feedback 引用）；
  - canary 观察窗：0% 结构上无样本，放量或明确关闭；
  - 架构阻塞与 Phase 0/1 的最小下一步（Qdrant server、P99 基线已量）。
- 不写代码、不改数据。

---

## 3. 合并与验收

1. 四条工作流完成后，我统一 review diff，跑：
   - `pytest tests/test_pipeline.py tests/test_pipeline_empty_context.py tests/test_structured_kind_persistence.py tests/test_evolution_candidate_gates.py -q`
   - `ruff check .`
   - 全量 `pytest -q`（约 30–40 分钟）
2. 分 3 个提交：R-1、R-3、R-2；运维/架构文档单独提交。
3. 不改 D-1 的通过语义、不改标签、不改阈值；R-4/R-5 只产出文档。
