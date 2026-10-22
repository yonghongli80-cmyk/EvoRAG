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
| R-4 | 候选 #30/#31、alice 账号、canary 观察窗 | `candidates`：30 条；#30 `canary`（percent=0）、#31 `candidate_testing`（code_patch，只有 scope 门禁）。`users`：4 个 `alice_*` + 4 个伴生 `admin_*`；#30 0% 导致 canary 窗无样本 | **账号已合并（见 §4.1）；候选/观察窗仍待人工决策** |
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

---

## 4. 实施结果（2026-10-22）

| 工作流 | 提交 | 结果 | 验证 |
| --- | --- | --- | --- |
| A（R-1） | `1c6ad64` | legacy 空上下文改为 `build_bundle=enterprise_request or not context`；新增 3 个用例，最小修改 1 个锁定旧行为的既有用例 | `test_pipeline.py + test_pipeline_empty_context.py` **33 passed**；ruff 通过 |
| B（R-3） | `b14ff05` | 真实 `FailureRecorder` + 临时 DB 证明 `failed` 会落库；**未改产品代码** | 2 passed；同链路 16 passed |
| C（R-2） | `28ca6e1` | `stats["net_zero_churn"]` 纯可见性；`passed`/`verdict`/阈值零改动 | 28 passed；ruff 通过 |
| D（R-4/R-5） | `2d49bf1` | 运维/架构审计 474 行；候选 #30/#31 决策表、账号删除顺序、canary 窗口、Phase 0/1 | 只读审计，DB 的 size/mtime 未变 |

### 4.1 审计发现（需要人拍板/执行）

1. **账号安全（已处置，2026-10-22 14:21）**：停服备份后删除 `users` id 3/4/6–12；
   保留 id5 改为 `admin`/`admin`、`is_admin=1`、`permission_level=1`、`org_people.active=1`；
   `FK check` 为空，旧账号全部 401，`admin` 登录 200 且可见 4 个 KB。
   附带的真实缺陷：admin 有 `org_people` 行时 KB 列表走组织过滤、legacy KB 无 policy ⇒ 列表 0；
   已修为 admin 不走组织列表（`f75e982`）。备份见
   `artifacts/users-db-backup-20261022-142100/`，执行细节见运维审计 §2.6。
   **注意 `admin/admin` 仅限本地测试，公网必须改密。**
2. **候选 #30**：状态 `canary@0%`，结构上零样本；`gate_log` 缺层 3 shadow。推荐用
   `POST /candidates/30/rollback` 明确关闭观察窗（行与轨迹保留），而不是继续挂“在观察”。
3. **候选 #31**：`code_patch/reranking`，只有 scope 门禁；静态核对显示它改的是 legacy
   reranker 的 `top_k` 截断，而 Jev 主链路判定不经过该路径，预期效果存疑。推荐先
   `run_patch_sandbox.py --candidate-id 31 --dry-run`，证伪则 `candidate_testing → rejected`。
4. **架构**：Phase 0（`/metrics`、`/ready`、可复现压测档案）与 Phase 1（Qdrant server）本周
   可做；PostgreSQL / 对象存储 / 多副本需要新环境，未实施。

### 4.2 全量回归

`pytest -q -p no:cacheprovider`：**2364 passed / 0 failed，34:26**（2026-10-22）；
`ruff check .` 全绿。
