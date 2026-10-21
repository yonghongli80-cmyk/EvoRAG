# RAGv3 统一修复计划（2026-10-21）

> **状态：待评审，未实施。**
> 本文把 `docs/handoff-2026-10-21-refusal-scoring-and-rewrite-instability.md` §3 的 9 项遗留，
> 与 2026-10-21 全库审查发现合并成一份可执行清单。基线：`HEAD=89aff32`，工作树干净，
> 后端收集 2313 个用例、前端 231 个用例通过、`ruff check .` 全绿、`/health` 正常。
> 评审通过后按 §7 的顺序实施；本文之外不夹带重构。

---

## 0. 一页结论

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| **P0 在线正确性** | 1 | 查询改写不稳定导致同一问题忽答忽拒（real-020 族） |
| **P1 可立即修** | 2 | 结构化数据集计数全为 0；三处文档口径过期 |
| **产品口径（不写代码解决）** | 3 | 层 2 零净变化语义、检索为空与证据不足是否区分、real-008/022 标签 |
| **运维动作** | 3 | 候选 #30/#31 处置、P99 与观察窗、4 个 `alice_*` 测试账号 |
| **技术债（本轮不动）** | 5 | pipeline 体量、api.py 转发 hack、单机存储、配置膨胀、最终答案缓存停用 |

**明确不改**：`reliability_min_score`、层 2 门禁阈值、证据门判据、Qdrant/SQLite 架构。
**唯一必须现在动的**是 P0-1；P1-1 是纯数据修复，与 P0 无耦合。

---

## 1. 核查方法与口径

- 代码：逐行读 `rag/pipeline.py`、`rag/reranker.py`、`rag/spreadsheets/*`、`rag/evolution/candidate_gates.py`、
  `rag/tracker.py`、`rag/api.py`、`rag/permissions.py`、`rag/org_auth/*`。
- 数据：只读打开 `data/users.db`、`data/evolution.db`（`sqlite3` URI `mode=ro`），不改库、不停服务。
- 测试：`pytest --collect-only` 验证 2313；`pytest tests/...` 定向验证；`ruff check .`。
- 未执行：全量 2313 用例（27–45 分钟）、Docker 构建、真机基准（需停服务独占 Qdrant）。
- 标注约定：**[代码]** 本次读到的实现；**[实测]** 本次命令输出；**[交接]** 沿用交接文档的实测记录
  （本轮未复跑，因为需要停服务与真实模型调用）。

---

## 2. 问题总表

| ID | 来源 | 现象 | 根因（已核实） | 证据 | 处置 |
| --- | --- | --- | --- | --- | --- |
| **P0-1** | 交接 §2 | 同一问题 6 轮 4 拒 2 答 | 经典链路**无条件**用 LLM 改写句做检索与重排；坏改写把"找资料"变成"如何查询资料"，同一候选池重排分从 0.7 掉到 0.03–0.06，跨过 0.35 | [代码] `rag/pipeline.py:573`、`:582`；[交接] 6 轮 6 句、双峰分布 | 代码修复（§3） |
| **P1-1** | 交接 §3.9 | `structured_datasets` 的 `sheet_count/total_rows/total_columns` 全是 0 | 早期镜像路径只写 `structured_sheets/columns`，不写计数；回填函数看到"已有镜像行"就跳过，计数永远不修 | [实测] 11 个数据集计数全 0，而镜像合计 **19 sheets / 789 rows / 192 cols**（逐数据集 1/2/3/3/3/2/1/1/0/2/1）；[代码] `rag/spreadsheets/migration.py:75` | 数据修复（§4） |
| **P1-2** | 全库审查 | 文档与代码不一致 | 模块化早已入库，文档仍写"未提交、HEAD 32f2b7c"；scratch 说"不在 CI 跑"但 ruff 会扫；脱敏价格未记为已知限制 | [代码]/[实测] HEAD 89aff32、api.py 791 行；ci.yml 第 18 行 | 文档修复（§5） |
| **P2-1** | 交接 §3.3 | `details_structured_query_kind` 只有 1 条真机样本 | 生产者已存在且覆盖 `failed`（协调器内部 catch 后返回 failed），缺的是**真机失败样本 + DB 级落库用例**，不是代码 | [代码] `pipeline.py:1092-1094`；[实测] 该列分布 `[(None,181), ('not_applicable',1)]`；测试只断言到 `record`/`detail`，没有写进 `failure_observations` 的用例 | 补一条 DB 级用例 + 文档记录；不改产品代码 |
| **D-1** | 交接 §3.4 | 层 2 门禁在 `route_delta_pp=0.0` 时通过，即使逐例有进有退 | 判定只看聚合 delta + 拒答护栏；逐例回归只记录不判死 | [代码] `candidate_gates.py:424`；[测试] `test_route_delta_of_exactly_zero_pp_passes_even_when_cases_change` | **产品决策**，不改 |
| **D-2** | 交接 §3.5 | 空检索与"检索到但都不过阈值"落到同一个 `insufficient_evidence`、同一句文案 | **交接原措辞需更正**：代码里没有"空检索跳过证据门"的分支。企业模式下 `context=[]` 仍会 `build_bundle=True` → 正常跑 `decide_answerability` ⇒ `insufficient_evidence`（`pipeline.py:587/1022/1035`）。真正跳过证据门的是**非企业模式**：`build_bundle=False` 时整块不执行，直接拿空上下文 `generate()`，有幻觉风险 | [代码] `pipeline.py:587,1022,1035`；`answerability.py:24-42` | **产品决策**，不改 |
| **O-1** | 交接 §3.6 | 候选 #30 留在 canary@0%；#31 停在 candidate_testing | #30 的措辞已作为生产修正落地（`d29e81d`），canary 实际 0%，保留供追溯；#31 是 `code_patch/reranking`，只过了 scope 门禁，补丁改 `rag/reranker.py`（取消 top_k 截断），**不在 HEAD/工作树**，且方向与"双路重排"不同 | [实测] 30 条候选：1 canary、1 testing、20 rejected、6 rolled_back、2 stable | 运维/产品决策 |
| **O-2** | 交接 §3.7 | real-008 / real-022 标签争议 | real-008 期望 structured，但 HR 语料没有招聘/离职统计表；real-022 期望 clarification（只给"r50"） | [实测] `benchmarks/real-cases-v1.json` 的 `expected_position` 为 null | 产品确认标签 |
| **O-3** | 交接 §3.8 | 服务端 P99 未测；"观察窗零样本"；4 个 `alice_*` 账号 | P99 口径已有（`scripts/locustfile.py:412`），缺重构后的运行报告；观测库实际 182 条（2026-09-23..2026-10-21），"零样本"指 **#30 灰度 0% 导致 canary 窗结构上无样本**；4 个 alice 各有 1 条 conversation，成对伴生 4 个 admin_* | [实测] 用户表 13 行；观测 182 条 | 运维动作 |
| **T-1** | 全库审查 | `pipeline.py` 1404 行，`_prepare_context` 一个方法承担守卫/路由/结构化/检索/判定 | 历史迭代叠加 | [代码] | 本轮不动，另立重构 |
| **T-2** | 全库审查 | `rag/api.py` 用模块子类同时写属性与 `__dict__`，兼容测试 monkeypatch | 模块化重构留下的接缝 | [代码] `api.py:585` | 本轮不动 |
| **T-3** | 全库审查 | Qdrant 本地模式独占目录；SQLite 单写；worker 在进程内；Uvicorn 单进程 | 单机设计 | [代码] `vector_store.py:40` | 架构，另议 |
| **T-4** | 全库审查 | `Settings` 113 个字段，大量实验旋钮 | 迭代快于收敛 | [实测] | 观察，不阻塞 |
| **T-5** | 全库审查 | 最终答案缓存 `cached = None`，代码路径仍在 | 等 ACL 版本化，暂时停用 | [代码] `pipeline.py:501` | P2：要么删死代码，要么注明条件 |
| **N-1** | 我的审查更正 | 曾把 `rag/test_artifact_cleanup.py` 当成"放错位置的测试" | 它是**产品清理模块**（脚本调用），只是文件名以 `test_` 开头，不在 pytest 收集范围 | [代码] 文件无 pytest 用例 | 不改名（改名会动调用方） |
| **N-2** | 交接 §3.2 | R50 电池价格拒答 | 语料价格列是 `<金额>` 占位符，证据在但金额不可用；按既定口径拒答正确 | [实测] real-017/018 `expected_position="refuse"` | 文档化为已知限制（§5） |

---

## 3. P0-1：改写不稳定（唯一在线正确性缺陷）

### 3.1 根因链（已闭合）

```
用户问题「找一下副厂配件明细」
  → 经典链路无条件 _rewrite_for_retrieval（LLM）
  → 有时被改写成「如何查询副厂配件明细？」（元问题）
  → 检索用改写句、重排也用改写句
  → 重排器认为文档不回答"如何查询"，top 分 0.03–0.06
  → 低于 reliability_min_score=0.35 → 证据门判 insufficient_evidence → 拒答
```

反证已完成：[交接] 6 轮候选池签名完全相同（检索稳定）、`passage_judged=0`（判定没跑）、
30 个块 `score_source` 全是 `reranker`（没有降级）——变量只剩改写句。

**不是阈值问题**：分数是双峰（0.03–0.06 与 0.53–0.70），0.35 落在两峰之间的空档；
放宽/收紧只会改变哪一峰占多数，不会消除分叉。因此**禁止**改 `reliability_min_score`。

### 3.2 修复设计（两项叠加，可独立回滚）

#### 3.2.1 改写结果缓存（止血 + 降本）

- 位置：`RAGPipeline._rewrite_for_retrieval`（`rag/pipeline.py:450` 附近）。
- Key：`(question, history_signature)`。
  - `query_rewrite_history_turns=0`（默认）时签名为空串，等价于按问题缓存；
  - 开启多轮时，签名 = `sha256(按序 (role, content) + summary)`，避免把上一轮的改写喂给另一轮。
- 值：仅缓存**非空且确实改变了问题**的改写。`rewrite_query` 在模型失败时返回原句，
  这种结果不缓存——否则一次瞬时故障会被永久固化，问题永远失去改写带来的召回。
- 容量：进程内 `OrderedDict` LRU，默认 512，可配置；命中时 `move_to_end`。
- 并发：字典读写加 `threading.Lock`；并发未命中最多重复调用一次模型，不影响正确性。
- 生命周期：随 pipeline 实例；重启即清空，不跨进程共享（多 worker 会各自缓存，
  可能得到不同改写，但双路重排保证结果不因此翻转）。

#### 3.2.2 双路重排（消除分叉的那一半）

- 位置：新增 `RAGPipeline._rerank_for_evidence(original, retrieval_query, candidates, record=None)`，
  在 `_prepare_context_standard` 与 Jev 的 reranker 兜底分支调用。
- 步骤：
  1. `primary = self.reranker.rerank(retrieval_query, candidates)`（**保持原调用签名**，
     不传新参数，避免破坏既有桩与调用方）；
  2. `retrieval_query == original` → 直接返回 primary；
  3. primary 里没有任何非 `None` 的 `rerank_score` → 说明重排器降级（断路器/接口失败），
     再调一次只会同样失败 → 直接返回 primary；
  4. 按策略决定是否调用 `secondary = self.reranker.rerank(original, candidates)`：
     - `always`（默认）：总是补一次；
     - `below_threshold`：仅当 primary 最高分 < `rewrite_dual_rerank_threshold`（默认 0.5）；
     - `off`：不补，回到旧行为；
  5. 用 `merge_ranked_chunks(primary, secondary)` 按 `(doc_name, chunk_index, text)` 去重、
     取较高分；有分块降序在前，无分块按首次出现顺序在后。
- 为什么不是"只用原问题重排"：多轮里原问题可能是"它呢？"，本身没有语义；改写句才是
  带上下文的检索句。双路取高分同时覆盖两种情形。
- 为什么不是"只用改写句但换提示词"：提示词修复需要评测集，且无法保证模型不再漂；
  双路重排不依赖改写质量，是确定性兜底。提示词优化可作为后续独立实验。
- 可观测：`decision_metadata.rewrite_dual_rerank`（布尔）写入轨迹白名单，便于灰度期核对。

#### 3.2.3 配置（新增 4 项，均可回退）

| 配置 | 默认 | 语义 |
| --- | --- | --- |
| `RAG_QUERY_REWRITE_CACHE_ENABLED` | `true` | 关闭即完全回到"每次都调模型" |
| `RAG_QUERY_REWRITE_CACHE_SIZE` | `512` | 进程内 LRU 上限 |
| `RAG_REWRITE_DUAL_RERANK_MODE` | `always` | `always` / `below_threshold` / `off` |
| `RAG_REWRITE_DUAL_RERANK_THRESHOLD` | `0.5` | 仅 `below_threshold` 模式生效 |

回滚：`RAG_REWRITE_DUAL_RERANK_MODE=off` + `RAG_QUERY_REWRITE_CACHE_ENABLED=false`，
逐字节回到 2026-10-21 之前的行为（缓存关闭后 `_rewrite_cached` 直接 `produce()`）。

### 3.3 用例（先写用例，再改实现）

新增 `tests/test_rewrite_stability.py`（或并入 `tests/test_pipeline.py`）：

1. 同一问题连续两次 `_rewrite_for_retrieval` 只调用一次 `rewrite_query`（缓存命中）；
2. 缓存关闭时调用两次；
3. 改写返回原句（模型失败）时不缓存，下次仍调用模型；
4. `query_rewrite_history_turns>0` 时，历史不同 ⇒ 缓存 key 不同 ⇒ 各调一次；
5. LRU 超限淘汰最旧项；
6. 双路重排：primary 低分、secondary 高分 ⇒ 最终块取高分且重排分来自 secondary；
7. 双路重排：两路块并集去重、顺序正确；
8. 改写句 == 原问题 ⇒ 只调一次；
9. primary 全为 `None`（重排器降级）⇒ 只调一次；
10. `mode=off` ⇒ 只调一次；`mode=below_threshold` 且高分 ⇒ 只调一次；
11. 集成：`_prepare_context_standard` 在坏改写（低分）+ 原问题（高分）下产出 `rag` 而非 `fixed`，
    且 `decision_metadata.rewrite_dual_rerank=True`。

更新既有断言：仅改那些 `assert_called_once` 且桩返回**有分数**的用例；桩返回无分数 `Chunk`
的用例因为"全 None 不二次调用"规则保持不变（见 §7 的测试影响清单）。

### 3.4 验收

1. 定向：`pytest tests/test_pipeline.py tests/test_pipeline_jev.py tests/test_rewrite_history.py tests/test_reranker.py -q`；
2. 全量：`pytest -q -p no:cacheprovider` 2313 passed / 0 failed；
3. `ruff check .` 全绿；
4. 真机（**先停服务释放 Qdrant**）：
   `python scripts/verify_rewrite_stability.py "找一下副厂配件明细" --rounds 6`
   - 期望：`distinct rewrites: 1/6`（缓存生效）；
   - 或至少：top 分不再跨 0.35 两侧，输出不再打印 "produced both answers and refusals"；
5. 基准（独占 Qdrant，必须 `--user-id 1`）：
   `python scripts/benchmark_jev_retrieval.py --cases benchmarks/real-cases-v1.json --runs 3 --user-id 1`
   - `refusal` 判据不降；`route` 不接受单次差值当结论，需与修复前同口径对照。

### 3.5 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 重排调用翻倍 | 每条 RAG 请求 +1 次重排（约 +0.3s、约 2× rerank token） | 默认 `always` 最稳；成本敏感可切 `below_threshold`；`off` 秒级回滚 |
| 缓存固化坏改写 | 同一问题稳定地错 | 双路重排兜底；缓存只影响"改写句"，不决定最终证据分 |
| 多进程缓存不一致 | 不同 worker 可能缓存不同改写 | 双路重排消除结果翻转；缓存只是优化，不是正确性依赖 |
| 多轮缓存串话 | 把上一轮改写用于下一轮 | key 带历史签名；`turns=0` 时签名固定为空 |
| 基准/诊断被缓存抹平 | `verify_rewrite_stability.py` 6 轮全部命中，永远打印 `distinct rewrites: 1/6`；基准的 `rewrite_called`/`external_calls` 计数失真 | 见 §3.7：诊断脚本默认绕过缓存，基准在每例前清缓存并分别记录命中/重排次数 |

### 3.6 实现约束（来自 261 个测试文件的只读影响分析）

按以下形状实现，**现有测试预计 0 处必改**；违反任一条都会红，且报错位置会误导排查方向：

1. **缓存属性不能叫 `_cache`**：`test_pipeline.py:428`、`test_cache_permission_isolation.py:102/111/126`
   断言 `not hasattr(p, "_cache")`；`test_pipeline_cache_security.py` 还会把 `_cache` 种成
   "一读就 AssertionError"的哨兵。用 `_rewrite_cache`。
2. **缓存必须实例级**：模块级/类级会让先跑的 `test_feedback_weights_wired.py` 把
   `("question", 空历史)` 写进全局，导致 `test_pipeline.py:66` 的 `assert_called_once_with("question")` 失败。
   不要用方法级 `functools.lru_cache`（永久持有 self，等价于全局）。
3. **`rerank` 必须保持两个位置参数**：8 处桩是 `lambda _query, chunks: chunks`；传 `top_k=` 或第三参
   会 TypeError。两路都用位置参数传**同一个** `candidates`。
4. **二路门禁必须看"结果中存在非 None 的 `rerank_score`"**：现有桩全部返回 `rerank_score=None` 的
   `Chunk`，因此二路永不触发；若改成"列表非空/`score_source`"判断，`test_pipeline.py:123`、
   `test_e2e.py:54`、`test_pipeline_jev.py:420` 三处会同时红。
5. **`turns<=0` 不得读 memory**：`test_rewrite_history.py:30` 把 `get_recent_messages`/`get_summary`
   换成 `pytest.fail`；算历史签名也不能碰它们。
6. **退化路径不缓存**：历史读取失败时直接 `rewrite_query(question)` 且不写缓存，否则 memory 恢复后
   仍会返回无上下文改写。
7. **Jev 兜底护栏**：`_prepare_rag_with_jev` 里只有 `query.strip() != question.strip()` 才走二路；
   `test_pipeline_jev.py:61/420` 依赖"原句只调一次"。
8. **顺序不变**：`rerank → merge → _post_rank_acl → _drop_injected_chunks → [:top_k]`；
   `test_guard_document_injection.py` 依赖合并后仍由注入 guard 决定去留。
9. **懒初始化不得抛异常**：`object.__new__(RAGPipeline)` 的用例没有 `__init__`；缓存创建失败若抛出，
   会被 `_route_with_history_correction` 的 `except Exception` 吞掉，最后报成"路由次数不对"。
10. **`stage["rewrites"]` 不因缓存命中改变**：`rewrite_count` 是决策分支的计数，不是模型调用计数。

新增测试放两个文件：`tests/test_pipeline_rewrite_cache.py`（缓存命中/未命中、历史签名、LRU、退化路径）、
`tests/test_pipeline_dual_rerank.py`（二路调用、取高分、去重、全 None/原句/关闭策略、Jev 兜底、签名约束）。

### 3.7 诊断与基准脚本的必然改动（不改就会得出错误结论）

1. `scripts/verify_rewrite_stability.py`：它用不同 `session_id` 连调 6 次来度量改写方差；缓存按
   `(question, 历史)` 建键，`turns=0` 时 6 次全部命中，脚本会永远打印 `distinct rewrites: 1/6`，
   **把要诊断的现象抹平**。改法：加 `--no-cache`（默认开启，逐轮清 `_rewrite_cache`），
   想要验证缓存本身时用 `--use-cache`。
2. `scripts/benchmark_jev_retrieval.py`：它用计数包装 `rewrite_query`/`rerank` 得出 `rewrite_called`、
   `external_calls`，且 `pipeline_for()` 按 KB 复用同一实例。缓存会降低改写计数，双路重排会让
   rerank 计数翻倍，old/new 对比会失真。改法：每例前清缓存；把 `rewrite_cache_hits`、
   `rerank_calls` 作为独立字段记录，而不是混进 `external_calls`；报告里注明本次跑的是
   `always` 还是 `below_threshold`。
3. 两条脚本改动属于阶段 2 的交付物，不进产品运行时。

---

## 4. P1-1：结构化数据集计数全为 0

### 4.1 根因

- 数据集文件（`data/structured_datasets/*.json`）是真相，`structured_sheets/columns` 是镜像，
  `structured_datasets.sheet_count/total_rows/total_columns` 是给目录页用的**派生计数**。
- 早期迁移只写了镜像行，没有写这三个计数；后来 `replace_structured_dataset_schema` 补上了
  同事务计数，但**回填函数** `_mirror_dataset_schema` 看到"已有镜像行"就直接 `return False`，
  于是历史数据集的计数永远停在 0。
- 实测：11 个数据集计数全 0，而镜像合计 **19 sheets / 789 rows / 192 cols**
  （逐数据集 1/2/3/3/3/2/1/1/0/2/1）。
  其中 dataset 9 属于文档 19 的**旧版本 20**（当前版本 23 有独立数据集），
  0 个 sheet 是正常的，catalog 会排除它——**不要**把它当缺陷修。

### 4.2 修复

1. `SpreadsheetRepository.recount_structured_dataset(dataset_id)`：一条 SQL 从镜像重算三项计数
   （`COUNT(sheets)`、`SUM(row_count)`、`COUNT(columns)`），幂等。
2. `UserDB` 门面转发，保持 `UserDB` 对外 API 风格。
3. `_mirror_dataset_schema`：镜像已存在时不再直接返回，而是先 `recount_structured_dataset` 再返回 False。
   不重新解析原文件（文件可能已不在），只信镜像。
4. 启动时已有的 `migrate_existing_spreadsheets` 会走到该路径，无需新增命令。

### 4.3 用例与验收

- 新增：镜像存在、计数被置 0 → 再跑一次迁移 → 计数恢复为 (1, 2, 2)，且 `migrated/mirrored` 仍为 0
  （修复不应被误计为"新建数据集"）。
- 验收 SQL：
  `select id, sheet_count, total_rows, total_columns from structured_datasets order by id;`
  期望与镜像一致（dataset 9 保持 0）。

### 4.4 风险

仅写派生计数，不改文件、不改版本指针、不动 Qdrant；失败由现有 `try/except` 记录，不影响启动。

---

## 5. P1-2：文档口径修正

| 文件 | 现状 | 修正 |
| --- | --- | --- |
| `docs/PROJECT_INTRODUCTION.md` L7 | 说模块化"已实现但尚未提交"、HEAD 32f2b7c、api.py 2834 行 | 改为"模块化已随 `fb68237` 入库，api.py 为装配器、user_db.py 为门面；HEAD 与行数以 `git log -1`/工作树为准"，不再硬编码快照 |
| 同文 §2.1 / 文末 | 仍有 3723/3722/521 等行数快照 | 改为不依赖行数的表述；保留 2026-10-18 数据时加"历史快照"限定 |
| `scratch/README.md` L4 | "不被测试收集、不在 CI 里跑" | 改为"不进 pytest 收集；但已入库的 scratch/ 会被 `ruff check .` 扫到，新探针必须 lint 干净" |
| `docs/jev-benchmark.md` | 未记脱敏价格限制 | 新增 §3.0.2「已知限制：脱敏价格造成的正确拒答（real-017/018）」：价格是 `<金额>`、证据不可用、拒答正确、不得放宽阈值、要答价格应改语料或脱敏策略 |

不新增"本轮/我"的过程叙述；不批量改写历史文档。

---

## 6. 需人工决策（不要用代码绕过）

| ID | 决策点 | 现状 | 我的建议 |
| --- | --- | --- | --- |
| D-1 | 层 2 门禁是否允许"聚合零净变化、逐例有进有退"通过 | 允许，且被测试钉死 | **保持**（它是净质量门，不是逐例回归门）；另加"逐例回归观察"报告，不改判定 |
| D-2 | 空检索是否要与"检索到但无证据"区分 | 企业模式下两者都落到 `insufficient_evidence`（同一 kind、同一文案）；非企业模式 `build_bundle=False`，直接空上下文生成 | 保持企业模式口径；若要区分，先加只读的 `decision.reasons`，别改文案；**非企业模式的空上下文生成需要单独评估**（幻觉风险） |
| O-1 | 候选 #30（canary@0%）与 #31（candidate_testing，target=None） | #30 措辞已上线；#31 未决 | #30 保留追溯；#31 人工看 payload 后推进或退回 |
| O-2 | real-008/022 标签 | real-008 期望 structured 但无 HR 统计表；real-022 期望 clarification | 请产品确认标签；**不要**为标签改检索或路由 |
| O-3 | P99、观察窗、4 个 `alice_*` | 压测只算 P50/P95；观测 182 条（09-23..10-21）；账号是 E2E 残留 | 跑一次 locust 出 P99；明确 canary 观察窗定义；账号由产品决定删/留 |
| N-2 | 脱敏价格 | 拒答正确 | 按已知限制记录，不改阈值、不改语料 |

---

## 7. 实施顺序与提交边界

| 阶段 | 内容 | 交付物 | 回滚 |
| --- | --- | --- | --- |
| 0 | 本文评审 + D-1..D-6 拍板 | 评审记录 | — |
| 1 | P1-1 计数修复 + 用例 | 1 个提交（repo/facade/migration/test） | 还原三个文件 |
| 2 | P0-1 改写缓存 + 双路重排 + 用例 | 1 个提交（pipeline/config/tracker/reranker/test） | 两个环境变量 |
| 3 | P1-2 文档修正 | 1 个提交（3 个 md） | 还原文档 |
| 4 | 全量验证 + 真机复现 + 交接更新 | 测试记录 + 新 handoff | — |

每个阶段独立可回滚；阶段 1 与阶段 2 无文件冲突，可并行开发、分开提交。

---

## 8. 验证清单（可直接复制）

```powershell
# 定向
.\.venv\Scripts\python.exe -m pytest tests/test_pipeline.py tests/test_pipeline_jev.py tests/test_rewrite_history.py tests/test_reranker.py tests/test_spreadsheet_migration.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check .

# 全量（27–45 分钟）
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider

# 真机复现（先停服务，Qdrant 本地模式独占目录；--no-cache 是新增参数）
.\.venv\Scripts\python.exe scripts\verify_rewrite_stability.py "找一下副厂配件明细" --rounds 6 --no-cache

# 基准（独占 Qdrant，必须带主体）
.\.venv\Scripts\python.exe scripts\benchmark_jev_retrieval.py --cases benchmarks\real-cases-v1.json --runs 3 --user-id 1

# 计数验收（只读）
.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/users.db');print(c.execute('select id,sheet_count,total_rows,total_columns from structured_datasets order by id').fetchall())"
```

---

## 9. 本轮明确不做

1. 不改 `reliability_min_score`、层 2 阈值、证据门判据。
2. 不拆 `pipeline.py`、不改 `api.py` 的模块转发、不动 Qdrant/SQLite 单机架构。
3. 不调整门禁语义、不替产品改标签、不删测试账号。
4. 不为 `rag/test_artifact_cleanup.py` 改名（它是产品模块，不是测试）。
5. 不加 CORS / 反向代理配置（同源 SPA 不需要；有外部集成需求再立项）。
6. 不把 `scratch/` 的探针改造成正式测试（它们依赖真实服务与 .env）。

---

## 10. 附录：本轮先于本文的改动（已撤回）

在本文完成前，我曾先行改动 12 个文件（改写缓存半成品、重排合并助手、计数修复、三处文档），
违反"先核查、后文档、再实现"的顺序。现已全部撤回到 `stash@{0}`：

```
git stash list
stash@{0}: On main: wip: pre-plan implementation (rewrite cache/dual-rerank, counters, docs)
```

- 撤回后 `git status` 干净，基线仍是 `89aff32`。
- 已核验 stash 完整：commit `ea858f4`，**12 个文件 / 247 insertions / 11 deletions**。
  注意：PowerShell 会把未加引号的 `stash@{0}` 当哈希表解析，`git stash show stash@{0}` 会误报为空；
  核对时用单引号 `'stash@{0}'`。
- 其中两项已通过定向测试（重排合并 10 passed；计数修复 7 passed），可作为阶段 1/2 的参考实现，
  但**在本文评审通过前不会恢复、不会提交**。
- 恢复方式（评审后由我执行，不建议手工 pop）：
  `git stash pop`，再按 §7 拆成独立提交。
