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
