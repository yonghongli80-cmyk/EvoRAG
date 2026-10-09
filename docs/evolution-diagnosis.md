# 自进化诊断阶段运维说明（evolution-diagnosis）

第 3 阶段（诊断辅助）只做一件事：为进入**候选池**的失败聚类生成结构化诊断**草案**，
供管理员在进化中心审核。草案永远不是结论；只有管理员确认根因后才推进状态机。
本文件覆盖启用、预算、审核、审计与回退；设计依据见
`docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md` 第 5/6/7/8/9 节，
实施计划见 `docs/superpowers/plans/2026-09-23-assisted-self-evolution-diagnosis.md`。

## 1. 启用与关闭

- 总开关 `RAG_EVOLUTION_ENABLED`（默认 false）。关闭时：不建 `evolution.db`、不写观测、
  不跑诊断 worker、API 返回空结构（不 500）。第 2 阶段观测同此开关。
- 开关打开后，`rag.api` 常驻 worker 按 `evolution_worker_interval_seconds`（默认 900s）
  执行一轮：聚类 → 留存清理 → 诊断（见下）。
- 另可脱离 API 单跑：`python -m rag.evolution.worker`（同样读取上述配置）。

## 2. 诊断预算与调用参数（config.py 末尾）

| 设置 | 默认 | 语义 |
| --- | --- | --- |
| `evolution_diagnosis_model_calls` | 3 | 每轮最多诊断的聚类数；**成功与失败都计 1**，达到即停，剩余聚类下轮重试。3 × 下面的 150s = 450s，与旧值 5 × 90s 的单轮最坏值相同 |
| `evolution_diagnosis_timeout_seconds` | 150 | 单次调用超时，注入诊断专用客户端（与在线生成的 30s、断路器相互隔离）；提到 150 是因为完成上限放宽后实测有 >90s 的调用 |
| `evolution_diagnosis_max_tokens` | 16000 | 单次完成 token 上限。该提供商的 reasoning token **计入**完成预算：无上限时调用可达数分钟，过小会把 JSON 正文挤没——8000 下实测 `reasoning_tokens=7942`、`text_tokens=0`、正文截断（见第 5.2 节基线的 13.3% 契约违规率） |
| `evolution_diagnosis_labels_path` | 空 | 管理员确认标签 JSON 路径（证据最高优先级）；为空时草案根因只能是假设级（后校验强制） |

诊断模型复用 `RAG_DEEPSEEK_*`（当前 Command Code `deepseek/deepseek-v4.1-flash`，
推理模型）。**注意**：它每次调用都产生 reasoning token，实测诊断尺寸提示词下单次
P50 约 40 秒、上限 150s；超时不是故障，该聚类无草稿、下轮自动重试。

## 3. 一次诊断里面发生什么

1. `observations_awaiting_diagnosis_without_draft` 取**待诊断的成员观测**（最旧优先；聚类状态为
   `awaiting_diagnosis_review` **或** `approved_for_candidate`，且该观测还没有成员级草案）。
   已批准聚类也在内：候选是**按观测**生成的、生成器要求该观测自己有一份草案，
   若批准后就停止起草，聚类里那些还没草案的成员将永远产不出候选
   （2026-09-29 修正；此前只收 `awaiting_diagnosis_review`，配件聚类 26 个成员因此只有 1 份草案）。
   成员数未达阈值的 `clustered` 聚类不在内——它还没被准入审核。
2. `assemble_evidence` 装配证据包（**确定性优先**，不调模型）：
   - 管理员确认标签（按规范化问题文本匹配，`priority="admin_confirmed"`）；
   - 同 kb 成功轨迹摘要（`answer_source="system_history"`，**不得**当管理员答案引用）；
   - 以系统管理员主体做只读 ACL 重检索，取块哈希；
   - **复现检查**：失败时 offered 的块哈希是否仍全部在重检索结果中——检索级、确定性，
     不可复现则该聚类建议 `needs_more_evidence`。
   - 哈希列表按排序取前 12 条进入提示词（控制成本），复现检查仍看全量。
3. `Diagnoser.draft`：恰好一次模型调用，版本化提示词（`DIAGNOSIS_PROMPT_VERSION`，
   当前 `diagnosis-v4`），输出经 Pydantic 校验 + 后校验硬规则：
   - 无管理员标签 → `confidence=low` 且根因含“假设”；
   - 不可复现 → `confidence=low`、根因含“待复核”、`reproducible=false`；
   - 违反任一即 `DiagnosisUnavailable("invalid")`，**不产半成品**。
4. 草稿落 `evolution.db` 的 `diagnosis_drafts`（payload + prompt_version + model）。

## 4. 管理员审核（进化中心 → 聚类与状态机）

- `GET /knowledge-operations/evolution/clusters/{id}/diagnosis`：最新草案 + 审计列表。
- `POST /knowledge-operations/evolution/clusters/{id}/decision`：
  `approve_for_candidate` / `reject` / `needs_more_evidence` + 备注。
- **每次决策先写 `evolution_audit` 行（actor/action/cluster/note/created_at）再推进状态**；
  400=未知动作、404=未知聚类、409=状态非 `awaiting_diagnosis_review` 或非法转移；
  失败的尝试不写审计。
- 端点清单（`GET /state-machine` 的 `endpoints`）如实反映全部 7 个端点，其中唯一的写操作
  就是这个带审计的决策端点（测试钉住该不变量）。
- 前后端均要求管理员（`require_admin`）；非管理员 403。

## 5. 开发集验收记录（2026-09-24，含标签审计）

**方法**：逐成员观测诊断（第 3 阶段后期把粒度从“每聚类一条”改为“每观测一条”——
聚类键会把不同根因混在一起，单类别草案无法对分混合标签）。开发集 = 第 2 阶段真实回放
的失败样本 + 9 个锚点的同义变体（设计第 10 节同义表达）+ 审计库中未回放的新问题，
共 40 个带标签问题；经生产同款聚类准入后 30 个进入待诊断，其中 28 个完成对比、
2 个模型输出违反契约（不可用，按设计下轮重试）。

**结果：一致率 24/28 = 86% ≥ 80% 验收线**（提示词版本 diagnosis-v2；v3 只加确定性证据，
参考口径仍是本条，见第 5 节 v3 复核）。

### 5.1 可重复测量协议（2026-09-28 起，调优前必须先跑它）

**为什么需要**：上面的 24/28 是**单次**测量，而同一问题、同一证据包，模型会在重复之间
改变类别（本会话实测：同一问题 3 次跑出 `ranking_error` / `undetermined` / 契约违规）。
单次一致率无法区分"改动有效"与"这次运气好"，因此任何提示词/证据改动之前，先建立可比基线。

**怎么跑**：

```powershell
# 1102 在跑时不能直接用本机 qdrant 目录（本地客户端按路径独占加锁）：
robocopy qdrant_data %TEMP%\qdrant-snapshot /E /XF .lock
.venv\Scripts\python.exe scripts\run_diagnosis_devset.py `
  --cases real-case-handoff\diagnosis-devset-cases.json `
  --repeats 3 --label "<这次的名字>" --qdrant-path %TEMP%\qdrant-snapshot `
  --json-out artifacts\diagnosis-devset-<label>.json --markdown-out artifacts\diagnosis-devset-<label>.md
```

- **用例集是显式文件**（`case_id / question / kb_id / expected_category`），不再依赖聚类状态或
  待诊断队列——上一轮把聚类推进过状态后，队列驱动的 harness 只能回来 12/30 条，无法复现参考口径。
  该文件含公司问题，只放本地 `real-case-handoff/`（已 ignore），不入库。
- 每个用例**只组装一次证据包**、在同一份证据包上重复 `--repeats` 次，因此测到的是**模型方差**
  而不是检索方差；缺失观测行的用例以 `no_observation` 如实列出，不静默缩小分母。
- 报告字段：`agreement_by_run`（每次重复各自的一致率 = 单次部署会拿到的分数）、
  `agreement_majority`（多数票上限）、`stable_rows`（重复间类别一致的行数）、
  `contract_violation_rate`（契约违规率）、`latency_p50_ms`。**改动只有超出 `agreement_by_run`
  的跨度才算有效。**
- 运行是只读的：不写 `evolution.db`、不调灰度、不改聚类状态。

**环境坑（本会话踩到）**：虚拟环境 `site-packages` 里有一份 **2026-09-22 的陈旧 `config.py`**
（不含 `evolution_db_path` 等第 3 阶段以后的字段）。直接 `python scripts\xxx.py` 时 `scripts\`
排在 `sys.path` 最前、而 editable 安装只把仓库根**追加在 site-packages 之后**，于是陈旧副本
会赢。仓库既有的修法是 `scripts/benchmark_jev_retrieval.py::_ensure_project_root_on_path`，
新协议脚本同法处理并有测试钉住。若要彻底消除，可清理该 venv 的陈旧副本（属环境操作，未擅自执行）。

### 5.2 基线测量（2026-09-28，diagnosis-v3，30 用例 × 3 次重复）

`artifacts/diagnosis-devset-v3.json` / `.md`（本地产物）：

| 指标 | 值 |
| --- | --- |
| 可测用例 | 30/30（按问题文本定位观测，不再受队列状态影响；队列驱动的 harness 只能回来 12/30） |
| 逐次一致率 `agreement_by_run` | **25 / 24 / 25** = 83.3% / 80.0% / 83.3% |
| 多数票一致率 | 26/30 = **86.7%** |
| 稳定行（重复间类别一致） | 26/30（不稳定：dev-10、dev-23、dev-27、dev-28） |
| 契约违规率 | **12/90 = 13.3%** |
| 单次耗时 P50 | 40.1s |

**结论（这三条决定了下一步该做什么）**：

1. **参考口径 86% 落在噪声带的乐观端**。单次部署实际会拿到的是 80–83.3%，多数票才到 86.7%。
   也就是说"24/28 = 86%"作为验收线成立，但它不是单次运行的期望值；后续比较必须看
   `agreement_by_run` 的跨度，而不是一个数字。
2. **`ranking_error` 家族是系统性失败，不是边界抖动**：三个期望 `ranking_error` 的用例
   9 次里 0 次正确——dev-07、dev-23 **9 次全部契约违规**（截断），dev-22 稳定地给
   `undetermined`。前两个正是第 5 节记录过的推理 token 吃满预算那一类。
3. **13.3% 的调用被丢弃**（12/90），比第 5 节原先记录的 2/30（6.7%）高一倍；配合第 5 节
   的截断探针（8000 上限下 `reasoning_tokens=7942`、`text_tokens=0`），这是当前最值得先修的
   工程缺口——它同时压低一致率、浪费模型调用，并让那一族**无法被测量**。

**调整后的印证（2026-09-28，单次观察，非全量复测）**：把 `max_tokens` 提到 16000、超时提到 150s
之后，`找一下副厂配件明细`（也就是上表 dev-07 那条**3 次全契约违规**的观测）产出了一份
**合法草案**：`undetermined`、`suggested_layer=none`、`confidence=low`、`reproducible=true`，
**耗时 92.5 秒**——比旧超时 90s 更长，也就是说单改上限而不动超时，这一条仍然会以超时告终。
这条印证只说明"截断/超时这一族现在能出草案了"，**不等于一致率提升**；要后者仍需跑第 5.1 节协议。

**三轮迭代过程（每轮都改机器不改标签，唯一一次改标签有据在案）**：
| 轮次 | 一致率 | 发现 | 机器侧修订 |
| --- | --- | --- | --- |
| v1（每聚类一条） | 0/1（混合标签无法对分） | 聚类键混合根因 | 改为逐观测诊断 |
| v2（逐观测，仅哈希证据） | 1/27 (4%) | 提示词渲染记录器的 root_cause_feature，模型复读它；仅哈希无法区分三类 | 提示词移除该字段；证据包加入重检索块预览；提示词写明三类判定口径 |
| v2b（预览 8 块） | 21/28 (75%) | 预览太少，深层相关块不可见 | 预览上限 8 → 24 |
| v2c（预览 24 块） | **24/28 (86%)** | 4 个偏差均为边界类（ranking_error↔generator_ignored_evidence、轻微漂移） | —— |
| v3（问题词覆盖度信号，仅作证据渲染） | 见下方"v3 复核"，未改变 24/28 的参考口径 | 覆盖度是确定性证据，但它**不是**那一族问题的主因 | 证据包加入逐词覆盖度（命中块数 + 最早排名）并渲染；**不附加判定口径** |
| v4（判定层统计量，仅作证据渲染） | 收益面**无样本**可测；风险面实测 5 条内容缺口 0 次假阳（见下方"v4 复核"） | 失败请求只记下"没采纳任何块"，判定层看到什么从不可见 | 轨迹与证据包加入 `passage_judging`（候选数/相关/答案证据/冲突/注入计数）并渲染；再一条口径尝试被实测否决 |
| v5（异常失败按缺陷判类） | 开发集复测 33/39 = 84.6%（无标签 2 重复，未退化）；异常族 6/6 判 `code_defect`+`code`（见下方"v5 复核"） | 异常类只进观测、没进证据包 → 6 条 `ValueError` 被解释成 `intent_route_error` | 证据包渲染 `failure_route`/`exception_class` + 条件口径（异常优先于召回启发式） |
| v6（散文字段限长）**已撤回** | 该族 5×3：契约违规 4/15 = 26.7%，一致率仍 0/5 —— **无改善** | 截断直接成因是模型把块哈希粘进散文字段 | 撤回规则（只留注释与测量留档）；改为诊断步骤的**同轮重试一次**（见 §3 与 v5 小节） |

### v3 复核（2026-09-28，含失败的尝试，如实记录）

**信号本身（确定性、无模型）**：证据包新增 `question_term_coverage`——把问题切成
ASCII 词 + CJK 二字组（最多 8 个词），在**重检索的前 24 块全文**上统计每个词命中几块、
最早出现在第几名（0 = 召回集合里没有该词）。只读证据，不含原文，不落库。

**第一次尝试（已撤回）**：同时加了一条口径规则"有覆盖 ⇒ ranking_error / 0 命中 ⇒
更支持 recall_miss 或内容缺口"。成对开发集测量显示它**污染了原本判对的口径**：
12 条"内容缺口"子集（全部期望 undetermined）在 v2 是 12/12，加了口径后两次运行是
10/11 与 8/8（其中一次把 `帮我查一下客户A的回收文件` 推成 `recall_miss`）。
故该规则被删除，只保留证据渲染。

**第二次尝试（保留）**：仅渲染覆盖度、不给口径。同一 12 条子集测得 11/11（+1 次契约违规），
与 v2 的 12/12 在噪声内。**没有证据表明覆盖度信号提高了类别一致率**；它的价值是把
"问题词在召回集合里有没有覆盖、覆盖在第几名"变成**确定性**证据，而不是只靠 160 字预览去猜。

**关键发现（比覆盖度更重要）**：`real-020` 家族（期望 `ranking_error`）的不稳定**不是判据偏弱**，
而是 completion 被推理 token 吃满而截断。逐请求实测（同一问题、同一证据包）：
- `max_tokens=8000`（当时默认）：`finish_reason=length`、`reasoning_tokens=7942`、
  `text_tokens=0`、正文仅 137 字符 → 判为契约违规（`response is not a JSON object`）；
- `max_tokens=16000`：同一请求 `finish_reason=stop`、正文 1428 字符、契约通过。
- 但把上限提到 16000 后，同一族 3 次里出现 1 次 `APITimeoutError`（90s 诊断超时）
  与 2 次字段级违规，**类别答案仍然在 `ranking_error` 与 `undetermined` 之间摇摆**。
**据此已调整（2026-09-28，依据第 5.2 节基线）**：`max_tokens` 8000 → **16000**、
超时 90s → **150s**、每轮调用 5 → **3**（3 × 150s 与旧值 5 × 90s 的单轮最坏值相同）。
这次调整**没有复测**：截断的因果（`finish_reason=length`、`reasoning_tokens` 吃满、
`text_tokens=0`）是实测确证的，但"调整后契约违规率是否下降"**未验证**——要验证就跑第 5.1 节
的协议，拿 `agreement_by_run` 与 `contract_violation_rate` 前后对比。
另需注意：类别边界（`ranking_error` ↔ `generator_ignored_evidence` ↔ `undetermined`）
不是 token 能解决的，这族即使不再截断也可能继续判错。

**测量口径的诚实说明**：本节 v3 的数字都在**12 条子集**上测得，不是原来的 28 条——
上一轮会话已把多数聚类推进过 `awaiting_diagnosis_review`，本轮 harness 的
`cluster_once` 扫描 0、只回来未被诊断过的 12 条（`devset-per-observation.json` 已被
本轮的中性 v3 结果覆盖）。因此 **24/28 = 86% 仍是唯一的参考验收数字**，v3 只声称
"未在可测子集上退化"（11/11 对 v2 的 12/12），不声称提升。中间产物留在本地
`real-case-handoff/`：`devset-v3-run1.json`（带口径第 1 次）、`devset-v3-naive-run2.json`
（带口径第 2 次）、`focused-coverage-ab.json`、`focused-coverage-repeat.json`、
`focused-truncation-probe.json`。

### v4 复核（2026-09-29，含失败的尝试，如实记录）

**信号本身（确定性、无模型）**：失败请求此前只留下"没有采纳任何块"这一个可见事实，
判定层到底看到了什么从不可见——于是诊断只能把失败读成内容缺口或召回缺口。v4 让链路
把它记下来：`PassageSelection` 增加 `judged/relevant/with_evidence/contradicted/injected`
四个**原始**计数（同一块可同时计入相关与注入），pipeline 写进 `decision_metadata`
（跨尝试取最大值），tracker 白名单放行，证据包以 `passage_judging` 渲染给模型。
只读统计量，不含块文本、不含问题原文。

**口径尝试（已撤回）**：曾同时加过一条"`passage_relevant > 0` 且 `passage_evidence == 0`
⇒ 取 `prompt`/`query_rewrite`"的口径。成对实测（同一批观测、同一证据包，v3 从 HEAD 取出
作为对照，见 `real-case-handoff/evolab/diagnosis-rule-ab-with-rule.json`）显示它把
**5 条内容缺口中的 2 条**推成判定层缺陷（"人员盘点覆盖哪些人" rel=6、"一般从哪里发货" rel=2，
两条的事实经确定性探针确认**不在库里**）。口径删除后复测，内容缺口族 **0/5** 假阳
（`diagnosis-rule-ab.json`）。这与 v3 覆盖度信号当年的结论一致：**证据可以渲染，口径不能
从启发式信号推**——`relevant` 只表示判定层认为文本相关，不等于库里写了所问的事实。

**收益面没有样本（这是当前的瓶颈）**：为测量口径而新问的 14 条里，8 条"内容存在"的问题
**全部答对**（无观测），只有 5 条内容缺口失败入观测。也就是说：本语料里"事实存在却失败"
的失败样本几乎为零，而**只有那类失败才可能被判定层口径正确修复**。因此 v4 只声称
"记录并渲染了判定层证据、且未在可测的风险面上引入假阳"，**不声称类别一致率提升**。
要继续推进需要一个能稳定产出"事实存在却失败"的语料（合成库已证明现构造造不出来：
答案不在库时管线根本不采纳任何块，`stage` 恒为 `retrieval`）。

| v7（label_evidence：答案文本的精确包含检查） | 命中即判对取用侧（obs#27 → `ranking_error`/`reranking`）；安全面无误伤（异常族 5/5、无标签缺口组 5/5 不变） | 管理员标签只说"内容在哪"、不说"答案是什么"，判据因此无从触发 | 证据包新增 `label_evidence`（型号串 × 召回块精确包含）+ 条件口径；标签补齐具体型号串 |
| v9（异常栈最后一帧进证据包） | 补丁生成器的首个目标文件从"按诊断措辞猜的 `rag/bm25_store.py`"变为**帧点名的 `rag/pipeline.py`**（见 `docs/evolution-code-patches.md`） | 逻辑缺陷没有事实定位，补丁只能按词频猜文件 | 证据包渲染 `exception_frame`，并要求草案写明 |
| v10（无证据护栏） | 7 条已标注观测 × 3 采样：**7/7 全对**（含一条"脱敏⇒不可答"的负向标签）；C 组**无标签**缺口 5/5 且**零拒绝** | 无标签却断言"库里没有"的结论**未经核实**，而它正是知识采编工单（队列 5）的输入 | 无标签 + `undetermined` ⇒ `root_cause` 必须含『需管理员标签』（硬校验）；提示词给出确切字样 |

### v5–v7 复核与 v6 的撤回（2026-10-01 ～ 10-02，含失败的尝试）

**v5 = 异常失败按缺陷判类**。cluster #30 的 6 条观测来自未捕获异常
`ValueError('versioned evidence required')`，用 v4 诊断成 **5×`intent_route_error` + 1×
`undetermined`**——因为异常类只进了观测、**没进证据包**，模型只能顺着管理员标签里的
`expected_route=rag` 把"请求异常"解释成"路由没走 RAG"。v5 起证据包渲染
`failure_route` 与 `exception_class`，并给出条件口径：异常失败的第一处偏离是异常本身，
类别取 `code_defect`/`prompt_or_workflow_defect`，层取 `code` 或异常实际指向的层，
**不得**判成 `intent_route_error`/`undetermined`/`none`（非异常失败不受影响）。

实测（预注册判据：≥4/6 判缺陷类别且层为 code、0 误判）：**6/6 `code_defect` + `code`**。

**v6 的撤回**：`ranking_error` 族的契约违规（"response is not a JSON object"）经复现确认是
**回复被截断**——模型把 8–12 个 64 字符块哈希粘进 `first_divergence` 等散文字段。据此加了
v6 规则（散文字段禁贴哈希、每字段 ≤120 字）。在该族 5 条 × 3 重复上复测：契约违规
**4/15 = 26.7%**（改动前同族 3/10）、多数派一致率仍 0/5，**无改善** → 按纪律撤回，
测量留档于 `real-case-handoff/evolab/ranking-v6.json`。

**替代方案（同轮重试，2026-10-02）**：诊断步骤在 `reason=invalid` 时对同一证据包**再采样
一次**，重试**计入每轮调用预算**；`timeout`/`rate_limited` 不重试（容量信号），预算不足时
不重试但仍计失败。实测（该族 15 抽样）：一次成功 12、重试后成功 3、仍失败 0 →
**有效失败率 0%**（重试前 20–26.7%），代价是总调用 15 → 18。**注意**：重试只治格式契约，
该族的**类别一致率仍 0/5**——"出得了草案"与"判得对"是两件事。

### 管理员标签（admin_labels）与 2026-09-30 的灰度验证

**标签是让"内容存在却未被采用"这一族可诊断的正路**。把已核对的事实写成
`benchmarks/diagnosis-labels.json`（按规范化问题文本精确匹配，字段含 expected_answer /
expected_source / admin_note），并用 `RAG_EVOLUTION_DIAGNOSIS_LABELS_PATH` 指过去：

- 成对实测（`real-case-handoff/evolab/admin-labels-alignment.json`）：带标签后 4 条"附件族"
  观测**全部**落到可寻址层（`prompt` ×3、`query_rewrite` ×1，其中两条 medium 置信、复现通过），
  而 3 条内容缺口对照**0 次**被推成判定层；**这是启发式口径做不到的**（那条口径 2/5 误伤，已撤回）。
- 标签不是结论而是事实：它只声明"答案在哪个文档里"，分类仍由模型按证据给出。
- **方差**：同一批观测换一次运行，4 条里只有 2 条落到可寻址层（另两条回退 `none`）。
  诊断温度 0.2 仍非确定性，所以"能否生成候选"是有概率的，不要当作稳定产能。

**生成时只取该观测最新的一份草案**：多次再诊断会让一个观测累积多份草案，而生成规则要求
传入的每一份都可寻址——把历史草案全传进去，等于让标签上线前的旧草案永久否决候选。

**灰度结果面验证（候选 #5，来自 obs#7 的 medium 档草案）**——同问题、同库、唯一变量是是否
命中灰度桶（`sha256("kb|session|question")%100 < 5`）：

| 侧 | 服务版本 | 来源 | 判定层 relevant | 判定层 evidence |
| --- | --- | --- | --- | --- |
| 命中桶 ×2 | `passages-v1+candidate-5` | 5、5 | 6、10 | **1、1** |
| 桶外 ×2 | `passages-v1` | 0、0 | 12、12 | **0、0** |

即候选把该问题从"拒答（0 来源、无答案证据）"翻转为"带 5 条来源作答"，两次重复一致，
归因为候选版本。层 2（固定 20 例）0pp 通过、层 3 路由一致率 1.0；回滚后立即失效，重启不复活。

**已知归因缺口（本次发现）**：preflight 低置信度时走 `route_decision.route == "legacy"`
分支，直接交给旧链路，**不附加 Jev 阶段元数据**——这些请求既没有 `passages_question_set_version`
也没有判定统计量，因此候选对它们既无影响也无从归因。上面 3 条问题里只有 1 条真正走到判定阶段，
其余 2 条都落在这个分支。# 若要扩大可灰度面，先补这条分支的归因（或提高 preflight 置信度）。

**"输出更紧凑"的调研（负结果，如实记录）**：同一问题、同一证据包，用真实 provider 对比三种写法：
| 变体 | prompt 字符 | max_tokens | finish_reason | reasoning_tokens | 正文 | 契约 | 类别 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 现状 | 8958 | 8000 | stop | 6714 | 1384 字符 | 通过 | generator_ignored_evidence |
| B 加"每字段一句话、正文 ≤500 字" | 9023 | 8000 | **length** | 8000（吃满） | **0 字符** | 失败 | —— |
| C B + 预览 24→8 | 5165 | 16000 | stop | 6853 | 622 字符 | 通过 | generator_ignored_evidence |

结论：要求"更紧凑"**没有**降低推理开销（B 反而把预算全花在推理上、正文为空）；把证据包从
8958 字符压到 5165 字符，推理仍在 6.7–6.9k。也就是说**推理开销由问题与判定难度决定，
不由提示词长度决定**；而两次给出合法 JSON 时类别仍是 `generator_ignored_evidence`（标签为
`ranking_error`）。**"输出更紧凑"这条路线不成立**，剩下的选择只有两个：放宽上限+超时
（用尾延迟与成本换可用的草稿），或不动、接受这一族按契约违规/超时在下一轮重试。

**仍未解决（如实记录）**：`ranking_error` ↔ `generator_ignored_evidence` ↔ `undetermined`
三者的边界在证据层面（内容存在/被采用/被生成器忽略）本就相邻，模型输出在同一证据包上
不稳定；要真正收敛，方向是"把被采用证据与生成器输入做确定性比对"（像复现检查那样），
而不是继续调提示词措辞。


**标签审计（唯一一次改标签，附证据）**：初版给 real-003/006 及其变体标了
`ranking_error`，但那是“疑似假拒答、待下钻”的未验证假设。全库核查（遍历 KB 全部
27/38 个块搜索答案内容）证明：库内**不存在**“发货地”内容（仅催单话术出现“发货”一词）、
**不存在**“人员盘点范围”定义（只有空白盘点模板）——模型判 `undetermined`（内容缺口）
是对的，标签改判。该修正使一致率从 75% 升至 86%，修正依据是全库搜索的实证输出
（label-verification.json，本地留档），不是迁就模型。real-020（副厂配件明细）维持
ranking_error：适马副厂遮光罩表内容确实存在。

**遗留问题（如实记录）**：
1. 契约违规率约 2/30（模型输出缺字段或破 JSON 契约）——按设计不产半成品、下轮重试；
2. ranking_error 与 generator_ignored_evidence 存在边界模糊，判定口径需第 4 阶段细化；
3. 单次调用 10–38 秒（24 块预览使提示词变大），5 成员以上的观测仍需关注预算；
4. 开发集集中在三个测试库与两个域，泛化到生产库需管理员确认标签后复测。

## 6. 回退

1. 停 worker：重启 API 前设 `RAG_EVOLUTION_ENABLED=false`（或 `python -m rag.evolution.worker`
   不跑任何 cron/计划任务）。
2. 删 `data/evolution.db`（草稿、审计、观测一并清除；该文件默认纳入备份，删除即放弃）。
3. 代码回退：诊断相关文件（`rag/evolution/{evidence,diagnoser,worker,store,api,models}.py`）
   全部为第 3 阶段增量，`git revert` 第 3 阶段提交不影响第 2 阶段观测。

## 7. 观测与排障

- 日志键：`evolution_diagnosis_unavailable`（reason=timeout/rate_limited/invalid）、
  `evolution_diagnosis_cluster_failed`、`evolution_diagnosis_resources_unavailable`。
  日志只含聚类 id 与原因分类，不含提示词/原文/密钥。
- 草稿与审计只能在 `evolution.db` 里看（管理员工具）；API 序列化前经
  `redact_source_config` 脱敏密钥形字段。
- 单轮成本上界：预算 3 × (超时 150s) = 最坏 7.5 分钟（与调整前的 5 × 90s 相同）；
  单次实测 P50 40s（第 5.2 节基线）。
