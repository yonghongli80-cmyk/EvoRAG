# 交接文档：RAGv3（2026-10-19 结构化链路与路由修复）

> 上一份是 `docs/handoff-2026-10-18-audit-and-remediation.md`（审计与收口）。本轮接着它把**审计暴露的
> 问题逐条修掉**，过程中又挖出四个更深的真缺陷。过程、根因与实测数字见
> `docs/development-log.md` **22.10**；README 级说明见 `docs/PROJECT_INTRODUCTION.md`。

## 0. 一句话现状

`HEAD = 5d82820`，全量套件 **2281 passed / 0 failed**，服务在 `127.0.0.1:8080` 健康（131 path / 141 ops）。
**结构化（表格）问答从"架构上不可能"变成真机可用**：镜像表补齐、多 sheet 可执行、聚合不再截断、
文本列不再 500；JEV 路径的企业语料 **Top-5 回归修好（0.923 → 1.000）**；判定器拿到字段名后
**JEV 单轮 route 0.750 → 0.950**。剩下的一条硬线（企业语料 route 0.95）**需要产品先定策略**，
我没有为了让它变绿去放宽阈值或改标签。

| 项 | 值 |
| --- | --- |
| 代码 | `HEAD = 5d82820`，与 gitee `main` 一致，工作树干净 |
| 后端 | 全量 **2281 passed / 0 failed**（37:00；上一轮 2223/0） |
| 前端 | `vue-tsc` 0 错、Vitest **231 passed**、构建通过（本轮未动前端） |
| 服务 | 健康；`/openapi.json` = **131 path / 141 ops**（口径：path×method） |
| 队列 | 29 条：`stable` 2、**`canary` 1（#30 @0%）**、`rolled_back` 6、`rejected` 20 |
| 结构化数据 | `structured_datasets` 11；镜像 **19 sheets / 192 columns**（修复前 0/0） |
| 知识库 | `kb_accessories`(5 数据集/128 列)、`kb_evolab`(3/42)、`kb_hr`(2/22)、`kb_after_sales` + 系统 `rag_docs` |
| 基准产物 | `artifacts/f-jev.json`、`f-real.json`、`g-real.json`、`b-*.json`、`cap-*.json` |

## 1. 这一轮修了什么（5 个提交）

| 提交 | 内容 |
| --- | --- |
| `16033f2` | **结构化链路**：镜像表写入 + 回填、多 sheet 可执行（sheet 维度）、聚合下推 SQL、非数值列澄清、coordinator 异常兜底、worker/extractor 也写镜像、注册表计数 |
| `6801501` | **JEV**：相关但无证据的块作为引用兜底（Top-5 回归）、能力摘要带字段名（路由）、结构化澄清不抢检索；顺手修一条依赖本地 `.env` 的测试 |
| `cebfeba` | **基准工具**：advisory 判定不再渲染成 PASS 且不再打印假不等式；5 条结构化用例改指真有数据的知识库 |
| `ffd5404` | 文档：dev-log 22.10 + 交接更新（含"没达到 0.95"的如实记录与我自己的错误归因更正） |
| `16f29cc` | 真机端到端复核 + 我差点按错误前提改接口的记录 |
| `5d82820` | 把两个验证脚本从被 gitignore 的 `artifacts/` 移进 `scripts/`（否则 §4 的命令在别人克隆下来的仓库里根本不存在） |

### 根因清单（下一个人不必重新推导）

1. **`structured_sheets` / `structured_columns` 从未被写过**：catalog 读这两张镜像表，而全仓只有
   `CREATE TABLE`（唯一 INSERT 在测试夹具里）；真实清单在 `dataset.sqlite3` 的
   `sheet_manifest`/`column_manifest`。→ `replace_structured_dataset_schema()` 成为唯一生产写入点，
   `migrate_existing_spreadsheets` 幂等回填。**这是"29 次结构化查询全部 unknown_field"的根因。**
2. **多 sheet 数据集不可能执行**：列 key 按 sheet 编号（每 sheet 都有 `c_0001`），执行层要求全库唯一
   → `unknown or ambiguous column key`。→ plan 与 Metric/Filter/Sort 补 `sheet_key`，按 `(sheet, column)`
   解析并限定 SQL，跨 sheet 聚合澄清。
3. **聚合在 `LIMIT ≤100` 的前缀上算**：104 行的表上 `max` 前 100 行 `'相机型号'`、全表
   `'眼罩 130-180（型号不同价格不同）'`。→ 下推 SQL + 精确十进制累加器；`rows_scanned` 语义 =
   实际参与计算的行数。
4. **JEV 丢弃"相关但无答案证据"的块**：`_prepare_rag_with_jev` 只在 `has_answer_evidence` 时写
   `context`，而 sources 由 context 派生 ⇒ 新路径 0 来源、旧路径同一文档排第 1。→ 引用兜底
   （只进 sources，不动 context／证据门／答案口径）。
5. **判定器看不到字段名**：能力摘要只有一个布尔 ⇒ 点名了列的问题被判成检索意图。→
   `KbCapabilities.structured_fields`（按权限过滤、去重、截断 40）。
6. **结构化澄清抢占检索**：问句提到字段就进结构化、然后只回"请指明字段"。→ 澄清让位检索，
   检索无来源时才回澄清。

## 2. 仍然开着的（按优先级）

1. **企业语料 route 0.806~0.875 vs 脚本要求 0.95 —— 需要你先定策略，不是 bug**：
   ① `real-012/017/018/020` 现在**拒答且带 5–8 个来源**。**更正（2026-10-19）**：此前写成
   "拒答被记成路由错"，与实际代码不符——路由标签只有 `rag/structured/tool/clarification` 四档，
   `route_correct` 只比较链路，拒答**不改变**路由分，这四条在路由上本来就是对的。掉分的是判定器
   逐轮抖动与两条标签有争议的用例。真正的问题是**拒答此前没有任何记分位置**，"该答却拒"与
   "该拒却答"都进不了数字。现已补上 `expected_position`（见
   `docs/plans/2026-10-19-refusal-answer-position.md`），其中 `real-020` 标为**必须作答**：
   内容存在却拒答是缺陷，不是保守；② 判定器逐轮抖动（同题 `intent_confidence` 0.19~0.61），
   ±3 样本 ≈ ±4pp 是噪声；③ `real-008`/`real-022` **旧路径也是 0/3**，标签本身有争议。
   **口径已定**：证据不足就拒答；因此 `evidence_threshold` 一个字不动，这条线要看的是
   新的 `refusal` 判据，而不是放宽阈值。
2. **结构化链路的剩余边界**（不影响安全，但影响答案/体验）：
   - **分组结果仍被 `plan.limit` 截断**（截的是分组行，无"已丢弃行"标记）；
   - **decimal 列 `min/max` 是字典序**（`'9.9' > '10.1'`）⇒ "最高/最低"类问题是潜在错值；
   - `_execute_multi_lookup` 对每个数据集都用 `selected[0].sheet_key`，非首个数据集的引用可能指错 sheet；
   - `structured_datasets` 的 `sheet_count/total_rows/total_columns` 对**既有 10 行仍是 0**
     （回填只在镜像为空时补，需一条"从既有镜像重算计数"的路径）；
   - catalog 构建（`has_datasets`）不在 coordinator 的异常兜底范围内；
   - `failed` 结果与成功结果一样进 128 条 LRU 缓存；
   - planner 双字段歧义（如无"各"的"脚架环价格合计"）先落 `ambiguous_field`。
3. **#30 在 `canary` @0% —— 它的内容已进生产，不要重复推进会它**：独立复测 3/3 passed（不回归稳），
   但**声称的改善未复现**（`route_delta` 三次全 0.0）。**2026-10-19 查明原因并处理**：
   #30 是 passages 问题集的文本变体，唯一实质改动是让 `contains_answer_evidence` 认"型号行/参数行"
   （根因见它的 rationale：`passage_relevant=12` 而 `passage_evidence=0`）。层 2 之所以测出 0.0pp，
   是因为 20 条 A/B 语料里没有一条依赖这个判据，且 `real-020` 族标了空的 `expected_source_names`
   ⇒ top5 不记分、路由又不因拒答而变 ⇒ 整条用例在门禁里什么也没测到。
   该措辞已作为生产修正提交（`d29e81d`），在固定候选池上实测 12 块中判为证据的块数 7 → 11。
   **#30 保留 0% 作追溯，不必放量**；层 2 现在会因"候选把能答的问题变成拒答"直接失败。
4. **4 个 `alice_*` 测试账号已中性化**（口令轮换，旧口令登录 401）；要**删除**需产品决定是否提供该能力。
5. **服务端 P99 仍未测**：只做过只读并发冒烟（20 并发 × 200 请求，全 200、无 `database is locked`）；
   并发延迟被 20 线程 Python 客户端主导（顺序 `/health` 4.7ms vs 并发 p50 61–65ms）。
6. 观察窗仍零样本（需真实流量），4 个聚类与 4 张工单仍需人的领域判断。

## 3. 坑清单（本轮新增，旧的仍然有效）

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 基准里 route 0.150、外调 0 次，像系统崩了 | 漏了 `--user-id`：`user_id=None` 会让路由退化成 `clarification` | 跑基准**必须**带 `--user-id 1` |
| 沙箱用例（`test_patch_sandbox.py`）突然红 | `cloned_repo` 是 `git clone`，**只含 HEAD**；未提交的修复不在克隆里 | 提交后再跑；别在未提交状态下把它当回归 |
| 测试在主树绿、克隆里红 | 测试读了运维本地 `.env`（gitignored） | 测试自带 key（本轮已修 `test_rerank_uses_cached_client`） |
| 把测试从"拒答路径"改成"作答路径"后，主树绿、沙箱红 | 作答路径会真的调用模型，克隆里没有 `.env` | 打桩 `rag.pipeline.generate`（2026-10-21 已修 `test_structured_clarification_yields_to_retrieval_evidence`） |
| 沙箱红，明明已经"提交了" | 沙箱克隆 HEAD，而修的是**测试文件**：先跑沙箱再提交 ⇒ 克隆里还是旧测试 | 顺序必须是**先提交、再跑沙箱**；本轮我在这上面白跑了一轮 |
| 改了判定措辞，真机仍回"资料不足"，像改动没生效 | 验证脚本复用了同一个 `session_id`，对话记忆把上一轮的拒答上下文带了进来 | 验证一次改动**必须换新 `session_id`**（或独立会话），否则测的是记忆不是改动 |
| 基准两次跑数字不一样 | 判定器逐轮抖动（同题 confidence 0.19~0.61），基线两侧都会漂 | 看多次运行的**区间**，别拿单次差值当结论 |
| 基准/沙箱/E2E 报 Qdrant 目录被占用 | Qdrant local 模式独占目录 | **先停服务**；E2E 脚本已加生产守卫（默认 exit 2） |
| `POST /query` 问企业库只回"证据不足" | `QueryRequest` **没有 `kb_id` 字段**，同步端点按契约只服务 `rag_docs` | 企业问答走 `/query/stream`（`StreamQueryRequest` 才有 `kb_id`）。**我差点改错这个接口**，见 16f29cc |
| 判断"某个能力坏了"之前 | 本轮两次教训：反链跨库、同步端点 kb_id —— 读接口定义再下结论 | CLAUDE.md 第 1 条：先查文档/签名，再动手 |
| `ruff check .` 突然报错，但产品代码没动 | `scratch/` 下的临时探查脚本未 gitignore | 要么清理，要么只在提交产品文件时忽略它们；别把"ruff 全绿"当成 `git status` 干净 |

## 4. 接手验证（约 40 分钟，含全量套件）

```powershell
git log --oneline -6                        # 期望：5d82820 / 16f29cc / ffd5404 / cebfeba / 6801501 / 16033f2
git status --porcelain                      # 期望：空
curl.exe -s http://127.0.0.1:8080/health    # 期望：{"status":"healthy",...}

# 结构化链路（真机、真实 SSE；自己铸 evolab-admin 的 token，无需手工准备）
.\.venv\Scripts\python.exe scripts\verify_structured_query_live.py
#   期望：「遮光罩售价最高是多少？」→ structured_result 事件（facts: c_0006 max）

# 数据面：镜像非空
.\.venv\Scripts\python.exe scripts\verify_structured_schema_mirror.py
#   期望：structured_sheets=19 / structured_columns=192；kb_accessories 5 数据集 128 列

# 知识闭环（词条→AST 切块→双路索引→工单关联→重检→修订回滚）
.\.venv\Scripts\python.exe scripts\verify_wiki_knowledge_loop.py     # 期望：5/5 全绿

# 全量套件（约 25–40 分钟；跑前若要用 Qdrant 做基准需停服务）
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider     # 期望：2281 passed

# 基准（必须带 --user-id；基准期独占 Qdrant ⇒ 先停服务）
.\.venv\Scripts\python.exe scripts\benchmark_jev_retrieval.py --runs 3 --user-id 1
#   期望：new route≈0.95、Top-1/Top-5 = 1.000
.\.venv\Scripts\python.exe scripts\benchmark_jev_retrieval.py --cases benchmarks\real-cases-v1.json --runs 3 --user-id 1
#   期望：Top-5=1.000（PASS）、route 0.81~0.88（FAIL，原因见 §2.1）
```

## 5. 文档导航

| 文档 | 看它做什么 |
| --- | --- |
| `docs/development-log.md` **22.9 / 22.10** | 能力实测（22.9）与本轮每个缺陷的根因→修法→实测（22.10），含我自己的两处错误归因更正 |
| `docs/handoff-2026-10-18-audit-and-remediation.md` | 上一轮（审计与收口）：红线、坑清单、#30 的处置理由 |
| `docs/PROJECT_INTRODUCTION.md` | 已按实测修正的白皮书（端点/路由器/PRAGMA/P99/运行前提/完成度） |
| `docs/handoff-2026-10-13-self-evolution.md` | 自演化回路的红线与运维命令（仍有效） |
| `docs/evolution-code-patches.md` / `docs/jev-benchmark.md` | 五层门禁设计 / 基准口径与验收档案 |
