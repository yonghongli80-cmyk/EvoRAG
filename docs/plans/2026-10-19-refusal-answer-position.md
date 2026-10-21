# 修复计划：把「证据不足就拒答」落成可测量的判据（2026-10-19）

> 起因：产品口径已定 —— **证据不足就拒答**。但当前基准里没有任何一档能表达"该拒答却没拒"
> 或"不该拒答却拒了"，于是这条决定无法验收。本计划只做加法：不改任何阈值、不删既有标签、
> 不动路由判定逻辑。

## 0. 探查结论（先摆事实，再决定改什么）

| 假设 | 探查结果 | 依据 |
| --- | --- | --- |
| 企业语料掉分是因为"拒答被记成路由错" | **不成立** | `ROUTE_EXPECTATIONS` 只有 4 个域，没有拒答档；`route_correct = route == expected_domain`。real-012/017/018/020 在路由上本就是对的 |
| 拒答信息是否本就可得 | **可得** | `prepared["decision"].kind` 在 `_finalize_rag_context` 里写入；`_reliability_payload()` 已把它透成 `answerability_kind`，SSE 的 `done` 事件里就在 |
| 层 2 门禁能否测出这类变化 | **不能** | `_aggregate` 只统计 route 与 top5；real-020 族标 `expected_source_names: []` → `top5_labeled=False` → 整条用例在门禁里只剩路由一项，而拒答不改变路由。这正是候选 #30 被判"+0.0pp"的原因 |
| 门禁是否有"零净变化"漏洞 | **有** | `route_delta_pp = 0.0` 时即使逐例有进有退也 pass（`test_route_delta_of_exactly_zero_pp_passes_even_when_cases_change` 钉着这个行为） |
| 证据门在真实端点可达 | **是** | 实测 `/query/stream`："公司明年的营收目标是多少亿元?" 与 "R50 电池价格是多少" 都返回 `insufficient_evidence` 固定文案，不是被别的前置分支短路 |

**根因一句话**：答案有没有被作答，从来不是一次被记分的事；它只作为 `decision.kind` 流过代码，
没有任何验收判据读它。

## 1. 设计（为什么这样改）

### 1.1 在用例上加"位置"，不动"路由"

新增用例字段 `expected_position`，而不是往 `expected_route` 里塞第五个值：

- `expected_route` 回答"哪条链路该接这个请求"（rag / structured / tool / clarification）；
- `expected_position` 回答"接住之后该不该作答"（**answer** / **refuse**）。

两者正交：一条用例可以同时要求"走 rag"且"必须拒答"，也可以"走 rag"且"必须作答"。
新增标签不会让任何既有标签失效，也不会改变任何既有用例的记分。

### 1.2 拒答的两种记分，都要能被看见

- **该拒答却作答** = 幻觉风险，单列计数 `refusal_escapes`，**一律算错**；
- **该作答却拒答** = 假拒答，单列计数 `refusal_misses`，**一律算错**；
- 两者都汇总成一条判据 `refusal`，阈值为 0（与 route 的 0.95 不同：这类错误没有"可容忍的几个"）。

真实性边界：`answerable_with_warning` 不算作答（那是有历史证据时的限定回答），
`clarification_required` / `permission_limited` 也不算作答——它们都是"没有给出答案"。

### 1.3 层 2 门禁加"拒答滥用"护栏

即使基准里一条实拒答用例都没有，候选也不该让"能答的问题变成拒答"。因此在门禁里加一条：
**候选侧在基线侧确实作答了的用例上拒答 → 判失败**。这条不看基准标签，只看两侧行为的差，
所以对 20 条固定 A/B 语料也生效。

## 2. 实施步骤（每步先写测试，再改实现）

### Step 1 — 数据模型与记分（`scripts/benchmark_jev_retrieval.py`）

1. 加常量 `ANSWER_POSITIONS = ("answer", "refuse")`；`BenchmarkCase` 加字段 `expected_position: str = ""`。
2. `load_cases`：缺省为空串 = 不记分；给了值就必须在 `ANSWER_POSITIONS` 内，否则 `ValueError`
   （与 `unknown expected_route` 同样的硬失败风格）。
3. `Sample` 加 `answerability: str | None`、`position_correct: bool | None`；
   `PathMetrics` 加 `refusal_escapes` / `refusal_misses` / `refusal_case_count` / `position_accuracy`。
4. `_sample_from`：`expected_position` 非空时，把 `outcome.answerability` 折算成 `answered: bool` 再比；
   空则 `position_correct=None`（不进分母）。
5. 验收加判据 `refusal`：有 position 用例时要求 `escapes == 0 and misses == 0`；
   一条都没有时该判据显示为 advisory，**不会**因为"没测"而失败。

### Step 2 — 采集层透出 answerability（同文件 `_prepare_new` 一带 + `PreparationOutcome`）

`PreparationOutcome` 加 `answerability: str | None = None`；`_prepare_new` 从
`prepared["decision"].kind` 取值（`_reliability_payload` 已证明该字段就在 prepared 上）。
旧路径同名字段留空——它不建 evidence bundle，没有这个判定，不能编。

### Step 3 — 用例标签（只加字段，不改路由、不改来源、不改 notes 里的证据）

| 用例 | 新标签 | 理由 |
| --- | --- | --- |
| `real-020` 找一下副厂配件明细 | `answer` | 表格里确有副厂配件型号行（本次已用 #30 措辞实测 11/12 块判为证据） |
| `real-012` 2025年招聘数据 | `refuse` | kb_hr 无招聘统计表 |
| `real-017` r50电池价格 | `refuse` | 价格列全为 `<金额>` 脱敏占位 |
| `real-018` r50原厂电池价格 | `refuse` | 同 real-017 |

### Step 4 — 门禁护栏（`rag/evolution/candidate_gates.py` + `scripts/benchmark_candidate_questions.py`）

1. `_SideOutcome` 加 `refused: bool`；`_run_side` 从 route 映射的 `answerability` 字段读
   （`insufficient_evidence` → True）。**注意**：`fixed` 这一路由既是拒答也是澄清文案，
   所以不能靠 route 猜，必须读 answerability。
2. 新增 `refusal_escape`：基线侧 `refused=False` 且候选侧 `refused=True` → 记入 `refusal_escapes`，
   非空即判失败（理由行写清用例编号）。
3. `stats` 加 `refusal_escapes` / `refusal_misses`（后者 = 基线拒、候选答），
   `refusal_misses` 只记录不判死：那是候选"更少拒答"，可能需要人看。
4. `build_gate_callables.route()` 把 `answerability` 一并返回。

### Step 5 — 文档

1. 更正交接文档 §2.1 的"拒答被记成路由错"表述（与代码不符），写清真实原因是三条相位叠加。
2. 在 `docs/jev-benchmark.md` 记录新字段与判据口径。
3. 记录本轮的两个新坑：`session_id` 复用会污染验证结论；`scratch/` 脚本会让 `ruff` 变红（产品代码不受影响）。

## 3. 验证清单（做完才算完成）

- 每条改动先有失败的测试，再有通过的实现；
- `pytest -q -p no:cacheprovider` 全量绿；`ruff check .` 全绿（不含未入库的 scratch）；
- `scripts/benchmark_jev_retrieval.py --cases benchmarks/real-cases-v1.json --runs 3 --user-id 1`
  能看到 `refusal` 判据与新计数，且 real-020 的 position 结论与实际一致；
- 层 2 门禁：用一个"故意把 contains_answer_evidence 收紧"的内联 payload 跑一次，
  必须被 `refusal_escape` 拦下（阴性对照，证明这条护栏有牙）。

## 4. 明确不在本计划内（需另立决定）

- 观察到的 `_prepare_context_standard` **无来源兜底**（检索为空时直接返回固定拒答，跳过证据门）：
  未做改动，因为"检索为空"与"证据不足"在可靠解释上是否该区分，属于产品口径；
- 层 2 门禁"零净变化也 pass"的漏洞：它被现有测试钉住，改它属于弱化/加强门禁语义，需单独批准；
- 放开候选 #30 的灰度百分比：措辞已进生产，候选留在 0% 供追溯，不另行放量。

---

## 5. 实施记录（2026-10-19，已完成）

### 5.1 落地内容

| 文件 | 改动 |
| --- | --- |
| `scripts/benchmark_jev_retrieval.py` | `ANSWER_POSITIONS`、`BenchmarkCase.expected_position`、`PreparationOutcome.answerability`、`Sample.{answerability,expected_position,answered,known_unanswered,position_correct}`、`PathMetrics.{refusal_case_count,refusal_escapes,refusal_misses,position_accuracy}`、`_answered()`、`_answerability_kind()`、`_refusal_check()` |
| `benchmarks/real-cases-v1.json` | 4 条用例加 `expected_position`（real-020=answer；real-012/017/018=refuse），**路由标签、来源标签、既有 notes 事实未改** |
| `rag/evolution/candidate_gates.py` | `_SideOutcome.answered`、`_answered_from()`、`refusal_escapes`（判失败）/`refusal_misses`（仅记录）、逐例 `refusal_escaped`/`refusal_recovered` |
| `scripts/benchmark_candidate_questions.py` | 门禁 `route()` 透出 `answerability`，并把结构化澄清折算成"未作答" |
| `tests/test_jev_benchmark_position.py`（新） | 13 条，覆盖加性、两个方向的错、明文/澄清边界、崩溃不重复记错、advisory 语义 |
| `tests/test_evolution_candidate_gates.py` | +3 条拒答护栏测试；键集合常量同步 |
| `tests/test_jev_benchmark.py` | 单轮档 advisory 用例改为精确断言（`refusal` 是唯一例外） |

### 5.2 实测

**正例（真机，`--runs 1`）**：`[PASS] refusal: answer position 1.000 over 4 sample(s);
answered where a refusal was required: 0, refused where an answer was required: 0`。
同一轮的 route 为 0.792（FAIL），说明新判据与路由判据互不干扰。

**阴性对照（同一条链路强制关掉 Jev 判定）**：位置准确率 **0.250**，报告
`answered where a refusal was required: 2, refused where an answer was required: 1`，
`refusal in failures: True`。**这条判据会失败**，不是永远 PASS 的摆设。

### 5.3 探查中发现、必须先于本计划修正的语义问题

第一版把"没有 `decision`"直接当成作答。阴性对照因此只报 1 条 escape，而实际有 2 条——
`real-020` 在结构化路由上落到**结构化澄清**（5 个来源、无 `decision`），被误记成"作答了"。
澄清的答案文本是"请求参数/意图不明确，需要您进一步说明"，**不是回答**。已改为：
结构化澄清记为**已知未作答**并进分母；而"既无判定、又不是已知未作答"的形态（带标签时）
直接抛 `ValueError`，不替它下结论。

### 5.4 验证状态

- 受影响测试：`test_jev_benchmark_position` / `test_jev_benchmark` / `test_evolution_candidate_gates` /
  `test_benchmark_candidate_gate` 共 **141 passed**；
- `ruff check rag/ scripts/ tests/ benchmarks/`：**All checks passed**；
- 全量套件结果见提交说明。

### 5.5 本轮剩下的、需要产品决定的（本次观测到的新证据）

1. **`real-017`（r50 电池价格）在真机上被判 `answerable` 并给出 7 个来源**：价格为 `<金额>` 占位符，
   这是**该拒答却作答**的潜在幻觉口，属于本判据要抓的问题，但要不要把它提到优先级最高由产品定。
2. **`real-020` 稳定落到结构化澄清**：证据在文档里，却被一次"请指明字段"挡住。按本计划它现在
   被正确记为**假拒答**（不再被误记成"作答"）。修法方向是让结构化澄清在检索有来源时让位，
   这条策略已存在于代码（`structured_clarification` 让位逻辑），但它在这条问句上没有生效——
   属于新的检索/路由缺陷，不在本计划范围。
3. **`real-012/018` 的判据在抖动**：三轮里 `insufficient_evidence` 与 `answerable` 交替出现，
   与判定器逐轮抖动是同一个根因。

---

## 6. 后续更正与追加（2026-10-21）

### 6.1 对 §5.5 第 2 条的更正（我的归因错了）

"`real-020` 稳定落到结构化澄清"**不成立**。逐层追踪（`_attempt_structured_query` →
`_is_structured_clarification` → `_finalize_rag_context` 全部打点）显示：

- `real-020` 的结构化协调器返回 **`not_applicable`**，`_attempt_structured_query` 因此返回
  `None`——它**根本没有进入**"结构化澄清"这一支；
- 我在 `diag_escape.py` 里看到的 `route=clarification` 是 **Jev 预判**的澄清路由
  （`_fixed_decision(clarification_required)`），不是结构化澄清。两者同名不同源，我读错了。

`real-020` 的真实形态是：预判判 `structured` 但置信度不足（0.63~0.67 < 0.75）→ 降级旧链路 →
检索 → 证据门。实测（服务在跑、完整混合检索）**1 次拒答 / 2 次作答**，不稳定。

### 6.2 顺手查出的三个真缺陷（都已修）

1. **澄清被拒答顶掉**（`89157da`）：丢弃判据是 `not prepared.get("sources")`，而 Jev 路径的拒答
   **本来就带来源**（引用兜底）⇒ "有来源"被当成"检索赢了"。实测「镜头价格合计」：规划器要求
   澄清、检索 7 个来源、证据门拒答 ⇒ 用户看到"现有可见资料不足"。改为读 `decision.kind`
   （`_produced_answer`），两条路径（旧链路与 Jev 主链路）都改。
2. **`done.reliability` 被路由层吞掉**：`rag/routers/query.py` 读到 `reliability` 后只用于记录拒答
   工单与落库，随后自己发一个不含它的 `done`。而前端读的正是 `done.reliability.answerability_kind`
   ——于是浏览器永远说不出"为什么没作答"。
3. **子串匹配脆弱**：上面那处用 `'"type": "done"' in event` 判断事件类型，依赖 `json.dumps` 的
   默认空格。改成解析一次再按 `type` 分派；桩用紧凑 JSON 时旧写法会静默丢掉判定结论。

### 6.3 `details_structured_query_kind` 从未被写入

该列只有建表、模型定义与读取端（`rag/evolution/api.py`），**没有任何写入点**，永远为 NULL。
后果：结构化澄清从未进入自演化观测，这也是这类缺陷此前没被发现的原因之一。未修，留作后续。

### 6.4 验证

- 全量套件：见提交说明；`ruff check config.py rag/ scripts/ tests/ benchmarks/` 全绿；
- 真机（`/query/stream`）：「镜头价格合计」返回 `structured_result` + `kind=clarification_required`，
  答案是可执行的澄清文案；`done.reliability.answerability_kind` 已能到达客户端。
