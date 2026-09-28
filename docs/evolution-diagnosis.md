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
| `evolution_diagnosis_model_calls` | 5 | 每轮最多诊断的聚类数；**成功与失败都计 1**，达到即停，剩余聚类下轮重试 |
| `evolution_diagnosis_timeout_seconds` | 90 | 单次调用超时，注入诊断专用客户端（与在线生成的 30s、断路器相互隔离） |
| `evolution_diagnosis_max_tokens` | 8000 | 单次完成 token 上限。该提供商的 reasoning token **计入**完成预算：无上限时调用可达数分钟，过小（如 2000）会把 JSON 正文挤没 |
| `evolution_diagnosis_labels_path` | 空 | 管理员确认标签 JSON 路径（证据最高优先级）；为空时草案根因只能是假设级（后校验强制） |

诊断模型复用 `RAG_DEEPSEEK_*`（当前 Command Code `deepseek/deepseek-v4.1-flash`，
推理模型）。**注意**：它每次调用都产生 reasoning token，实测诊断尺寸提示词下单次
10–80 秒不等，5 成员聚类的提示词可能超过 90s 超时——超时不是故障，该聚类无草稿、
下轮自动重试。

## 3. 一次诊断里面发生什么

1. `clusters_awaiting_diagnosis_without_draft` 取待诊断聚类（最旧优先，已生成草稿的不重复）。
2. `assemble_evidence` 装配证据包（**确定性优先**，不调模型）：
   - 管理员确认标签（按规范化问题文本匹配，`priority="admin_confirmed"`）；
   - 同 kb 成功轨迹摘要（`answer_source="system_history"`，**不得**当管理员答案引用）；
   - 以系统管理员主体做只读 ACL 重检索，取块哈希；
   - **复现检查**：失败时 offered 的块哈希是否仍全部在重检索结果中——检索级、确定性，
     不可复现则该聚类建议 `needs_more_evidence`。
   - 哈希列表按排序取前 12 条进入提示词（控制成本），复现检查仍看全量。
3. `Diagnoser.draft`：恰好一次模型调用，版本化提示词（`DIAGNOSIS_PROMPT_VERSION`，
   当前 `diagnosis-v3`），输出经 Pydantic 校验 + 后校验硬规则：
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

**三轮迭代过程（每轮都改机器不改标签，唯一一次改标签有据在案）**：
| 轮次 | 一致率 | 发现 | 机器侧修订 |
| --- | --- | --- | --- |
| v1（每聚类一条） | 0/1（混合标签无法对分） | 聚类键混合根因 | 改为逐观测诊断 |
| v2（逐观测，仅哈希证据） | 1/27 (4%) | 提示词渲染记录器的 root_cause_feature，模型复读它；仅哈希无法区分三类 | 提示词移除该字段；证据包加入重检索块预览；提示词写明三类判定口径 |
| v2b（预览 8 块） | 21/28 (75%) | 预览太少，深层相关块不可见 | 预览上限 8 → 24 |
| v2c（预览 24 块） | **24/28 (86%)** | 4 个偏差均为边界类（ranking_error↔generator_ignored_evidence、轻微漂移） | —— |
| v3（问题词覆盖度信号，仅作证据渲染） | 见下方"v3 复核"，未改变 24/28 的参考口径 | 覆盖度是确定性证据，但它**不是**那一族问题的主因 | 证据包加入逐词覆盖度（命中块数 + 最早排名）并渲染；**不附加判定口径** |

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
因此默认值暂不动（8000），取舍交管理员决定：放宽上限必须与放宽超时、
或与"缩小证据包/要求更紧凑输出"一起评估；单纯调一个值只是把截断换成超时。

**测量口径的诚实说明**：本节 v3 的数字都在**12 条子集**上测得，不是原来的 28 条——
上一轮会话已把多数聚类推进过 `awaiting_diagnosis_review`，本轮 harness 的
`cluster_once` 扫描 0、只回来未被诊断过的 12 条（`devset-per-observation.json` 已被
本轮的中性 v3 结果覆盖）。因此 **24/28 = 86% 仍是唯一的参考验收数字**，v3 只声称
"未在可测子集上退化"（11/11 对 v2 的 12/12），不声称提升。中间产物留在本地
`real-case-handoff/`：`devset-v3-run1.json`（带口径第 1 次）、`devset-v3-naive-run2.json`
（带口径第 2 次）、`focused-coverage-ab.json`、`focused-coverage-repeat.json`、
`focused-truncation-probe.json`。

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
- 单轮成本上界：预算 5 × (超时 90s) = 最坏 7.5 分钟；成功调用实测 10–80s。
