# Assisted Self-Evolution (Observation Phase) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不动在线链路行为的前提下，建设"人工主导的知识库自进化系统"的第 2 阶段（观测基础）：把权限过滤后的可诊断失败轨迹异步落入独立存储，按知识库/失败阶段/根因特征聚类并执行"同类根因累计三次进候选池"的准入规则，用有预算、可暂停的后台队列驱动聚类，并向管理端"进化中心"提供只读观测 API 与最小指标看板。本阶段不生成任何候选修改，不调用诊断模型，不触碰发布门禁。

**Architecture:** 新增独立包 `rag/evolution`，与在线链路解耦：`FailureRecorder` 复用 `rag/tracker.py` 的 `ExecutionTrace`（追踪落库路径完全不变），以 `rag/jev/shadow.py` 的有界非阻塞提交模式把 ACL 过滤后的可诊断轨迹异步写入独立 SQLite（`settings.evolution_db_path`）；`FailureClusterer` 用确定性签名（知识库 × 失败阶段 × 根因特征）聚类，落实设计第 5 节准入——单次/不可复现停留观察队列，同类根因累计三次（阈值可配，默认 3，沿用 `feedback_cluster_users` 约定）自动进入候选池；`EvolutionWorker` 以 `rag/backups/scheduler.py` 的守护线程模式运行，受 CPU/运行时长/并发/模型调用预算约束，系统繁忙时暂停；`rag/evolution/api.py` 挂到知识运营路由下，全部管理员只读端点；Vue "进化中心"页面只渲染观察列表、聚类状态机只读视图与指标卡片。观测记录通过 tracker 既有白名单决策元数据绑定 Jev 版本化协议（`preflight-v1` / `passages-v1`），为后续阶段候选生成预留协议边界。`RAG_EVOLUTION_ENABLED=false`（默认）时在线路径逐字节不变。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLite（独立 evolution 库，沿用 `sqlite3` + 锁模式）、pytest、Vue 3 + TypeScript + vitest（前端测试与 `frontend/src/**/__tests__` 既有约定一致）、ruff；复用现有 `rag/jev` 版本化协议常量，不引入新依赖、不引入新模型调用。

**Spec:** `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`（约束来源：第 2 节范围、第 4 节 Jev 兼容边界、第 5 节失败准入、第 7 节组件与状态机、第 8 节人工核验、第 9 节门禁与禁止条件、第 11 节指标、第 12 节实施阶段）

## Global Constraints

- **权限与审计边界禁改**：本计划不得修改 `rag/permissions.py`、`rag/api_security.py`、`rag/org_auth/*` 及任何 ACL 过滤逻辑；新端点一律 `require_user` + `require_admin`，且第 2 阶段只读；观测数据只能来自权限过滤后的可诊断轨迹（设计第 5 节末段）。
- **验收器禁改**：不得改动 `benchmarks/jev_retrieval_cases.json`、Jev A/B 五项阈值、既有用例断言与任何验收脚本的判定标准。
- **失败测试禁删**：不得删除、跳过或弱化任何既有测试（含 `tests/test_pipeline*.py`、`tests/test_kb_security_boundaries.py`、`tests/test_org_retrieval_scope.py`、`tests/test_tracker.py` 等关键能力回归）；新失败必须修复或上报，禁止靠改标准换绿。
- **硬编码答案禁项**：任何位置（含指标夹具、测试数据、聚类规则）都不得硬编码具体问题→答案映射或按用例特判；聚类与指标只使用存储信号与确定性规则。
- **无法复现禁入**：没有可重复复现、没有确切证据的观测只能停留观察队列（`observed`）；进入候选池的唯一自动路径是同类根因累计达到阈值（默认 3），其余一律留待管理员显式标记（第 2 阶段不开放标记，见开放问题）。
- **关键能力回归一票否决**：权限、文件访问、普通 RAG、结构化表格查询、Agent 权限过滤、Jev 主/影子/回退链路任一回归即阻断本阶段，不因指标提升而豁免（设计第 9、11 节）。
- **无可靠回退点禁入**：每个新能力默认关闭（`RAG_EVOLUTION_ENABLED=false`），开启时也只观测不改在线行为；回退 = 关 flag + 停 worker + 删除 evolution 库文件，不留现场反向编辑。
- **在线路径零影响**：`FailureRecorder.submit` 必须 O(1) 非阻塞，队列满即丢弃并计数，绝不阻塞、改写或降级在线响应（有计时测试兜底）；`rag/tracker.py` 本计划只读复用、不做修改。
- **禁写敏感位置**：不得写入 `.env`、`data/`、`qdrant_data/`，不得提交任何密钥或线上产物；evolution 库默认路径 `data/evolution.db`（运行期创建，测试用 tmp_path）。
- **版本化协议复用**：观测必须记录 `question_set_version`、`jev_route`、`jev_mode`、`failure_kind` 等既有白名单元数据；后续阶段候选只能生成版本化 Jev 问题集/策略候选，沿用 `rag/jev/questions.py` 与 `rag/jev/policy.py` 的协议（设计第 4 节）。

## Review Focus

- 被 guard 拦截或权限不足的请求（`route="blocked"`）绝不能产生观测，只能进入跳过计数；覆盖于 Task 1 准入测试。
- 原始块文本、未授权文档内容或任何密钥不得落库；`evolution.db` 只存哈希、分数、版本与枚举；覆盖于 Task 1 落库测试与 Task 4 序列化测试。
- store 阻塞时 `submit` 必须立即返回且在线响应计时不变；覆盖于 Task 1 计时测试与 Task 6 流水线计时回归。
- 两次同类失败不得晋升，第 3 次同类根因才把聚类推入候选池，且重复聚类不重复计数；覆盖于 Task 2 阈值与幂等测试。
- 系统繁忙或预算耗尽时 worker 必须暂停/停止当轮而不丢已入队观测，单批异常不得杀死线程；覆盖于 Task 3 测试。
- 非管理员调用任一 evolution 端点必须 403，且第 2 阶段没有任何端点可改变状态机；覆盖于 Task 4 测试。
- 指标必须只由存储信号计算，除零安全，定义文本随 API 返回；覆盖于 Task 5 测试。

---

## File Map

- `config.py`: `RAG_EVOLUTION_*` 设置（开关、独立库路径、队列/预算/聚类阈值、繁忙判定参数）。
- `rag/evolution/__init__.py`: 包导出（`FailureRecorder`、`FailureClusterer`、`EvolutionWorker`、状态与转移表）。
- `rag/evolution/models.py`: `EvolutionStatus`、`ObservationPool`、`NonEvolvableReason`、`FailureStage`、`RootCauseFeature`、`FailureObservation`、`FailureCluster`、`DiagnosticDetail`、状态转移表（设计第 7 节）。
- `rag/evolution/store.py`: `FailureTraceStore`（独立 SQLite：观测、聚类、跳过日志、计数与乐观并发转移，沿用 `rag/knowledge_tasks.py`/`rag/user_db.py` 的 `VersionConflict` 模式）。
- `rag/evolution/recorder.py`: `FailureRecorder`（有界非阻塞异步提交，模式复用 `rag/jev/shadow.py`）。
- `rag/evolution/clusterer.py`: `FailureClusterer`（确定性分类签名、三次累积准入、优先级信号）。
- `rag/evolution/worker.py`: `EvolutionWorker` + `python -m rag.evolution.worker` 入口（守护线程模式复用 `rag/backups/scheduler.py`、预算与繁忙暂停）。
- `rag/evolution/metrics.py`: 指标定义与窗口聚合（设计第 11 节最小集，除零安全）。
- `rag/evolution/api.py`: 管理端只读路由（观察列表、聚类列表/详情、状态机参考、指标）。
- `rag/pipeline.py`: 构造注入 `failure_recorder`（所有权模式同 `jev_service`）、`query`/`query_stream` 追踪落库后非阻塞提交、`close()` 释放。
- `rag/api.py`: `_make_pipeline` 注入共享 recorder 单例（模式同 `get_feedback_processor`）、挂载 evolution 路由、lifespan 启停 worker（模式同 `start_backup_scheduler`）、`close_pipeline_processors` 释放 recorder。
- `rag/jev/questions.py`、`rag/jev/policy.py`、`rag/tracker.py`、`rag/gap_analyzer.py`、`rag/feedback_processor.py`、`rag/knowledge_tasks.py`、`rag/api_security.py`、`rag/operations_api.py`、`rag/backups/scheduler.py`: 只读复用，本计划不修改。
- `frontend/src/api/evolution.ts`、`frontend/src/views/EvolutionCenterView.vue`、`frontend/src/router/index.ts`: 进化中心只读页面与路由（挂靠 `operations` 知识运营区）。
- `frontend/src/api/__tests__/evolution.test.ts`、`frontend/src/views/__tests__/EvolutionCenterView.test.ts`: 前端测试（vitest，与既有 `__tests__` 约定一致）。
- `tests/test_evolution_recorder.py`、`tests/test_evolution_clusterer.py`、`tests/test_evolution_worker.py`、`tests/test_evolution_api.py`、`tests/test_evolution_metrics.py`、`tests/test_pipeline_evolution.py`: 新测试；`tests/test_pipeline.py`、`tests/test_pipeline_stream.py`、`tests/test_pipeline_lifecycle.py` 等仅追加回归断言。
- `docs/evolution-observations.md`: 运维说明（页面读法、指标定义、预算/繁忙配置、回退步骤）。

### Task 1: Record ACL-filtered failure observations

**Files:**
- Create: `rag/evolution/__init__.py`
- Create: `rag/evolution/models.py`
- Create: `rag/evolution/store.py`
- Create: `rag/evolution/recorder.py`
- Create: `tests/test_evolution_recorder.py`
- Modify: `config.py`
- Modify: `rag/pipeline.py`
- Modify: `rag/api.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_pipeline_lifecycle.py`

**Interfaces:**
- Consumes: `ExecutionTrace`（`rag/tracker.py`，不改动）、`RAGPipeline._prepare_context` 产出的 `prepared`（ACL 过滤后的 `chunk_hashes`/`weights`/`stage_timings`/`decision_metadata`/`sources`）、`settings.evolution_*`。
- Produces: `FailureRecorder(store, *, max_queue=256, max_workers=1)`，`submit(trace: ExecutionTrace, detail: DiagnosticDetail | None = None) -> bool`，`stats() -> dict`（submitted/completed/dropped/skipped/closed），`close(wait=True)`；`FailureTraceStore(db_path=None)`（`add_observation`、`log_skip`、`get_observation`、`count_by_status`、`close`）；`DiagnosticDetail` 冻结数据类；设置项 `evolution_enabled`、`evolution_db_path`、`evolution_queue_limit`。

- [ ] **Step 1: Write failing recorder tests**

```python
import time

from rag.evolution.models import DiagnosticDetail, FailureObservation, NonEvolvableReason
from rag.evolution.recorder import FailureRecorder
from rag.evolution.store import FailureTraceStore
from rag.tracker import ExecutionTrace


def trace(**kw):
    base = dict(question="q", route="rag", answer="a", total_ms=10.0)
    base.update(kw)
    return ExecutionTrace(**base)


def evolvable_detail(**kw):
    base = dict(kb_id="kb1", failure_stage="retrieval", chunk_hashes=("a" * 64,), best_score=0.11)
    base.update(kw)
    return DiagnosticDetail(**base)


def test_submit_never_blocks_on_store(tmp_path):
    class BlockingStore(FailureTraceStore):
        def add_observation(self, observation):
            time.sleep(0.5)
            return super().add_observation(observation)

    recorder = FailureRecorder(BlockingStore(tmp_path / "e.db"))
    started = time.perf_counter()
    assert recorder.submit(trace(), evolvable_detail()) is True
    assert time.perf_counter() - started < 0.05
    recorder.close()


def test_full_queue_drops_instead_of_blocking(tmp_path):
    class BlockingStore(FailureTraceStore):
        def add_observation(self, observation):
            time.sleep(0.3)
            return super().add_observation(observation)

    recorder = FailureRecorder(BlockingStore(tmp_path / "e.db"), max_queue=1)
    assert recorder.submit(trace(), evolvable_detail()) is True
    assert recorder.submit(trace(), evolvable_detail()) is False
    recorder.close()


def test_blocked_and_transient_requests_never_become_observations(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    recorder = FailureRecorder(store)
    assert recorder.submit(trace(route="blocked"), evolvable_detail()) is True  # logged as skip
    detail = evolvable_detail(non_evolvable_reason="transient_external")
    assert recorder.submit(trace(), detail) is True  # logged as skip
    recorder.close(wait=True)
    assert store.count_by_status() == {}
    skips = store.count_skips_by_reason()
    assert skips["permission_denied"] == 1
    assert skips["transient_external"] == 1


def test_persisted_row_has_no_chunk_text_or_secret(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    recorder = FailureRecorder(store)
    t = trace(decision_metadata={"question_set_version": "passages-v1", "jev_route": "rag"})
    recorder.submit(t, evolvable_detail(adopted_evidence_hashes=("b" * 64,), secret_like="secret-test-key"))
    recorder.close(wait=True)
    row = store.get_observation(1)
    blob = json.dumps(row, ensure_ascii=False)
    assert "secret-test-key" not in blob
    assert row["question"] == "q"  # question text is diagnosable, already stored by tracker
    assert row["stage"] == "retrieval" and row["kb_id"] == "kb1"
    assert row["details_decision_metadata"]["question_set_version"] == "passages-v1"
```

`DiagnosticDetail` 必须 `extra="forbid"`：未知字段（如 `secret_like`）在构造时即报错，从结构上保证不进库。

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_evolution_recorder.py -q`

Expected: FAIL because `rag/evolution` does not exist.

- [ ] **Step 3: Add evolution settings**

```python
evolution_enabled: bool = False
evolution_db_path: str = str(Path(__file__).resolve().parent / "data" / "evolution.db")
evolution_queue_limit: int = Field(default=256, ge=1, le=100_000)
```

默认关闭；开启也只写观测，不改变在线行为。

- [ ] **Step 4: Implement models, store, and bounded recorder**

实现 `FailureObservation`（frozen dataclass：`kb_id`、`failure_stage`、`root_cause_feature`（聚类阶段回填）、`status="observed"`、`pool="observation_queue"`、`priority="normal"`、`question`、`route`、`answerability_kind`、`confidence_level`、`chunk_hashes`、`best_score`、`adopted_evidence_hashes`、`tool_names`、`structured_query_kind`、`exception_class`、`feedback_kind`、`jev_question_set_version`、`total_ms`、`created_at`、`updated_at`）与 `DiagnosticDetail`（pipeline 侧构造，`extra="forbid"`）。`FailureTraceStore` 用 `sqlite3.connect(..., check_same_thread=False)` + `threading.Lock` 建 `failure_observations` / `failure_clusters` / `evolution_skip_log` 三张表（DDL 内 `CHECK` 约束状态与池取值，模式参照 `rag/knowledge_tasks.py` 的 `_TASKS_TABLE_SQL`）。`FailureRecorder` 复用 `ShadowEvaluator` 的 `BoundedSemaphore` + `ThreadPoolExecutor(max_workers=1)` 模式：`submit` 立即返回，满队丢弃并计数，`detail.non_evolvable_reason` 非空时只写 `evolution_skip_log` 并返回 `True`。集合关闭语义与 `stats()` 同 `ShadowEvaluator`。

- [ ] **Step 5: Wire the pipeline and API singletons**

`RAGPipeline(..., failure_recorder: FailureRecorder | None = None)`：未注入且 `settings.evolution_enabled` 时自建并标记 `_owns_failure_recorder`（所有权模式同 `jev_service`）；`_make_pipeline` 注入 api 级单例 `get_failure_recorder()`（仿 `get_feedback_processor`）；`close_pipeline_processors` 释放单例。在 `query()` 的 `self.tracker.save(...)` 之后与 `query_stream()` 的对应位置，新增非阻塞提交：

```python
def _evolution_submission(self, question, prepared, error=None):
    """Build a diagnosable detail from ACL-filtered prepared data; None means nothing to record."""
```

`route="blocked"`（guard/权限拒绝）映射 `non_evolvable_reason="permission_denied"`；`decision.kind == "insufficient_evidence"` 的拒答是可观测失败（`answerability_kind="insufficient_evidence"`），不跳过；外部异常（embed/rerank/generate 抛错且已被捕获降级）映射 `transient_external`。两条路径合计不超过 10 行改动，在线返回值与计时不变。

- [ ] **Step 6: Run recorder plus pipeline regression tests**

Run: `python -m pytest tests/test_evolution_recorder.py tests/test_pipeline.py tests/test_pipeline_stream.py tests/test_pipeline_lifecycle.py tests/test_tracker.py -q`

Expected: PASS；既有流水线断言零改动即绿（`evolution_enabled` 默认关闭时提交为空操作）。

- [ ] **Step 7: Commit**

```powershell
git add rag/evolution config.py rag/pipeline.py rag/api.py tests/test_evolution_recorder.py tests/test_pipeline.py tests/test_pipeline_lifecycle.py
git commit -m "feat: record ACL-filtered failure observations"
```

### Task 2: Cluster failures and apply admission rules

**Files:**
- Create: `rag/evolution/clusterer.py`
- Create: `tests/test_evolution_clusterer.py`
- Modify: `rag/evolution/models.py`
- Modify: `rag/evolution/store.py`
- Modify: `config.py`

**Interfaces:**
- Consumes: `FailureTraceStore` 中 `status="observed"` 的观测、`EvolutionStatus`/`ObservationPool` 转移表、`settings.evolution_cluster_threshold`（默认 3）、`settings.evolution_cluster_window_days`（默认 30，沿用 `feedback_cluster_days` 约定）。
- Produces: `FailureClusterer(store, *, threshold=3, window_days=30)`，`classify(observation) -> tuple[FailureStage, RootCauseFeature]`，`cluster_once(*, limit=200) -> dict`（scanned/clustered/promoted/skipped）；`FailureCluster` 记录（`cluster_key`、`kb_id`、`failure_stage`、`root_cause_feature`、`pool`、`status`、`priority`、`member_count`、`first_seen_at`、`last_seen_at`）。

- [ ] **Step 1: Write failing clustering and admission tests**

```python
from rag.evolution.clusterer import FailureClusterer
from rag.evolution.models import DiagnosticDetail, FailureObservation
from rag.evolution.store import FailureTraceStore


def observation(store, question, *, stage="retrieval", score=0.11):
    return store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage=stage, question=question, route="rag",
        chunk_hashes=(f"{abs(hash(question)):064x}",), best_score=score,
        answerability_kind="insufficient_evidence",
    ))


def test_same_root_cause_promotes_only_on_third_occurrence(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    clusterer = FailureClusterer(store, threshold=3, window_days=30)
    for i in range(2):
        observation(store, f"年假额度{i}")
        clusterer.cluster_once()
        clusters = store.list_clusters()
        assert clusters[0]["pool"] == "observation_queue"
        assert clusters[0]["member_count"] == i + 1
    observation(store, "年假额度2")
    result = clusterer.cluster_once()
    assert result["promoted"] == 1
    cluster = store.list_clusters()[0]
    assert cluster["pool"] == "candidate_pool"
    assert cluster["status"] == "awaiting_diagnosis_review"
    members = store.list_observations(cluster_id=cluster["id"])[0]
    assert all(m["status"] == "clustered" for m in members)


def test_reclustering_is_idempotent(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    observation(store, "q1")
    clusterer = FailureClusterer(store, threshold=3)
    clusterer.cluster_once()
    result = clusterer.cluster_once()
    assert result == {"scanned": 0, "clustered": 0, "promoted": 0, "skipped": 0}
    assert store.list_clusters()[0]["member_count"] == 1


def test_priority_signals_elevate_the_cluster(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="generation", question="q", route="rag",
        feedback_kind="not_useful", chunk_hashes=("a" * 64,),
    ))
    FailureClusterer(store, threshold=3).cluster_once()
    assert store.list_clusters()[0]["priority"] == "high"


def test_stage_and_root_cause_are_deterministic(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    o = store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="retrieval", question="q", route="rag",
        best_score=0.05, chunk_hashes=(), answerability_kind="insufficient_evidence",
    ))
    stage, feature = FailureClusterer(store).classify(o)
    assert (stage, feature) == ("retrieval", "recall_miss")
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_evolution_clusterer.py -q`

Expected: FAIL because `FailureClusterer` does not exist.

- [ ] **Step 3: Add deterministic classification rules**

`classify` 只用存储信号做确定性判定，禁止任何模型调用与硬编码答案：

- `failure_stage`：`route="structured"` 且带 `exception_class` → `structured_query`；`tool_names` 非空 → `tool`；`best_score` 低且无 `chunk_hashes` → `retrieval`；`answerability_kind="insufficient_evidence"` → `generation`；其余 `unknown`。
- `root_cause_feature`（设计第 6 节根因清单的确定性子集）：无候选块/低分 → `recall_miss`；候选存在但 `answerability_kind="insufficient_evidence"` → `generator_ignored_evidence`；`feedback_kind` 为 `not_useful`/`outdated` → `prompt_or_workflow_defect`；`exception_class` 非空 → `code_defect`；冲突信号（观测关联 `evidence_conflict` 任务存在）→ `evidence_conflict`；均不匹配 → `undetermined`。LLM 诊断归第 3 阶段 `EvolutionDiagnoser`。
- 聚类键：`sha256(kb_id | failure_stage | root_cause_feature)`（哈希约定沿用 `rag/knowledge_tasks.py:refusal_active_key`）。

- [ ] **Step 4: Implement admission and promotion**

窗口 = `now - window_days*86400`；同一 `cluster_key` 窗口内 `member_count` 达到 `threshold` 时：聚类 `pool="candidate_pool"`、`status="awaiting_diagnosis_review"`，其下观测 `observed → clustered`（转移表驱动，非法转移抛 `ValueError`）。`priority="high"` 条件（设计第 5 节末条）：任一成员带 `feedback_kind` 负向、存在关联冲突记录、`exception_class` 非空或 `route` 触及安全边界事件；其余 `normal`。窗口外旧成员不计入分子。`cluster_once` 单批处理上限 `limit`，返回计数字典供运营对账。

- [ ] **Step 5: Add cluster threshold settings and run tests**

```python
evolution_cluster_threshold: int = Field(default=3, ge=1, le=100)
evolution_cluster_window_days: int = Field(default=30, ge=1)
```

Run: `python -m pytest tests/test_evolution_clusterer.py tests/test_evolution_recorder.py -q`

Expected: PASS。

- [ ] **Step 6: Commit**

```powershell
git add rag/evolution/clusterer.py rag/evolution/models.py rag/evolution/store.py config.py tests/test_evolution_clusterer.py
git commit -m "feat: cluster failures into observation and candidate pools"
```

### Task 3: Bound the background worker with budgets and busy pause

**Files:**
- Create: `rag/evolution/worker.py`
- Create: `tests/test_evolution_worker.py`
- Modify: `config.py`
- Modify: `rag/api.py`
- Modify: `rag/evolution/__init__.py`

**Interfaces:**
- Consumes: `FailureClusterer.cluster_once`、`settings.evolution_worker_*`、可注入的 `busy_provider`/`clock`。
- Produces: `EvolutionBudget(max_concurrency=1, max_batch=200, max_run_seconds=120.0, max_model_calls=0, max_rss_mb=None)`；`BudgetExhausted(Exception)`（携带 `reason`，如 `max_run_seconds`/`max_model_calls`）；`EvolutionWorker(run_once, *, interval_seconds, budget, busy_provider=default_busy_provider)`，`start()`、`stop(timeout=5.0)`、`pause()`、`resume()`、`run_once_when_idle() -> str`（返回 `busy`/`budget_exhausted`/`ok`/`failed`）、`is_alive`、`stats() -> dict`（runs/last_status/paused/busy/budget_exhausted）；模块入口 `python -m rag.evolution.worker`。

- [ ] **Step 1: Write failing worker tests**

```python
import threading
import time

from rag.evolution.worker import BudgetExhausted, EvolutionBudget, EvolutionWorker


def test_busy_system_pauses_and_resumes_without_losing_runs():
    busy = threading.Event()
    calls = []
    worker = EvolutionWorker(lambda: calls.append(1), interval_seconds=0.01,
                             busy_provider=busy.is_set)
    busy.set()
    worker.start()
    worker.run_once_when_idle()  # no-op while busy
    assert calls == []
    busy.clear()
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert calls  # resumes after busy clears


def test_budget_exhaustion_stops_the_run_and_is_reported():
    runs = []
    def one_run():
        runs.append(1)
        raise BudgetExhausted("max_run_seconds")

    worker = EvolutionWorker(one_run, interval_seconds=0.01, budget=EvolutionBudget(max_run_seconds=0.0))
    worker.start()
    deadline = time.monotonic() + 2
    while worker.stats()["budget_exhausted"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert worker.stats()["budget_exhausted"] >= 1
    assert runs == []


def test_one_failing_batch_never_kills_the_thread():
    state = {"n": 0}
    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")

    worker = EvolutionWorker(flaky, interval_seconds=0.01)
    worker.start()
    deadline = time.monotonic() + 2
    while state["n"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert worker.is_alive is False and state["n"] >= 3
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_evolution_worker.py -q`

Expected: FAIL because `EvolutionWorker` does not exist。

- [ ] **Step 3: Implement the worker on the backup-scheduler pattern**

守护线程 + `threading.Event` 停止/暂停语义整体复用 `rag/backups/scheduler.py:BackupScheduler`；差异只有三点：每轮执行前检查 `busy_provider()`（True 则跳过本轮并保持入队数据不动）、把 `run_once` 包进预算上下文（超时用注入 `clock` 判定，模型调用计数由 run_once 回报，`max_model_calls` 第 2 阶段恒为 0 即"禁止模型调用"——聚类器是确定性的，任何模型调用企图都应使该轮以 `budget_exhausted` 收尾）、单轮异常只记日志不退出循环。`default_busy_provider()`：有 `os.getloadavg()` 时与配置倍数比较，否则返回 False（文档注明 Windows 上需显式提供判定或由管理员手动 `pause()`）。第 2 阶段进程内独立线程 + 独立库连接即满足"独立队列/数据表"，完整独立进程/容器留作开放问题 4。

- [ ] **Step 4: Add worker settings and lifecycle registration**

```python
evolution_worker_enabled: bool = True
evolution_worker_interval_seconds: int = Field(default=900, ge=1)
evolution_worker_run_seconds: float = Field(default=120.0, gt=0.0)
evolution_worker_max_batch: int = Field(default=200, ge=1)
evolution_worker_busy_load_factor: float = Field(default=2.0, gt=0.0)
```

`rag/api.py` lifespan 内在 `start_backup_scheduler()` 旁注册 `start_evolution_worker()`（仅 `settings.evolution_enabled` 时），`close_pipeline_processors` 内 `stop_evolution_worker()`；单例 + 锁模式照抄 scheduler。

- [ ] **Step 5: Run worker and lifecycle tests**

Run: `python -m pytest tests/test_evolution_worker.py tests/test_evolution_clusterer.py tests/test_backup_scheduler.py -q`

Expected: PASS（`tests/test_backup_scheduler.py` 若不存在则以 `tests/test_backups.py` 或既有备份测试文件代替，确认 scheduler 未回归）。

- [ ] **Step 6: Commit**

```powershell
git add rag/evolution/worker.py rag/evolution/__init__.py rag/api.py config.py tests/test_evolution_worker.py
git commit -m "feat: bound the evolution background worker"
```

### Task 4: Expose the read-only evolution observation APIs

**Files:**
- Create: `rag/evolution/api.py`
- Create: `tests/test_evolution_api.py`
- Create: `frontend/src/api/evolution.ts`
- Create: `frontend/src/views/EvolutionCenterView.vue`
- Create: `frontend/src/views/__tests__/EvolutionCenterView.test.ts`
- Create: `frontend/src/api/__tests__/evolution.test.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `rag/api.py`
- Modify: `rag/evolution/__init__.py`

**Interfaces:**
- Consumes: `FailureTraceStore` 只读方法、`EvolutionStatus` 转移表、`require_user`/`require_admin`（`rag/api_security.py`）、`rag/operations_api.py` 的分页与序列化约定。
- Produces: 挂载于 `/knowledge-operations/evolution` 的只读路由：`GET /observations`、`GET /clusters`、`GET /clusters/{cluster_id}`、`GET /state-machine`；分页响应形状 `{"items": [...], "total": n, "page": p, "page_size": s}`；序列化函数 `_serialize_observation` / `_serialize_cluster`。

- [ ] **Step 1: Write failing API tests**

```python
def test_observation_list_is_admin_only(client, user_headers, admin_headers):
    assert client.get("/knowledge-operations/evolution/observations", headers=user_headers).status_code == 403
    response = client.get("/knowledge-operations/evolution/observations", headers=admin_headers)
    assert response.status_code == 200
    assert set(response.json()) >= {"items", "total", "page", "page_size"}


def test_cluster_detail_exposes_members_and_state(client, admin_headers, store_with_cluster):
    cluster_id = store_with_cluster["cluster_id"]
    detail = client.get(f"/knowledge-operations/evolution/clusters/{cluster_id}", headers=admin_headers).json()
    assert detail["status"] == "awaiting_diagnosis_review"
    assert detail["pool"] == "candidate_pool"
    assert [m["status"] for m in detail["members"]] == ["clustered"]


def test_state_machine_matches_design_section_seven(client, admin_headers):
    body = client.get("/knowledge-operations/evolution/state-machine", headers=admin_headers).json()
    assert body["transitions"]["observed"] == ["clustered", "rejected", "blocked", "needs_more_evidence"]
    assert body["transitions"]["canary"] == ["stable", "rolled_back", "rejected", "blocked", "needs_more_evidence"]
    assert "POST" not in {m for path in body["endpoints"] for m in path["methods"]}


def test_response_never_contains_chunk_text(client, admin_headers, store_with_observation):
    body = client.get("/knowledge-operations/evolution/observations", headers=admin_headers).text
    assert "chunk-secret-text" not in body
    assert "RAG_" not in body
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_evolution_api.py -q`

Expected: FAIL because the router does not exist。

- [ ] **Step 3: Implement the read-only router**

全部端点 `Depends(require_user)` + `require_admin`（仿 `operations_api.list_all_tasks` 的 403 模式）；本阶段没有任何写端点、没有转移端点（管理员显式标记留待第 3 阶段，见开放问题 3）；`GET /state-machine` 直接返回 `rag/evolution/models.py` 的转移表与端点清单（只读视图的数据源，设计与实现单一出处）。序列化只含：ID、状态、池、优先级、kb_id、失败阶段、根因特征、问题文本（管理员可见，与 `execution_logs` 一致）、块哈希列表、分数、`answerability_kind`、Jev 版本元数据、时间戳；绝不返回原始块文本或决策元数据白名单之外的键。`rag/api.py` 在既有 `include_router` 序列中挂载。

- [ ] **Step 4: Build the minimal evolution-center page**

`frontend/src/api/evolution.ts` 按 `knowledgeOperations.ts` 风格封装五个 GET；`EvolutionCenterView.vue` 三个只读区块：问题观察列表（筛选 status/kb_id + 分页）、聚类与状态机只读视图（候选池/观察队列分组 + 状态机图例）、指标占位（Task 5 填充）；路由 `path: 'operations/evolution'`、`name: 'evolution-center'` 挂入 `MainLayout` children。前端测试：非管理员不渲染写操作入口（本阶段无）、列表/详情/状态机三类渲染与错误分支。

- [ ] **Step 5: Run API and frontend tests**

Run: `python -m pytest tests/test_evolution_api.py tests/test_operations_api.py -q`

Run from `frontend`: `npm test -- --run src/api/__tests__/evolution.test.ts src/views/__tests__/EvolutionCenterView.test.ts src/views/__tests__/KnowledgeOperationsView.test.ts`

Expected: PASS；知识运营既有页面测试不回归。

- [ ] **Step 6: Commit**

```powershell
git add rag/evolution/api.py rag/evolution/__init__.py rag/api.py frontend/src/api/evolution.ts frontend/src/views/EvolutionCenterView.vue frontend/src/views/__tests__/EvolutionCenterView.test.ts frontend/src/api/__tests__/evolution.test.ts frontend/src/router/index.ts tests/test_evolution_api.py
git commit -m "feat: expose read-only evolution observations"
```

### Task 5: Report the minimal observation metrics

**Files:**
- Create: `rag/evolution/metrics.py`
- Create: `tests/test_evolution_metrics.py`
- Modify: `rag/evolution/api.py`
- Modify: `frontend/src/views/EvolutionCenterView.vue`
- Modify: `frontend/src/api/evolution.ts`
- Modify: `frontend/src/views/__tests__/EvolutionCenterView.test.ts`
- Modify: `config.py`

**Interfaces:**
- Consumes: `FailureTraceStore` 观测与跳过日志、`rag/user_db.py` 反馈行（`get_feedback`、`count_distinct_feedback_users` 等既有只读方法，跨库在 Python 侧聚合）。
- Produces: `compute_metrics(store, *, feedback_rows, window_days=30, now=None) -> dict`；`METRIC_DEFINITIONS: dict[str, str]`（指标名 → 中文定义与计算口径）；`GET /knowledge-operations/evolution/metrics` 响应 `{"window_days": 30, "counts": {...}, "rates": {...}, "definitions": {...}}`。

- [ ] **Step 1: Write failing metric tests**

```python
from rag.evolution.metrics import METRIC_DEFINITIONS, compute_metrics


def test_empty_window_is_zero_division_safe(store):
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=1_000_000.0)
    assert metrics["rates"]["problem_solve_rate"] is None
    assert metrics["counts"]["observed"] == 0
    assert set(metrics["definitions"]) == set(METRIC_DEFINITIONS)


def test_wrong_answer_rate_uses_stored_signals_only(store):
    store.add_observation(observation(kind="answered", question="q1"))
    store.add_observation(observation(kind="answered", question="q2", feedback_kind="not_useful"))
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=1_000_000.0)
    assert metrics["rates"]["wrong_answer_rate"] == 0.5


def test_refusal_accuracy_counts_confirmed_refusals(store):
    store.add_observation(observation(kind="refused", question="q1", feedback_kind="useful"))
    store.add_observation(observation(kind="refused", question="q2", feedback_kind="not_useful"))
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=1_000_000.0)
    assert metrics["rates"]["refusal_accuracy_rate"] == 0.5


def test_window_boundaries_are_inclusive_and_ordered(store):
    now = 1_000_000.0
    store.add_observation(observation(question="old", created_at=now - 31 * 86400))
    store.add_observation(observation(question="fresh", created_at=now))
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=now)
    assert metrics["counts"]["observed"] == 1
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_evolution_metrics.py -q`

Expected: FAIL because `compute_metrics` does not exist。

- [ ] **Step 3: Implement the minimal metric set**

第 2 阶段最小集（设计第 11 节中可用现有信号计算的部分；其余显式缺席并注明阶段）：

| 指标 | 口径（只用存储信号） |
| --- | --- |
| `problem_solve_rate` 问题解决率 | 窗口内已离开观察队列的聚类数 ÷ 聚类总数 |
| `wrong_answer_rate` 错误答案率 | 已回答观测中带负向反馈/冲突记录者 ÷ 已回答观测 |
| `refusal_accuracy_rate` 拒答准确率 | 拒答观测中获 `useful` 反馈者 ÷ 带反馈的拒答观测 |
| `citation_accuracy_rate` 引用正确率 | 引用反馈中 `useful` ÷ 引用反馈总数 |
| `structured_accuracy_rate` 结构化计算准确率 | 结构化路由无错误信号观测 ÷ 结构化路由观测 |
| `avg_latency_ms` 平均延迟 | 观测 `total_ms` 均值 |
| `funnel` 观测漏斗 | observed/clustered/awaiting_diagnosis_review/skip 按原因计数 |

`旧能力回归数 / 人工核验通过率 / 灰度回退率 / 同类问题泛化率 / 候选成本` 在第 2 阶段恒不返回（无候选、无灰度），`definitions` 中注明归属阶段，禁止用占位假数据。分母为 0 时比率返回 `None`；所有比率随 `definitions` 一起下发，保证口径可审计。

- [ ] **Step 4: Expose metrics and fill the dashboard**

`rag/evolution/api.py` 增加 `GET /metrics`（同样管理员只读，feedback 行经 `rag/user_db.py` 既有只读方法获取，跨库在 Python 侧关联，不 ATTACH、不写库）；前端指标卡片区渲染 `rates`/`counts` 与 `definitions` 提示，`None` 显示"样本不足"。

Run: `python -m pytest tests/test_evolution_metrics.py tests/test_evolution_api.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add rag/evolution/metrics.py rag/evolution/api.py config.py frontend/src/views/EvolutionCenterView.vue frontend/src/api/evolution.ts frontend/src/views/__tests__/EvolutionCenterView.test.ts tests/test_evolution_metrics.py
git commit -m "feat: report evolution observation metrics"
```

### Task 6: Verify the observation phase and hand off

**Files:**
- Modify: `docs/evolution-observations.md`（新建）
- Modify: 仅当验证发现 Task 1–5 所属文件的缺陷时，才改对应实现文件。

**Interfaces:**
- Consumes: Task 1–5 的实现与配置。
- Produces: 运维文档 + 可复核的验证结论；不新增应用接口，不合并、不切换线上服务。

- [ ] **Step 1: Run the focused evolution tests**

Run: `python -m pytest tests/test_evolution_recorder.py tests/test_evolution_clusterer.py tests/test_evolution_worker.py tests/test_evolution_api.py tests/test_evolution_metrics.py -q`

Expected: PASS。

- [ ] **Step 2: Run the key-capability regression set**

Run: `python -m pytest tests/test_pipeline.py tests/test_pipeline_stream.py tests/test_pipeline_lifecycle.py tests/test_pipeline_reuses_connections.py tests/test_retriever.py tests/test_reranker.py tests/test_answerability.py tests/test_confidence.py tests/test_kb_security_boundaries.py tests/test_org_retrieval_scope.py tests/test_agent_permission_filter.py tests/test_tracker.py tests/test_gap_analyzer.py tests/test_knowledge_tasks.py tests/test_jev_config.py tests/test_jev_questions.py tests/test_jev_client.py tests/test_jev_routing_policy.py tests/test_jev_passage_policy.py tests/test_jev_shadow.py tests/test_jev_benchmark.py -q`

Expected: PASS。任一失败按 Global Constraints 处理：修实现，不改阈值、不删用例。

- [ ] **Step 3: Run lint and the frontend regression**

Run: `python -m ruff check rag/evolution rag/pipeline.py rag/api.py config.py tests/test_evolution_*.py`

Run from `frontend`: `npm test -- --run`

Expected: PASS。

- [ ] **Step 4: Verify offline behavior with the flag off and on**

确认 `RAG_EVOLUTION_ENABLED` 缺省时：pipeline 不创建 recorder、`submit` 路径为空操作、`GET /knowledge-operations/evolution/*` 仍返回 403/200 仅取决于身份且无数据写入 `data/evolution.db`（文件不存在即未创建）；开启后:worker 线程随 lifespan 启停、观测与跳过计数可见、`git status --short` 不含 `.env`/`data/`/`qdrant_data/`/日志产物。

- [ ] **Step 5: Document operation and rollback**

`docs/evolution-observations.md` 记录：页面读法、指标口径（与 `METRIC_DEFINITIONS` 一致）、预算与繁忙暂停配置项、三张表含义、回退步骤（停 worker → `RAG_EVOLUTION_ENABLED=false` → 删除 `data/evolution.db` → 重启），以及"第 2 阶段不生成候选、不放宽任何门禁"的边界声明。不含任何密钥值。

- [ ] **Step 6: Review the branch diff before handoff**

Run: `git diff --stat main...HEAD` 与 `git diff main...HEAD`。

检查：无权限/审计/ACL 代码改动；无验收器、阈值、benchmarks 用例改动；无失败测试被删或被跳过；无硬编码答案；无 `.env`/`data/`/`qdrant_data/` 提交；`rag/tracker.py` 未被修改。

- [ ] **Step 7: Stop before merging**

不合并到 `main`、不切换 1002 服务、不启动新服务，将提交范围、测试计数、页面截图/录屏、指标样例与回退步骤提交给管理员，由其确认后合并。

---

## 后续阶段路线图（仅目标与入口，不展开 TDD）

> 以下阶段在本阶段观测数据积累后，由管理员确认再编写对应实施计划。

### 第 3 阶段：诊断辅助（EvolutionDiagnoser）

- **目标**：对候选池聚类生成参考轨迹对比与结构化诊断草案（失败类别、发生阶段、第一处偏离、直接证据、根因、影响范围、建议修改层级、复现方法、验收标准），人工确认或修订根因后才允许进入候选生成（设计第 6、7、8 节）。
- **入口**：`status="awaiting_diagnosis_review"` 的候选池聚类；复用本阶段观测中的块哈希、分数、Jev 版本元数据与 `execution_logs` 成功轨迹构造参考证据优先级（确定性证据 > 管理员确认答案 > 原始文档引用）。
- **明确不做**：不自动改任何配置或代码；根因为"无法确定"或不可复现的留在观察队列。

### 第 4 阶段：低风险候选（提示词/检索参数/字段映射/适配规则）

- **目标**：开放提示词、检索配置、查询改写、字段映射与知识库适配规则的最小候选生成；每个候选绑定 Git 提交与版本快照，通过五层门禁（证据/功能/回归/工程/资源）并经人工核验（设计第 9、10 节）。
- **入口**：状态机 `approved_for_candidate → candidate_testing → awaiting_verification`；候选以版本化协议（`rag/jev/questions.py` 问题集版本、`rag/jev/policy.py` 阈值）表达，供 Jev 在线层执行（设计第 4 节）。
- **明确不做**：不触碰切块、重排、解析器、工具工作流与项目代码（第 5 阶段开放）。

### 第 5 阶段：隔离代码进化（worktree、多候选、五层门禁、灰度与快照回退）

- **目标**：开放切块策略、重排流程、解析器与工具工作流，最终到项目代码；候选只在隔离 Git worktree 中生成补丁，DGM 式多分支探索（低分候选只进探索档案），完整五层门禁 + 知识库级灰度 + 双重人工确认 + 快照回退（设计第 3、9、10 节）。
- **入口**：状态机 `approved_for_canary → canary → stable` 与 `rolled_back`；每个不可变版本快照绑定 Git 提交并保存提示词、检索参数、切块策略、解析规则、Jev 问题集及索引版本。
- **明确不做**：权限策略、审计记录、验收器、测试基准、发布门禁、密钥与用户原始数据永久禁止由进化任务修改（设计第 2 节末段）。

---

**本计划只覆盖设计第 12 节的第 2 阶段（观测基础）。后续阶段（诊断辅助、低风险候选、隔离代码进化）的实施计划，待本阶段观测数据积累并经管理员确认后再行编写。**

---

## 开放问题与决策记录（2026-09-22，管理员已确认）

1. **evolution 数据落地与备份**：默认独立库 `data/evolution.db`；**纳入**既有备份策略（随 `RAG_BACKUP_ROOT`，保留 8 份）。落实于 `docs/evolution-observations.md` §2。
2. **默认开关**：`RAG_EVOLUTION_ENABLED` **默认 false**（安全关闭，与 Jev 一致）；开启时也只观测、零在线影响。
3. **管理员手动标记**（rejected / needs_more_evidence / blocked）：第 2 阶段**不开放**（纯只读），随第 3 阶段诊断辅助一起做；候选池目前只能靠"同类根因累计三次"自动准入。
4. **进程隔离程度**：第 2 阶段按"进程内独立线程 + 独立库连接"实现，并支持 `python -m rag.evolution.worker` 单独启动；完整独立进程/容器留到第 5 阶段。
5. **保留期与窗口**：聚类窗口 **30 天**（`RAG_EVOLUTION_CLUSTER_WINDOW_DAYS`）；观测保留 **90 天**（`RAG_EVOLUTION_OBSERVATION_RETENTION_DAYS`），由 worker 每轮清理（`store.purge_observations_before`，不动聚类）。
