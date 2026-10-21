# scratch/ —— 一次性排查脚本（不是产品代码）

这些脚本是 2026-10-19 ~ 2026-10-21 两轮排障时写的**取证探针**：每个都对应一次"我怀疑是 X，
先测出来"的动作。它们**不被产品导入、不被测试收集、不在 CI 里跑**，唯一的用途是让下一个人
能复现某个结论，而不是重新推导。

约定：

- 需要 Qdrant 完整检索的先**停服务**（本地模式独占目录），否则会退化成 BM25，
  结论会偏（这一条本人踩过，见交接文档坑清单）；
- 需要模型/重排 key 的读运维本地 `.env`（因此它们永远不该进测试）；
- 已经正式化的两个已移出本目录：`scripts/verify_rewrite_stability.py`、
  以及上一轮的 `scripts/verify_structured_query_live.py`。

## 与真实结论对应的关键脚本（要复现先看这几条）

| 脚本 | 证了什么 |
| --- | --- |
| `probe_rewrite_variance.py` | **本轮最重要的结论**：同一问题 6 轮改写 6 个句子，重排 top 分跨 0.35 两侧 ⇒ `real-020` 忽答忽拒的根因是改写，不是检索/判定/重排。正式版见 `scripts/verify_rewrite_stability.py` |
| `locate_020_instability.py` | 逐层排除：6 轮候选池签名完全相同、`passage_judged=0` ⇒ 检索稳定且判定没跑，排除了前两个嫌疑 |
| `probe_reranker_fallback.py` | 排除重排降级：30 个块 `score_source` 全是 `reranker`，分数真实存在 |
| `replay_c30_fixed.py` | 候选 #30 的措辞有效：**同一固定 12 块池**，旧措辞 7/12 判为证据（首位 0.65），新措辞 11/12（首位 0.83） |
| `find_dropped_clarification.py` | 澄清被拒答顶掉：`镜头价格合计` 规划器要澄清、检索 7 来源、证据门拒答 ⇒ 旧逻辑丢澄清 |
| `verify_kind_db_stream.py` | `details_structured_query_kind` 落库的唯一成功样本来源（`not_applicable`） |
| `negcontrol_position.py` | 答案位置判据的**阴性对照**：关掉 Jev 后必须报错（0.250，2 条 escape + 1 条 miss），证明判据不是永远 PASS |
| `variance_decision.py` | 判定抖动的幅度（同问题 6 轮 3:3）——投票修复的动机 |
| `cost_repeat.py` | 单次段落判定约 0.3s、5 轮分数 0.50/0.52/0.49/0.54/0.50 ⇒ 投票的成本依据 |

## 其余脚本按用途分组

**候选 #30 取证**

| 脚本 | 用途 |
| --- | --- |
| `inspect_30.py` | 找候选表、看行数与表结构 |
| `inspect_c30.py` | 打印候选 #30 的完整记录与 payload 前 6000 字 |
| `inspect_c30_audit.py` | #30 的审计轨迹 + 全部候选状态 |
| `replay_c30.py` | 用真机客户端重放 #30（初版：两轮各取各的池，不可比，已被 `_fixed` 取代） |

**结构化链路**

| 脚本 | 用途 |
| --- | --- |
| `probe_coordinator_kind.py` | 协调器对各问句返回的 kind（证明 `real-020` 是 `not_applicable`） |
| `probe_planner_clarify.py` | 哪些问句会触发规划器澄清（`ambiguous_field` / `cross_sheet_unsupported`） |
| `trace_chain.py` | 给 `_attempt_structured_query`/`_is_structured_clarification`/`_finalize` 打点，看完整决策链 |
| `trace_discard.py` | 追踪澄清是否被丢弃（来源数 vs 决策） |
| `repro_real020.py` | 复现 `real-020` 的结构化尝试结果（返回 None） |
| `inspect_anomalies.py` | 查 dataset 9（文档 19 旧版本残留）与 `candidate_testing` 候选 |
| `verify_structured_kind_db.py` | 初版：用 `_prepare_context` 验证落库——**它不提交观测**，所以拿不到数据；留作对照 |

**real-017 / 脱敏价格**

| 脚本 | 用途 |
| --- | --- |
| `repro_real017.py` | 复现 `answerable` 判定，打印上下文重排分（证明证据存在但价格是 `<金额>`） |

**检索与召回**

| 脚本 | 用途 |
| --- | --- |
| `probe_recall_020.py` | `real-020` 族的 24 块召回明细，看答案行排第几 |
| `trace_legacy_020.py` | 旧链路逐步：route → 改写 → 候选 → 重排分数 |
| `trace_real020.py` | `_prepare_context` 全流程追踪 |
| `diag_pool_vs_vote.py` | 区分"同池内投票抖动"与"跨请求池差异"（结论：池稳、投票也稳） |

**判定与观测**

| 脚本 | 用途 |
| --- | --- |
| `dist_020.py` | `real-020` 历史观测的 (route / answerability / structured_kind) 分布 |
| `obs_meta.py` | 读最近观测的 `decision_metadata` |
| `diag_escape.py` | 逐条标签订用例打判定，找"该拒却答"的那条 |
| `live_020.py` / `live_020_detail.py` | 真机 SSE 跑 `real-020` 族（后者打印全部事件类型与 `reliability`） |
| `live_clarification.py` | 真机核验 6 条澄清用例的"答案赢 / 澄清返还" |
| `verify_kind_live.py` | 走真机端点验证 `structured_query_kind` 落库 |

**投票实现期调试（历史，已定论）**

| 脚本 | 用途 |
| --- | --- |
| `dbg_vote.py` / `dbg_vote2.py` / `dbg_vote3.py` | 投票调用计数与内部状态的逐轮打印；查出"预算被预判吃掉一轮"的 bug |
| `dbg_clarify.py` / `dbg_clarify2.py` | 澄清判据的独立复现；后者定位出"测试缺 `user_db` ⇒ 入口变 `permission_limited`" |

**数据速查**

| 脚本 | 用途 |
| --- | --- |
| `list_tables.py` | 列出 `users.db` 全部表 |
| `inspect_evolution.py` | `evolution.db` 的表与行数 |
| `inspect_cases.py` | 固定 A/B 用例集的 kb / route 分布 |
| `rollout_state.py` | 灰度控制器状态与当前服务的问题集版本 |
| `collect_handoff_facts.py` | 汇总交接文档引用的数字（镜像 / 候选 / 观测 / 设置），避免凭记忆写 |
| `compare_principals.py` | 比较 `user 1` 与 `evolab-admin` 对同一问句的判定 |

**非脚本**

| 文件 | 说明 |
| --- | --- |
| `commit_msg.txt` | `git commit -F scratch\commit_msg.txt` 的提交信息草稿（含引号时避免 shell 解析问题） |
| `test_live_e2e_pipeline.py` | 上一轮留下的真机 E2E（9 步：健康检查 → 注册/登录 → 多租户 KB → 上传/版本/索引 → 检索 → 反馈 → 数据源脱敏 → Wiki → 删除与越权边界）。它会在隔离快照上跑并自行清理；**文件名带 `test_` 但不属 pytest 收集范围**（不在 `tests/`，且需真实服务） |
