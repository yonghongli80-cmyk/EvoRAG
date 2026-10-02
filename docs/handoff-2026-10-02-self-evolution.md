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
