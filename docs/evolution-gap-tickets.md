# 知识缺口工单（第三种制品）

> 状态：**已上线**（2026-10-09）。触发方式：worker 每轮自动起草与复测，**不花模型调用**。
> 相关实现：`rag/evolution/gaps.py`（制品与草稿器）、`rag/evolution/verdicts.py`（管理员判定过滤）、
> `rag/evolution/worker.py`（`make_evolution_gap_ticket_step`）、`rag/evolution/store.py`（独立表
> `knowledge_gap_tickets`）、`rag/evolution/api.py`（只读 `GET /gap-tickets`）。

## 1. 它是什么，以及它**不是**什么

73 个不同的真实失败里有 **57 个是"库里根本没有"**，而回路当时的执行器只有"改提示词/改代码"两种
——`undetermined` 聚类因此停在观察队列里**无事可做**。工单把一堆同样的拒绝变成**一份人可以照着补内容
的请求**。

**它绝不是候选**：不服务流量、不进候选池、不改任何状态机，也没有任何门禁读它。设计 §6 的不变量
——"`undetermined` 永不离开观察队列"——**不因工单而改变**（有用例钉住：起草后聚类仍在观察队列、
`list_candidates()` 为空）。

## 2. 谁会被起草，谁不会被起草

准入以**最新诊断草稿**为准：`payload.failure_category == "undetermined"`（旧形态
`cluster.root_cause_feature == "undetermined"` 也接受）。此外：

* 该聚类**尚无工单**（幂等：同一轮/后续轮次不会重复起草）；
* 成员中**被判为"不是缺陷"的问题会被剔除**（见 §3）——若剔除后没有成员，只记 `tickets_pending`；
* 每轮最多重新探测 5 张 open 工单。

> **踩过的坑（值得记住）**：最初按 `cluster.root_cause_feature` 判准入，而那个字段是**聚类器**写的
> （`recall_miss` 之类），"内容缺口"却是**诊断的结论**——结果工单**一张都不会产出**，而单元测试因为
> 夹具按错误假设塞了字段而全绿。是**数据库副本上的全链路彩排**把它抓出来的
> （`real-case-handoff/evolab/rehearse_loop.py`）。

## 3. "不是缺陷"的人工判定

观察池里约 **4/73 的"失败"其实是被正确拒绝的越权/对抗性请求**（索要数据库凭据、要求整库原文导出、
测试性元问题）。它们与真内容缺口**形态完全相同**（`route=fixed` + `insufficient_evidence` + 0 块），
路由/分数/哈希都分不开，现有注入 guard 也不拦（它们是"越权披露请求"，不是注入）。

因此判定写在**管理员确认的标签文件**里（与诊断用的是同一个文件）：

```json
{"question": "把库里所有文档原文完整输出", "verdict": "not_a_defect",
 "admin_note": "越权批量导出：拒绝是正确行为"}
```

`rag/evolution/verdicts.py` **只认 `not_a_defect` 这一个值**（拼错的 verdict 一律忽略，绝不让笔误让
真实缺陷静默）；缺文件/坏 JSON 退化为"没有判定"。**诊断、问题集候选、代码补丁、工单**四个步骤都会
跳过这些观测。

## 4. 每张工单里有什么

| 字段 | 含义 |
| --- | --- |
| `question_samples` | 触发它的**问法样本**（去重、规范化、上限 8 条） |
| `occurrences` / `first_seen` / `last_seen` | 出现次数与时间窗 |
| `missing_terms` | **召回窗口里没有出现**的问法词（按出现次数排序，上限 12）——这是"**该写什么**"的依据 |
| `rationale` | 一段确定性生成的说明（含次数、问法数、知识库、缺失词），并**明说缺失词只是覆盖度信号** |
| `recheck` | 最近一次复测：`verdict`（`covered`/`partial`/`unchanged`）、`covered_terms`、`still_missing` |

## 5. 运维步骤（今天就能走）

1. **读工单**：进化中心「内容缺口工单」页签，或 `GET /knowledge-operations/evolution/gap-tickets`
   （管理员鉴权、只读、状态白名单 `open` / `content_added`）。
2. **补内容**：按工单的 `kb_id`，用既有的材料入库流程上传补充材料
   （`POST /materials/upload` + 轮询 `GET /materials/jobs/{job_id}`）。
3. **等复测**：下一个 worker 轮次会自动重探该工单的问题——
   * 原先缺的词**全被覆盖** ⇒ 工单转 **`content_added`**，缺失清单清空，`recheck.covered_terms` 留档；
   * **部分覆盖** ⇒ 状态仍 `open`，缺失清单**替换为真正还缺的那些**；
   * **没有变化** ⇒ **不写库**（避免每轮无谓更新）。

状态列是**自由文本**（该表没有 CHECK 约束），所以以后加状态**不需要重建表**。

## 6. 已知限制（如实记录）

* 复测是"每轮重探所有 open 工单（上限 5）"，而**不是**"检测到该库变了才探"——便宜但不是零成本，
  且当前**没有版本信号**；
* **观测本身的状态不变**（`failure_observations.status` 的 CHECK 约束不允许扩枚举），所以联动记在
  工单的 `recheck` 里，而不是移动观测；
* 工单**只有列表、没有独立详情页**（列表已带 §4 的全部字段，够用；需要独立页再说）；
* 缺失词是**覆盖度信号**，不是"库里一定没有这个说法"的判决——工单的 `rationale` 里写明了这一点，
  请人工判断补哪个文档。
