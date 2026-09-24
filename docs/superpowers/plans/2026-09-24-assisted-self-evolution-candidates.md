# Assisted Self-Evolution (Candidate Generation & Gates) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对**已被管理员确认根因并批准**（`approved_for_candidate`）的失败生成**低风险候选补丁**，并按设计第 13 节的五层门禁逐层放行：层 1 产物边界过滤 → 层 2 基准集门禁 → 层 3 影子评测 → 层 4 灰度（百分比放量）→ 层 5 管理员确认 + 一键回滚。**本阶段唯一在线的产物类型是 `jev_question_set`**（Jev 判定层的问题集文本变体）：它是纯数据、可影子对比、可秒级回滚、已有请求级版本归属（`decision_metadata.jev_question_set_version` 与观测同字段）。其余层级（prompt/检索配置/查询改写/字段映射/切块/重排/解析器/工具工作流/代码）不在本阶段范围——第 5 阶段（隔离代码进化）另立计划。

**Architecture:** 第 3 阶段的 `rag/evolution` 包原样复用；新增 `rag/evolution/candidates.py`（候选模型/载荷校验/生成器）与 `rag/evolution/candidate_store.py`（`candidates` 表 + 状态机推进 + 审计）；层 2 门禁复用 `scripts/benchmark_jev_retrieval.py` 的回放能力（新写以候选问题集为参数的基准 runner）；层 3 影子评测复用 `rag/jev/shadow.py` 的有界执行器模式（新写候选-vs-当前问题集对比器，不阻塞在线路径）；层 4/5 的在线接入点是 `JevClient` 的问题集提供者注入（默认仍是 `rag/jev/questions.py` 静态实现，零行为变化），百分比分流 + 管理端点 + 审计。全部状态推进只走 `EVOLUTION_TRANSITIONS`（`approved_for_candidate → candidate_testing → awaiting_verification → approved_for_canary → canary → stable`，任何阶段可 `rejected` 并回滚）。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLite、pytest、Vue 3 + vitest；候选生成复用第 3 阶段诊断器同款模型通道（`settings.deepseek_*`，Command Code v4.1-flash，隔离客户端 + 90s 超时 + 8000 token）；不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`（第 6 节建议修改层级、第 12–15 节阶段 4/5 与放量、第 10 节门禁与权限边界）；前置：第 2 阶段（观测/聚类/worker/影子评测器）、第 3 阶段（诊断草案/管理员确认，`docs/evolution-diagnosis.md`）。

## Global Constraints

- **候选禁越层**：任何候选必须能追溯到一条**已确认根因**（`approved_for_candidate` 聚类，且其诊断草案经管理员确认）；没有确认根因的聚类不生成候选。确认动作本身在第 3 阶段已完成并有审计。
- **产物边界禁扩**：本阶段候选的 `artifact_type` 只允许 `jev_question_set`（`target` ∈ {preflight, passages}）；载荷只能修改 Jev 问题的 `instructions`/`criteria` **文本**，不得改问题 ID、问题数量、 primitive 类型（`Choice`/`Noul`）——保证映射代码（`_map_preflight`/`_map_passages`）与存储 schema 不变。生成器后校验硬性拒绝越界载荷。
- **门禁不可跳过、不可弱化**：五层按序执行，任一层失败即 `rejected`（含理由）；层 2 阈值与 A/B 验收同源（路线准确率不劣于基线 -0pp、Top-5 降幅 ≤2pp——**不得**为了让候选通过而放宽）；层 3 未达样本量不出结论；层 4 未管理员批准不上线；任何层发现回归立即回滚并 `rejected`。
- **在线路径零风险默认**：百分比分流默认 0%（等效当前行为）；候选集未经层 4 批准时在线请求 100% 走当前问题集；回滚 = 置 0%（秒级生效）+ 审计行。候选集服务期间的请求必须在 `decision_metadata` 标注所用版本（现有 `jev_question_set_version` 即此用途，不得丢弃）。
- **无秘密外溢与越权**：候选载荷、提示词、报告不含密钥；影子对比只用权限过滤后的候选块；观测/审计边界不变（`evolution.db` 管理员可见，API 需 `require_admin`）。
- **可回滚性硬件化**：每个候选落库时必须带 `rollback_ref`（当前问题集版本 + 载荷快照哈希），回滚操作只依赖该记录，不依赖任何外部状态。
- **人在环内**：层 5 之前无任何自动放量；`canary → stable` 必须管理员显式确认（设计第 8 节）。

## Review Focus

- 生成器载荷边界：改文本可以，动结构（ID/数量/类型）必拒；覆盖于 Task 2 测试。
- 层 2 门禁用同一基准集与既有阈值跑候选 vs 当前，输出逐用例对比与总判定；不得为通过而调阈值；覆盖于 Task 3 测试（含“候选明显更差 → rejected”的反向用例）。
- 层 3 影子对比不阻塞在线路径、队列满丢弃、只对比不改响应；覆盖于 Task 4 测试。
- 层 4 分流默认 0%；批准后按百分比分流且请求带版本标注；回滚置 0% 且写审计；覆盖于 Task 5 测试（含在线构造 pipeline 的分流单测）。
- 状态机全程只走 `EVOLUTION_TRANSITIONS`；非法推进抛错不写库；覆盖于 Task 1 测试。

---

## File Map

- `rag/evolution/candidates.py`（新建）：`JevQuestionSetCandidate`（frozen Pydantic：artifact_type/target/payload/rationale/expected_effect/rollback_ref/confidence）、载荷边界校验（结构不可动、文本可改、版本号递增）、`CandidateGenerator`（一次模型调用 + 版本化提示词 `CANDIDATE_PROMPT_VERSION` + 后校验，诊断器同款隔离客户端）。
- `rag/evolution/candidate_store.py`（新建）：`candidates` 表（id/observation_id/cluster_id/artifact_type/target/payload/status/pool/gate_log/layer 结果/created_at）、状态推进（复用 `EVOLUTION_TRANSITIONS` + `decide` 风格的单事务方法）、审计写入（复用 `evolution_audit`，action ∈ 生成/晋级/驳回/灰度/回滚白名单）。
- `rag/evolution/candidate_gates.py`（新建）：层 2 基准门禁（`evaluate_on_benchmark(candidate, baseline_runner, cases)`：逐用例路由/Top-5 对比 + 阈值判定）、层 3 影子对比器（`CandidateShadowEvaluator`：每次请求用当前集与候选集各判一次，记录路由/采用证据一致率，有界 executor）。
- `rag/jev/questions.py`（修改）：问题集构建函数保持为**默认实现**；新增 `question_set_registry()`（返回 target → {version: builder}），候选版本以数据形式注册/反注册（不改默认集合）。
- `rag/jev/client.py`（修改）：`JevClient` 增加可选 `question_set_provider` 注入（默认 None = 静态模块，行为零变化）；preflight/judge_passages 经提供者取 builder；响应映射把所用版本写回（passages 侧同步）。
- `scripts/benchmark_candidate_questions.py`（新建）：层 2 的基准 runner（复用 `scripts/benchmark_jev_retrieval.py` 的 case 加载与统计），以“候选问题集 vs 当前问题集”为参数输出对比报告。
- `rag/evolution/api.py`（修改）：`GET /candidates`、`GET /candidates/{id}`（含 gate_log）、`POST /candidates/{id}/canary`（设百分比）、`POST /candidates/{id}/rollback`（置 0% + 审计）——全部 `require_admin` + 审计。
- `frontend/src/api/evolution.ts`、`frontend/src/views/EvolutionCenterView.vue`（修改）：候选页签（列表 + 五层门禁轨迹 + 灰度/回滚按钮）。
- `rag/evolution/worker.py`（修改）：轮次在聚类/诊断之后增加候选步骤（只处理 `approved_for_candidate` 且无候选的聚类/观测；生成 → 层 1 → 层 2 → 层 3 排队；预算沿用诊断的模型调用预算常量或新增 `evolution_candidate_model_calls`）。
- `config.py`（修改）：`evolution_candidate_enabled`（默认 false——生成默认关闭，与观测总开关独立）、`evolution_candidate_model_calls`（默认 2/轮）、`evolution_candidate_canary_percent`（默认 0）、`evolution_candidate_shadow_sample_rate`（默认 0.05）。
- `tests/test_evolution_candidates.py`、`tests/test_evolution_candidate_store.py`、`tests/test_evolution_candidate_gates.py`、`tests/test_candidate_shadow.py`、`tests/test_jev_question_registry.py`、`tests/test_candidate_api.py`。
- `docs/evolution-candidates.md`（新建）：运维说明（五层门禁、灰度与回滚、审计查看、关闭开关）。

### Task 1: Candidate contracts and the store with the state machine

**Files:**
- Create: `rag/evolution/candidates.py`（仅模型与载荷校验）
- Create: `rag/evolution/candidate_store.py`
- Create: `tests/test_evolution_candidates.py`、`tests/test_evolution_candidate_store.py`

**Interfaces:**
- Consumes: `EVOLUTION_TRANSITIONS`、第 3 阶段 `decide_cluster`/`add_audit` 模式、`FailureObservation`/cluster 行。
- Produces: `JevQuestionSetCandidate`（`artifact_type: Literal["jev_question_set"]`、`target: Literal["preflight","passages"]`、`payload: dict`（问题集文本覆写：`{question_id: {"instructions": str, "criteria": {...}}}`）、`rationale`、`expected_effect`、`rollback_ref: dict`、`confidence: Literal["low","medium","high"]`）、`validate_payload(target, payload, base_builder) -> None`（结构断言：ID 集合/数量/类型不变，只文本变）、store 方法 `add_candidate(...) -> int`、`list_candidates(...)`、`get_candidate(id)`、`advance_candidate(id, new_status, *, note, actor) -> dict`（走转移表 + 审计）、`candidates_for_observation(observation_id)`。

- [ ] **Step 1: Write failing tests**（载荷边界：文本改动能过、增删问题 ID/改类型被拒；store：写入/查询/状态推进/审计/非法推进抛错不写库）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（DDL 与索引同第 3 阶段风格；`advance_candidate` 单事务校验 + 先审计后推进）
- [ ] **Step 4: Run focused tests**（两个新文件 + `test_evolution_store_drafts.py` 零回归）
- [ ] **Step 5: Commit**：`feat: model versioned Jev question-set candidates`.

### Task 2: The constrained candidate generator

**Files:**
- Modify: `rag/evolution/candidates.py`
- Modify: `tests/test_evolution_candidates.py`

**Interfaces:**
- Consumes: Task 1 契约、已确认诊断（`approved_for_candidate` 聚类 + 其成员草案）、诊断器同款模型通道与失败分类。
- Produces: `CandidateGenerator(model_call=None, *, prompt_version=CANDIDATE_PROMPT_VERSION, timeout_seconds=90.0, max_tokens=8000)`、`.generate(cluster, drafts) -> JevQuestionSetCandidate | raise CandidateUnavailable(reason)`；后校验：① `suggested_layer` 必须映射到 `jev_question_set`（prompt/query_rewrite/intent 校准类）；② 载荷只改允许文本；③ `rollback_ref` 必须指向当前版本；④ 变更幅度约束（单个问题文本变动 ≤ 40% 字符、不得整集重写）。

- [ ] **Step 1: Write failing tests**（合法载荷生成；越界载荷（改 ID/加问题/大改）拒；模型违约 → CandidateUnavailable；提示词含“只改文本/保持 ID 与类型/给出 rollback_ref”合约与版本戳）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（提示词版本化；一次调用；复用诊断器的 `_classify`/隔离客户端模式——提炼到共享小 helper 避免复制）
- [ ] **Step 4: Run focused tests**（+ `test_evolution_diagnoser.py` 零回归）
- [ ] **Step 5: Commit**：`feat: generate bounded question-set candidates from confirmed diagnoses`.

### Task 3: Layer 2 — benchmark gate on the fixed corpus

**Files:**
- Create: `rag/evolution/candidate_gates.py`（层 2 部分）
- Create: `scripts/benchmark_candidate_questions.py`
- Modify: `tests/test_evolution_candidate_gates.py`（新建）

**Interfaces:**
- Consumes: `benchmarks/jev_retrieval_cases.json`（20 用例）+ 第 2 阶段真实用例标注（`benchmarks/real-cases-v1.json` 若存在则附加）、`JevDecisionService`（可用候选问题集构造第二个服务实例）。
- Produces: `evaluate_on_benchmark(candidate, *, baseline_route_fn, candidate_route_fn, cases) -> GateResult(passed: bool, stats: dict, per_case: list)`；判定规则：路线准确率相对基线不降（≤0pp 降幅）且 Top-5 命中降幅 ≤2pp；输出落 `gate_log` JSON。

- [ ] **Step 1: Write failing tests**（候选不差 → passed；候选明显更差 → failed 且给出逐用例差异；阈值即验收阈值、测试钉住不放宽）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（判定纯函数化，模型调用经注入 fn——测试不触网）
- [ ] **Step 4: Run focused tests**；用真实候选（Task 6 前可用手工构造的“等价候选”）跑一次 runner 冒烟（本地，输出留 artifacts）
- [ ] **Step 5: Commit**：`feat: gate candidates on the fixed benchmark corpus`.

### Task 4: Layer 3 — bounded shadow comparison against the live question set

**Files:**
- Modify: `rag/evolution/candidate_gates.py`（层 3 部分）
- Create: `tests/test_candidate_shadow.py`（或并入 gates 测试文件）

**Interfaces:**
- Consumes: `rag/jev/shadow.py` 的有界执行器模式、候选问题集、在线请求的 state 与候选块。
- Produces: `CandidateShadowEvaluator(current_service, candidate_service, *, sample_rate, on_comparison)`：按 `sample_rate` 采样（默认 5%），每次请求用两套问题集各判一次，记录路由一致率/段落选择一致率/可用性差异；`comparisons(limit)` 汇总；队列满丢弃、绝不阻塞、不改变当前响应。

- [ ] **Step 1: Write failing tests**（采样率生效；两服务各被调一次且比较落库；满队列丢弃不阻塞；候选服务失败不影响当前路径）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run focused tests**（+ `tests/test_jev_shadow.py` 零回归）
- [ ] **Step 5: Commit**：`feat: shadow-compare candidate question sets without touching the live path`.

### Task 5: Layers 4–5 — registry, percentage rollout, admin control

**Files:**
- Modify: `rag/jev/questions.py`（registry）
- Modify: `rag/jev/client.py`（provider 注入）
- Modify: `rag/evolution/api.py`（灰度/回滚端点）
- Modify: `frontend/src/api/evolution.ts`、`frontend/src/views/EvolutionCenterView.vue`
- Modify: `tests/test_jev_question_registry.py`（新建）、`tests/test_candidate_api.py`（新建）

**Interfaces:**
- Produces: `rag/jev/questions.py: question_set_registry()` + `register_candidate_question_set(target, version, builder)` / `unregister(...)`（仅数据注册，默认集合不变）；`JevClient(question_set_provider=None)`（None=默认静态；否则按 provider 取 builder， provider 由灰度控制器提供——按 `settings.evolution_candidate_canary_percent` 与候选版本决定；百分比 0 时直接返回当前 builder）；`POST /candidates/{id}/canary`（body `percent: 0..100`，写注册 + 审计 + 状态推进 awaiting_verification→approved_for_canary→canary）、`POST /candidates/{id}/rollback`（反注册 + 百分比 0 + 审计 + 状态 canary→rejected 或回退 awaiting_verification）、`GET /candidates`、`GET /candidates/{id}`（gate_log 五层轨迹）。

- [ ] **Step 1: Write failing tests**（provider 默认零变化；百分比分流确定性可测；canary 端点写注册+审计+推进；rollback 反注册+置 0+审计；非管理员 403；非法百分比 400）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（分流用每请求确定性抽样：请求序号哈希 % 100 < percent，便于日志归因；候选服务请求的 `question_set_version` 必须带候选版本）
- [ ] **Step 4: Run focused tests**（后端 api/jev 测试 + 前端 vitest + `vue-tsc`）
- [ ] **Step 5: Commit**：`feat: roll out candidate question sets by percentage with instant rollback`.

### Task 6: End-to-end verification and operations doc

**Files:**
- Create: `docs/evolution-candidates.md`
- Produce locally ignored: `artifacts/evolution-candidates-e2e.json`

**Interfaces:**
- Consumes: Task 1–5 全部、第 3 阶段已批准簇（本机 `evolution.db` 内 `approved_for_candidate` 数据，若为空则用回放数据构造一条已确认根因的批准簇作为演示输入）。
- Produces: 一条候选“生成 → 层1 → 层2 → 层3 冒烟 → 灰度 5% → 回滚”的完整留痕报告 + 运维文档；不产出额外在线接口变更。

- [ ] **Step 1: 构造/确认输入**：确保至少一个 `approved_for_candidate` 且带已确认诊断草案的聚类（真实数据优先；不足则明确标注演示输入）。
- [ ] **Step 2: 跑五层**：生成候选 → 层 2 基准门禁（真实模型）→ 层 3 影子冒烟（对 1102 服务发若干请求或进程内回放）→ canary 5% → 验证在线版本标注 → rollback → 验证回零。
- [ ] **Step 3: 记录与复核**：报告含每层输入/输出/耗时/模型成本；五层顺序与人工确认点逐一核对设计第 13 节。
- [ ] **Step 4: 全量验证**：`python -m pytest tests/test_evolution_candidates.py tests/test_evolution_candidate_store.py tests/test_evolution_candidate_gates.py tests/test_jev_question_registry.py tests/test_candidate_api.py -q` 全 PASS；`python -m pytest -q` 全量零回归；`cd frontend && npm test -- --run` 全 PASS；`npx vue-tsc --noEmit -p tsconfig.app.json` 干净；`ruff check rag/ config.py scripts/ tests/` 通过。
- [ ] **Step 5: 运维文档**：`docs/evolution-candidates.md`（五层门禁说明与阈值、灰度/回滚操作、审计查看、`evolution_candidate_enabled` 与总开关的组合语义、回退步骤）。
- [ ] **Step 6: Commit**：`docs: document candidate generation and gate operations`；e2e 报告留本地 `artifacts/`。
- [ ] **Step 7: 停合并，交管理员**：提交范围、测试计数、e2e 各层结果与成本、已知限制；合并与启用由管理员确认。

## 后续阶段（不变）

第 5 阶段（隔离代码进化：沙箱验证、补丁生成与人工核验）以本阶段 `canary/stable` 的候选基建为模板，另立计划。本阶段明确**不**生成任何代码级候选。

## 需管理员确认的开放问题（开工前）

1. **候选产物范围**：本阶段仅 `jev_question_set`（preflight/passages 文本变体）；层 2 阈值直接用 A/B 验收同款（路线准确率不降、Top-5 降幅 ≤2pp）。是否同意？
2. **生成默认关闭**：`evolution_candidate_enabled` 默认 false（候选生成要显式开）；同意？
3. **影子采样率 5%、每候选放量上限 100% 由管理员逐步给**（5% → 30% → 100%，每级需观察期）；同意？
4. **回滚语义**：任何时刻 rollback = 秒级置 0% + 审计；canary 期间观测到的失败聚类若根因指向本候选，是否允许维护者直接回滚后报告（不等管理员）？建议允许（设计第 13 节“随时一键回滚”）。
5. **基准集**：层 2 用 20 用例 A/B 集 + 第 2 阶段真实集（若已确认标签）；是否附加更多域？
