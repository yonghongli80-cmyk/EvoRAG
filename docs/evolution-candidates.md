# 自进化候选生成与门禁运维说明（evolution-candidates）

第 4 阶段为**已确认根因并获管理员批准**（`approved_for_candidate`）的失败生成唯一一种
低风险候选——**Jev 问题集文本变体**（`jev_question_set`，target ∈ preflight/passages），
并按设计第 13 节五层门禁逐层放行。本文件覆盖启用、五层流程、灰度/回滚、审计与关闭。
设计依据 `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md` 第 12–15 节，
实施计划 `docs/superpowers/plans/2026-09-24-assisted-self-evolution-candidates.md`。

## 1. 开关与默认（全部默认关闭）

| 设置 | 默认 | 语义 |
| --- | --- | --- |
| `RAG_EVOLUTION_ENABLED` | false | 第 2/3 阶段总开关（观测/聚类/诊断） |
| `RAG_EVOLUTION_CANDIDATE_ENABLED` | false | 第 4 阶段开关：API pipeline 注入灰度控制器、启动时恢复灰度轨迹 |
| `RAG_EVOLUTION_CANDIDATE_CANARY_PERCENT` | 0 | 灰度百分比**硬上限**；0 = 不配置就不允许任何在线放量（canary 端点 400） |
| `RAG_EVOLUTION_CANDIDATE_MODEL_CALLS` | 2 | 每轮候选生成的模型调用预算 |
| `RAG_EVOLUTION_CANDIDATE_SHADOW_SAMPLE_RATE` | 0.05 | 层 3 影子对比的流量采样率 |

## 2. 五层门禁（按序，不可跳过、不可弱化）

| 层 | 名称 | 做什么 | 通过后状态 | 失败 |
| --- | --- | --- | --- | --- |
| 1 | scope | 载荷边界：只改 `instructions`/`criteria` 文本；问题 id/数量/primitive 不可动；字段级编辑距离 ≤40%、instructions ≤500、criteria 每条 ≤200 | `candidate_testing`（首条 gate_log） | 拒绝（PayloadBoundaryError） |
| 2 | benchmark | 固定基准集（20 用例 A/B 集）上候选 vs 当前：**路线准确率不降（≤0pp）、Top-5 降幅 ≤2pp**，逐用例留痕 | `awaiting_verification` | `rejected` |
| 3 | shadow | 在线旁路采样（默认 5%）双服务对比（当前集 vs 候选集）：路由一致率、段落选择 Jaccard、可用性差异；不阻塞、不改响应 | 采样达量且无回归 → 可进层 4 | 不达标不出结论 |
| 4 | canary | 管理员设百分比放量（`POST /candidates/{id}/canary`，受配置硬上限约束）；注册候选问题集；同一时间仅一个候选在线 | `approved_for_canary → canary` | 非法状态/超上限 409/400 |
| 5 | accept/rollback | 稳定后管理员确认（`→ stable`）；任何时刻 `POST /candidates/{id}/rollback` 秒级置 0% + 反注册 + 审计 | `stable` | `rolled_back` |

阈值常量在 `rag/evolution/candidate_gates.py`（`ROUTE_DELTA_MAX_PP = 0.0`、
`TOP5_DELTA_MAX_PP = 2.0`），测试钉死含 -2.0pp 闭区间；**不得为让候选通过而放宽**。

## 3. 在线分流与回滚的语义

- 分流决策：`sha256(request_key) % 100 < percent`——**确定性**（重启不重排，日志可归因）；
  request_key = `kb|session|question`（ContextVar 作用域，并发请求不互覆）。
- 0% 时 `provide()` 直接返回当前问题集：**漏接线/零配置不可能把流量导给候选**。
- 候选服务期间的版本归属：preflight 侧写回 `decision_metadata.jev_question_set_version`；
  passages 侧暂无版本字段（`PassageJudgment` 无该字段），响应级归属留待补 `models.py` 字段——
  这是已知限制，见第 5 节。
- 回滚 = 反注册 + 百分比 0 + 审计 + 状态 `rolled_back`；重启后 `restore()` 只恢复
  `canary/stable` 状态的候选，已回滚的不会复活（单元测试 + e2e 双重钉住）。

## 4. 端到端留痕（2026-09-24 首跑，真实模型调用）

输入：第 3 阶段开发集中聚类 3（kb_accessories）经管理员批准
（`approve_for_candidate`，审计在案），取其一条 `generator_ignored_evidence / prompt` 层草案。

| 层 | 结果 | 证据 |
| --- | --- | --- |
| 生成 | ✅ candidate#1（passages，48 问，40.5s，confidence=low，rollback_ref=passages-v1+sha256） | e2e-candidate-part1.json |
| 层 2 基准 | ✅ 通过：路线 delta +0.0pp、Top-5 delta +0.0pp（20 用例零回归）→ awaiting_verification | 同上 |
| 层 3 影子 | ✅ 10 样本：路由一致率 1.0、双侧可用 10/10、可用性差异 0（selection 未测：冒烟未传 candidates） | e2e-candidate-part2-shadow.json |
| 层 4 灰度 | ✅ canary 5%：状态 canary、审计 canary_percent=5；在线 8/8 请求正常 | API 交互记录 |
| 层 5 回滚 | ✅ rollback：状态 rolled_back；重启后注册表仅默认版本、snapshot 空 | API + 重启验证 |

过程修复两处真实缺陷（均有回归测试）：管理员批准对“准入后加入的 observed 成员”
状态不可达（409）；启动恢复把 `FailureTraceStore` 传给了需要 `CandidateStore` 的
`restore()`（服务起不来）。

## 5. 已知限制（如实记录）

1. **响应级版本归属不完整**：passages 候选的灰度无法从 `decision_metadata` 直接观测
   （无版本字段），在线验证依赖端点/审计/注册表与单测；补 `PassageJudgment` 版本字段后补齐。
2. **候选生成对提示词尺寸敏感**：passages 全量问题集（48 问）下 8000 token 曾截断 JSON，
   e2e 用 16000 通过；生产默认 8000，聚簇候选较大时可能需临时调大或改提示词要求稀疏载荷
   （只输出实际变更的 id）——后者需要载荷契约放宽，列为后续事项。
3. **层 3 无自动样本量阈值**：evaluator 只报计数，“达量且无回归才进层 4”的判定在
   gate runner/人工，尚未自动化。
4. **同一时间仅一个候选在线**（控制器单例语义）。
5. 候选审计在新表 `evolution_candidate_audit`（第 3 阶段 `evolution_audit` 的 CHECK 只放行
   三个管理员决策动作）；API 时间线需读两表——当前详情端点已合并展示。

## 6. 关闭与回退

1. `RAG_EVOLUTION_CANDIDATE_ENABLED=false`（默认）→ 不注入灰度控制器、不恢复轨迹；
   已有候选停在当前状态，在线 100% 当前问题集。
2. 已灰度中的候选先 `POST /candidates/{id}/rollback` 再关开关。
3. 删除候选数据：删 `evolution.db` 即清除候选/草稿/观测/审计（该文件默认纳入备份）。
4. 代码回退：第 4 阶段文件（`rag/evolution/{candidates,candidate_store,candidate_generator,candidate_gates,rollout}.py`、
   `rag/jev/{candidate_shadow,questions,client}.py` 的增量、`rag/evolution/api.py` 与
   `rag/api.py` 的增量）`git revert` 不影响第 2/3 阶段。

## 7. 日常操作速查

```bash
# 查看候选与五层轨迹（管理员）
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:1102/knowledge-operations/evolution/candidates
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:1102/knowledge-operations/evolution/candidates/1

# 灰度 5% / 回滚（管理员，均先写审计）
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"percent":5}' .../candidates/1/canary
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' \
  .../candidates/1/rollback

# 层 2 基准门禁 runner（进程内，需停 1102 释放 qdrant 锁）
python scripts/benchmark_candidate_questions.py --candidate-id 1 --apply \
  --provider "rag.jev.questions:build_candidate_questions"
```
