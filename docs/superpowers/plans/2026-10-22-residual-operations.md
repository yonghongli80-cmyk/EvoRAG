# 残留项运维与架构审计（工作流 D，2026-10-22）

> 状态：**只读审计完成**。本次没有改代码、没有写数据库、没有提交 Git，也没有停服务。
> 唯一的产物就是本文件。所有"建议动作"都写成**待管理员执行**的命令，我一条都没有跑。
> 基线：审计**读取时** `HEAD=d56d29a`、工作树不干净（工作流 A/B/C 正在改
> `rag/pipeline.py`、`rag/evolution/candidate_gates.py`、`tests/test_pipeline.py`、
> `tests/test_evolution_candidate_gates.py` 并新增两个测试文件）；审计**结束时**它们已提交，
> `HEAD=28ca6e1`（`1c6ad64` / `b14ff05` / `28ca6e1`），工作树只剩本文件未跟踪。
> 本文行号已对 `28ca6e1` 复核过（`pipeline.py` 的 714/718/954/958/962 未偏移）。
> 审计时刻：2026-10-22 13:35–13:45 (+08:00)。服务 `127.0.0.1:8080` `/health` = `healthy`
> （`{"status":"healthy","components":{"qdrant":"ok","sqlite":"ok"}}`，只读探测）。
> 上游文档：`docs/superpowers/plans/2026-10-22-residual-audit.md` §1（R-4/R-5）、
> `docs/handoff-2026-10-21-refusal-scoring-and-rewrite-instability.md` §3、
> `UPGRADE.md` §0/§1/Phase 0/Phase 1。

---

## 0. 一屏结论

1. **候选 #30 的灰度是 0%，它在结构上永远拿不到样本**（0% 时 `rollout.activate` 直接不注册，
   见 §3.1）。它现在的 `canary` 状态只说明"挂在那里"，不说明"在观察"。
2. **#31 的预期效果与代码路径对不上**（静态核对，§1.4）：它的补丁改的是 legacy 重排器的
   `top_k` 截断，而 Jev 主链路的判定输入不经过这个重排器。**推进层 2 前必须先 dry-run 证明它
   真的改变了什么**，否则会得到一条"通过但无效"的沙箱结论。
3. **8 个测试账号删得掉，但顺序不能反**：`PRAGMA foreign_keys=ON`，`conversations` / `feedback` /
   `org_people` / `document_versions` 都是 `NO ACTION`（§2.2）。另外 4 个 `admin_*` 的口令
   **仍是脚本里的 `Password123!`**（只读校验，§2.5）。
4. **架构阻塞的最小下一步只有两件**：Phase 0 的观测基线（`/metrics`、`/ready`、可复现压测档案）
   和 Phase 1 的 Qdrant server。PostgreSQL / 对象存储 / 多副本都要新环境，**本周做不了**（§4）。

| 项 | 值 | 来源 |
| --- | --- | --- |
| 候选总数 | 30：`rejected` 20 / `rolled_back` 6 / `stable` 2 / `canary` 1 / `candidate_testing` 1 | `candidates` group by status |
| #30 | `jev_question_set`，`target=passages`，`status=canary`，`layer=NULL`，`confidence=low`，`canary_percent=0` | `candidates#30` + `evolution_candidate_audit#113` |
| #30 层 2 | 2026-10-14 09:54 passed：route **+2.3pp**（限 −0.0pp）、top-5 **+5.0pp**（限 −2.0pp），44 用例 × 3 轮全过 | `gate_log[1]` |
| #30 层 3 | **缺失**，gate_log 只有 scope / benchmark / canary | `gate_log` 长度=3 |
| #31 | `code_patch`，`layer=reranking`，`status=candidate_testing`，gate_log **只有 scope**（1 文件） | `candidates#31` |
| #30 归属观测 | **0 条**（`details_decision_metadata` 含 `candidate-30` 的观测数 = 0） | 观测表全表扫描 |
| 历史候选归属观测 | **8 条**（`candidate-4` ×1、`-5` ×2、`-7` ×2、`-9` ×3） | `passages_question_set_version` 分布 |
| 观测总数 | 207（`insufficient_evidence` 199 + `NULL` 8；route：`fixed` 197 / `error` 8 / `feedback` 2） | `failure_observations` |
| 灰度硬上限 | **20**（`.env: RAG_EVOLUTION_CANDIDATE_CANARY_PERCENT=20`；`config.py` 默认 **0**） | `.env` / `config.py:237` |
| 8 个测试账号 | `users` id 5–12，创建于 2026-10-18 16:16–16:21 | `users` 表 |
| 引用总量 | conversations 4 / chat_messages 8 / feedback 3 / document_permissions 3 / document_versions 3 / org_people 4 / org_audit_events 11 | §2.1 矩阵 |
| `kb_shares` | 对这 8 个账号 **0 行**（全表 5 行都属 `user_id=2`） | `kb_shares` |

---

## 1. 候选 #30 / #31 决策表

### 1.1 现状（查到的，不是推断的）

| 项 | #30 | #31 |
| --- | --- | --- |
| artifact / target | `jev_question_set` / `passages` | `code_patch` / `NULL` |
| status / layer | `canary` / `NULL` | `candidate_testing` / `reranking` |
| confidence / 来源观测 | `low` / obs#91 | `high` / obs#169 |
| created → updated | 2026-10-14 09:38 → 2026-10-19 09:10 | 2026-10-19 16:19（未再更新） |
| 内容 | 只改 `contains_answer_evidence` 的 instructions/criteria（c0–c11 共 12 问） | 只改 `rag/reranker.py` 两处：请求 `len(documents)` 而非 `min(top_k,…)`；返回不再 `[:top_k]` |
| rollback_ref | `passages-v1` + `payload_sha256=7cf5ab61…6ccbf` | `base_commit=5adfbb0` |
| gate_log | scope(accepted, 10-14 09:38) → benchmark(passed) → canary(passed, `percent=0`, 10-19 09:10) | scope(accepted, `code_patch/reranking files=1`) |
| 审计行 | generate(#110) → advance 层2(#111, actor 1) → advance 灰度 0%(#112, actor 2) → canary(#113, actor 2) | generate(#114) |
| 归属聚类 | cluster 3（`kb_accessories` / `generation` / `recall_miss`，`member_count=133`，`confirmed_layer=reranking`） | 同上，cluster 3 |

补充事实：#30 的措辞修正**已经以提交 `d29e81d` 进入生产代码**（见 handoff-2026-10-21 §1）。
也就是说，这条候选行现在记的是"已经落地的改动"，不是"待验证的改动"。

### 1.2 决策表

> "不代做"列写的是管理员要敲的命令；我**没有执行**其中任何一条。

| 候选 | 选项 | 具体动作 | 推荐 | 风险 / 代价 |
| --- | --- | --- | --- | --- |
| **#30** | **保留追溯**（维持 `canary@0%`，不动） | 无 | 次选 | 追溯完整；但"在观察"这个说法是假的，且继续占用"同一时间仅一个候选在线"的名额（`api.py:837-841`） |
| #30 | **推进层 2** | — | **不适用** | 层 2 已于 2026-10-14 通过（+2.3pp / +5.0pp，44 例 ×3 轮），重跑只会复现同一组数字 |
| #30 | 层 4 放量（`percent=X`，X ≤ 20） | `POST /knowledge-operations/evolution/candidates/30/canary {"percent":5}` | **不推荐**（见 §3.3） | 样本只来自**失败**观测；无回退率指标；改动已在生产代码里，无法把效果归因给候选 |
| #30 | **明确关闭观察窗**（层 5 rollback） | `POST /knowledge-operations/evolution/candidates/30/rollback` → `rolled_back`，审计 note 写"0% 无样本关闭；措辞已由 d29e81d 进生产" | **★ 推荐** | `rolled_back` 是终态：日后要再放量必须**重新生成**候选（`canary` 端点只接受 `awaiting_verification`/`approved_for_canary`） |
| #30 | 退回 `rejected` | **没有正规入口**（问题集候选没有 reject/decision 端点）。理论上 `POST /code-candidates/30/decision {"action":"reject"}` 能写成功（该端点未按 `artifact_type` 过滤），但那是**误用代码补丁端点**，会在轨迹里落一条 `review` 门禁 | **不建议** | 语义污染：一条问题集候选的门禁轨迹里出现代码补丁审核条目，后面读轨迹的人会误判 |
| **#31** | **保留追溯**（继续挂 `candidate_testing`） | 无 | **不推荐** | 它是 30 条里唯一"待审"的补丁候选；挂着就等于把一个已验证的根因（`reranking`）无限期悬置 |
| #31 | **推进层 2**（沙箱门禁） | 先 `python scripts/run_patch_sandbox.py --candidate-id 31 --dry-run`；确认结论后再去掉 `--dry-run` 写回 `gate=sandbox` | **★ 推荐（先 dry-run）** | 沙箱只证明"能应用 + 目标测试 + 固定回归网"；见 §1.4，它可能**证明不了任何效果** |
| #31 | 退回 `rejected` | `POST /knowledge-operations/evolution/code-candidates/31/decision {"action":"reject"}`（`candidate_testing → rejected` 是合法转移） | 备选：dry-run 证伪后用 | 丢掉一条真实 patch，但轨迹保留、聚类的 `confirmed_layer` 不受影响 |
| #31 | 推进到 apply | — | **不允许** | `approve_for_apply` 目标是 `approved_for_canary`，不在 `candidate_testing` 的转移表里 → 409（`api.py:2095-2101`） |

**给管理员的一句话**：#30 建议**关闭观察窗**（保留行、不删数据）；#31 建议**先 dry-run 沙箱**，
再根据"它究竟改变了什么"决定 `rejected` 还是继续。两条都不需要动 `evolution.db` 的原始数据。

### 1.3 执行 #31 沙箱的硬前置

1. **主树必须干净**：`scripts/run_patch_sandbox.py` 从 `repo_root` 建两棵 worktree。
   审计期间工作流 A/B/C 已在改 `rag/pipeline.py` / `candidate_gates.py` / 两个测试文件，
   现在它们已提交（`HEAD=28ca6e1`）、工作树只剩本文件未跟踪 → **条件已具备**。
   跑之前请再确认一次 `git status --porcelain` 只有本文件。
2. **不要带 `--fixture`**：带 fixture 需要凭据，且"两侧同样坏"会得出"无回归"的假绿
   （脚本 docstring 已写明）。不带 fixture 时层 2 回放不跑，门禁会如实记"未测量"。
3. `--data-snapshot` 只在需要回放时给；不给也不会伪造对比。

### 1.4 我对 #31 预期效果的静态核对（**风险提示，不是结论**）

#31 的 `expected_effect` 写的是"`passage_judged`/`passage_evidence` 大于 0"。按 `28ca6e1` 的代码（行号已核对）：

- 判定统计量来自 `self.jev.select(...)`（`rag/pipeline.py:954`），它的候选直接来自
  `_retrieve_visible_candidates(top_k=retrieve_top_k)`，**不经过** `Reranker.rerank()`。
- `Reranker.rerank()` 只有两个调用点：`_prepare_context_standard`（`:714`）和 Jev 不可用时的
  回退（`:958`）。两处后面都紧跟 `_drop_injected_chunks(...)[:top_k]`（`:718`、`:962`），
  `top_k` 默认 5（`config.py:151`），而候选池是 `top_k * 3 = 15`（`:81`、`:707`）。
- 于是：不改代码时进生成的是"重排前 5 名"；改了代码进生成的仍是"前 5 名"——**只有当 ACL/
  注入过滤把前 5 名里的某些块剔掉时**，扩容后的列表才可能补位。`obs#169`（`jev_route=legacy`，
  `passage_judged=0`）恰好说明 legacy 路径**根本不做段落判定**，`passage_judged > 0` 在这条路径上
  不可能出现。

**推论**：这条补丁的价值边界比它的 rationale 窄得多。**先 dry-run 拿到 `applied / tests passed`
和门禁 detail，再判断**；如果沙箱通过但 `metrics` 里没有任何判定量变化，就按 §1.2 走 `rejected`，
并在 note 里写明"沙箱通过但效果未证实"。

### 1.5 观察窗里的另一个事实：#30 跳过了层 3

`docs/evolution-candidates.md` §3/§4 的五层是 scope → benchmark → shadow → canary → accept/rollback。
#30 的 gate_log 没有 `shadow` 条目。这不影响本决策（层 3 本来就不阻塞），但**发布流程的一致性**
值得记一笔：一个未跑旁路对比的候选直接进了灰度。

---

## 2. 4 个 `alice_*` + 4 个伴生 `admin_*`

### 2.1 引用矩阵（`data/users.db`，逐表逐列 `count(*) where <col> in (5..12)`）

| 账号 | id | `is_admin` | conversations | feedback | document_permissions(owner) | document_versions(uploaded_by) | org_people | org_audit_events(actor) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `admin_81018c` | 5 | 1 | 0 | 0 | 1（doc#26） | 1 | 1（`P-ADMIN-81018c`） | 3 |
| `alice_81018c` | 6 | 0 | 1（conv#2） | 0 | 0 | 0 | 0 | 0 |
| `admin_a33187` | 7 | 1 | 0 | 0 | 1（doc#27） | 1 | 1（`P-ADMIN-a33187`） | 3 |
| `alice_a33187` | 8 | 0 | 1（conv#3） | 1（fb#6） | 0 | 0 | 0 | 0 |
| `admin_1c0f75` | 9 | 1 | 0 | 0 | 1（doc#28） | 1 | 1（`P-ADMIN-1c0f75`） | 2 |
| `alice_1c0f75` | 10 | 0 | 1（conv#4） | 1（fb#7） | 0 | 0 | 0 | 0 |
| `admin_7d175f` | 11 | 1 | 0 | 0 | 0 | 0 | 1（`P-ADMIN-7d175f`） | 3 |
| `alice_7d175f` | 12 | 0 | 1（conv#5） | 1（fb#8） | 0 | 0 | 0 | 0 |
| **合计** | | | **4** | **3** | **3** | **3** | **4** | **11** |

**下游子行**（随父行级联，不计入上表）：`chat_messages` 8 行（每会话 2 行）、
`answer_citations`/`structured_query_runs` 对这些账号为 0。

**确认无关的表**（全表计数为 0 或无匹配）：`kb_shares` 0、`document_shares` 0、
`data_sources` 0、`analysis_cards` 0、`knowledge_tasks.assigned_to` 0、
`spreadsheet_edit_sessions.actor_user_id` 0、`kb_org_grants` 0 行（全表空）、
`org_seed_state` 0 行。

**伴生组织行**：4 个 `org_units`（`ORG-81018c` / `ORG-a33187` / `ORG-1c0f75` / `ORG-7d175f`，
均 `name='Test Org'`, `kind='company'`）、4 个 `org_memberships`（`is_primary=1`）、
4 个 `org_manager_scopes`。4 个 `org_people.active=0`。

**文件残留**：`data/document_versions/{26,27,28}/1/doc_spec_*.txt`（各 252 B，仍在盘上）；
`data/upload/` 下无匹配文件；`qdrant_data/collection` 5 个集合里**没有** `kb_techspecs_*`
（历史清理生效）；`wiki_pages` = 0。

### 2.2 删除顺序（`PRAGMA foreign_keys=ON` 下的硬约束）

依据：应用连接在 `rag/db/connection.py:60` 显式 `PRAGMA foreign_keys = ON`（读写连接都开）。
外键实测（`pragma foreign_key_list`）：

| 子表.列 | → 父表 | on_delete | 后果 |
| --- | --- | --- | --- |
| `conversations.user_id` | `users.id` | NO ACTION | **挡住删 users** |
| `chat_messages.conversation_id` | `conversations.id` | NO ACTION | **挡住删 conversations** |
| `feedback.user_id` / `feedback.message_id` | `users.id` / `chat_messages.id` | NO ACTION | **挡住删 users 和 messages** |
| `document_versions.uploaded_by` | `users.id` | NO ACTION | **挡住删 users**（即使文档已删，也要先删版本行） |
| `document_versions.document_id` | `document_permissions.id` | CASCADE | 删文档即级联版本 |
| `org_people.user_id` | `users.id` | NO ACTION | **挡住删 users** |
| `answer_citations.assistant_message_id` | `chat_messages.id` | CASCADE | 删消息即级联引用 |
| `structured_query_runs.user_id` / `.assistant_message_id` | `users.id` / `chat_messages.id` | SET NULL | 不挡 |
| `org_audit_events` / `org_memberships` / `org_manager_scopes` / `org_units` | — | 无外键 | 留孤儿行，需显式删 |

**顺序（一对账号内，事务执行）**：

```sql
-- 0) 先备份并校验：scripts/backup.py create / verify（见 2.3）
-- 1) 反馈（user_id + message_id 双 NO ACTION，必须最先）
DELETE FROM feedback            WHERE user_id IN (:admin, :alice);
DELETE FROM feedback            WHERE message_id IN (SELECT id FROM chat_messages
                                WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = :alice));
-- 2) 消息与引用（answer_citations 走 CASCADE，显式删更可读）
DELETE FROM answer_citations    WHERE assistant_message_id IN (SELECT id FROM chat_messages
                                WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = :alice));
DELETE FROM structured_query_runs WHERE assistant_message_id IN (SELECT id FROM chat_messages
                                WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = :alice));
DELETE FROM chat_messages       WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = :alice);
-- 3) 会话（user_id NO ACTION，必须在 users 之前）
DELETE FROM conversations       WHERE user_id = :alice;
-- 4) 文档（无外键入 users；版本行随 document_id CASCADE，且版本行的 uploaded_by 是 NO ACTION）
DELETE FROM document_permissions WHERE owner_id = :admin;
-- 5) 组织行（org_people 是 NO ACTION；其余为孤儿行）
DELETE FROM org_manager_scopes  WHERE person_code = :person_code;
DELETE FROM org_memberships     WHERE person_code = :person_code;
DELETE FROM org_people          WHERE person_code = :person_code;   -- 或 WHERE user_id = :admin
DELETE FROM org_units           WHERE code = :org_code;
-- 6) 审计行（无外键，可不删；删则与 scratch 清理脚本一致，但会失去登录/KB 创建的历史证据）
DELETE FROM org_audit_events    WHERE actor_id IN (:admin, :alice);
-- 7) 最后才是账号
DELETE FROM users               WHERE id IN (:admin, :alice);
```

**为什么是这个顺序**：第 1 步到第 3 步解掉 `user_id / message_id` 的 NO ACTION；
第 4 步解掉 `document_versions.uploaded_by`；第 5 步解掉 `org_people.user_id`；
第 7 步之前所有指向 `users.id` 的 NO ACTION 引用必须全部归零，否则 `DELETE` 直接报
`FOREIGN KEY constraint failed`——**在事务里被拒绝，等于什么都没删**。

### 2.3 删除前置条件（缺一不可）

1. **已验证备份**。仓库自带的清理脚本把这条做成硬门槛：
   `scripts/cleanup_test_artifacts.py` 要求 `--verified-backup-id` 匹配
   `\d{8}T\d{6}Z-[0-9a-f]{12}` 且 `verify_backup(...).status == "verified"`（`scripts/cleanup_test_artifacts.py:36-42`）。
2. **写窗口**。服务现在在 8080 跑（`/health` healthy），`users.db` 走单写连接 + WAL。
   8 行用户 + 30 余行子行的删除建议在**停服或维护窗**做，避免与 WAL 检查点/在途请求抢写锁。
3. **现成工具清不掉这 8 个账号**：`rag/test_artifact_cleanup.py:8` 的
   `_TEST_USER = ^_(?:test(?:_|$)|perm_|dl_test$|data_source_test_)` 只匹配**下划线前缀**，
   `alice_81018c` / `admin_81018c` 不在其中；`find_candidates()` 会返回空集。
   → 要么按 §2.2 手工执行，要么另开一个"扩正则"的小改动（属代码改动，本文不做）。
4. **`scratch/test_live_e2e_pipeline.py` 的 `_cleanup` 仍有缺口**（静态推断，未实跑）：
   它只删 `org_*` 行与 `users`（`:322-329`），**不删 `conversations` / `chat_messages` / `feedback`**，
   且整块包在 `contextlib.suppress(Exception)` 里、成功提示 `:330` 在 `with` 之外。
   在 FK ON 下 `DELETE FROM users` 会被 `conversations.user_id` 拒绝 → 整块回滚，但**照样打印
   "accounts/org rows … removed"**。这 4 对账号本身是 2026-10-18 16:16–16:21 留下的
   （`_cleanup` 补丁 19:23 才进 `fb68237`），成因在补丁之前；但**下一次运行仍会留下同类残留**。
5. **文件与孤儿行**：删行不删盘（`data/document_versions/26..28`），也不删 Qdrant 集合
   （本次已确认无 `kb_techspecs_*`）。删完应在同一变更里清这三个目录。

### 2.4 是否建议保留为测试夹具？

**不建议保留。** 理由：

1. 它们的来源脚本**自己就会创建**（`uuid4().hex[:6]` 后缀，`scratch/test_live_e2e_pipeline.py:41-45`），
   不需要一份常驻夹具；保留反而让"每次运行一对新账号"变成"账号表里长草"。
2. 它们没有任何不可再生的状态：4 个会话标题全空、8 条消息、3 条 `not_useful` 反馈、
   3 个 252 B 的 `doc_spec_*.txt`——都是脚本产物。
3. 4 个 `admin_*` 是 `is_admin=1` 的**静态权限残留**（见 §2.5），留着就是留一个隐患。
4. 真正需要夹具的场景（权限/多租户）已被 `tests/` 里的 `make_user("alice")` 覆盖，
   那些用临时库、不写生产 `users.db`。

**若管理员坚持保留**（例如还要复跑同一套 E2E）：至少要做三件事——
轮换 4 个 `admin_*` 的口令、把 `org_people.active` 处置成明确状态、在 `docs/` 里登记
"这 8 个 id 是夹具，勿作业务账号"。

### 2.5 安全附注（本次最重要的一条）

- **登录只查 `users` 表**：`rag/repositories/user_repo.py` 的 `authenticate()` 只比对
  `users.password`；`rag/routers/auth.py:112-123` 在密码通过后再查 `org_people` profile，
  `profile.active=False` 才拒绝并记 `account_inactive`。
- 因此：4 个 `admin_*`（`org_people.active=0`）**登录被拦**——有实测痕迹：
  `org_audit_events` 在 2026-10-21 17:07:09 / 17:07:10 对 actor 5、7 各记一条
  `login_failed / account_inactive`。
- 但 4 个 `alice_*` **没有 `org_people` 行** → `profile is None` → **不受这条检查约束**，
  只要口令正确就能登录。它们的 `password_changed_at` 都是 2026-10-19 09:11:24（同一秒批量写入），
  现值**未验证**。
- 4 个 `admin_*` 的口令**仍是 `Password123!`**：我按 `rag/auth.py:56-67` 的
  `pbkdf2_hmac(sha256, salt, 600000/260000)` 对 `users.password` 做了只读比对，
  4 条全部命中；该常量就是 `scratch/test_live_e2e_pipeline.py:45` 里的 `password`。
  即：**弱口令 + `is_admin=1` 的账号行仍在表里**，只靠 `account_inactive` 挡在登录口。
- 处置建议：这 8 行按 §2.2 删除即可一次性消掉上述两点；若暂不删，**先轮换 4 个 `admin_*` 口令**
  （`POST /users/{id}/reset-password`，或让管理员改）。

---

## 3. canary 观察窗：#30 为什么不可能有样本

### 3.1 结构性零样本（代码证据）

`rag/evolution/rollout.py` 的语义是"百分比 0 = 不注册"：

- `activate(..., percent)`：`if percent > 0: register_candidate_question_set(...)` / `else:
  unregister_candidate_question_set(...)` 并**丢弃内存条目**（`:171-184`）。
- `should_use_candidate`：`bucket(key) < live.percent`（`:238-248`），`_live_rollout()` 只挑
  `percent > 0` 的条目（`:331-336`）。
- `provide`：`live is None` 时直接返回**基线问题集**（`:250-262`）。

⇒ `percent=0` 时 false 分支恒成立，**任何请求都不可能命中候选 #30**。这不是"样本还没攒够"，
是"没有任何一条路径会产生样本"。

### 3.2 观测侧也确认了 0

- 观测表里能归因到候选的唯一字段是 `details_decision_metadata.passages_question_set_version`
  （`rag/pipeline.py:968` 写回；`details_jev_question_set_version` **只记 preflight 版本**，
  这是 `docs/evolution-candidates.md` §5.1 已登记的残余限制）。
- 全表扫描：`passages-v1+candidate-*` 共 **8 条**（candidate-4/5/7/9，都是 2026-09-29~30 的历史灰度），
  **candidate-30 为 0 条**。
- 而且观测表**只收失败请求**（`rag/evolution/recorder.py`，`route=blocked` 直接不记），
  所以即使放量，能看到的也只是"失败样本"，**没有分母**。

### 3.3 三个选项

> 端点前缀都是 `/knowledge-operations/evolution`（`rag/evolution/api.py:72`），下表只写尾部。

| 选项 | 做法 | 收益 | 代价 |
| --- | --- | --- | --- |
| A. 放量到 X% | `POST /candidates/30/canary {"percent":X}`，X ≤ **20**（`.env` 硬上限；超限 400） | 理论上能拿到在线样本 | 只有失败观测、无回退率指标、改动已在生产代码里 → **样本不可归因**；且占住唯一灰度名额 |
| B. **明确关闭观察窗** | `POST /candidates/30/rollback` → `rolled_back`（0% + 反注册 + 审计），**保留候选行与全部 gate_log** | 状态与事实一致；释放灰度名额；追溯不丢 | 终态，日后放量需重新生成候选 |
| C. 维持 `canary@0%`，只在文档标注"观察窗已失效" | 不动 DB | 零风险 | 文档与 DB 不一致，下一次审计会再把 #30 当"在观察" |

**推荐 B**，理由排序：
1. #30 的实质内容**已经是生产代码**（`d29e81d`），灰度的对象只剩"问题集数据"，放量测的是
   一个已经落地的改动，归因链断了；
2. 0% 下再观察一年也是 0 条样本，"在观察"这句话本身失真；
3. `canary_rollback_rate` 这个指标**要到第 5 阶段才实现**（`rag/evolution/metrics.py:47/98-100`，
   当前恒不返回），观察窗没有配套的量化口径。

**若管理员选 A**，建议 X = **5**（不是 20）：理由是历史灰度在同等规模下只沉淀了 8 条归属观测，
5% 与 20% 都会落在"个位数样本"区间，20% 只是把无效暴露放大四倍；同时**先补分母**
（`/metrics` 或至少按 KB 统计请求量），否则 5% 也读不出结论。

### 3.4 无论选哪个，都缺一块：观察窗没有量化口径

- `canary_rollback_rate`：Phase 5 才下发，当前 `rates` 里没有这个键。
- `manual_verification_pass_rate` / `regression_count` / `candidate_cost`：同样是第 4/5 阶段，
  当前恒不返回（`metrics.py:83-97` 的定义里写得很明确）。
- 现有可用的只有：`gate_log`（离线基准，可控样本）+ `failure_observations`（在线，只有失败）。

⇒ 结论：**"在线观察窗"在本仓库当前是不成立的机制**。要么承认它只是"登记状态"，要么先补
Phase 0 的观测（§4）再谈放量。

---

## 4. 架构阻塞的最小下一步（`UPGRADE.md` Phase 0 / Phase 1）

### 4.1 现状核对（本次实测，不是抄文档）

| 组件 | 核对结果 | 证据 |
| --- | --- | --- |
| `/health` | 有，返回 `qdrant/sqlite = ok` | `rag/routers/system.py:22`；本次只读探测 |
| `/ready` | **没有**（`rag/routers/` 全部路由里无 `/ready`） | 目录扫描 |
| `/metrics` | **没有**；仓库无 prometheus / opentelemetry 依赖 | `requirements.lock` 只有 `qdrant-client` / `redis` |
| Qdrant | 本地嵌入式 `QdrantClient(path=settings.qdrant_path)`，进程级独占 | `rag/vector_store.py:37-41` |
| SQLite | 单写连接 + 线程本地只读；两个连接都 `PRAGMA foreign_keys=ON` | `rag/db/connection.py:47-62` |
| Redis | 依赖在（`redis==5.3.1`），compose 里有 `redis` 服务；仅用于 LLM 池并发门 | `docker-compose.yml`、`config.py:92` |
| Postgres | **无驱动**（`requirements.lock` 无 psycopg/asyncpg），无迁移目录 | 依赖清单 |
| 对象存储 | **无 SDK**（无 boto3/minio），文件仍在本机 `data/upload` 等 | 依赖清单 |
| Worker | 素材/演化 worker 在 API 进程 lifespan 内启动 | `rag/api.py:123-135`、`:214-253` |
| Docker | 本机可用：`Docker version 29.8.0` | `docker --version` |

### 4.2 两栏：本周可做 / 需要环境

> 口径：**本周可做** = 不需要新主机、新外部依赖，在本机（现有进程或本机 Docker）就能推进并验收；
> **需要环境** = 需要新的服务实例/驱动/凭据，先要环境才能动手。

| 阻塞项 | 本周可做 | 需要环境 |
| --- | --- | --- |
| **Phase 0 观测基线** | ✅ 固化 `scripts/locustfile.py` 的压测档案（固定 8 用户 / 60s / 目标 KB / `--user-id`），把 P50/P95/P99 + 错误率写成可复跑的一条命令；补 `/ready`；补只读 `/metrics`（请求量、延迟、检索/重排/生成分段、Qdrant/SQLite/Redis 健康、LLM 池排队）。**注意**：`UPGRADE.md` §3 的两次基线（10-21 `/query` P50 10.8s/P95 17.0s/P99 17.9s；10-22 locust 73 请求 0 失败 P50 530ms/P95 13s/P99 19s）是**不同负载**，不能互相替代 | ❌ 不需要 |
| **Phase 1 Qdrant server** | ✅ 起一个 `qdrant` 容器（本机 Docker 可用）；加 `RAG_QDRANT_URL`/`RAG_QDRANT_API_KEY`，**默认为空 → 保持本地 `path` 模式**（向后兼容，回滚只需清空变量）；`rag/vector_store.py` 按 URL/Path 二选一 | ⚠️ 部分：迁移 5 个集合（snapshot/restore 或 scroll+upsert）并做"双进程并发 + top-5 与迁移前一致"的验收，**需要独占窗口停服务**（现在 8080 在跑），且需保留旧目录只读副本 |
| **PostgreSQL** | ❌ 做不了（无驱动、SQL 仍是 SQLite 方言：`INSERT OR IGNORE`/`lastrowid`/`PRAGMA`/FTS5，`docs` 已盘点 56 张表） | ✅ 需要一台 pg（或容器）+ 新迁移目录 + 影子读比对环境；按 `UPGRADE.md` Phase 2 是 **1–2 周**的工作量 |
| **Redis 共享缓存/锁** | ⚠️ 只能做"不依赖多副本"的那半：改写缓存后端接口化（`memory` 默认 / `redis` 可切，故障降级内存）、限流指标。**答案缓存不能开**（ACL 未版本化，会跨权限返回旧答案） | ✅ "共享"这件事本身需要多副本环境才有意义；分布式锁（备份/演化/素材 cleanup）同理 |
| **对象存储** | ❌ 做不了（无 SDK；`data/upload`、`data/document_versions`、`data/structured_datasets` 是本地路径） | ✅ 需要 MinIO/S3 桶 + 凭据 + DB 增加 `storage_key` 的迁移；按 Phase 4 是 3–5 天 |
| **多副本 / 独立 worker** | ❌ 做不了（会直接失败：Qdrant 本地目录锁；worker 在 API 进程内会重复跑） | ✅ 需要 LB/TLS + 共享存储（依赖上面四项）+ 独立 worker 部署；按 Phase 5 是 3–5 天 |

### 4.3 本周的最小动作清单（按依赖排序，全部不需要新环境）

1. **停服 → 跑一次可复现压测 → 记 P99 到文档**（Qdrant 独占，必须停服务）。
   这是 Phase 0 唯一有硬验收的一步。
2. **加 `/ready`**（存活与就绪分离）：`/health` 现在把"进程活着"和"依赖可用"混在一起，
   多副本后无法摘流。
3. **加只读 `/metrics`**：至少 4 个面——请求量/延迟、检索/重排/生成分段、Qdrant/SQLite/Redis 健康、
   LLM 池排队。没有它，Phase 1 之后的每一步都只能靠"感觉没变慢"验收。
4. **Qdrant 配置面改造**（`RAG_QDRANT_URL`，默认空）：先把"能切/能回滚"做出来，
   迁移放独立窗口。
5. **不要动**：SQLite 共享盘、ACL 未版本化时开答案缓存、Qdrant 本地模式扩副本（`UPGRADE.md` §7 已列为禁止项）。

---

## 5. 本次明确不改的清单

1. **不改 `data/evolution.db`**：#30/#31 的状态、百分比、`gate_log`、审计行一律不动；
   不跑 `canary` / `rollback` / `decision` 端点；不删候选、不改聚类。
2. **不改 `data/users.db`**：不删 8 个账号及其 30 余行子行；不改 `is_admin`、不改
   `org_people.active`、不轮换口令；**没有执行 §2.2 的任何一条 SQL**（只作为待办写在这里）。
3. **不改代码**：`rag/vector_store.py`（Qdrant URL 分支）、`rag/evolution/metrics.py`
   （`canary_rollback_rate`）、`rag/test_artifact_cleanup.py`（账号匹配正则）、
   `rag/routers/system.py`（`/ready`、`/metrics`）、`scratch/test_live_e2e_pipeline.py`
   （`_cleanup` 缺口）**都只是建议**，本次一行未改。
4. **不改其它文档**：`UPGRADE.md`、`docs/evolution-candidates.md`、
   `docs/superpowers/plans/2026-10-22-residual-audit.md` 均未修改；本文件是唯一新增。
5. **不动服务**：没有停 8080、没有释放 Qdrant 目录锁、没有跑基准/沙箱/E2E（它们都要求独占
   Qdrant 或写库）。
6. **不提交 Git**：审计期间 `HEAD` 从 `d56d29a` 前进到 `28ca6e1`（那是 A/B/C 三个提交，
   不是我做的）；我这边没有 `git add` / `commit`，工作树新增的只有本文件。
7. **不动工作流 A/B/C 的改动**：`rag/pipeline.py`、`rag/evolution/candidate_gates.py`、
   `tests/test_pipeline.py`、`tests/test_evolution_candidate_gates.py` 以及两个新增测试文件
   是它们的产出；我只读、没碰，也没有为它们的改动做任何 review 结论。

---

## 6. 未验证项（不要当结论用）

| 项 | 为什么没验证 |
| --- | --- |
| 4 个 `alice_*` 的当前口令 | 只确认它们**不等于** `Password123!`；实际值无法从 pbkdf2 反推 |
| 这 4 对账号残留的确切成因 | 创建时间 2026-10-18 16:16–16:21，`_cleanup` 补丁 19:23 才进 `fb68237`；两次运行与补丁的先后只能从时间推，没有运行日志 |
| `_cleanup` 在 FK ON 下是否真的会失败 | 静态推断（`conversations.user_id` NO ACTION + `DELETE FROM users` + `suppress(Exception)`），**没有实跑**（会写库） |
| 在线请求量 / 拒答率分母 | `evolution.db` 只收失败观测，没有分母；`/metrics` 不存在，无法取得 |
| 两个 API 进程能否同时打开 Qdrant | 没有试（需要起第二个进程；即使只读也可能抢目录锁） |
| `#31` 补丁的真实效果 | 只做了静态核对（§1.4）；要结论必须跑 `run_patch_sandbox.py --candidate-id 31 --dry-run`（主树现在只剩本文未跟踪，条件已具备，但我没跑——它会在两棵 worktree 里 `git apply` 并跑 pytest） |

---

## 附录 A：本次实际执行的只读查询

全部通过 `sqlite3.connect("file:<path>?mode=ro", uri=True)` 打开：**只读连接，没有执行
INSERT/UPDATE/DELETE，也没有设置任何写相关 PRAGMA**；`-wal`/`-shm` 由 SQLite 自行以只读方式处理。

```python
# A1 候选分布与 #30/#31 全行
select id,status,artifact_type,target,payload,gate_log,rollback_ref,rationale,expected_effect,
       confidence,diff,layer,test_paths,created_at,updated_at from candidates where id in (30,31)
select status,count(*) from candidates group by 1

# A2 候选审计轨迹
select * from evolution_candidate_audit where candidate_id in (30,31) order by id

# A3 候选归属观测（canary 样本数）
select count(*) from failure_observations where details_decision_metadata like '%candidate-30%'
json: details_decision_metadata -> passages_question_set_version  （全表 Counter）

# A4 观测/聚类规模
select count(*) from failure_observations
select details_answerability_kind,count(*) ... group by 1
select details_structured_query_kind,count(*) ... group by 1
select route,count(*) ... group by 1
select * from failure_observations where id in (91,169)
select id,member_count,confirmed_layer,status,pool from failure_clusters where id=3

# A5 账号与引用（users.db）
select id,username,permission_level,is_admin,created_at,password_changed_at from users order by id
-- 逐表逐列：select count(*) from <table> where <col> in (5,6,7,8,9,10,11,12)
--   命中列：conversations.user_id / feedback.user_id / document_permissions.owner_id /
--           document_versions.uploaded_by / org_people.user_id / org_audit_events.actor_id
pragma foreign_key_list(conversations|chat_messages|feedback|document_permissions|
                        document_versions|answer_citations|org_people|org_audit_events|
                        kb_documents|structured_query_runs|knowledge_tasks|material_audit_log)
select * from org_people / org_memberships / org_manager_scopes / org_units
select * from org_audit_events where actor_id in (5..12) order by created_at

# A6 口令常量比对（只读；按 rag/auth.py:56-67 的两个迭代数）
pbkdf2_hmac("sha256", b"Password123!", salt, 600000 | 260000) vs users.password

# A7 其它库
data/wiki.db:  select count(*) from wiki_pages            -> 0
data/users.db 文件系统核对：
  qdrant_data/collection/*                                -> 5 个，无 kb_techspecs_*
  data/document_versions/{26,27,28}/1/doc_spec_*.txt      -> 3 个，各 252 B
  data/upload/                                            -> 无 doc_spec_* 文件
```

服务与仓库状态（只读）：`curl http://127.0.0.1:8080/health`、`git log -1 --oneline`、
`git status --porcelain`、`docker --version`、`requirements.lock` 依赖检索。

**未写库的证据**：审计前后 `data/evolution.db`（1,175,552 B / 11:12:42）、`evolution.db-wal`
（107,152 B / 12:12:18）、`users.db`（598,016 B / 11:55:33）、`users.db-wal`
（24,752 B / 12:02:54）的**大小与 mtime 完全未变**；`git status --porcelain` 只有本文件一条
`??`。

## 附录 B：本次相关的代码/文档位置（`28ca6e1`，已逐条核对行号）

| 位置 | 用途 |
| --- | --- |
| `rag/evolution/rollout.py:171-262,331-336` | 0% = 不注册；唯一的在线分流语义 |
| `rag/evolution/api.py:787-880` | canary 端点：状态机、`同一时间仅一个`、硬上限 400 |
| `rag/evolution/api.py:923-940,2063-2128` | 代码补丁四种决策；`approve_for_apply` 的 409 守卫 |
| `rag/evolution/api.py:2131-2169` | 问题集候选 rollback（唯一的"关闭"入口） |
| `rag/evolution/models.py:80-96` | 状态转移表（含 `canary → rejected/rolled_back`） |
| `rag/evolution/metrics.py:47,98-100` | `canary_rollback_rate` 属第 5 阶段，当前不下发 |
| `rag/pipeline.py:707-723,954-962` | 候选池 15、`[:top_k]` 截断、判定输入不经过 legacy 重排器 |
| `rag/reranker.py:135-162` | #31 patch 的两个 hunk 现场 |
| `rag/db/connection.py:56-62` | 读写连接都 `foreign_keys=ON`、读连接 `query_only=ON` |
| `rag/routers/auth.py:112-123` | `account_inactive` 只对**有 org profile** 的账号生效 |
| `rag/test_artifact_cleanup.py:8,24-74` | 清理工具只匹配 `_` 前缀账号 |
| `scripts/cleanup_test_artifacts.py:36-42` | 清理的"已验证备份"硬门槛 |
| `scripts/run_patch_sandbox.py` | #31 层 2 的唯一写回途径（`--dry-run` 可先不写） |
| `scratch/test_live_e2e_pipeline.py:41-45,297-330` | 8 个账号的来源与 `_cleanup` 的缺口 |
| `docs/evolution-candidates.md` §3/§4/§5 | 五层语义、0% 语义、已知限制 |
| `docs/evolution-code-patches.md` §4/§5.1/§5.2 | 沙箱能证明什么、回归 A/B 归管理员 |
