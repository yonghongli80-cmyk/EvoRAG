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
4. 测试运行 `pytest <候选 test_paths> + 固定回归网`（cwd/PYTHONPATH=worktree、`PYTHONHASHSEED=0`、
   不继承 `.env`）；失败 → `rejected`；超时或收集失败 → `needs_more_evidence`（不误杀）。
   固定回归网见 `rag/evolution/sandbox.py` 的 `REGRESSION_TEST_PATHS`（24 个离线确定性文件，
   覆盖 Jev 策略与注册表、管线装配、检索、重排、切块、结构化查询与工具）。补丁永远不能改
   `tests/**` 与 `**/conftest.py`（第 2 节黑名单），所以这张网对候选恒定，无法被它裁掉、跳过或改写；
   候选没写 `test_paths` 时仍是 `needs_more_evidence`——"我们跑了自己的基线"不等于"候选自带
   的验证目标通过了"。
5. 基准回放（子进程 `scripts/benchmark_jev_retrieval.py`）：回归 → `rejected`；
   **回放跑不起来（环境问题）不判死**，记 "replay unavailable" 仍可进入人工审核；
   回放"跑了但什么都没测到"（两侧同样空转）同样不算证据，记
   `benchmark replay measured nothing: <原因>`，**任何情况下都不会被判成 pass**（见第 7 节第 1 条）。
6. `finally` 强制 `git worktree remove --force` + `prune` + 删临时目录。
结论经 `sandbox_gate_outcome(report)` 映射为 (状态, gate_detail)，以 `gate="sandbox"` 落 gate_log。

## 5. 管理员审核与导出

- `GET /knowledge-operations/evolution/code-generation-queue`（**只读"待生成清单"**）：
  已批准聚类里还没有代码补丁候选的成员观测，逐行给出 `confirmed_layer`、
  草案 `draft_suggested_layer` 及其 `prompt_version`、`effective_layer`、
  `layer_authority`（`confirmed` / `draft` / 空）与 `blocked_reason`
  （未定层级，或层级不在可确认枚举里）。该端点**不调模型、不写任何东西**：
  第 5 阶段的生成仍由管理员/集成脚本显式触发（见第 8 节）。
  当前只有后端端点，前端"代码补丁"页签尚未渲染这份清单。
- `GET /knowledge-operations/evolution/code-candidates`（列表：layer/状态/文件/沙箱摘要/置信度）
- `GET .../code-candidates/{id}`（+ diff 全文 + 五层 gate_log + 审计）
- `POST .../code-candidates/{id}/decision`：
  `approve_for_apply`（导出补丁与快照 + 记录 apply/revert 命令 + 推进 `approved_for_canary`，**不应用**）、
  `reject`、`needs_revision`。400/404/409 边界均不写审计、不导出。

导出文件 `artifacts/patches/candidate-<id>.patch` 头部含 apply 与 revert 两条命令，例如：

```
# apply: git -C <repo> apply --3way artifacts/patches/candidate-<id>.patch && python -m pytest <test_paths>
# revert: git -C <repo> apply -R artifacts/patches/candidate-<id>.patch
```

同目录另写一份机器可读快照 `artifacts/patches/candidate-<id>.snapshot.json`：`base_commit`、
`paths`、`test_paths`、`diff_sha256`、两条命令、沙箱门禁摘要、`prompt_version`/`model` 与导出时间。
这是设计 §10"不可变版本快照"的**当前最小形态**：不建快照库（那是一个尚不存在的子系统），
只保证一次导出自描述、后来的人能干净回退。回退仍由人执行——系统不留任何自动合入或自动回退路径。

应用后人工检查、跑全量测试、重启服务；回退同样由人执行 `# revert` 那条命令。
测试在真实仓库的一次隔离 worktree 上验证过 apply 与 revert 互为逆操作，且导出前后主工作树字节不变。

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

1. **沙箱内的端到端基准回放当前必然空转，层 2 对代码补丁实际不生效**（2026-09-28 实测）。
   回放命令不带主体，而语料每条都带 `kb_id`：`rag/pipeline.py` 的"企业模式但无主体"守卫在
   路由与检索**之前**就返回 `route="fixed"` + `permission_limited`，于是每条 preparation 在
   微秒级返回、`external_calls=0`，而基准把它记成"成功样本"（`failed_calls=0`），路线准确率
   恒为 0.150。补上知识库快照与嵌入凭证也不成立：准备路径要出网四次（路由模型、改写、嵌入、
   重排），而沙箱按红线**永不**把凭证交给子进程；且路由/改写的模型调用不确定，实测同一份
   **未打补丁**的代码两次跑 0.579 / 0.529，会让"route 不得降 0pp"的层 2 阈值产生**假红**。
   因此本阶段的实际回归判定 = **固定回归网 + 候选自带目标测试**（第 4 节第 4 步）。
   防御性守卫已就位：`_execution_evidence_reason` 要求基线侧"确有工作"（样本数 > 0、
   无失败调用、`prep P50 ≥ MIN_PREP_P50_MS`），否则整份报告记为"measured nothing"而不是通过；
   另在验收侧加了 `executed` 检查，让不带 `--user-id` 的知识库跑法**当场报错**而不是静默产出
   0.150 的假数字。
   要恢复端到端回放作为判定输入，需要"录制-回放"fixture：沙箱外一次性录下 route 决策、改写文本、
   query 向量与重排顺序并入库，沙箱内重放这四个 seam——列为后续事项（当前不阻塞门禁有效性）。
2. **目标测试由模型提议**：`test_paths` 必须真实存在（层 1 校验），但模型可能只挑弱测试；
   固定回归网已覆盖仓库共享路径（缓解此项），人工审核仍应自行补充更严的验证命令。
3. **一次一轮、单候选**：不做多分支探索（DGM 式）与知识库级灰度。
   快照回退的**不可变版本存储**（绑定提示词/检索参数/切块/解析/Jev 集/索引版本）仍不存在；
   当前只有第 5 节的"导出即自描述"最小形态（apply/revert 命令 + `snapshot.json`）。
   这三项出自设计 **§3/§7/§10/§12**（注：设计正文只有 13 节，此前文档里引用的"第 15 节"不存在）。
4. **审计表沿用阶段 3 的白名单动作**：`confirm_layer` 的审计动作复用 `approve_for_candidate` +
   明确备注（表带 CHECK 约束，扩枚举需重建表）。

## 8. 关闭与回退

1. 关 `RAG_EVOLUTION_CANDIDATE_ENABLED`（代码补丁生成也受该开关与 `evolution_candidate_enabled` 语义管辖）；
   或直接不调用生成器。**该能力默认不在 worker 轮次里自动跑**，且目前没有管理员触发端点：
   `PatchGenerator`/`run_patch_sandbox` 的唯一调用者是未入库的
   `real-case-handoff/e2e_code_patch.py`。接进 worker 需要先立两样现在没有的东西——预算维度
   （`evolution_candidate_model_calls` 是只有定义、无读取点的死配置）与"已批准 + 已确认层级 +
   无代码补丁候选"的批量查询契约。
2. 删除候选：`evolution.db` 的 `candidates` 行（或整库）。
3. 已导出的补丁文件在 `artifacts/patches/`，删除即撤销导出；**代码回退**按普通 git 流程（`git revert`）。
