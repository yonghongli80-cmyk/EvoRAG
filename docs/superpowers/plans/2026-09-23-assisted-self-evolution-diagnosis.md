# Assisted Self-Evolution (Diagnosis Phase) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为进入候选池的失败聚类生成**结构化诊断草案**：按设计第 6 节输出失败类别、发生阶段、第一处实质偏离、直接证据、根因、影响范围、建议修改层级、复现方法和验收标准；参考证据严格按“确定性证据 > 管理员确认答案/成功轨迹 > 文档引用与多模型一致”的优先级构造。管理员在进化中心审核草案，确认根因后才允许进入第 4 阶段候选生成；根因“无法确定”或不可复现的一律留在观察队列。本阶段是自进化系统**首次引入模型调用**（第 2 阶段零调用），全部在后台有界预算内进行。

**Architecture:** 第 2 阶段的 `rag/evolution` 包（store/clusterer/worker/api）原样复用；新增三块——`evidence.py`（确定性优先的证据装配：管理员确认标签、同库成功轨迹、按哈希 ACL 范围重检索、确定性复现检查）、`diagnoser.py`（每聚类一次结构化 LLM 调用 + Pydantic 校验 + 提示词版本化）、以及**进化中心首批写端点**（诊断草案查看 + 管理员三动作：approve_for_candidate / reject / needs_more_evidence，全部 require_admin 并落审计表）。worker 每轮对“无草稿的 awaiting_diagnosis_review 聚类”限流生成草案；模型调用预算从设置读取（替换第 2 阶段硬编码的 0）。诊断草案只落 `evolution.db`（管理员可见），不进在线轨迹、不进日志原文。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLite（`diagnosis_drafts` / `evolution_audit` 两张新表）、pytest、Vue 3 + vitest；模型调用复用既有 `settings.deepseek_*`（当前为 Command Code `deepseek/deepseek-v4.1-flash`）与 `rag/generator.py` 的结构化 JSON 提取；不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`（第 5 节失败准入、第 6 节诊断与参考依据、第 7 节组件与状态机、第 8 节人工核验、第 9 节门禁与禁止条件、第 11 节指标、第 12 节阶段 3）；前置：`docs/superpowers/plans/2026-09-22-assisted-self-evolution.md`（第 2 阶段，已实施）；实证输入：`docs/real-cases-v1-replay-report.md`（2 例“无证据作答”、3 例“疑似假拒答”等已知标签案例）。

## Global Constraints

- **候选禁入自动路径**：诊断草案永远只是草案；只有管理员确认根因（approve_for_candidate）才推进状态机。任何代码不得把“模型认为的根因”当作已确认结论。
- **无法复现禁入**：证据装配必须先做确定性复现检查（按原问题重检索，核对同一批块哈希是否仍缺失/仍无证据）；不可复现的，草案必须标注 `reproducible=false` 并建议 `needs_more_evidence`，停留在观察队列（设计第 5、9 节）。
- **证据优先级不可颠倒**：确定性证据（管理员确认标签、数据库/表格精确结果、程序测试）优先于模型判断；没有确定性或管理员确认证据时，草案只能给“假设级”根因并显式标注置信度。
- **权限与审计边界禁改**：不得修改 `rag/permissions.py`、`rag/api_security.py`、`rag/org_auth/*`；证据重检索必须走管线同一套 ACL（复用 `RAGPipeline._retrieve_visible_candidates` 或等价只读路径），不可越权取文本。
- **无秘密外溢**：模型提示词与草案只含权限过滤后的问题文本与候选块文本；严禁把密钥、未授权文档内容写进提示词、草案、日志或审计。`evolution.db` 本身按第 2 阶段约定纳入备份。
- **在线路径零影响**：诊断只在后台 worker 进行；任何情况下不得阻塞或改写在线问答。
- **预算硬上限**：每轮模型调用次数、单次超时、每聚类token上限均由设置限定（第 2 阶段 `max_model_calls=0` 的硬编码随本阶段替换为设置驱动）；超预算即停当轮，不重试风暴。
- **提示词版本化**：诊断提示词有版本号并随草案落库；改提示词必须升版本，历史草案可回溯（与 Jev 问题集版本化同一纪律）。

## Review Focus

- 无管理员确认证据的聚类，草案根因必须是“假设级”且 `reproducible=false` 时建议 needs_more_evidence；覆盖于 Task 3 草案生成测试。
- 写端点三动作全部 require_admin，非管理员 403，且每次都写审计行（actor/action/cluster/note/time）；覆盖于 Task 5 测试。
- 模型超时/限流/无效 JSON 时该聚类不产生草案、worker 当轮继续，绝不影响在线路径与其它聚类；覆盖于 Task 4 韧性测试。
- 复现检查是确定性的（不调模型）：原问题重检索的块哈希集合与失败时一致才判可复现；覆盖于 Task 2 测试。
- 已知标签开发集上，草案根因类别与管理员标签一致率达标（阈值见 Task 6，未达标不得靠改标签蒙混）；覆盖于 Task 6 验收。

---

## File Map

- `rag/evolution/evidence.py`（新建）：`EvidencePack`（确定性证据/成功轨迹/重检索候选/复现结论）、`assemble_evidence(cluster, store, ...)`；引用证据只含权限过滤后内容。
- `rag/evolution/diagnoser.py`（新建）：`DiagnosisDraft`（固定九字段 + 置信度 + reproducible + evidence_refs）、`Diagnoser(settings-driven model call)`、提示词版本 `DIAGNOSIS_PROMPT_VERSION`、结构化输出校验（复用 `rag/generator.py` 的 JSON 提取与 `StructuredOutputError`）。
- `rag/evolution/store.py`（修改）：新增 `diagnosis_drafts` 与 `evolution_audit` 两张表；`add_draft` / `list_drafts` / `get_draft` / `add_audit` / `list_audit` / `clusters_awaiting_diagnosis_without_draft(limit)`；管理员动作复用 Task 2 的 `promote_cluster`（已带 `EVOLUTION_TRANSITIONS` 校验）。
- `rag/evolution/worker.py`（修改）：诊断步骤接入轮次；预算从 `settings.evolution_*` 读取（替换 319 行附近的 `max_model_calls=0` 硬编码）。
- `config.py`（修改）：`evolution_diagnosis_model_calls`（每轮，默认 5）、`evolution_diagnosis_timeout_seconds`（默认 30）、`evolution_diagnosis_max_tokens`（默认 2000）。
- `rag/evolution/api.py`（修改）：`GET /clusters/{id}/diagnosis`（草案+证据摘要）、`POST /clusters/{id}/decision`（approve_for_candidate | reject | needs_more_evidence + note，require_admin，写审计）。
- `frontend/src/api/evolution.ts`、`frontend/src/views/EvolutionCenterView.vue`（修改）：诊断页签（草案九字段渲染、证据列表、复现结论、三个决策按钮 + 备注输入）。
- `rag/tracker.py`（只读复用）：成功轨迹查询（`get_recent` 按 kb/route 过滤），不做修改。
- `tests/test_evolution_evidence.py`、`tests/test_evolution_diagnoser.py`、`tests/test_evolution_worker.py`（追加）、`tests/test_evolution_api.py`（追加写端点用例）。
- `docs/evolution-diagnosis.md`（新建）：运维说明（预算/版本/审计/回退：删表 + 关 flag）。
- 已知标签开发集：`benchmarks/real-cases-v1-draft.json` 的管理员确认标签（Task 6 前由管理员确认 real-023/real-016/003/006/020 等条目的期望结论；无法确认的条目不进入开发集）。

### Task 1: Store diagnosis drafts and the admin audit trail

**Files:**
- Modify: `rag/evolution/store.py`
- Modify: `tests/test_evolution_clusterer.py`（或新建 `tests/test_evolution_store_drafts.py`；归入 Task 1 指定文件）

**Interfaces:**
- Consumes: `EVOLUTION_TRANSITIONS`（`rag/evolution/models.py`，不改）、现有 `failure_clusters` 表。
- Produces: `store.add_draft(cluster_id, draft: DiagnosisDraft) -> int`、`list_drafts(cluster_id=None) -> list[dict]`、`get_draft(draft_id) -> dict | None`、`add_audit(actor_id, action, cluster_id, note) -> int`、`list_audit(cluster_id=None) -> list[dict]`、`clusters_awaiting_diagnosis_without_draft(limit) -> list[dict]`；DDL：`diagnosis_drafts(id, cluster_id, payload JSON, prompt_version, model, reproducible, created_at)`、`evolution_audit(id, actor_id, action, cluster_id, note, created_at)`。

- [ ] **Step 1: Write failing store tests**（先写失败测试）
  - `add_draft` 后 `get_draft` 返回等价 payload 且 `prompt_version` 原样保存；
  - `clusters_awaiting_diagnosis_without_draft` 只返回 `awaiting_diagnosis_review` 且无草稿的聚类，且受 limit 约束；
  - `add_audit` 后 `list_audit(cluster_id)` 按时间正序返回 actor/action/note；
  - 非法 action 直接拒之（枚举白名单：approve_for_candidate/reject/needs_more_evidence）。
- [ ] **Step 2: Run and confirm failure**：`python -m pytest tests/test_evolution_store_drafts.py -q` → FAIL（表与方法不存在）。
- [ ] **Step 3: Implement tables and methods**（SQLite 标准 DDL + 现有 `_lock` 纪律；草稿 payload 用 JSON 列）
- [ ] **Step 4: Run focused tests**：上述文件全 PASS；`python -m pytest tests/test_evolution_recorder.py tests/test_evolution_clusterer.py -q` 零回归。
- [ ] **Step 5: Commit**：`git add rag/evolution/store.py tests/test_evolution_store_drafts.py` → `feat: persist diagnosis drafts and admin audit trail`。

### Task 2: Assemble deterministic-first evidence with a reproduction check

**Files:**
- Create: `rag/evolution/evidence.py`
- Create: `tests/test_evolution_evidence.py`
- Modify: `rag/evolution/models.py`（仅追加 `EvidencePack` / `ReproductionResult` 数据类，不动既有符号）

**Interfaces:**
- Consumes: 聚类成员观测（`store.list_observations(cluster_id)`，含 question/kb_id/阶段/块哈希）、管理员确认标签（`benchmarks/real-cases-v1.json` 的 `expected_route`/`expected_source_names`/`expected_answer`，缺省视为无标签）、成功轨迹（`ExecutionTracker.get_recent` 只读）、只读重检索（`RAGPipeline._retrieve_visible_candidates` 静态调用或等价只读封装）。
- Produces: `assemble_evidence(cluster, *, store, tracker, labels, kb_id) -> EvidencePack`；`EvidencePack` 含 `admin_labels: tuple[...]`、`success_traces: tuple[...]`（仅同 kb 的成功轨迹摘要：问题+采用块哈希）、`recall_hashes: tuple[str, ...]`（重检索命中的块哈希）、`reproduction: ReproductionResult(reproducible: bool, missing_hashes: tuple[str, ...], note: str)`。

- [ ] **Step 1: Write failing evidence tests**
  - 有管理员标签时标签进 pack 且优先级最高；
  - 复现检查确定性：重检索哈希集合与失败时一致 → `reproducible=True`；块消失/证据仍缺 → `False` 且记录缺失哈希（用假 retriever 注入）；
  - 成功轨迹只取同 kb 且非拒答轨迹；无 tracker 时为空而非报错；
  - 不可复现时 pack 仍生成（草案必须能标注 `reproducible=false`）。
- [ ] **Step 2: Run and confirm failure**：`python -m pytest tests/test_evolution_evidence.py -q` → FAIL。
- [ ] **Step 3: Implement**（复现检查不调模型；重检索只读、复用 ACL 路径；引用内容仅权限过滤后文本）
- [ ] **Step 4: Run focused tests + 零回归**：`tests/test_evolution_evidence.py` PASS；`tests/test_evolution_recorder.py tests/test_evolution_clusterer.py tests/test_versioned_retrieval.py` 零回归。
- [ ] **Step 5: Commit**：`feat: assemble deterministic-first diagnosis evidence`。

### Task 3: Generate structured diagnosis drafts with a versioned prompt

**Files:**
- Create: `rag/evolution/diagnoser.py`
- Create: `tests/test_evolution_diagnoser.py`

**Interfaces:**
- Consumes: `EvidencePack`、`settings.deepseek_*`（当前 Command Code v4.1-flash）、`rag/generator.py` 的结构化 JSON 提取（`_extract_json_object` / `StructuredOutputError`）。
- Produces: `DiagnosisDraft`（ frozen Pydantic：`failure_category`、`stage`、`first_divergence`、`direct_evidence`、`root_cause`、`impact_scope`、`suggested_layer`（提示词/检索配置/查询改写/字段映射/切块/重排/解析器/工具工作流/代码/无法确定）、`reproduction_steps`、`acceptance_criteria`、`confidence`、`reproducible`、`evidence_refs`）、`DIAGNOSIS_PROMPT_VERSION`、`Diagnoser.draft(cluster, pack) -> DiagnosisDraft`（内部一次模型调用 + 校验；失败抛 `DiagnosisUnavailable`）。

- [ ] **Step 1: Write failing draft tests**（fake 模型客户端）
  - 合法 JSON → 九字段草案且 `prompt_version`/`model` 记录；
  - 无管理员确认证据时 `confidence` 必须为 `low` 且 root_cause 带“假设”前缀（提示词合约 + 后校验）；
  - `reproducible=False` → 草案必须含 `needs_more_evidence` 建议；
  - 模型返回非 JSON/缺字段/超时 → `DiagnosisUnavailable`，不产生半成品草案；
  - 提示词与草案文本中不得出现密钥（哨兵断言）。
- [ ] **Step 2: Run and confirm failure**：`python -m pytest tests/test_evolution_diagnoser.py -q` → FAIL。
- [ ] **Step 3: Implement**（提示词版本化；一次调用；Pydantic 校验；token/耗时记录进草案元数据；失败分类：timeout/rate_limited/invalid/budget）
- [ ] **Step 4: Run focused tests**：PASS；`tests/test_generator.py` 零回归。
- [ ] **Step 5: Commit**：`feat: draft structured failure diagnoses`。

### Task 4: Run diagnosis in the worker under a model-call budget

**Files:**
- Modify: `rag/evolution/worker.py`
- Modify: `config.py`
- Modify: `tests/test_evolution_worker.py`

**Interfaces:**
- Consumes: `clusters_awaiting_diagnosis_without_draft`、`assemble_evidence`、`Diagnoser`、`settings.evolution_diagnosis_*`。
- Produces: `build_evolution_worker` 的 `max_model_calls=settings.evolution_diagnosis_model_calls`（替换第 2 阶段硬编码 0）；轮次返回 mapping 增加 `diagnosed` 计数；单聚类失败不影响当轮其它聚类与后续轮次。

- [ ] **Step 1: Write failing worker tests**
  - 有待诊断聚类且预算充足 → 生成草稿并计数；
  - 预算耗尽（`max_model_calls=0`）→ 不调用模型、`diagnosed=0`、当轮正常结束；
  - 单个聚类诊断失败（抛 `DiagnosisUnavailable`）→ 当轮继续、不写草稿、worker 线程存活；
  - 已存在草稿的聚类不重复生成。
- [ ] **Step 2: Run and confirm failure**：`python -m pytest tests/test_evolution_worker.py -q` → FAIL。
- [ ] **Step 3: Implement**（预算透传；诊断步骤置于聚类之后；失败隔离）
- [ ] **Step 4: Run focused测试**：`tests/test_evolution_worker.py tests/test_evolution_diagnoser.py tests/test_evolution_evidence.py` PASS；`tests/test_backup_schedule.py` 零回归。
- [ ] **Step 5: Commit**：`feat: diagnose candidate clusters within a model-call budget`。

### Task 5: Admin review endpoints and the Evolution Center diagnosis tab

**Files:**
- Modify: `rag/evolution/api.py`
- Modify: `frontend/src/api/evolution.ts`
- Modify: `frontend/src/views/EvolutionCenterView.vue`
- Modify: `tests/test_evolution_api.py`
- Create: `frontend/src/views/__tests__/EvolutionCenterDiagnosis.test.ts`

**Interfaces:**
- Consumes: `store.list_drafts/get_draft`、`promote_cluster`（带转移校验）、`add_audit`。
- Produces: `GET /knowledge-operations/evolution/clusters/{cluster_id}/diagnosis`（最新草案 + 证据摘要：标签/复现结论/引用哈希，**不返回密钥**）、`POST /knowledge-operations/evolution/clusters/{cluster_id}/decision`（body：`action` ∈ approve_for_candidate/reject/needs_more_evidence、`note`；require_admin；成功返回更新后的聚类；非法转移 409；每次写审计）。

- [ ] **Step 1: Write failing API/前端测试**
  - 非管理员 GET/POST 均 403、未认证 401；
  - POST approve_for_candidate 后聚类状态变为 `approved_for_candidate` 且审计行含 actor/note；
  - POST reject / needs_more_evidence 同样落审计；重复 approve（已不在 awaiting 状态）→ 409；
  - GET diagnosis 对无草稿聚类返回空草案而非 500；
  - 前端：草案九字段渲染、复现结论徽标、三按钮 + 备注提交（mock API）。
- [ ] **Step 2: Run and确认 failure**：后端 `python -m pytest tests/test_evolution_api.py -q` → FAIL；前端 `npx vitest run src/views/__tests__/EvolutionCenterDiagnosis.test.ts` → FAIL。
- [ ] **Step 3: Implement**（端点参照第 2 阶段只读端点风格；写操作全部审计；状态推进只走 `promote_cluster`）
- [ ] **Step 4: Run focused测试 + 类型检查**：后端 PASS；前端 vitest PASS；`npx vue-tsc --noEmit -p tsconfig.app.json` 干净。
- [ ] **Step 5: Commit**：`feat: review and confirm evolution diagnoses`。

### Task 6: Verify against the known-label development set and document operations

**Files:**
- Create: `docs/evolution-diagnosis.md`
- Modify: `benchmarks/real-cases-v1.json`（管理员确认标签后的正式版，由管理员提供；本计划不改写标签）
- Produce locally ignored: `artifacts/evolution-diagnosis-devset.json`

**Interfaces:**
- Consumes: 第 2 阶段回放已入 `evolution.db` 的真实观测/聚类、Task 1–5 全部能力。
- Produces: 已知标签开发集上的草案-标签对分报告（一致率、偏差案例、耗时与 token 成本）+ 运维文档；不产出任何在线接口变更。

- [ ] **Step 1: 固化开发集**：与管理员确认 real-023/real-016（无证据作答）与 real-003/006/020（疑似假拒答）的期望根因类别；A 类拒答（real-012/014/017/018/025/026）标注“内容缺口”期望。无法确认的条目不进开发集。
- [ ] **Step 2: 跑端到端诊断**：对 `evolution.db` 中候选池聚类逐一生成了草案（后台 worker 或一次性脚本），逐条对照期望标签。
- [ ] **Step 3: 对分与修订**：一致率低于 80% 时逐case 修订提示词或证据装配（**不得改期望标签**），复跑同一开发集。
- [ ] **Step 4: 全量验证**：`python -m pytest tests/test_evolution_evidence.py tests/test_evolution_diagnoser.py tests/test_evolution_store_drafts.py tests/test_evolution_worker.py tests/test_evolution_api.py -q` 全 PASS；`python -m ruff check rag/evolution config.py tests/test_evolution_*.py` 通过；`cd frontend && npm test -- --run` 全 PASS。
- [ ] **Step 5: 写运维文档**：`docs/evolution-diagnosis.md`（预算与限流、提示词版本升级纪律、审计查看、回退=停 worker+关 `RAG_EVOLUTION_ENABLED`+删 `evolution.db`；不含任何密钥）。
- [ ] **Step 6: Commit**：`docs: document evolution diagnosis operations`；开发集报告留在本地 `artifacts/`（不提交）。
- [ ] **Step 7: 停合并，交管理员**：提交范围、测试计数、开发集一致率、成本（token/次）、已知限制；合并与启用由管理员确认后执行（第 4 阶段计划另行编写）。

## 后续阶段（不变）

第 4 阶段（低风险候选生成与五层门禁）以本阶段 `approved_for_candidate` 状态为入口；第 5 阶段（隔离代码进化）不变。两阶段计划待本阶段验收后编写。

## 需管理员确认的开放问题（开工前）

1. **诊断模型**：默认复用 `settings.deepseek_*`（当前 Command Code `deepseek/deepseek-v4.1-flash`）；是否改用更便宜的型号（如同平台 `deepseek/deepseek-v4-flash-fast`）？
2. **生成方式**：候选池聚类自动生成草案（每轮限流，默认每轮 5 次调用）还是管理员手动触发？默认自动。
3. **参考轨迹范围**：同 kb 的成功 `execution_logs` + 管理员确认标签；是否需要纳入跨库相似问题？默认不跨库。
4. **草案内容边界**：允许引用权限过滤后的原文片段（仅落 evolution.db、管理员可见）？默认允许。
5. **开发集门槛**：草案根因类别与管理员标签一致率 ≥80% 作为本阶段验收线（第 2 阶段回放的已知标签案例为开发集）？可调整。
