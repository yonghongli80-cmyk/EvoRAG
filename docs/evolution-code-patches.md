# 自进化隔离代码进化运维说明（evolution-code-patches）

第 5 阶段（设计阶段表的最后一段）为**已确认根因层级**的失败生成**代码候选补丁**，在
**隔离 Git worktree** 中应用并跑目标测试与固定回归网，结论写入门禁轨迹，最后由管理员审核；
**回归 A/B 由管理员在应用之后跑**（第 5.1 节，理由见第 7 节第 1 条）。
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
5. **回归 A/B 不由沙箱做**（默认 `run_benchmark=False`，2026-10-02 决定）。理由是可核实的：
   这个系统的嵌入（`rag/embedder.py`）与重排（`rag/reranker.py`）都是**外部 API 调用**，而沙箱
   子进程刻意剥掉所有 `RAG_*`（含凭据）。在无凭据的沙箱里跑回放，量到的是"没有密钥"而不是补丁
   ——实测干净树那一侧 100 次调用全部失败、而候选 #11 恰好兜住了这些异常，于是门禁会以**错误的
   理由**给出结论。所以沙箱只声称它能证明的事：**补丁能应用 + 候选目标测试与固定回归网通过**；
   门禁结论里会明写"回归 A/B 不在这里量、属于管理员应用之后的步骤"。
   回放代码仍然保留且有用例覆盖（`run_benchmark=True` + `data_snapshot` + `replay_user_id`）：
   它在**干净工作树**与**打过补丁的工作树**各跑一次并**逐侧比较**（`patch_verdict`，路由 ≤0pp、
   Top-5 ≤2pp、失败调用不增加），是将来"无凭据回放"（下一节第 5 步）落地后的现成机制。
6. `finally` 强制 `git worktree remove --force` + `prune` + 删临时目录（两个工作树都清）。
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

### 5.1 管理员在应用之后要跑的回归 A/B（层 2 的代码版）

沙箱只证明"能应用 + 测试通过"（第 4 节第 5 步），**"无回归"由管理员在应用补丁之后、在服务
所在的环境（有凭据）里按既有协议测**。这也是唯一能测到嵌入/重排/路由这些模型路径的地方：

1. 在**独立检出**（或先建分支）上 `git apply --3way artifacts/patches/candidate-<id>.patch`；
2. 跑目标测试与固定回归网：`python -m pytest <test_paths>`；
3. 跑既有的 20 用例回归协议（`benchmarks/real-cases-v1.json`，与问题集候选同一套阈值：
   路由 0pp、Top-5 ≤2pp），对照**未打补丁的同一检出**；
4. 只有 3 通过才把补丁合入；任何一步不通过就 `git apply -R` 回退（导出头的 revert 命令）。

**下一步（未做）**：如果希望沙箱自动给出回归信号，方向是给回放一个**不依赖凭据的确定性模式**
（只走词法检索 + 融合 + 过滤 + 确定性判定，不碰嵌入/重排）。机制已就位（双工作树 + 逐侧比较），
缺的只是这样一个"能在无密钥环境里跑完"的回放脚本。

### 5.2 已脚本化的回归 A/B（2026-10-02 起）

第 5.1 节的三步已有入库脚本，运维直接跑：

```powershell
# 1) 放一份只读数据副本（语料 + ACL 库）；服务占用主库时必须有副本：
#    目录里放 qdrant_data/ 与 users.db
# 2) 同一份回放在"干净工作树"与"打过补丁的工作树"各跑一次，逐侧比较（层 2 阈值）
python scripts/measure_patch_regression_ab.py --candidate-id 10 --snapshot <只读副本目录> `
  --kb-id kb_accessories --user-id 1 --runs 2 --warmups 0
```

脚本在补丁的 `base_commit` 上建两棵临时工作树、只对其中一棵 `git apply`，两次回放各自使用
**自己工作树的 `PYTHONPATH`**（否则两次都在量同一份代码），最后用与沙箱相同的 `patch_verdict`
给出逐侧结论并写入 `real-case-handoff/evolab/regression-ab-candidate-<id>.json`；工作树在
`finally` 里移除，**主树永远不动**。

**实测（候选 #10，20 用例，每侧 2 次重复）**：`applied: true`；Jev 侧 route 1.0 → 1.0
（Δ0.0pp，限 ≤0）、Top-5 1.0 → 1.0（Δ0.0pp，限 ≤2）、失败调用 0 → 0、prep P50 951ms → 635ms；
legacy 侧 route Δ+2.5pp、Top-5 Δ0.0pp、失败 0 ⇒ **`passed: true`**。

**同一轮发现的运维点**：带凭据的回放走完整企业路径，脚本默认的 `--runs 5 --warmups 1` 每侧会
超过 900s 超时（实测两侧各 900s 全超时）；因此 `_benchmark_command` 增加 `runs`/`warmups`
参数（沙箱默认不变），人工 A/B 用 `--runs 2 --warmups 0`（约 5–10 分钟）。

应用后人工检查、跑全量测试、重启服务；回退同样由人执行 `# revert` 那条命令。
测试在真实仓库的一次隔离 worktree 上验证过 apply 与 revert 互为逆操作，且导出前后主工作树字节不变。

### 5.3 沙箱运行器与"过期 fixture"规则（2026-10-09 起）

**沙箱结论以前没有生产写回者**：`sandbox_gate_outcome` 只被测试调用过，所以 `candidates.gate_log`
里从来没有真实的沙箱条目，审查台上也看不到任何数字。现在由管理员手动跑：

```powershell
# 不带 fixture：回放会因无凭据而报"不可用"（方案 C 的原有行为，门禁结论照旧待人工）
python scripts/run_patch_sandbox.py --candidate-id 13 --data-snapshot <只读副本目录>
# 带 fixture：四个（实为五个）出网 seam 从录制重放，跑完给出干净侧 vs 打补丁侧的对照
python scripts/run_patch_sandbox.py --candidate-id 13 --data-snapshot <副本> `
  --fixture real-case-handoff/evolab/replay-fixture.json --runs 2
```

脚本把 `(状态, 详情, 结构化指标)` 一并写回（`gate="sandbox"`），所以审查台的"应用与回滚命令"旁边会
显示 route / Top-5 / 失败调用 / 预处理 P50 的两侧对照；`--dry-run` 只跑不写，状态机不允许这次迁移时
它会**如实打印"轨迹未写入"**而不是假装写成功。

**过期规则（要紧，不要绕过）**：fixture 记录的是**录制时那份代码**给出的答案，所以补丁只要碰到
`rag/embedder.py`、`rag/reranker.py`、`rag/agent.py`、`rag/query_rewriter.py`、`rag/jev/client.py`
中的任一个，fixture 就**判过期并被拒绝注入**，门禁结论落到 `awaiting_verification` + "replay unavailable"
——**绝不会给出"通过"**。实测：库里现有的 5 份代码补丁**全部**触及 seam（`agent.py` ×3、`reranker.py` ×2），
因为路由/排序逻辑本来就住在那里；对这类补丁，正确做法是回到 **§5.2 的宿主 A/B（带凭据）**，而不是放宽
这里的判定（放宽就等于拿"另一份代码的答案"当证据）。

**录制**（有凭据处跑一次即可，`kb_accessories` 实测约 136s）：
`python scripts/record_replay_fixture.py --kb-id kb_accessories --out real-case-handoff/evolab/replay-fixture.json --runs 1`
—— 实测会记下 `reranks / routes / rewrites / jev_preflights / jev_passages` 五类答案（`embeddings` 为 0：
Jev 路径把检索交给外部服务，不经过本地嵌入器）。


### 5.4 上下文预算与「加大上下文」实验（2026-10-10）

生成器给模型看的正文有**两个**限制，改的时候必须一起改：

* 每个文件的**头 400 行**（`MAX_EXCERPT_LINES`，`_excerpt`）；聚焦目标则改为**靶心窗**；
* 靶心窗的**半径 120 行**（`BULLSEYE_RADIUS`）—— 关键点：窗宽是 `min(上限, 半径*2+1)`，
  **只调上限是扩不出窗口的**（半径 120 时窗宽上限就是 241）。

因此新增 `RAG_EVOLUTION_PATCH_CONTEXT_LINES`（默认 **0**）：

* **0 = 完全保持既有行为**（头 400 / 半径 120，既有用例覆盖了这条，改不动它）；
* **N > 0** ⇒ 每文件上限 N 行，且靶心窗半径取 **N//2**（例如 1600 ⇒ 窗宽 1600、半径 800）。

这一档实验用的就是它：`RAG_EVOLUTION_PATCH_CONTEXT_LINES=1600` + 基线测量台
（`scripts/measure_patch_generation.py`，同一批 6 对 × 2 次，与基线同源可比）。

**若实验仍不达标，下一步是"结构化输出"而不是继续加大上下文**——基线证据（`does_not_apply` /
`corrupt_patch` / 无 diff）指向的是**模型在做不可靠的行数算术**：让它手写 `@@ -a,b +c,d @@` 与
精确上下文，本质上是让它手算并核对行数。设计（**待你确认后再动契约**）：

1. 模型改为输出**编辑块**：`{"patches": [{"path": ..., "old_text": ..., "new_text": ...}], ...四行元数据}`；
2. 我们**自己**把编辑块转成 unified diff —— 于是 `corrupt_patch`（hunk 畸形）与 `does_not_apply`
   （上下文不符）**在构造上不可能发生**；
3. 校验变成确定性的：`old_text` 在文件里**恰好出现一次**（0 次 ⇒ 明确的"未命中"错误；≥2 次 ⇒
   "不唯一"），且改后仍过层 1 的路径/配额边界；
4. 人类可读性由**我们的 diff 生成器**保证（满足 §5.3 的审查要求），而不是靠模型排版。

### 5.5 edits 输出格式：让模型别再算行数（2026-10-10，`patch-v5`）

**为什么**：基线测量（§5.4）显示失败集中在 `does_not_apply` / `corrupt_patch` / 压根没给 diff ——
两类上下文规模下都存在，所以问题不是"看得不够多"，而是**模型在手写 `@@ -a,b +c,d @@` 与精确上下文，
本质是在做并核对行数算术**。

**怎么做**：`RAG_EVOLUTION_PATCH_FORMAT=edits`（默认仍 `diff`，零行为变化）时，提示词改问**精确替换块**：

```json
{"edits": [{"path": "rag/reranker.py", "old_text": "逐字原文（恰好出现一次）", "new_text": "替换后"}],
 "test_paths": ["tests/test_reranker.py"], "rationale": "…", "expected_effect": "…", "confidence": "low"}
```

然后由 :func:`edits_to_diff` **自己拼 diff**（`difflib.unified_diff`）⇒ **hunk 头与上下文构造上自洽**
⇒ `corrupt_patch` 与 `does_not_apply` 在这条路径上**不可能发生**。边界仍然照跑（`git apply --check`、
路径白名单、黑名单、配额），所以是**双重保证**而不是放宽。

**拒绝语义（都不含内容）**：`old_text` 找不到 ⇒ `old_text not found`；出现多次 ⇒
`old_text is ambiguous (N)`；路径越层/命中黑名单 ⇒ `path outside the layer` / `forbidden path`；
JSON 结构不对 ⇒ `invalid_json`。

**两档如何对照**：`scripts/measure_patch_generation.py --observation-ids <固定列表> --repeats 2`
配合上面那个环境变量，**同一批样本**跑两轮（**必须串行**：并行会触发服务商限流，实测让一次基线侧
26 次调用失败）。验收线仍是 **≥80%**。

### 5.6 一键应用与重启（2026-10-11）

审查台上「**应用并重启**」按钮对应 `POST /code-candidates/{id}/apply`。它**不软化**"系统永不自动合入"：
只对**管理员已批准**（`approved_for_canary`）的补丁、且只在**人点击**时动作 —— 点击本身就是那个决定。
它比手动路径**更安全**，因为补上了手动流程缺的三道前置检查：

1. **补丁状态**（`git apply --check` / `-R --check`）必须是 `not_applied`（或用 `-R` 判定为已应用）；
2. **导入冒烟** `python -c "import rag.api"` —— 最便宜地拦住"补丁语法/导入就坏"的情况；
3. **该补丁自己的目标测试**（`test_paths`，带 `-p no:cacheprovider` 以免污染工作树）。

任一步失败 ⇒ **还原补丁**（`git apply -R`）并且**绝不重启**，运行中的服务保持原样 ✓。

全部通过才写重启请求：请求本身**不能**在同一个进程里重启（要杀的就是它），所以它把意图写成一份
**分离式助手**（`artifacts/restart-candidate-<id>.py`，`DETACHED_PROCESS` 启动）：等端口释放 →
按 `start.py` 拉起 → 轮询 `/health`；**起不来就回滚补丁再拉一次**，让失败停在"旧代码在跑"而不是
"什么都没有在跑"。

**应用前还会跑全量套件**（`RAG_EVOLUTION_APPLY_TEST_SCOPE=full`，默认）：补丁自带的 `test_paths` 是
**模型自己挑的**，它拦不住"改坏别处" —— 而那正是把代码库拖成屎山的主要来源。全量套件放在**分离式助手**里跑
（约 18 分钟），所以接口立即返回；套件不通过就**回滚补丁且不重启**。请求里 `restart=false` 时不会跑套件，
接口会在响应里明确警告"未请求重启 ⇒ 全量测试没有跑"。

**堆放治理**（与生成侧配套）：准入从"按观测去重"改为**按聚类去重** —— 一个聚类只要还有一条**未决**候选，
就不再为它的任何成员生成新变体，**轮内也挡**。实测动机：`rag/reranker.py` 上曾堆了 11 条候选、其中 5 条是
同一个"同义对齐"想法的不同写法，而 cluster 3 有 112 个成员 ⇒ 不去重就能一口气产出上百条。
驳回（`rejected`）会释放**聚类级**闸，但**不会**让同一个观测自动重试（重新生成是管理员的显式动作）。

**记录位置**：结果在**接口返回**里，并追加到 `artifacts/service-control.log`。**刻意不写演化审计表**：
它的 action 列是 CHECK 约束的三个诊断决策，为一个运维事件重建表不划算。

**顺带修掉一个坑**：面板给出的应用命令原本带 `--3way` ✗，而**导出的补丁没有 `index` 行**（`difflib`
不产生）⇒ 三路合并会直接报 `repository lacks the necessary blob` 而失败 ✓。命令与状态判断现已统一为
**不带 `--3way`** ✓；另外 `patch_state` 早期版本从 **stdin** 喂补丁 ✗，而 `read_text` 会把 CRLF 归一成 LF、
仓库文件仍是 CRLF ⇒ 同一份补丁"从文件能应用、从 stdin 失败" ✓ ⇒ 现在一律**写临时文件再 `git apply <file>`**，
与运维命令同机制 ✓。

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

   **2026-10-02 更新（本条已按上段结论落地）**：

   * 语料快照（`RAG_EVOLUTION_SANDBOX_DATA_SNAPSHOT`，含 `qdrant_data/` 与 `users.db`）与主体
     （`..._REPLAY_USER_ID`）已加，回放**不再因"没语料/没主体"而不可用**；报告文件也改为
     "存在即按报告判定"（runner 因自身阈值不达标而 exit 1 时不再被当成"没跑完"）。
   * 门禁的**比较对象**同时修正：原先误把 runner 自己的 `old/new`（legacy vs Jev）当基线/候选，
     现在改为**同一回放在干净工作树与打过补丁的工作树各跑一次、逐侧比较**（`patch_verdict`）。
   * 但**结论仍然是"无凭据"而不是补丁**：实测候选 #11（给 `route_question` 加 try/except）拿到
     `benchmark replay measured nothing: the baseline side failed 100 call(s)`——干净树里 legacy
     侧每次调用都因缺密钥抛异常（100 = 10 用例 × 5 次 × 2 面），而该补丁恰好兜住了它们。
     这正印证了本条第 1 段的判断：无凭据沙箱里的回放**量的是密钥的有无**。
   * 所以**默认关闭**（`run_patch_sandbox(..., run_benchmark=False)`），且门禁结论明写"回归 A/B
     不在这里量、属于管理员应用之后的步骤"；回放代码与双层比较机制保留备用（实验或将来落地
     无凭据回放时即为现成机制）。
   * 使沙箱能自动给回归信号的两条路：(a) **词法专用确定性回放**（只走 BM25 + 融合 + 过滤 +
     确定性判定，不碰嵌入/重排）；(b) 本段的**录制-回放 fixture**。两者都还没做。
2. **目标测试由模型提议**：`test_paths` 必须真实存在（层 1 校验），但模型可能只挑弱测试；
   固定回归网已覆盖仓库共享路径（缓解此项），人工审核仍应自行补充更严的验证命令。
3. **一次一轮、单候选**：不做多分支探索（DGM 式）与知识库级灰度。
   快照回退的**不可变版本存储**（绑定提示词/检索参数/切块/解析/Jev 集/索引版本）仍不存在；
   当前只有第 5 节的"导出即自描述"最小形态（apply/revert 命令 + `snapshot.json`）。
   这三项出自设计 **§3/§7/§10/§12**（注：设计正文只有 13 节，此前文档里引用的"第 15 节"不存在）。
4. **审计表沿用阶段 3 的白名单动作**：`confirm_layer` 的审计动作复用 `approve_for_candidate` +
   明确备注（表带 CHECK 约束，扩枚举需重建表）。
5. **目标文件曾经只能靠关键词猜——本条已修（2026-10-09，`patch-v3`）**。原实现按"诊断措辞在文件里
   出现的次数"排序：中文诊断贡献的中文二字组在英文代码路径里命中率为 0，排序退化为文件字母序，
   于是证据边界的缺陷产出了三份改 `rag/agent.py` 的补丁。现在 `rag/evolution/symbols.py` 用 `ast`
   建符号表（`_rrf_fuse` → `rag/retriever.py:230-266`），生成器据**异常帧（文件+行）**与
   **诊断里真实存在的符号**定位文件，并把该文件按**靶心窗口**（目标行 ±120 行、至多 400 行）展示，
   而不是只给前 400 行；提示词里另有"定位块"说明帧与符号的行区间。
   验收（无模型调用）：无定位时首目标是 `rag/bm25_store.py`（猜的），有定位时是 `rag/pipeline.py`；
   窗口 `580–820` 覆盖目标第 745 行；中文诊断里的 `_finalize_rag_context`/`_rrf_fuse` 解析为
   `rag/pipeline.py`/`rag/retriever.py`。**残留**：只覆盖 `ast` 能看懂的 Python；动态调用、字符串里的
   符号名、非 Python 文件不在表内；同类内同名函数按 `(path, start)` 取第一个，不做语义消歧。

## 8. 关闭与回退

1. 关 `RAG_EVOLUTION_CANDIDATE_ENABLED`（代码补丁生成同样受该开关管辖）。
   **2026-10-09 起，生成已接进 worker 轮次**：`make_evolution_patch_step` 每轮扫描"已批准 +
   已确认层级 + 草案层级一致 + 尚无补丁候选"的成员观测，自动生成补丁候选并落库，预算来自
   `RAG_EVOLUTION_CANDIDATE_MODEL_CALLS`（此前只有定义、没有读取点）。每轮汇总新增
   `patches_created / patch_failed / patch_pending / patch_model_calls` 四个计数；开关关闭或预算 0
   时**零调用**。**沙箱与回归 A/B 不在这一步**（需要干净主树；无凭据时量不了回归，见第 4 节第 5 步），
   仍由管理员按第 5.1/5.2 节执行。`PatchGenerator`/`run_patch_sandbox` 的手工调用脚本
   （`real-case-handoff/evolab/e2e_code_patch.py`、`sandbox_stored_patch.py`）仍然可用，用于实验。
2. 删除候选：`evolution.db` 的 `candidates` 行（或整库）。
3. 已导出的补丁文件在 `artifacts/patches/`，删除即撤销导出；**代码回退**按普通 git 流程（`git revert`）。

**实测对照（2026-10-10，严格同一样本：`--observation-ids 131,151,153,154,155,156 --repeats 2`，串行）**：

| 格式 | 成功率 | 首试成功率 | 失败类别 | 单次耗时 |
| --- | --- | --- | --- | --- |
| `diff`（对照） | **8/12 = 66.7%** | 6/12 = 50% | `corrupt_patch`×2、无 diff×1、`does_not_apply`×1 | 39–251s（均值≈110s） |
| **`edits`（新，现为默认）** | **12/12 = 100%** | **12/12 = 100%** | **无** | **19–168s（均值≈59s）** |

⇒ **达标（判据 ≥80%）** ✓，且**零重试、耗时约减半**（此前的慢主要来自重试与失败的生成）。默认因此取
`edits`；要回到旧行为只需把 `RAG_EVOLUTION_PATCH_FORMAT` 设成 `diff`（可随时回退）。
**样本局限**：6 个观测 ×2 次、都在 cluster 3（`reranking` 层）⇒ 结论强但不外推；边界未放宽，
所以风险与旧路径相同。
