# Jev Retrieval Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the serial DeepSeek router and external reranker with a confidence-aware two-stage Jev path, retain the old path as a safe fallback, and prove latency improvement without material retrieval-quality regression.

**Architecture:** A small `rag/jev` package owns TypeSafe SDK contracts, questions, policy, and fault isolation. `RAGPipeline` calls one Jev preflight decision before retrieval and one batched passage judgment after ACL filtering; deterministic code routes, filters, retries at most once, and falls back to the existing router/reranker. A benchmark runner compares old and Jev preparation paths on the same labeled cases and produces P50/P95 plus retrieval-quality results.

**Tech Stack:** Python 3.12, `typesafe-sdk>=0.7,<0.8`, Pydantic 2, pytest, SQLite execution tracing, existing FastAPI/RAG pipeline.

**Spec:** `docs/superpowers/specs/2026-09-21-jev-retrieval-optimization-design.md`

## Global Constraints

- Preserve the existing ACL-before-external-rerank boundary; unauthorized text must never reach TypeSafe.
- Keep permissions, file existence checks, precise calculations, and final workflow execution in deterministic code.
- Allow at most one DeepSeek query rewrite retry per request.
- Jev timeout, rate limit, authentication failure, invalid response, or open circuit must fall back to the existing router/reranker.
- Never log or commit the TypeSafe API key; read it from `RAG_TYPESAFE_API_KEY` through server-side settings.
- Keep the existing 1002 service running during development; use port 1102 for the isolated worktree service.
- Keep Jev question sets and policies versioned and include their versions in traces.
- Acceptance: preparation P50 improves by at least 30%, P95 by at least 20%, ordinary rewrite calls fall by at least 70%, route accuracy is at least 95%, Top-5 evidence hit rate loses no more than 2 percentage points, and security/fallback tests all pass.

## Review Focus

- A request with no API key must use the legacy path without exposing configuration details; covered in Task 3 adapter tests and Task 6 pipeline tests.
- An ACL-hidden candidate must never appear in the Jev passage request; covered in Task 6 enterprise pipeline tests.
- A low-confidence or malformed Jev preflight result must not silently select a high-risk tool route; covered in Task 4 policy tests.
- A candidate marked both answer-bearing and prompt-injected must be excluded, not promoted; covered in Task 5 passage-policy tests.
- A first-pass evidence miss may rewrite once, but a second miss must stop with insufficient evidence rather than loop; covered in Task 6 retry-bound tests.

---

## File Map

- `config.py`: Jev credentials, mode, model, timeout, thresholds, and knowledge-base allowlist.
- `pyproject.toml`: pinned compatible TypeSafe SDK dependency.
- `rag/jev/models.py`: internal immutable contracts independent of SDK response classes.
- `rag/jev/questions.py`: versioned Jev question definitions.
- `rag/jev/client.py`: TypeSafe SDK adapter, response mapping, retry/failure normalization, and circuit breaker.
- `rag/jev/policy.py`: deterministic route and passage-selection policy.
- `rag/jev/service.py`: public preflight and passage-judgment facade used by the pipeline.
- `rag/pipeline.py`: two-stage integration, conditional rewrite, one retry, fallback, and trace enrichment.
- `rag/models.py`: candidate Jev score and semantic flags.
- `rag/reliability/models.py`: explicit `jev` score provenance.
- `rag/reliability/bundle.py`: preserve Jev provenance in evidence bundles.
- `rag/tracker.py`: structured stage timing and Jev decision metadata.
- `rag/jev/shadow.py`: bounded background shadow comparison for mode `shadow`.
- `scripts/benchmark_jev_retrieval.py`: repeatable old-versus-new live benchmark runner.
- `benchmarks/jev_retrieval_cases.json`: fixed labeled route and evidence cases with no secrets.
- `tests/test_jev_*.py`: adapter, question, policy, shadow, and benchmark tests.
- Existing pipeline, reliability, security, and stream tests: regression coverage.

### Task 1: Add Jev configuration and dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `config.py`
- Create: `tests/test_jev_config.py`

**Interfaces:**
- Consumes: Pydantic `Settings` and the project `.env` convention.
- Produces: `settings.typesafe_api_key: str`, `settings.jev_mode: Literal["off", "shadow", "active"]`, `settings.jev_model: str`, `settings.jev_timeout_seconds: float`, `settings.jev_route_confidence: float`, `settings.jev_kb_allowlist: str`.

- [ ] **Step 1: Write failing configuration tests**

```python
from config import Settings


def test_jev_defaults_are_safe():
    configured = Settings(_env_file=None)
    assert configured.typesafe_api_key == ""
    assert configured.jev_mode == "off"
    assert configured.jev_model == "jev-latest"
    assert configured.jev_timeout_seconds == 1.5
    assert configured.jev_route_confidence == 0.75
    assert configured.jev_kb_allowlist == ""


def test_jev_rejects_unknown_mode():
    try:
        Settings(_env_file=None, jev_mode="unsafe")
    except ValueError:
        return
    raise AssertionError("invalid Jev mode must be rejected")
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_jev_config.py -q`

Expected: FAIL because Jev settings do not exist.

- [ ] **Step 3: Add the SDK and settings**

Add `"typesafe-sdk>=0.7,<0.8"` to dependencies and add:

```python
from typing import Literal

typesafe_api_key: str = ""
jev_mode: Literal["off", "shadow", "active"] = "off"
jev_model: str = "jev-latest"
jev_timeout_seconds: float = Field(default=1.5, gt=0.0, le=10.0)
jev_route_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
jev_kb_allowlist: str = ""
```

- [ ] **Step 4: Install the updated project and run tests**

Run: `python -m pip install -e .`

Run: `python -m pytest tests/test_jev_config.py tests/test_reliability_models.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml config.py tests/test_jev_config.py
git commit -m "build: add Jev configuration"
```

### Task 2: Define internal Jev contracts and versioned questions

**Files:**
- Create: `rag/jev/__init__.py`
- Create: `rag/jev/models.py`
- Create: `rag/jev/questions.py`
- Create: `tests/test_jev_questions.py`

**Interfaces:**
- Consumes: no SDK response types.
- Produces: `PreflightDecision`, `PassageJudgment`, `JevBatchResult`, `PREFLIGHT_QUESTION_SET_VERSION`, `PASSAGE_QUESTION_SET_VERSION`, `build_preflight_questions()`, and `build_passage_questions(chunks)`.

- [ ] **Step 1: Write failing contract and question tests**

```python
from rag.jev.models import IntentKind, PassageJudgment, PreflightDecision
from rag.jev.questions import (
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
)


def test_question_ids_are_stable_and_complete():
    assert PREFLIGHT_QUESTION_SET_VERSION == "preflight-v1"
    assert set(build_preflight_questions()) == {"intent", "complexity", "needs_query_rewrite"}
    assert PASSAGE_QUESTION_SET_VERSION == "passages-v1"
    assert set(build_passage_questions(["c0"])) == {
        "c0.is_relevant",
        "c0.contains_answer_evidence",
        "c0.contradicts_query_premise",
        "c0.contains_prompt_injection",
    }


def test_internal_models_reject_invalid_probabilities():
    try:
        PassageJudgment(chunk_id="c0", relevance=1.2, answer_evidence=0.8, contradiction=0.0, prompt_injection=0.0)
    except ValueError:
        return
    raise AssertionError("probabilities outside [0,1] must fail")
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_jev_questions.py -q`

Expected: FAIL because `rag.jev` does not exist.

- [ ] **Step 3: Implement immutable internal models**

Define exact contracts:

```python
IntentKind = Literal["structured_query", "retrieval_qa", "tool_task", "clarification"]
ComplexityKind = Literal["simple", "multi_step", "specialist"]

class PreflightDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: IntentKind
    intent_confidence: float = Field(ge=0.0, le=1.0)
    complexity: ComplexityKind
    complexity_confidence: float = Field(ge=0.0, le=1.0)
    rewrite_probability: float = Field(ge=0.0, le=1.0)
    model: str
    question_set_version: str
    duration_ms: float = Field(ge=0.0)

class PassageJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chunk_id: str
    relevance: float = Field(ge=0.0, le=1.0)
    answer_evidence: float = Field(ge=0.0, le=1.0)
    contradiction: float = Field(ge=0.0, le=1.0)
    prompt_injection: float = Field(ge=0.0, le=1.0)
```

Define `JevBatchResult[T]` with `value: T | None`, `available: bool`, `failure_kind: Literal["disabled", "timeout", "rate_limited", "auth", "invalid_response", "service"] | None`, and `duration_ms`.

- [ ] **Step 4: Define SDK question builders**

Use TypeSafe `Choice`, `Score`, and `Noul`. Keep full meanings in instructions and criteria; passage IDs remain in question IDs. Include explicit `clarification`/no-match intent and concrete complexity levels. Each passage question references `passages.<chunk_id>.text` in shared state.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_jev_questions.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add rag/jev tests/test_jev_questions.py
git commit -m "feat: define versioned Jev judgments"
```

### Task 3: Implement the fault-isolated TypeSafe adapter

**Files:**
- Create: `rag/jev/client.py`
- Create: `tests/test_jev_client.py`

**Interfaces:**
- Consumes: `Settings`, question builders, `TypeSafeClient.system_one(state=..., questions=..., model=...)`.
- Produces: `JevClient.preflight(state: dict) -> JevBatchResult[PreflightDecision]` and `JevClient.judge_passages(state: dict, chunk_ids: list[str]) -> JevBatchResult[tuple[PassageJudgment, ...]]`.

- [ ] **Step 1: Write failing adapter mapping tests with a fake SDK client**

```python
def test_preflight_maps_choice_score_and_noul(fake_typesafe_client):
    fake_typesafe_client.answers = {
        "intent": FakeChoice("retrieval_qa", confidence=0.94),
        "complexity": FakeChoice("simple", confidence=0.91),
        "needs_query_rewrite": FakeNoul(0.08),
    }
    result = JevClient(client=fake_typesafe_client).preflight({"question": "年假几天"})
    assert result.available is True
    assert result.value.intent == "retrieval_qa"
    assert result.value.rewrite_probability == 0.08


def test_missing_key_returns_disabled_without_constructing_sdk(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    result = JevClient().preflight({"question": "test"})
    assert result.available is False
    assert result.failure_kind == "disabled"
```

Add parameterized tests for timeout, 429, authentication, invalid answer IDs, out-of-range values, and a response missing one passage question.

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_jev_client.py -q`

Expected: FAIL because `JevClient` does not exist.

- [ ] **Step 3: Implement lazy client creation and normalized failures**

Construct `TypeSafeClient(api_key=settings.typesafe_api_key, model=settings.jev_model, retry=RetryPolicy(max_retries=1, timeout=settings.jev_timeout_seconds))` only when a call is made and a key exists. Reuse the client. Catch TypeSafe SDK timeout, rate-limit, authentication, API, and validation errors and map them to `failure_kind` without including response bodies or credentials in logs.

- [ ] **Step 4: Add a circuit breaker**

Use the project `CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)`. An open circuit returns `service` immediately. Successful valid mappings close the failure streak; invalid responses count as failures.

- [ ] **Step 5: Run adapter and secret-leak tests**

Run: `python -m pytest tests/test_jev_client.py -q`

Expected: PASS and captured logs do not contain the configured fake key.

- [ ] **Step 6: Commit**

```powershell
git add rag/jev/client.py tests/test_jev_client.py
git commit -m "feat: add resilient TypeSafe adapter"
```

### Task 4: Add deterministic preflight routing policy

**Files:**
- Create: `rag/jev/policy.py`
- Create: `rag/jev/service.py`
- Create: `tests/test_jev_routing_policy.py`

**Interfaces:**
- Consumes: `JevClient.preflight`, confidence thresholds, knowledge-base capabilities.
- Produces: `RouteDecision(route: Literal["structured", "rag", "tool", "clarification", "legacy"], should_rewrite: bool, reason: str, preflight: PreflightDecision | None)` and `JevDecisionService.route(question, capabilities) -> RouteDecision`.

- [ ] **Step 1: Write a routing decision table as tests**

```python
@pytest.mark.parametrize(
    ("intent", "confidence", "expected"),
    [
        ("structured_query", 0.95, "structured"),
        ("retrieval_qa", 0.95, "rag"),
        ("tool_task", 0.95, "tool"),
        ("clarification", 0.95, "clarification"),
        ("tool_task", 0.40, "legacy"),
    ],
)
def test_route_policy_is_confidence_gated(intent, confidence, expected):
    decision = preflight(intent=intent, confidence=confidence)
    assert choose_route(decision, threshold=0.75).route == expected
```

Add tests that missing structured capability prevents a structured route, a disabled/unavailable client returns `legacy`, and `should_rewrite` is true only for a confident rewrite probability above its configured threshold.

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_jev_routing_policy.py -q`

Expected: FAIL because the policy is absent.

- [ ] **Step 3: Implement policy and facade**

Keep all thresholds in a `JevPolicy` dataclass. Do not encode route policy in prompt prose. Low-confidence `tool_task` must return `legacy`; low-confidence `clarification` must default to `rag` unless deterministic input validation already requires clarification.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_jev_routing_policy.py tests/test_jev_client.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/jev/policy.py rag/jev/service.py tests/test_jev_routing_policy.py
git commit -m "feat: route requests with confidence-gated Jev decisions"
```

### Task 5: Add deterministic passage selection and Jev provenance

**Files:**
- Modify: `rag/models.py`
- Modify: `rag/reliability/models.py`
- Modify: `rag/reliability/bundle.py`
- Modify: `rag/jev/policy.py`
- Modify: `rag/jev/service.py`
- Create: `tests/test_jev_passage_policy.py`
- Modify: `tests/test_reliability_models.py`
- Modify: `tests/test_answerability.py`

**Interfaces:**
- Consumes: ACL-filtered `list[Chunk]` and `tuple[PassageJudgment, ...]`.
- Produces: `PassageSelection(accepted: tuple[Chunk, ...], conflicts: tuple[Chunk, ...], excluded_ids: tuple[str, ...], has_answer_evidence: bool)` and chunks carrying `rerank_score=<Jev relevance probability>`, `score_source="jev"`, plus semantic flags.

- [ ] **Step 1: Write failing selection tests**

```python
def test_injected_answer_bearing_passage_is_excluded():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.99, evidence=.99, contradiction=.0, injection=.95)],
        policy=JevPolicy(injection_threshold=.7),
    )
    assert selected.accepted == ()
    assert selected.excluded_ids == ("c0",)


def test_conflict_is_preserved_separately():
    selected = select_passages(
        [chunk("c0")],
        [judgment("c0", relevance=.9, evidence=.8, contradiction=.9, injection=.01)],
        policy=JevPolicy(),
    )
    assert selected.conflicts[0].text == "c0"
```

Add tests for stable sorting, background-only chunks, all-no-evidence, missing judgment, and `EvidenceRef(score_source="jev")` validation.

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_jev_passage_policy.py tests/test_reliability_models.py -q`

Expected: FAIL because Jev provenance and selection do not exist.

- [ ] **Step 3: Extend explicit score provenance**

Change `ScoreSource` to `Literal["reranker", "jev", "unavailable"]`. Add `score_source` to `Chunk` with default `"unavailable"`; set legacy reranker output to `"reranker"` and Jev output to `"jev"`. Update `build_evidence_bundle` to copy the chunk's explicit source rather than infer it from score presence.

- [ ] **Step 4: Implement passage policy**

Match candidates by stable request-local IDs, exclude injection first, preserve contradictions separately, rank answer evidence before background relevance, and cap accepted results at the requested `top_k`. Missing judgments cause the entire Jev batch to be unavailable so the caller can use the legacy reranker; never partially trust a malformed batch.

- [ ] **Step 5: Run reliability and passage tests**

Run: `python -m pytest tests/test_jev_passage_policy.py tests/test_reliability_models.py tests/test_answerability.py tests/test_reranker.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add rag/models.py rag/reliability rag/jev tests/test_jev_passage_policy.py tests/test_reliability_models.py tests/test_answerability.py
git commit -m "feat: rank evidence with Jev passage judgments"
```

### Task 6: Integrate the two-stage path into `RAGPipeline`

**Files:**
- Modify: `rag/pipeline.py`
- Modify: `rag/reranker.py`
- Create: `tests/test_pipeline_jev.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_pipeline_stream.py`
- Modify: `tests/test_kb_security_boundaries.py`

**Interfaces:**
- Consumes: `JevDecisionService`, `RouteDecision`, `PassageSelection`, legacy `route_question`, `rewrite_query`, and `Reranker`.
- Produces: identical public `query()` and `query_stream()` contracts, with internal active Jev routing, one bounded rewrite retry, and old-path fallback.

- [ ] **Step 1: Write failing active-path tests**

Cover these exact behaviors:

```python
def test_clear_rag_question_skips_deepseek_rewrite(jev_pipeline, mock_rewrite):
    jev_pipeline.jev.route.return_value = rag_route(should_rewrite=False)
    jev_pipeline.query("年假有几天")
    mock_rewrite.assert_not_called()


def test_no_evidence_rewrites_only_once(jev_pipeline, mock_rewrite):
    jev_pipeline.jev.select.side_effect = [no_evidence(), no_evidence()]
    jev_pipeline.query("换个说法才能找到的问题")
    mock_rewrite.assert_called_once()


def test_hidden_chunks_never_reach_jev(enterprise_jev_pipeline):
    enterprise_jev_pipeline.query("内部制度", user=ordinary_user, kb_id="kb1")
    sent = enterprise_jev_pipeline.jev.select.call_args.args[1]
    assert [chunk.doc_name for chunk in sent] == ["visible.txt"]
```

Add tests for structured route, clarification route, Jev preflight failure to legacy route, Jev passage failure to legacy reranker, active KB allowlist mismatch, and streaming parity.

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_pipeline_jev.py -q`

Expected: FAIL because the pipeline does not use Jev.

- [ ] **Step 3: Add the service dependency and feature gate**

Allow `RAGPipeline(..., jev_service: JevDecisionService | None = None)`. Create a default service only when `jev_mode != "off"`. Enable active routing only when mode is `active` and the effective KB is in the parsed allowlist; an empty allowlist means no KB is active until explicitly configured.

- [ ] **Step 4: Extract one retrieval attempt**

Create a private method with this exact responsibility:

```python
def _retrieve_visible_candidates(
    self,
    query: str,
    *,
    top_k: int,
    doc_name: str | None,
    tags: list[str] | None,
    user: dict | None,
    kb_id: str,
) -> list[Chunk]:
    """Retrieve, version-scope, and ACL-filter candidates before any external judge."""
```

Move existing current/history retrieval and ACL filtering into it without changing permission behavior.

- [ ] **Step 5: Implement active preflight and passage decisions**

Apply Jev preflight after input guard and enterprise identity checks. Route structured questions before expensive retrieval. For RAG, retrieve with the original question unless policy requests rewrite. Send only `_retrieve_visible_candidates` output to Jev. If Jev passage judgment fails, call the legacy reranker. Preserve the existing post-rank ACL defense-in-depth filter.

- [ ] **Step 6: Add the one-rewrite state machine**

Represent attempts explicitly as `[(question, False)]`, append `(rewrite_query(question), True)` at most once when policy requests rewrite or first-pass evidence is absent, and never append another attempt. If the second result lacks evidence, build an empty evidence bundle so the existing answerability gate produces `insufficient_evidence`.

- [ ] **Step 7: Run pipeline and security regression tests**

Run: `python -m pytest tests/test_pipeline_jev.py tests/test_pipeline.py tests/test_pipeline_stream.py tests/test_kb_security_boundaries.py tests/test_org_retrieval_scope.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add rag/pipeline.py rag/reranker.py tests/test_pipeline_jev.py tests/test_pipeline.py tests/test_pipeline_stream.py tests/test_kb_security_boundaries.py
git commit -m "feat: integrate Jev into the retrieval pipeline"
```

### Task 7: Add stage tracing and non-blocking shadow evaluation

**Files:**
- Modify: `rag/tracker.py`
- Create: `rag/jev/shadow.py`
- Modify: `rag/pipeline.py`
- Create: `tests/test_jev_shadow.py`
- Modify: `tests/test_pipeline_reuses_connections.py`

**Interfaces:**
- Consumes: visible candidates, legacy decisions, Jev service, bounded executor.
- Produces: `ExecutionTrace.stage_timings: dict[str, float]`, `ExecutionTrace.decision_metadata: dict[str, object]`, and `ShadowEvaluator.submit(case) -> bool`.

- [ ] **Step 1: Write failing trace and shadow tests**

```python
def test_shadow_submit_does_not_wait_for_jev(blocking_jev):
    evaluator = ShadowEvaluator(blocking_jev, max_workers=1, queue_limit=8)
    started = time.perf_counter()
    assert evaluator.submit(shadow_case()) is True
    assert time.perf_counter() - started < 0.05


def test_trace_serializes_stage_timings_without_secret(tmp_path):
    tracker = ExecutionTracker(tmp_path / "trace.db")
    tracker.save(ExecutionTrace(question="q", route="rag", stage_timings={"jev_preflight_ms": 12.5}))
    assert tracker.get_recent(1)[0]["details"]
    assert "secret-test-key" not in tracker.get_recent(1)[0]["details"]
```

Add tests for full shadow queue dropping work instead of blocking, clean executor shutdown, and reused Jev clients.

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_jev_shadow.py -q`

Expected: FAIL because trace fields and shadow evaluator do not exist.

- [ ] **Step 3: Extend tracker JSON without a migration**

Store `tool_calls`, `stage_timings`, and `decision_metadata` in the existing `details` JSON field. Sanitize metadata through an explicit allowlist: model, question-set version, route, confidence, failure kind, attempt count, and durations only.

- [ ] **Step 4: Implement bounded shadow execution**

Use a process-local `ThreadPoolExecutor(max_workers=2)` plus a semaphore queue limit. `submit` must return immediately; when full it returns `False` and increments a dropped counter. Shadow results may write comparison metadata but must never alter the current response or candidate order.

- [ ] **Step 5: Wire `jev_mode="shadow"`**

Run the legacy path normally. After ACL filtering, submit the preflight state, visible candidates, legacy route, and legacy ranks to `ShadowEvaluator`. Close it from `RAGPipeline.close()` only when the pipeline owns it.

- [ ] **Step 6: Run tracing, lifecycle, and concurrency tests**

Run: `python -m pytest tests/test_jev_shadow.py tests/test_pipeline_lifecycle.py tests/test_pipeline_reuses_connections.py tests/test_stream_eventloop_nonblocking.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add rag/tracker.py rag/jev/shadow.py rag/pipeline.py tests/test_jev_shadow.py tests/test_pipeline_reuses_connections.py
git commit -m "feat: trace and shadow Jev decisions"
```

### Task 8: Build the repeatable A/B benchmark and report

**Files:**
- Create: `scripts/benchmark_jev_retrieval.py`
- Create: `benchmarks/jev_retrieval_cases.json`
- Create: `tests/test_jev_benchmark.py`
- Create: `docs/jev-benchmark.md`

**Interfaces:**
- Consumes: JSON cases with `id`, `question`, `expected_route`, `kb_id`, `expected_source_names`, and optional `notes`; legacy and Jev preparation callables.
- Produces: JSON and Markdown reports containing sample count, warmup count, P50/P95 by stage, rewrite rate, route accuracy, Top-1/Top-5 evidence hit rate, failures, and acceptance verdicts.

- [ ] **Step 1: Write failing percentile and acceptance tests**

```python
def test_percentiles_are_computed_from_sorted_samples():
    summary = summarize([10, 20, 30, 40, 50])
    assert summary.p50_ms == 30
    assert summary.p95_ms == 50


def test_acceptance_rejects_fast_but_inaccurate_candidate():
    verdict = evaluate_acceptance(
        old=metrics(prep_p50=1000, prep_p95=2000, top5=.90),
        new=metrics(prep_p50=500, prep_p95=1000, top5=.80),
    )
    assert verdict.passed is False
    assert "top5" in verdict.failures
```

Add tests for empty samples, one sample, failed calls, warmup exclusion, rewrite-rate calculation, route threshold, and Markdown escaping.

- [ ] **Step 2: Run and confirm failure**

Run: `python -m pytest tests/test_jev_benchmark.py -q`

Expected: FAIL because benchmark functions do not exist.

- [ ] **Step 3: Implement the benchmark CLI**

Support:

```text
python scripts/benchmark_jev_retrieval.py \
  --cases benchmarks/jev_retrieval_cases.json \
  --runs 5 --warmups 1 \
  --json-out artifacts/jev-benchmark.json \
  --markdown-out artifacts/jev-benchmark.md
```

Use `time.perf_counter_ns()`. Run old and new paths in alternating order per case to reduce time-of-day bias. Do not benchmark answer generation when calculating preparation acceptance, but record optional end-to-end timings separately. Redact question text in the report unless `--include-questions` is explicitly supplied.

- [ ] **Step 4: Add a deterministic starter case set**

Create at least 20 cases covering five ordinary RAG questions, five spreadsheet questions, three tool/calculation requests, three ambiguous questions, two no-answer questions, one conflicting-evidence question, and one prompt-injection candidate. Every case must name an expected route; retrieval cases must name at least one expected source from local non-sensitive fixtures or administrator-approved data.

- [ ] **Step 5: Run benchmark unit tests**

Run: `python -m pytest tests/test_jev_benchmark.py -q`

Expected: PASS.

- [ ] **Step 6: Document safe live execution**

In `docs/jev-benchmark.md`, document setting `RAG_TYPESAFE_API_KEY` in ignored `.env`, using `RAG_JEV_MODE=active`, specifying `RAG_JEV_KB_ALLOWLIST`, starting port 1102, running the benchmark, interpreting thresholds, and removing/rotating the locally exposed key. Do not include any credential value.

- [ ] **Step 7: Commit**

```powershell
git add scripts/benchmark_jev_retrieval.py benchmarks/jev_retrieval_cases.json tests/test_jev_benchmark.py docs/jev-benchmark.md
git commit -m "test: benchmark Jev retrieval latency and quality"
```

### Task 9: Verify the branch and run the live comparison

**Files:**
- Modify only if verification reveals defects in files owned by Tasks 1–8.
- Produce locally ignored: `artifacts/jev-benchmark.json`, `artifacts/jev-benchmark.md`, `.codex-ragv3-1102.out.log`, `.codex-ragv3-1102.err.log`.

**Interfaces:**
- Consumes: completed implementation and configured local TypeSafe key.
- Produces: a reviewed verification result and live A/B evidence; no new application interface.

- [ ] **Step 1: Run focused Jev tests**

Run: `python -m pytest tests/test_jev_config.py tests/test_jev_questions.py tests/test_jev_client.py tests/test_jev_routing_policy.py tests/test_jev_passage_policy.py tests/test_pipeline_jev.py tests/test_jev_shadow.py tests/test_jev_benchmark.py -q`

Expected: PASS.

- [ ] **Step 2: Run related regression tests**

Run: `python -m pytest tests/test_pipeline.py tests/test_pipeline_stream.py tests/test_pipeline_lifecycle.py tests/test_pipeline_reuses_connections.py tests/test_retriever.py tests/test_reranker.py tests/test_answerability.py tests/test_confidence.py tests/test_kb_security_boundaries.py tests/test_org_retrieval_scope.py tests/test_agent_permission_filter.py -q`

Expected: PASS.

- [ ] **Step 3: Run lint and the full frontend regression**

Run: `python -m ruff check config.py rag/jev rag/pipeline.py rag/reranker.py rag/reliability rag/tracker.py scripts/benchmark_jev_retrieval.py tests/test_jev_*.py`

Run from `frontend`: `npm test -- --run`

Expected: PASS.

- [ ] **Step 4: Store the provided key locally without echoing it**

Ensure `.env` is ignored with `git check-ignore -v .env`. Add `RAG_TYPESAFE_API_KEY=<value>` and Jev flags through a non-echoing editor or patch operation. Confirm only key names, never values, and verify `git status --short -- .env` is empty.

- [ ] **Step 5: Start the isolated service on port 1102**

Run the worktree virtual environment with `python -m uvicorn rag.api:app --host 127.0.0.1 --port 1102`, hidden, with stdout/stderr redirected to ignored local logs. Verify `/health` returns HTTP 200 and both SQLite and Qdrant report `ok`.

- [ ] **Step 6: Run the live A/B benchmark**

Run: `python scripts/benchmark_jev_retrieval.py --cases benchmarks/jev_retrieval_cases.json --runs 5 --warmups 1 --json-out artifacts/jev-benchmark.json --markdown-out artifacts/jev-benchmark.md`

Expected: report contains both paths, sample counts, P50/P95, route accuracy, Top-1/Top-5, rewrite rate, external-call counts, failures, and a threshold verdict.

- [ ] **Step 7: Inspect failures rather than weakening thresholds**

For any failed threshold, inspect exact case IDs, states, questions, Jev answers, candidate coverage, and legacy results. Fix implementation or question definitions and rerun the same case set. Do not delete failed cases or change expected labels solely to obtain a passing report.

- [ ] **Step 8: Commit only source fixes, never secrets or live artifacts**

Run: `git status --short` and confirm `.env`, logs, and `artifacts/` are ignored. If source fixes were needed, commit them with a focused message such as `fix: calibrate Jev passage policy`.

### Task 10: Final review and handoff

**Files:**
- Modify: `docs/jev-benchmark.md` only if the verified command or interpretation changed.

**Interfaces:**
- Consumes: green verification and live benchmark report.
- Produces: merge recommendation with exact evidence and rollback instructions.

- [ ] **Step 1: Review the complete branch diff**

Run: `git diff --stat main...HEAD` and `git diff main...HEAD`.

Check that no credential, raw unauthorized content, benchmark answer, or generated artifact is committed; no test or acceptance threshold was weakened; and legacy fallback remains reachable.

- [ ] **Step 2: Re-run the focused verification after review fixes**

Run the Task 9 focused tests and lint again.

Expected: PASS with fresh output.

- [ ] **Step 3: Present the evidence for human merge approval**

Report exact commit range, test counts, old/new preparation P50/P95, rewrite-rate reduction, route accuracy, Top-1/Top-5 change, security/fallback results, port 1102 health, known limitations, and rollback action (`RAG_JEV_MODE=off` or removal of the feature branch before merge).

- [ ] **Step 4: Stop before merging**

Do not merge to `main`, switch the 1002 service, or delete the worktree until the administrator explicitly approves the benchmark evidence.
