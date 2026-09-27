# Assisted Self-Evolution (Isolated Code Evolution) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把进化和门的范围从“Jev 问题集文本变体”扩展到**代码层候选补丁**（设计第 2 节阶段 2/3：切块策略、重排流程、解析器、工具工作流，直至项目代码），但坚持设计第 2 节的硬边界：**只能在隔离 Git worktree 中形成候选补丁**，权限策略、审计、验收器、测试基准、发布门禁、密钥与用户原始数据永久禁止。补丁的验证与放行沿用第 4 阶段五层门禁骨架（层 1 边界 → 层 2 沙箱测试+基准回放 → 层 3 影子（代码层退化为沙箱回归，不引入在线双跑）→ 层 4 灰度对代码层不适用，改为**管理员审核补丁** → 层 5 由管理员执行应用，系统永不自动合入主链）。

**Architecture:** 第 4 阶段的候选基建（`candidate_store`/门禁/审计/状态机）原样复用；`artifact_type` 扩展为 `jev_question_set | code_patch`；新增 `rag/evolution/code_patches.py`（补丁契约 + 路径边界白名单/黑名单 + diff 解析与限额）、`rag/evolution/patch_generator.py`（一次模型调用产出 unified diff）、`rag/evolution/sandbox.py`（`git worktree` 隔离 → `git apply --check`/`git apply` → 目标测试 + 基准回放 → 清理）、`rag/evolution/api.py` 增加补丁审核端点（diff 只读展示 + 校验结论 + 管理员三动作，approve 只导出补丁文件与 apply 命令，**不自动应用**）、前端补丁审核页签。`rag/evolution/**` 自身禁止被补丁修改（防自改性），`tests/**`、`scripts/**`（验收器）、`config.py`（发布门禁）、安全与权限模块、`frontend/**` 一律禁止。

**Tech Stack:** Python 3.12、FastAPI、SQLite、pytest、Vue 3 + vitest；`git worktree`/`git apply`（子进程，受超时与路径约束）；候选生成复用第 3/4 阶段同款隔离模型通道（Command Code v4.1-flash，timeout/token 从 `evolution_diagnosis_*` 读取）；不引入新依赖。

**Spec:** `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`（第 2 节分层开放与永久禁止项、第 3 节后台链路与 worktree、第 9 节门禁、第 13–15 节）；前置：第 4 阶段（`docs/evolution-candidates.md`）。

## Global Constraints

- **补丁只在 worktree 里存在**：候选 diff 绝不写入主工作树；验证在 `git worktree add` 出的隔离副本中进行；验证结束 `git worktree remove --force`。任何情况下不得 `git apply` 到主工作树、不得 `git commit`、不得切换分支。
- **路径边界即安全边界**：层 → 允许 glob 的映射（见 Task 1 表）外加全局黑名单；越界即 `PayloadBoundaryError`。黑名单至少覆盖：`rag/permissions.py`、`rag/api_security.py`、`rag/org_auth/**`、`rag/auth.py`、`rag/user_db.py`、`rag/evolution/**`（自改性）、`tests/**` 与 `**/conftest.py`（测试基准）、`scripts/**`（验收器）、`config.py`（发布门禁）、`frontend/**`、`.github/**`、`docs/superpowers/**`（设计文档）、以及任何 `.env*`/密钥文件。
- **限额**：单补丁 ≤5 个文件、≤300 行变动、不得含二进制、不得删除文件、不得新增文件到黑名单路径；超过即拒。
- **没有自动应用、没有自动合并**：管理员 approve 只做三件事——导出 `.patch` 到 `artifacts/patches/`、写审计、把候选推进到 `approved_for_canary`（对代码层的语义是“待人工应用”）。应用动作由管理员按导出命令手动执行（`git apply --3way` + 跑测试 + 重启服务），全过程留痕。
- **验证失败即 reject**：apply 失败、目标测试失败、基准回放回归（路线 ≤0pp / Top-5 ≤2pp，与层 2 同阈值）任一发生 → `rejected` 并附完整日志摘要；超时（默认 600s）记 `needs_more_evidence` 而非 reject。
- **无秘密外溢**：提示词只含目标文件正文（已在允许 glob 内）、诊断草案与约束；diff/日志/审计不含密钥；worktree 内 `.env` 不复制（用测试夹具的环境隔离）。
- **在线路径零影响**：补丁候选的生成与验证全部在后台/隔离环境；服务进程不被 patch 触碰；代码层不设百分比灰度（无第二实例），层 4 对 `code_patch` 恒为“管理员审核”。

## Review Focus

- 路径边界：每个允许层能改什么、黑名单为什么那些（尤其自改性与测试基准）；覆盖于 Task 1 测试（含“改 `rag/evolution/**` 被拒”）。
- 沙箱真隔离：worktree 创建/应用/测试/清理全流程，主工作树 `git status` 前后一致（测试钉住）；覆盖于 Task 3 测试。
- 补丁生成不越界：模型多给了黑名单路径或删文件 → invalid 不落库；覆盖于 Task 2 测试。
- 审核端点：diff 只读、approve 不应用只导出+审计、reject/needs_revision 语义、403/400/404/409；覆盖于 Task 4 测试。
- e2e：用**真实存在的开放缺陷**（第 2 阶段回放报告记录的检索撞名/排序问题 → rerank 层）走完“诊断→生成→沙箱验证→管理员审核→导出补丁”，证明闭环且不自动应用；覆盖于 Task 5。

---

## File Map

- `rag/evolution/code_patches.py`（新建）：`CodePatchCandidate`（frozen：artifact_type="code_patch"、`layer: Literal["chunking","reranking","parser","tool_workflow","code"]`、`paths: tuple[str,...]`、`diff: str`、`test_paths: tuple[str,...]`、rationale、expected_effect、rollback_ref（base commit sha）、confidence）、`LAYER_PATH_GLOBS`、`FORBIDDEN_GLOBS`、`validate_patch(candidate, *, repo_root) -> None`（glob/黑名单/限额/`git apply --check` 在临时 worktree 或 `--check` 模式）。
- `rag/evolution/patch_generator.py`（新建）：`PatchGenerator`（版本化提示词 `PATCH_PROMPT_VERSION="patch-v1"`、一次调用、隔离客户端、后校验越界拒）。
- `rag/evolution/sandbox.py`（新建）：`run_patch_sandbox(candidate, *, repo_root, tests_timeout=600) -> SandboxReport(applied/tests_passed/failed_tests/benchmark/per_case摘要/duration/error)`；`git worktree add <tmp> --detach HEAD` → apply → `pytest test_paths`（复用 tests/conftest 环境隔离）→ 基准回放（`scripts/benchmark_jev_retrieval.py` 子进程，`PYTHONPATH` 指向 worktree）→ 阈值判定 → `git worktree remove --force`；任何一步失败都清理。
- `rag/evolution/candidates.py`（修改）：`artifact_type` Literal 增加 `"code_patch"`；`validate_payload` 仅对 `jev_question_set` 生效（补丁走 `validate_patch`）。
- `rag/evolution/api.py`（修改）：`GET /code-candidates`、`GET /code-candidates/{id}`（含 diff 与沙箱报告）、`POST /code-candidates/{id}/decision`（approve_for_apply / reject / needs_revision + note，全部 require_admin + 先审计后推进；approve 导出 `.patch` + 记录 apply 命令，**不执行**）。
- `frontend/src/api/evolution.ts`、`frontend/src/views/EvolutionCenterView.vue`（修改）：补丁审核页签（文件清单 + diff 只读 + 沙箱结论 + 三按钮）。
- `tests/test_code_patches.py`、`tests/test_patch_generator.py`、`tests/test_patch_sandbox.py`、`tests/test_code_candidate_api.py`。
- `docs/evolution-code-patches.md`（新建）：运维说明（边界表、沙箱流程、导出与应用命令、回退）。

### Task 1: Patch contracts and the path boundary

**Files:**
- Create: `rag/evolution/code_patches.py`
- Modify: `rag/evolution/candidates.py`（仅 Literal 扩容）
- Create: `tests/test_code_patches.py`

**Interfaces:**
- Consumes: `rag/evolution/candidates.py` 的 frozen 模型模式、`rag/jev/questions.py` 不被触碰。
- Produces: `CodePatchCandidate`、`LAYER_PATH_GLOBS`（chunking → `rag/chunk*/**`、`rag/chunker*/**`；reranking → `rag/rerank*/**`、`rag/rrf*/**`、`rag/fusion*/**`；parser → `rag/parsers/**`、`rag/document_sources/**`、`rag/extract*/**`；tool_workflow → `rag/agent*.py`、`rag/tools/**`、`rag/coordinator*/**`、`rag/spreadsheets/**`、`rag/structured_query*/**`；code → `rag/**` 减黑名单）、`FORBIDDEN_GLOBS`（上文 Global Constraints 清单）、`validate_patch(candidate, *, repo_root) -> None`（glob 匹配用 `fnmatch` 语义 + `pathlib.PurePosixPath`；diff 行数/文件数/二进制/删除/新增黑名单路径检查；`git apply --check` 在调用方给出的 repo 内以 `--check` 模式执行，不落盘）。

- [ ] **Step 1: Write failing tests**（层 glob 命中/不命中；黑名单每个前缀至少一例（含 `rag/evolution/**` 自改性）；限额（文件数/行数/二进制/删文件）；`git apply --check` 失败拒绝；合法小补丁通过）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（契约 + 边界 + 检查；`git apply --check` 经 subprocess，超时 60s，输出只留分类）
- [ ] **Step 4: Run focused tests**（+ `tests/test_evolution_candidates.py tests/test_evolution_candidate_store.py` 零回归）
- [ ] **Step 5: Commit**：`feat: model code-patch candidates with a path boundary`.

### Task 2: The constrained patch generator

**Files:**
- Create: `rag/evolution/patch_generator.py`
- Create: `tests/test_patch_generator.py`

**Interfaces:**
- Consumes: Task 1 契约、已确认诊断（`approved_for_candidate` 聚类 + 成员草案）、目标文件当前正文（从主工作树读取，只读）、诊断器/候选生成器同款隔离模型通道与失败分类。
- Produces: `PatchGenerator(model_call=None, *, prompt_version=PATCH_PROMPT_VERSION, timeout_seconds=90.0, max_tokens=16000)`、`.generate(cluster, drafts, *, layer, repo_root, observation_id, cluster_id, trace_store) -> CodePatchCandidate | raise PatchUnavailable(reason)`；提示词硬合约：只输出单个 unified diff（```diff 代码块或裸 diff）、只改允许 glob 内文件、≤5 文件 ≤300 行、不删文件、不改测试/配置/安全/自演化模块、附 `test_paths`（验证用测试文件，必须在 `tests/` 内且存在）；后校验：layer 合法、`validate_patch` 通过、`test_paths` 存在且非空、diff 非空。

- [ ] **Step 1: Write failing tests**（合法 diff 生成；模型输出越界路径/删文件/空 diff/坏 diff → PatchUnavailable("invalid")；timeout/rate_limited 分类；提示词含版本戳与硬合约、无密钥哨兵）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（提示词版本化；一次调用；`test_paths` 由模型提议但必须存在性校验）
- [ ] **Step 4: Run focused tests**（+ `tests/test_evolution_candidate_generator.py` 零回归）
- [ ] **Step 5: Commit**：`feat: generate bounded code-patch candidates from confirmed diagnoses`.

### Task 3: The isolated sandbox (worktree apply + tests + benchmark replay)

**Files:**
- Create: `rag/evolution/sandbox.py`
- Create: `tests/test_patch_sandbox.py`

**Interfaces:**
- Consumes: Task 1 的 `validate_patch`、临时目录（`tempfile.mkdtemp`）、`git` 子进程。
- Produces: `SandboxReport`（frozen：`applied: bool`、`tests_passed: bool | None`、`failed_tests: tuple[str,...]`、`benchmark: dict | None`（路线/Top-5 对比与阈值判定）、`duration_s: float`、`error: str`）、`run_patch_sandbox(candidate, *, repo_root, tests_timeout=600) -> SandboxReport`：
  1. 断言主工作树干净（`git status --porcelain` 为空，否则拒绝运行并报告）；
  2. `git worktree add <tmp> --detach HEAD`；
  3. `git apply`（worktree 内）；失败 → applied=False + error（分类）；
  4. `python -m pytest <test_paths>`（cwd=worktree，`PYTHONPATH` 指向 worktree，远离主仓数据）；超时记 error；
  5. 基准回放：子进程跑 `scripts/benchmark_jev_retrieval.py`（cwd=worktree）；解析输出得当前/候选两侧路线与 Top-5（基准 runner 侧需支持 `--question-provider` 或等价参数——若已有则以现有参数接入，否则本任务在 scripts 侧加最小参数并在报告说明）；
  6. `git worktree remove --force <tmp>`（finally 保证，含 `git worktree prune`）。
- **安全**：worktree 路径必须在临时目录下；不复制 `.env`；测试用 conftest 的环境隔离；子进程全部带超时。

- [ ] **Step 1: Write failing tests**（应用成功+测试通过→报告全绿；apply 失败→applied=False 且主树不变；测试失败→failed_tests 非空；worktree 泄漏为 0（前后 `git worktree list` 一致）；主树脏→拒绝运行）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**（补丁用测试夹具构造真实小 diff；基准回放用假 runner 注入以保持测试离线）
- [ ] **Step 4: Run focused tests**（真跑一次小补丁沙箱冒烟，本地留痕）
- [ ] **Step 5: Commit**：`feat: validate patch candidates in an isolated worktree sandbox`.

### Task 4: Patch review endpoints and the admin UI

**Files:**
- Modify: `rag/evolution/api.py`
- Modify: `frontend/src/api/evolution.ts`、`frontend/src/views/EvolutionCenterView.vue`
- Create: `tests/test_code_candidate_api.py`

**Interfaces:**
- Produces: `GET /knowledge-operations/evolution/code-candidates`（分页 + status 过滤）、`GET .../code-candidates/{id}`（候选 + diff 全文 + 沙箱报告 + gate_log + 审计）、`POST .../code-candidates/{id}/decision`（`approve_for_apply` → 导出 `artifacts/patches/candidate-<id>.patch`（目录不存在则建）+ 审计 note 含 apply 命令 + 推进 `awaiting_verification → approved_for_canary`；`reject` → rejected；`needs_revision` → needs_more_evidence；全部 require_admin、先审计后推进、400/404/409 边界）。**端点不执行 apply**；导出的 apply 命令为 `git -C <repo> apply --3way artifacts/patches/candidate-<id>.patch && python -m pytest <test_paths>`。前端：补丁审核页签（文件清单、diff `<pre>` 只读、沙箱结论徽标、三按钮 + 备注）。

- [ ] **Step 1: Write failing tests**（列表/详情形状含 diff 与沙箱报告；approve 只导出不应用（主树 `git status` 不变）+ 审计 + 状态推进；reject/needs_revision；403/400/404/409；重复 approve 409）
- [ ] **Step 2: Run and confirm failure**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run focused tests**（后端 + 前端 vitest + `vue-tsc`）
- [ ] **Step 5: Commit**：`feat: review code-patch candidates with exportable diffs`.

### Task 5: End-to-end run on a real open defect and the operations doc

**Files:**
- Create: `docs/evolution-code-patches.md`
- Produce locally ignored: `artifacts/evolution-code-patches-e2e.json`、`artifacts/patches/candidate-<id>.patch`

**Interfaces:**
- Consumes: Task 1–4；输入根因取自**第 2 阶段回放报告记录的真实开放缺陷**：检索分数区分度低、词面撞名淹没候选（`docs/real-cases-v1-replay-report.md` B 类）→ layer=reranking，目标 `rag/reranker.py` 一侧。该根因已被回放数据证实（非构造）。
- Produces: 一条“诊断→生成→沙箱→审核→导出”的完整留痕 + 运维文档；不应用补丁。

- [ ] **Step 1: 构造真实输入**：把该开放缺陷登记为一条已确认诊断（管理员批准，审计在案；如无现成观测，用回放证据新建一条观察并走诊断—批准流程，全部留痕）
- [ ] **Step 2: 生成补丁候选**（真实模型调用；预期目标 `rag/reranker*.py`）
- [ ] **Step 3: 沙箱验证**（worktree + 目标测试 + 基准回放；记录 applied/tests/benchmark/duration）
- [ ] **Step 4: 管理员审核**（approve_for_apply → 导出补丁；**不应用**；随后 reject 或保留待人工）
- [ ] **Step 5: 全量验证**：`python -m pytest tests/test_code_patches.py tests/test_patch_generator.py tests/test_patch_sandbox.py tests/test_code_candidate_api.py -q` 全 PASS；`python -m pytest -q` 全量零回归；前端全量 + `vue-tsc`；`ruff check rag/ config.py scripts/ tests/`。
- [ ] **Step 6: 运维文档**：`docs/evolution-code-patches.md`（层→glob 表、黑名单与理由、沙箱流程与限额、导出与应用命令、回退步骤、已知限制）。
- [ ] **Step 7: Commit**：`docs: document isolated code-patch evolution`；e2e 报告与导出的补丁留本地。
- [ ] **Step 8: 停合并，交管理员**：提交范围、测试计数、e2e 各步结果、导出补丁的审查建议；应用与否由管理员决定。

## 后续阶段

本设计阶段表到此结束（阶段 5 完成）。后续增强（多分支探索 DGM 式、知识库级灰度、快照回退自动化）按设计第 15 节另立计划。

## 需管理员确认的开放问题（开工前）

1. **层范围**：代码补丁覆盖 chunking/reranking/parser/tool_workflow/code 五层，映射到上述允许 glob；同意？
2. **黑名单**：含 `rag/evolution/**`（禁止自改性）、`tests/**`、`scripts/**`、`config.py`、安全/权限/用户库、`frontend/**`、`.github/**`、设计文档；同意？
3. **审批即导出、应用靠人工**：approve 只导出 `.patch` 与 apply 命令并写审计，**系统永不自动 apply/commit/重启**；应用由管理员手动执行；同意？
4. **沙箱限额**：worktree + 目标测试 + 基准回放，超时 600s 记 needs_more_evidence；同意？
5. **e2e 输入**：用第 2 阶段回放报告记录的真实开放缺陷（检索撞名/排序 → reranking 层）作首条真实补丁候选；同意？
