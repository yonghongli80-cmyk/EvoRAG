# 交接文档：RAGv3（2026-10-21 拒答记分 / 结构化可观测 / 改写抖动定位）

> 上一份是 `docs/handoff-2026-10-19-structured-chain-and-routing.md`（结构化链路与路由修复）。
> 本轮接着它把"证据不足就拒答"这条产品口径**变成可验收的判据**，修掉三个拦在路上的真缺陷，
> 并把最后一个悬案（`real-020` 忽答忽拒）定位到了**查询改写**上。
> 过程与实测见 `docs/plans/2026-10-19-refusal-answer-position.md`（含 §5 实施记录与 §6 我的更正）。

## 0. 一句话现状

`HEAD` = 本文件所在提交（`git log -1 --oneline` 可查），工作树干净（只剩未入库的 `scratch/`
探查脚本），与服务一致；服务在
`127.0.0.1:8080` 健康。**"拒答"第一次有了记分位置**，结构化尝试第一次进了观测，
`real-020` 的抖动**已定位到改写**（下一步动它，见 §2）。

| 项 | 值 |
| --- | --- |
| 代码 | `HEAD` = 本文件所在提交，与 gitee `main` 一致 |
| 后端 | 全量套件 **2313 passed / 0 failed**（22:57；含沙箱 60 条） |
| 服务 | 健康（`/health` 返回 `healthy`，qdrant/sqlite 均 ok） |
| 结构化数据 | 镜像 **11 datasets / 19 sheets / 192 columns** |
| 知识库 | `kb_accessories` 5 数据集/12 sheets/128 列；`kb_evolab` 3/4/42；`kb_hr` 2/3/22 |
| 观测 | `failure_observations` **182** 条；`answerability` 174 × `insufficient_evidence` + 8 × NULL |
| 新列 | `details_structured_query_kind` **此前 181/181 全 NULL，现在开始有值** |
| 候选 | **30** 条：`canary` 1（#30 @0%）、`candidate_testing` 1、`rejected` 20、`rolled_back` 6、`stable` 2 |
| 聚类 / 工单 | 11 个聚类、5 张知识缺口工单 |

## 1. 这一轮修了什么（8 个提交）

| 提交 | 内容 |
| --- | --- |
| `5adfbb0` | 交接文档自身：正文还写着 `HEAD = 16f29cc`，与它声称的 `5d82820` 不一致 |
| `d29e81d` | **判定措辞**：`contains_answer_evidence` 认"型号行/参数行"（候选 #30 的实质内容） |
| `5bdce90` | **答案位置记分**：`expected_position`（answer/refuse）+ `refusal` 判据 + 层 2 拒答护栏 |
| `857e4e8` | **段落判定多数表决**：同一候选池重复采样再裁决，不动阈值 |
| `89157da` | **澄清不再被拒答顶掉**：判据从"有没有来源"改为"有没有答案"（两条路径都改） |
| `c563304` | **`done.reliability` 不再被路由层吞掉**；顺带修掉脆弱的子串事件匹配 |
| `80a87a8` | 我自己引入的测试依赖：作答路径会真调模型，克隆里没有 `.env` |
| `45fd851` | **`details_structured_query_kind` 第一次有生产者** |

### 根因清单（下一个人不必重新推导）

1. **"拒答"在基准里没有记分位置**：`ROUTE_EXPECTATIONS` 只有 `rag/structured/tool/clarification`
   四档，`route_correct = route == expected_domain`。所以"检索到了、判定说证据不足、于是拒答"
   **不改变任何路由分**——无论它该不该发生。候选 #30 的层 2 报 `+0.0pp` 就是这个原因：
   它想修的那类用例（`real-020`，`expected_source_names` 为空 ⇒ top5 不记分）在门禁里什么都没测到。
2. **判定措辞把表格里的证据判成"无证据"**：#30 的 `rationale` 写明 `passage_relevant=12` 而
   `passage_evidence=0`。同一候选池实测：旧措辞 7/12 判为证据（首位块 0.65），新措辞 11/12（0.83）。
3. **拒答带来源是常态**：Jev 路径把"相关但无答案证据"的块放进 citations 兜底，于是
   `not prepared.get("sources")` **不能**用来判断"检索有没有给出答案"。实测「镜头价格合计」：
   规划器要求澄清、检索 7 个来源、证据门拒答 ⇒ 用户看到"现有可见资料不足"，既没答案也没澄清。
4. **路由层吞掉 `done.reliability`**：它读 `reliability` 只为记拒答工单与落库，然后自己发一个
   不含它的 `done`；而前端读的正是 `done.reliability.answerability_kind`（见打平后的 js）。
5. **事件类型用子串匹配**：`'"type": "done"' in event` 依赖 `json.dumps` 的默认空格；紧凑序列化
   会让判定结论静默丢失。
6. **`details_structured_query_kind` 没有生产者**：只有建表、模型字段与读取端。`not_applicable` /
   `unsupported` / `failed` 三种结果在 `_attempt_structured_query` 里全部塌成 `None`，
   于是"结构化执行失败"与"没有表格数据"无法区分，也从未进过自演化观测。

## 2. 刚刚定位到的真根因：**改写不稳定**（下一步就该动它）

`real-020`（`找一下副厂配件明细`）在同一进程里 6 轮：**4 次拒答 / 2 次作答**。逐层排除：

| 排查 | 结果 | 结论 |
| --- | --- | --- |
| 候选池是否变 | 6 轮 top-5 签名**完全相同**（1 种签名 × 6） | 检索稳定，不是它 |
| 段落判定是否抖 | `passage_judged=0`、`passage_evidence=0`（走的是旧链路，判定没跑） | 不是判定 |
| 重排器是否降级 | 30 个块的 `score_source` **全是 `reranker`**，分数真实存在 | 不是降级 |
| **改写出的句子** | **6 轮 6 个不同句子** | **就是它** |

实测（`scripts/verify_rewrite_stability.py`，可直接复跑）：

| 改写结果 | 重排 top 分 | 结果（阈值 `reliability_min_score=0.35`） |
| --- | --- | --- |
| 请提供副厂配件的详细明细清单。 | 0.703 | 作答 |
| 请问能否提供副厂配件明细清单？ | 0.672 | 作答 |
| 请提供副厂配件明细。 | 0.583 | 作答 |
| 请查询副厂配件明细。 | 0.531 | 作答 |
| **如何查询副厂配件明细？** | **0.063** | 拒答 |
| **如何查询或获取副厂配件明细？** | **0.035** | 拒答 |

**因果链**：旧链路无条件改写 → LLM 把"帮我找这批资料"改写成"**如何查询**这批资料"（元问题）
→ 重排器认为文档不句答这个元问题 → 分数掉到 0.03~0.06 → 低于 0.35 → 拒答。

**注意**：这不是"阈值太高"。分数不是连续抖动，而是**双峰**（0.03~0.06 与 0.53~0.70），
阈值 0.35 恰好落在两峰之间的空档。放宽或收紧阈值只会改变哪一峰占多数，不会消除分叉。

### 建议的修法（代价递增，我推荐先做 1+3）

1. **改写结果按问题缓存**：同一 `(kb, question)` 复用一次改写，消掉重复抖动。小、安全、
   不动阈值；代价是同一问题改了上下文也不会重新改写。
2. **改写提示词加约束**：禁止把"找资料"改写成"如何查询/怎么获取"这类元问题，保留原句祈使语气。
   需要评测集，别凭感觉改。
3. **重排双路取高分**：对"原问题"和"改写句"各取一次重排分，用较高的那个判断证据。
   不依赖改写质量，最稳，代价是多一次重排调用（约 0.3s/次）。
4. （更根本但改动大）**让改写变成可选项**：`needs_query_rewrite` 已经由预判给出概率，
   旧链路却无条件改写。是否回落到"只在必要时改写"，需要一次基准对照再定。

## 3. 仍然开着的（按优先级）

1. **改写不稳定**（§2）——影响每一条走旧链路的企业问答，`real-017/018/020` 的抖动都是它。
   **这是当前最该做的一项。**
2. **脱敏价格应记为已知限制**：`real-017/018`（R50 电池价格）价格列全是 `<金额>` 占位符。
   证据存在但不可用，按"证据不足就拒答"拒答**是对的**，不算缺陷。产品已同意按此口径记录
   （原话："2听你的"），但**文档还没写**，下一步应补进 `docs/jev-benchmark.md` 或本文件 §2。
3. **`details_structured_query_kind` 只验到 1 条样本**：唯一那次落库是 `not_applicable`。
   `failed`（内部执行错误）还没有一次真机样本——需要构造一次数据集读取失败来确认它也能落库，
   否则这条修复只覆盖了一半。
4. **层 2 门禁"零净变化也 pass"**：`route_delta_pp = 0.0` 时即使逐例有进有退也判通过，
   被 `test_route_delta_of_exactly_zero_pp_passes_even_when_cases_change` 钉着。
   改它属于调整门禁语义，需单独批准。
5. **`_prepare_context_standard` 的无来源兜底**：检索为空时直接返回固定拒答、跳过证据门。
   "检索为空"与"证据不足"是否该区分，属产品口径，未动。
6. **候选 #30 留在 canary @0%**：它的措辞已作为生产修正落地（`d29e81d`），候选保留供追溯。
   另有 1 条 `candidate_testing` 状态的候选需要人看一眼是否要推进或退回。
7. **`real-008` / `real-022` 标签争议**：旧路径也是 0/3，标签本身待产品确认。
8. **服务端 P99 仍未测**；观察窗零样本（需真实流量）；4 个 `alice_*` 账号待产品决定是否删除。
9. **`structured_datasets` 计数**：既有行仍是 `sheet_count/total_rows/total_columns = 0`
   （回填只在镜像为空时补）。顺带记一条**不是缺陷**的：dataset 9 属于文档 19 的**旧版本 20**，
   当前版本是 23（有独立数据集），所以它 0 sheets 且被 catalog 正常排除——别去追它。

## 4. 坑清单（本轮新增，旧的仍然有效）

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 改了判定措辞，真机仍回"资料不足"，像改动没生效 | 验证脚本复用了同一个 `session_id`，对话记忆把上一轮的拒答上下文带了进来 | 验证一次改动**必须换新 `session_id`**（或独立会话） |
| 把测试从"拒答路径"改成"作答路径"后，主树绿、沙箱红 | 作答路径会真的调用模型，克隆里没有 `.env` | 打桩 `rag.pipeline.generate` |
| 沙箱红，明明已经"提交了" | 沙箱克隆 HEAD；若修的是**测试文件**且先跑沙箱再提交，克隆里还是旧测试 | 顺序必须是**先提交、再跑沙箱** |
| `ruff check .` 报错但产品代码没动 | `scratch/` 下的探查脚本未 gitignore | 只在提交产品文件时忽略它们；"ruff 全绿"不等于 `git status` 干净 |
| 进程内测出来的结论与真机不一致 | 服务占着 Qdrant 时，进程内的密集检索退化成 BM25 | 需要完整检索时**先停服务**；或明确说明用的是哪条降级路径 |
| 以为抖动来自判定/检索/重排，其实来自改写 | 旧链路**无条件改写**，改写是 LLM 生成的（见 §2） | 定位不稳定时先打印改写句，别先怀疑下游 |
| 基准/沙箱/E2E 报 Qdrant 被占用 | 本地模式独占目录 | 先停服务 |
| 基准里 route 0.150、外调 0 次，像系统崩了 | 漏了 `--user-id`：`user_id=None` 会让路由退化成 `clarification` | 跑基准**必须**带 `--user-id 1` |

## 5. 接手验证（可直接复制）

```powershell
git log --oneline -10                       # 期望首项是 docs(handoff): carry the refusal-scoring round...
git status --porcelain                      # 期望：空（scratch/ 除外）
curl.exe -s http://127.0.0.1:8080/health    # 期望：{"status":"healthy",...}

# 结构化镜像与目录（不动 Qdrant）
.\.venv\Scripts\python.exe scripts\verify_structured_schema_mirror.py
#   期望：11 / 19 / 192；kb_accessories 5/12/128、kb_evolab 3/4/42、kb_hr 2/3/22

# 真机结构化问答（服务在跑即可）
.\.venv\Scripts\python.exe scripts\verify_structured_query_live.py
#   期望：structured_result 事件；「遮光罩售价最高是多少？」facts: c_XXXX max

# 【本轮新增】改写稳定性 —— 这是定位 real-020 的关键，需先停服务以释放 Qdrant
.\.venv\Scripts\python.exe scripts\verify_rewrite_stability.py "找一下副厂配件明细" --rounds 6
#   期望（修复前）：distinct rewrites 6/6；top 分跨 0.35 两侧 → 明确打印
#   "the same question produced both answers and refusals; the variable is the rewrite..."
#   修好之后这里的期望会变：distinct rewrites 应为 1，或分数不再跨阈值

# 可观测性：结构化尝试的种类是否落库
.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/evolution.db');print(c.execute('select details_structured_query_kind,count(*) from failure_observations group by 1').fetchall())"
#   修复后应能看到 not_applicable / failed 之类的非 NULL 值（修复前恒为 [(None, N)]）

# 全量套件（约 27–45 分钟；要跑基准需先停服务）
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

基准（必须带 `--user-id`，且基准期独占 Qdrant）：

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_jev_retrieval.py --cases benchmarks\real-cases-v1.json --runs 3 --user-id 1
```

上一轮实测参考：`refusal` 判据 `[PASS] answer position 1.000 over 4 sample(s)`，
同轮 `route` 0.792（FAIL，原因见上一份交接 §2.1）。**单次差值不算结论**，判定器逐轮会漂。

## 6. 文档导航

| 文档 | 看它做什么 |
| --- | --- |
| `docs/plans/2026-10-19-refusal-answer-position.md` | 本轮的设计、实施记录（§5）与**我的三处错误更正**（§6） |
| `docs/handoff-2026-10-19-structured-chain-and-routing.md` | 上一轮：结构化链路与路由，含 `real-020` 的旧证据 |
| `docs/jev-benchmark.md` §3.0.1 | 答案位置记分口径（哪些判定算"作答"） |
| `config.py` 的 `jev_vote_rounds` 注释 | 投票能消什么、不能消什么 |
| `docs/development-log.md` 22.9 / 22.10 | 结构化能力实测与逐缺陷根因 |
| `docs/evolution-candidates.md` | 五层门禁与候选运维 |
| `docs/PROJECT_INTRODUCTION.md` | 白皮书（端点/路由/运行前提） |

## 7. 我这一轮的三处错误（下一个人不必重走）

1. **以为掉分是"拒答被记成路由错"**——路由标签没有拒答档，那四条用例在路由上本来就是对的。
2. **以为 `real-020` 落到"结构化澄清"**——协调器对它返回 `not_applicable`，那支根本没进；
   我看到的 `route=clarification` 是 **Jev 预判**的澄清路由，同名不同源。
3. **以为 `real-020` 已修好（answerable 3/3）**——那是单次采样；真机 3 轮里 1 拒 2 答。
   真相是 §2 的改写抖动。

**一句话给下一个对话**：判据补齐了、观测通了，剩下的核心问题是**改写不稳定**——
先跑 `scripts/verify_rewrite_stability.py` 复现，再按 §2 的 1+3 选项动手；`real-017/018`
的拒绝是正确行为，只需记录，不要为它放宽任何阈值。
