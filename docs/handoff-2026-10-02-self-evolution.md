# 辅助自进化交接文档（2026-10-02）

> 上一份：`docs/handoff-2026-09-28-self-evolution.md`。本文只写**这一轮长会话**做了什么、
> 实测到什么、以及**下一步该按什么顺序做**。所有数字都能追溯到仓库内提交或本地测量文件
> （`real-case-handoff/evolab/`，未入库），不含主观估计。

## 0. 一句话现状

五层回路（观测 → 聚类 → 诊断 → 候选 → 门禁 → 灰度 → 归因 → 回滚）**端到端跑通且有受控实测**：
20% 灰度下同族问题的 "找到答案证据" 比例 **11/15 vs 基线 5/15**；同时**修掉 5 个归因缺口**、
**留档 3 条被实测否决的规则**，并用数据确认了三个瓶颈（失败族构成、`ranking_error` 族判断、
代码补丁生成稳定性）。**系统仍然永不自动应用/合并**：层 4 之后一律是人的动作。

## 1. 本会话 17 笔提交（`ab1f5c3..1ee297e`）

| 提交 | 主题 | 一句话 |
| --- | --- | --- |
| `ab1f5c3` | 已批准聚类仍欠草案 | 诊断队列原来只收 `awaiting_diagnosis_review`，配件聚类 26 个成员里 25 个永远拿不到草案 |
| `bf03ba8` | 请求 pipeline 走注入工厂 | `/query/stream` 对非默认库直接构造 pipeline，拿不到灰度控制器 |
| `cf68ecb` | 控制器交给 Jev 客户端 | `rollout.provide()` 在 `rag/` 里从未被调用 → 灰度注册了也永远不服务 |
| `b5a067b` | 两个构造用例与机器开关解耦 | 本机开关一开就误报 |
| `8871d91` | 确认真实门禁语料 24 条 | 草稿里每条路由/库都是 `unknown`，门禁只能跑 `rag_docs` |
| `278ef60` | 记录判定层看到了什么 | `passage_judging`（judged/relevant/evidence/contradicted/injected）进轨迹与证据包 |
| `1de45f5` | 管理员标签通道 + 首次灰度结果 | 标签让"内容存在却未被采用"这族可诊断，且 0 误伤 |
| `90a4807` | 放行标签文件 | `benchmarks/` 是白名单制 |
| `ae9ec75` | legacy 分支归因 | 降级请求此前连"是谁降级的"都不记 |
| `f7b4b43` | 流式失败留痕 | 失败请求此前 0 轨迹 0 观测 0 跳过记录 |
| `d6c62bb` | 门禁未决不烧候选 | 基础设施故障被判成 `rejected`（终态），白烧候选 |
| `03f34b9` | 无版本块在证据边界摘除 | `ValueError: versioned evidence required` 让整条请求失败 |
| `3a88417` | BM25 快照带版本元数据 | 快照只有 text/doc_name/chunk_index，降级检索拿不出可引用证据 |
| `0df1843` | 异常失败按缺陷判类 | 6 条 `ValueError` 观测被误判成 `intent_route_error` |
| `f3ef9c7` | 聚类器异常优先 | `exception_class ⇒ code_defect` 被召回启发式挡在前面，**永远不可达** |
| `a76dced` | 留档被否决的提示词规则 | 散文字段限长规则实测无效，撤回但记录 |
| `1ee297e` | `invalid` 同轮重试一次 | 计入每轮调用预算；timeout/限流不重试 |

## 2. 实测数字（唯一可信的"效果"表）

| 测量 | 数字 | 留档 |
| --- | --- | --- |
| 五层回路（候选 #9，20% 灰度，3 题 × 5 重复 × 两侧 = 45 请求） | 桶内 **14/15 由候选服务**、有来源 **11/15**；桶外 15/15 基线、有来源 5/15 | `canary-focused.json` |
| 逐题（同上，**不均匀**） | 「帮我找副厂配件清单」**5/5 vs 0/5**；「找一下第三方的配件表」4/5 vs 2/5；「第三方配件都有什么」2/5 vs 3/5（噪声内） | 同上 |
| 单题受控（候选 #5，5%） | 桶内 `passages-v1+candidate-5` + evidence=1 + 5 来源；桶外 `passages-v1` + evidence=0 + 0 来源（各 2 次） | `canary-attribution.json` |
| 门禁（层 2/3） | 候选 #7：0pp 通过；**候选 #6：−5.0pp 被否（真实回归）**；候选 #8：因基础设施崩溃被误拒（已修） | `real-corpus-measurement.json` |
| 开发集一致性（`diagnosis-v5`，无标签，2 重复，39 可测） | 多数派 **33/39 = 84.6%**（每次 87.2% / 84.6%）、稳定 34/39、契约违规 4/78 = 5.1%、p50 27.9s | `devset-v5.json` |
| 参考口径（`diagnosis-v2`，3 重复，28 条） | 24/28 = 86% | 上一轮文档 |
| 重试效果（`ranking` 族，15 抽样） | 一次成功 12、**重试后成功 3**、仍失败 0 → **有效失败率 0%**（重试前 20–26.7%）；总调用 18（+20%） | `ranking-retry-effect.json` |
| 快照 v2 效果 | 强制密集失败：修前 `ValueError`/0 来源 → 修后 **`route=fixed` + 5 条可引用来源**；5 个集合按需自愈（104/27/25/38/146 行全带版本 id） | 本文 §4 |
| 异常缺陷诊断 | 6 条 `ValueError` 观测：v4 **5×`intent_route_error` + 1×`undetermined`** → v5 **6/6 `code_defect` + `code`** | `exception-diagnosis-ab.json` |
| 失败族构成（73 个不同失败问题） | 可写标签 **4（5.5%）**、内容缺口 **57**、未核对 12 | `labellable-audit.json` |

## 3. 三条被实测否决的规则（不要再试）

1. **覆盖度口径**（2026-09-28）："有覆盖 ⇒ `ranking_error` / 无覆盖 ⇒ `recall_miss`" 污染了
   原本判对的内容缺口子集（12/12 → 10/11、8/8），撤回；只保留覆盖度作为**证据**渲染。
2. **判定统计量口径**（2026-09-29）："`relevant>0` 且 `evidence==0` ⇒ 取 `prompt`/`query_rewrite`"
   把 5 条内容缺口中的 **2 条**推成判定层缺陷；撤回后复测 0/5 误伤。
   （`diagnosis-rule-ab-with-rule.json` / `diagnosis-rule-ab.json`）
3. **散文字段限长规则**（`diagnosis-v6`，2026-10-01）：为治 JSON 截断而加"禁贴哈希 + 每字段
   ≤120 字"，在该族 5 条 × 3 重复上契约违规 **4/15 = 26.7%**、一致率仍 0/5，**无改善**，撤回。
   （`ranking-v6.json`）

**共同教训**：启发式信号可以**渲染成证据**，但不能**推出分类口径**；规则必须有可测的正样本
才算数。

## 4. 五个归因缺口（都已修，附证据）

| 缺口 | 证据 | 修复 |
| --- | --- | --- |
| legacy 降级不留痕 | 同一问题 3 次里 2 次只有 `legacy_route`，无预判版本/原因 | `ae9ec75`：降级分支同样附加 Jev 阶段元数据（实测 `reason=structured_low_confidence`、`conf=0.47/0.52`） |
| 流式失败不留痕 | HTTP 200 + 单个 `error` 事件，**0 轨迹 0 观测** | `f7b4b43`：准备/流式两处兜底；传输故障记跳过、其余记观测 + `exception_class`（实测 3/4 失败 → 3 条 `route=error` 轨迹） |
| 门禁把基础设施故障当候选失败 | 两侧同崩 8 用例 → `rejected`（终态） | `d6c62bb`：`GateResult.inconclusive` + 未决不计入门禁结果（实测：超时轮保持 `candidate_testing`，下轮重试并通过） |
| 无版本块让整条请求失败 | `ValueError: versioned evidence required`（`bundle.py:29`）；原因是 BM25 快照无版本元数据 + 管理员绕过版本过滤使守卫失效 | `03f34b9`（证据边界摘除无版本块）+ `3a88417`（快照 v2，按需自愈） |
| 异常缺陷被解释成路由问题 | 6 条 `ValueError` 观测判成 `intent_route_error` | `0df1843`（证据包渲染异常 + v5 口径）+ `f3ef9c7`（聚类器异常优先） |

## 5. 必须分清的界限：**格式契约 ≠ 判断正确率**

`invalid` 重试把"出不了草案"从 20–27% 降到 **0%**（实测 15 抽样），但那些重试恢复出来的草案
类别仍是 `undetermined`，而该族期望 `ranking_error` → **`ranking_error` 族一致率仍 0/5**。
即：契约问题已解决并实测；**判断/类别问题仍未解决**，且已两次证明不能靠规则推。

## 6. 三个瓶颈（数据，不是印象）

1. **失败族构成**：73 个不同失败问题里 78% 是真内容缺口，只有 5.5% 可写标签。问题集候选
   （唯一可灰度产物）要求草案落在 `prompt`/`query_rewrite`，因此**供给天然稀缺**。
   打标签能把命中族推到可寻址层（一次 4/4、一次 2/4，**有 run 间方差**）。
2. **`ranking_error` 族的判断**：0/5 一致率 + 契约违规集中在此族；截断的直接成因已查明
   （模型把 8–12 个 64 字符哈希粘进散文字段），但**限长规则无效**，重试只治格式。
3. **代码补丁生成**：同一诊断 5 次尝试里 **1 次成功**（3 次模型只回 9 字符、1 次 diff 改
   `tests/**` 被红线正确拒绝）；成功的那份**打偏**（改 `route_question`，而缺陷在证据边界）；
   沙箱只能证明"测试通过"，**基准回放不可用**（子进程拿不到知识库数据）。

## 7. 本机状态与复现

**代码/数据**：`HEAD = origin/main = 1ee297e`，工作树干净；全量 **1879 passed / 1 skipped**。
观测 158 / 聚类 8 / 草案 162（v3 77、v4 79、v5 6）/ 候选 9。
候选：#3、#10 为 `code_patch/approved_for_canary`（补丁已导出待人应用），#4/#5/#7/#9
`rolled_back`（**终态，不可再灰度**），#6/#8 `rejected`；聚类 #3 已批准且成员 112。

**`.env`（本机，会改变行为）**：`RAG_EVOLUTION_ENABLED=true`、`RAG_EVOLUTION_CANDIDATE_ENABLED=true`、
`RAG_EVOLUTION_CANDIDATE_CANARY_PERCENT=20`、`RAG_JEV_KB_ALLOWLIST=rag_docs,kb_hr,kb_accessories,kb_after_sales,kb_evolab`、
`RAG_EVOLUTION_DIAGNOSIS_LABELS_PATH=D:/Desktop/RAGv3/benchmarks/diagnosis-labels.json`。
**当前无候选在服务流量**（全部 rolled_back/rejected）。

**复现命令**（都需要一份 Qdrant 快照，因为服务占着 `qdrant_data` 的锁）：
```powershell
# 协议：开发集一致性
python scripts/run_diagnosis_devset.py --cases real-case-handoff\diagnosis-devset-cases.json `
  --repeats 2 --label diagnosis-v5 --qdrant-path $snap
# 灰度受控对比（需先 POST canary）
python real-case-handoff\evolab\verify_canary_focused.py --password $pw --candidate-id N
# 沙箱（层 2/3，导出前）
python real-case-handoff\evolab\sandbox_stored_patch.py --candidate-id 10
```
**本地留档**：`real-case-handoff/evolab/README.md` §1–§13 是这一轮的过程记录（含两个被否决
的构造、快照 v2、异常闭环），同目录还留了各测量的 JSON。

## 8. 下一步优先级建议

1. **`ranking_error` 族的类别判据**：先做**确定性前置**——"召回块预览里是否真的出现了所问
   的那条事实"（而不是 `relevant>0` 这类启发式），把它当证据渲染；**不要**再加阈值规则
   （已被两次否决）。判据仍用固定开发集口径测。
2. **代码补丁生成的稳定性**：先查那 3 次"只回 9 字符"是不是 provider 侧截断（`completion_chars=9`
   而 `thinking` 已关闭）；再考虑与诊断同一套**预算记账**下的重采样。
3. **沙箱回放**：给沙箱子进程一份只读知识库快照（或允许它连只读副本），否则层 2/3 永远只能
   证明"目标测试通过"，不能证明"无回归"。
4. **已批准聚类的草案供给**：聚类 #3 有 112 个成员、多数无草案（诊断预算 3/轮）。若要让这族
   重新可生成候选，需要提高每轮预算或批量补诊断——这是**成本决策**，由管理员定。
5. 其它仍开着的：`kb_documents` 对 `rag_docs` 仍无登记行（内置库、详情端点 404 属设计）；
   `docs/evolution-diagnosis.md` 与本文的数字都应视为同一来源，改一处要同步另一处。

## 9. 当天补充：`label_evidence` 与"族期望"复核（2026-10-02）

按 §8 的第 1 条做了**确定性前置**：管理员标签里写下的答案型号串，拿去在召回窗口里做
**精确包含检查**（`EvidencePack.label_evidence`，无模型）。命中即"答案文本确实在召回里"，
v7 据此要求判 `ranking_error`（层 `reranking`）。

**实测（4 条已标注的配件族观测，判据 A ≥3/4）**：

| 观测 | 检查命中 | 判出类别 | 说明 |
| --- | --- | --- | --- |
| obs#27 帮我找副厂配件清单 | `LH878-01`/`LH1113-01`/`B060` ✓ | **`ranking_error` + `reranking`** ✓ | 该族**第一次判对** |
| obs#28 第三方配件明细查一下 | `LH878-01`/`B060` ✓ | `invalid`（契约违规） | worker 的同轮重试会救回 |
| obs#8 富士 X-S20… | 无（答案列在窗口外） | `chunking_structure_damage` + `chunking` | 与核实的语料事实一致：该行 22 列、召回块只有前 12 列 |
| obs#7 找一下副厂配件明细 | 无（窗口里没有答案块） | `generator_ignored_evidence` | 其召回窗口不含遮光罩答案行 |

**安全面**：异常族 5/5 仍判 `code_defect`（异常优先），无标签的内容缺口组 5/5 仍 `undetermined`
——这条口径只在"有管理员答案且该答案文本确实在召回里"时触发，**结构上不可能在无标签行误伤**。

**同时暴露的问题（需要人决策，我没有擅自改）**：这一族"期望 `ranking_error`"**并不普遍成立**。
#8 与 #7 的答案内容都不在各自的召回窗口内（#8 是**行被切块**），它们是**取用之前**的
结构/召回问题；系统的判断（`chunking`/生成层）有语料证据支持。也就是说，`ranking_error`
族 0/5 的"一致率"里，**至少 2 条是在量错东西**。开发集期望值的复核属于移动球门，应由管理员定。

**标签侧的必要修正**：判据要求标签**写出答案值**，而原来 3 个标签只"指向内容"。现已把
两个同族标签补上具体型号串（`LH878-01`/`LH1113-01`/`LH716-01`/`B060`），X-S20 那条补记
已核实的切块事实；`benchmarks/diagnosis-labels.json` 随之更新。

## 10. 代码补丁生成的稳定性：已定位并改善（2026-10-02）

§8 的第 2 条做完了，两个原因都是实测出来的：

1. **`thinking: {"type": "disabled"}` 在这个 provider 上不生效**。用同一提示词（30k 字符）
   连打 4 次并读用量：每次 `completion_tokens` 里 **5076–10259 是推理 token**，正文在推理
   **之后**才输出。`max_tokens=16000` 被推理吃掉时，正文只剩 **9 个字符** ⇒ "response
   carries no unified diff"（5 次尝试里 3 次如此）。
   → 新增常量 `PATCH_MAX_TOKENS = 32000` 作为默认预算（不再指望那个开关），空回消失（修后 6 次 0 空回）。
2. **模型把"提供测试"误解成"往 diff 里加测试"**：规则 5 要求元数据 `test_paths`，于是它 3/3 次
   提交新建 `tests/test_spreadsheets_query.py` 的 diff，被 `tests/**` 红线正确拒绝。
   → `patch-v2` 在两处写死这个区分（"测试只进元数据的 test_paths 行，不进 diff"、"diff 不得新建任何文件"）。

**实测（同一诊断 obs#132，每批 3–5 次）**：

| 批次 | 预算 | 提示词 | 成功 |
| --- | --- | --- | --- |
| 1 | 16000 | patch-v1 | **1/5**（3 次 9 字符空回、1 次 tests 拒绝） |
| 2 | 32000 | patch-v1 | **0/3**（空回消失，3 次 tests 拒绝） |
| 3 | 32000 | patch-v2 | **2/3**（残留 1 次 tests 拒绝） |

**仍未解决（下一步）**：成功的补丁（#10/#11/#12）都收敛到"给 `route_question` 加 try/except
兜底"——**症状级修复**，因为诊断里没有"异常发生在哪个函数/帧"。要让它打在根因上，应把
**异常栈的最后一帧**（`rag/...py:行号:函数名`）写进证据包与诊断，再由补丁生成器据此选目标
文件（现在是按诊断关键词从 `rag/**` 里挑 3 个，所以只能猜）。

## 11. 沙箱回放能读到语料了（2026-10-02）

§8 的第 3 条：沙箱子进程会剥掉**所有** `RAG_*` 设置（隔离所需），于是回放连语料都拿不到，
只能报 `benchmark replay did not complete: exit_1` ⇒ **层 2/3 永远只能证明"目标测试通过"，
不能证明"无回归"**。

修法（保守版，默认关闭）：

* 新增设置 `RAG_EVOLUTION_SANDBOX_QDRANT_SNAPSHOT`（`evolution_sandbox_qdrant_snapshot`）：
  运维放一份**只读**知识库副本，把它指过去即可；
* 新增 `_replay_env(worktree, snapshot)`：**只有回放这一条命令**的 env 会带上
  `RAG_QDRANT_PATH=<快照>`；目标测试仍跑在剥离后的真空环境里，隔离语义不变；
* 没配置、或目录不存在 ⇒ 不注入 ⇒ 行为与以前一致（报"回放不可用"，**不是**拒绝，也不是假通过）；
* 既有防空转守卫（`MIN_PREP_P50_MS` + `--user-id`）继续兜底："跑了但什么都没量到"仍判为不可用。

边界说明（安全）：沙箱子进程因此获得对**语料副本**的只读访问；它依旧没有凭据、没有网络、
只写在自己的临时工作树里。是否开启由运维决定，默认关闭。

### 11.1 实测：回放能出结论了，但**这个结论还不是补丁门禁**（2026-10-02）

拿着 ③ 的改动跑真实补丁（候选 #11，只给 `route_question` 加 try/except）验证：

* 好的一面：`benchmark` 不再是 `null`，`error` 变空，**真的产出了门禁结论**（`passed: false`、
  `route_delta_pp: -30.0`、9 条 regressed 用例、耗时 55s）。此前这里永远是
  `benchmark replay did not complete: exit_1`。
* **但结论的含义不对**：`scripts/benchmark_jev_retrieval.py` 的 `old`/`new` 是**"legacy 路径
  vs Jev 路径"**（脚本描述原文：*Benchmark legacy versus Jev preparation paths*），
  不是"打补丁 vs 不打补丁"。所以 `route_delta_pp: -30.0` 说的是 **Jev 比 legacy 差 30pp**，
  与候选 #11 无关。把它当补丁回归门禁 ⇒ 要么全拒、要么以错误的理由放行。

**结论**：③ 修的是"**回放能不能跑、能不能读到结论**"（已修并实测）；
"**结论是不是在量补丁**"还没有——真正的补丁回归门禁需要**打补丁 vs 不打补丁各跑一次**
（即两次回放：干净工作树 + 打过补丁的工作树，用同一主体、同一语料），成本翻倍，属于下一步设计决策。
在那之前，层 2/3 对代码补丁的"无回归"仍然**不成立**：它能证明的只有"目标测试通过"。

### 11.2 双层回放已实现，但它撞上沙箱的**无凭据**边界（2026-10-02）

按 11.1 的结论把门禁改成"**同一回放跑两次**：HEAD 的干净工作树 + 打过补丁的工作树"，逐侧
（legacy / Jev）比较，沿用未放宽的层 2 阈值（路由 ≤0pp、Top-5 ≤2pp、失败调用不增加）。
`patch_verdict` 已实现、`__all__` 已替换、整套沙箱用例按新语义重写并通过（44 项）。
关键实现细节：**两次回放各自 `PYTHONPATH` 指向自己的工作树**，否则两次都在量同一份代码。

**实测（候选 #11，真实补丁，`qdrant_data` + `users.db` 快照、主体 id=1）**：

```
applied: true, tests_passed: true
benchmark: null
error: "benchmark replay measured nothing: the baseline side failed 100 call(s)"
```

**这条错误其实是补丁的真实效果**：沙箱子进程会剥掉所有 `RAG_*`（含模型凭据），而该回放的
legacy 侧要**调模型**——干净树里每次调用都抛异常（100 = 10 用例 × 5 次 × 2 面），而候选 #11
恰好是给 `route_question` 加 try/except 兜底 ⇒ 打过补丁那侧 **0 失败**。也就是说：
**它量到的是"沙箱没有凭据"这个条件，不是语料层面的回归。**

**由此暴露的硬约束（下一步的决策点）**：**依赖模型的基准无法当沙箱门禁**，除非放宽隔离。
两条路：

1. **给回放凭据**（只给回放这一条命令）——立刻可用，但**候选补丁的代码就在那个子进程里跑**，
   等于把凭据交给待评审的补丁：这是安全决策，且违反"沙箱不带凭据"的既有原则；
2. **让回放不依赖凭据**（推荐）：给回放脚本一个"确定性子集"模式——只走检索与确定性路由，
   不调模型；门禁量的是 Top-5/召回这类**确定性指标**。这与沙箱的隔离原则一致，也让门禁可复现。
   代价是要改 `scripts/benchmark_jev_retrieval.py`（它的两侧目前都经过模型路由）。

在选定之前，层 2/3 对代码补丁能证明的仍然只有"目标测试通过"；双层回放的**机制**已经就位，
缺的是一个**能在无凭据沙箱里跑完的回放**。
