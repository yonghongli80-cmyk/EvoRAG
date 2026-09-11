# Department Spreadsheet Structured Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer numeric questions across authorized, current spreadsheets in one departmental knowledge base using typed SQLite computation, with clarification instead of guessed values.

**Architecture:** Add a spreadsheet query coordinator beside semantic retrieval. It builds an authorized schema catalog, classifies and validates a fixed JSON plan, executes compatible datasets read-only, and returns computed facts plus range citations to the existing streaming chat path.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, pytest, Vue 3, Pinia, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-11-department-spreadsheet-structured-query-design.md`

## Global Constraints

- Numeric truth must come from typed structured execution, never model arithmetic.
- Scope is one active departmental knowledge base and only documents the caller may view.
- Only current spreadsheet versions with ready datasets are eligible.
- No arbitrary SQL and no joins between unrelated schemas.
- Supported operations are lookup, typed filtering, count, distinct count, sum, average, min, max, and Top N up to 100.
- Compatible schemas may be unioned; incompatible field types or units require clarification.
- Every computed result must identify all contributing document, worksheet, version, and row or cell ranges.
- Ordinary structured execution targets approximately one second locally, excluding model interpretation.

---

## File Map

- `rag/spreadsheets/query.py`: validated single- and multi-dataset execution primitives.
- `rag/spreadsheets/catalog.py`: authorized current-dataset schema catalog and compatibility metadata.
- `rag/spreadsheets/planner.py`: deterministic intent cues, fixed plan models, validation, and clarification outcomes.
- `rag/spreadsheets/coordinator.py`: orchestration and structured/hybrid/clarification result contract.
- `rag/user_db.py`: current spreadsheet dataset lookup and structured-query audit persistence.
- `rag/pipeline.py`: route eligible chat questions through the coordinator and preserve computed facts.
- `rag/api.py`: stream structured metadata and citations without changing the public request endpoint.
- `frontend/src/stores/chat.ts`: consume structured SSE events.
- `frontend/src/components/MessageBubble.vue`: render computed-result provenance and clarification.
- `tests/test_structured_query.py`: executor behavior.
- `tests/test_spreadsheet_catalog.py`: ACL, current-version, and catalog behavior.
- `tests/test_spreadsheet_planner.py`: intent, plan validation, ambiguity, and compatibility.
- `tests/test_spreadsheet_coordinator.py`: end-to-end backend orchestration and no-guess fallback.
- `tests/test_pipeline_spreadsheet_query.py`: pipeline and streaming integration.
- `frontend/src/stores/__tests__/chat.test.ts`: SSE state handling.
- `frontend/src/components/__tests__/MessageBubble.test.ts`: structured result and clarification rendering.

### Task 1: Extend the typed executor for lookup, ordering, Top N, and multi-dataset aggregation

**Files:**
- Modify: `rag/spreadsheets/query.py`
- Test: `tests/test_structured_query.py`

**Interfaces:**
- Consumes: existing `QueryScope`, `FilterClause`, `Metric`, and dataset SQLite files.
- Produces: `DatasetSheetRef`, `SortClause`, extended `StructuredQueryPlan`, `SourceRange`, and `execute_multi_dataset_query(plan, scope)`.

- [ ] **Step 1: Write failing executor tests**

Add literal fixtures for two compatible dataset files and tests equivalent to:

```python
def test_top_n_orders_typed_values_and_returns_exact_ranges(two_sales_datasets):
    plan = StructuredQueryPlan(
        sources=(DatasetSheetRef(dataset_id=1, sheet_key="sheet_0001"),), operation="lookup",
        select_columns=("c_0001", "c_0002"),
        order_by=(SortClause(column_key="c_0002", direction="desc"),), limit=3,
    )
    result = execute_multi_dataset_query(plan, two_sales_datasets.scope)
    assert [row.values for row in result.rows] == [
        {"c_0001": "A", "c_0002": "30"},
        {"c_0001": "C", "c_0002": "20"},
        {"c_0001": "B", "c_0002": "10"},
    ]
    assert result.citations[0].cell_range == "sheet_0001!A2:B4"

def test_sum_combines_compatible_dataset_partial_results(two_sales_datasets):
    plan = StructuredQueryPlan(
        sources=(
            DatasetSheetRef(dataset_id=1, sheet_key="sheet_0001"),
            DatasetSheetRef(dataset_id=2, sheet_key="sheet_0001"),
        ), operation="aggregate",
        metrics=(Metric(column_key="c_0002", function="sum"),),
    )
    result = execute_multi_dataset_query(plan, two_sales_datasets.scope)
    assert result.values[0].canonical == "100"
    assert {item.dataset_id for item in result.citations} == {1, 2}
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/test_structured_query.py -q`

Expected: FAIL because `SortClause`, multi-dataset plans, result rows, and `execute_multi_dataset_query` do not exist.

- [ ] **Step 3: Implement the minimal typed interfaces and executor**

Use these public shapes:

```python
class SortClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    column_key: str
    direction: Literal["asc", "desc"] = "asc"

class DatasetSheetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_id: int
    sheet_key: str

@dataclass(frozen=True)
class SourceRange:
    dataset_id: int
    document_id: int
    version_id: int
    document_name: str
    sheet_key: str
    sheet_name: str
    cell_range: str

class StructuredQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sources: tuple[DatasetSheetRef, ...]
    operation: Literal["lookup", "aggregate"]
    select_columns: tuple[str, ...] = ()
    metrics: tuple[Metric, ...] = ()
    filters: tuple[FilterClause, ...] = ()
    order_by: tuple[SortClause, ...] = ()
    limit: int = Field(default=100, ge=1, le=100)

def execute_multi_dataset_query(
    plan: StructuredQueryPlan, scope: QueryScope
) -> StructuredQueryResult:
    ...
```

Keep identifier interpolation allowlisted and values parameterized. Execute per dataset in read-only mode, then merge rows or decimal-safe aggregate partials. Do not silently deduplicate identical business rows.
Resolve every column key within its paired `DatasetSheetRef.sheet_key`; a repeated key such as `c_0001` on another worksheet must never be treated as the same column without an explicit compatible source mapping.

- [ ] **Step 4: Run executor tests and the spreadsheet suite**

Run: `pytest tests/test_structured_query.py tests/test_spreadsheet_extraction.py tests/test_spreadsheet_chunks.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rag/spreadsheets/query.py tests/test_structured_query.py
git commit -m "feat: extend typed spreadsheet query execution"
```

### Task 2: Build an authorized current-version schema catalog

**Files:**
- Create: `rag/spreadsheets/catalog.py`
- Modify: `rag/user_db.py`
- Test: `tests/test_spreadsheet_catalog.py`

**Interfaces:**
- Consumes: `UserDB`, `require_doc_view`, `structured_datasets_root`, `kb_id`, and authenticated user dictionaries.
- Produces: `CatalogColumn`, `CatalogSheet`, `CatalogDataset`, and `build_authorized_catalog(...)`.

- [ ] **Step 1: Write failing catalog tests**

Cover ready/current inclusion, historical exclusion, failed dataset exclusion, document ACL filtering, another-KB exclusion, and admin access:

```python
def test_catalog_contains_only_authorized_current_ready_datasets(catalog_fixture):
    catalog = build_authorized_catalog(
        catalog_fixture.db, catalog_fixture.root, "kb_sales", catalog_fixture.user
    )
    assert [(d.document_id, d.version_id) for d in catalog.datasets] == [(7, 12)]
    assert catalog.datasets[0].sheets[0].columns[1].display_name == "销售额"
```

- [ ] **Step 2: Run the catalog test and verify RED**

Run: `pytest tests/test_spreadsheet_catalog.py -q`

Expected: FAIL because the catalog module and DB lookup do not exist.

- [ ] **Step 3: Add the DB lookup and catalog builder**

Add this DB boundary:

```python
def list_current_ready_spreadsheet_datasets(self, kb_id: str) -> list[dict[str, Any]]:
    """Return ready datasets joined to each document's current version in one KB."""
```

Define immutable catalog models and builder:

```python
@dataclass(frozen=True)
class CatalogColumn:
    column_key: str
    display_name: str
    normalized_name: str
    inferred_type: str
    unit_hint: str | None
    samples: tuple[str, ...]

def build_authorized_catalog(
    db: UserDB, datasets_root: Path, kb_id: str, user: dict
) -> SpreadsheetCatalog:
    ...
```

Call `require_doc_view` before opening each dataset. Read manifests and at most five non-null representative values per column. Reject storage paths outside the configured root.

- [ ] **Step 4: Run catalog and security tests**

Run: `pytest tests/test_spreadsheet_catalog.py tests/test_kb_security_boundaries.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rag/spreadsheets/catalog.py rag/user_db.py tests/test_spreadsheet_catalog.py
git commit -m "feat: catalog authorized spreadsheet schemas"
```

### Task 3: Add deterministic routing, schema compatibility, and a validated planning boundary

**Files:**
- Create: `rag/spreadsheets/planner.py`
- Test: `tests/test_spreadsheet_planner.py`

**Interfaces:**
- Consumes: `SpreadsheetCatalog` from Task 2.
- Produces: `QueryIntent`, `PlanningResult`, `Clarification`, `classify_query`, `validate_candidate_plan`, and `plan_spreadsheet_query`.

- [ ] **Step 1: Write failing planner tests**

Test numeric cues, qualitative routing, exact aliases, compatible union, ambiguous fields, type conflicts, unit conflicts, and unsupported joins:

```python
def test_numeric_question_routes_to_structured():
    assert classify_query("各品类销售额合计是多少") == QueryIntent.STRUCTURED

def test_incompatible_units_require_clarification(sales_catalog):
    result = plan_spreadsheet_query("汇总销售额", sales_catalog.with_units("元", "万元"))
    assert result.kind == "clarification_required"
    assert result.clarification.code == "incompatible_units"
    assert result.plan is None
```

- [ ] **Step 2: Run planner tests and verify RED**

Run: `pytest tests/test_spreadsheet_planner.py -q`

Expected: FAIL because the planner module does not exist.

- [ ] **Step 3: Implement fixed planner contracts**

```python
class QueryIntent(str, Enum):
    STRUCTURED = "structured"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"

class PlanningResult(BaseModel):
    kind: Literal["planned", "clarification_required", "unsupported"]
    intent: QueryIntent
    plan: StructuredQueryPlan | None = None
    clarification: Clarification | None = None

def validate_candidate_plan(candidate: dict, catalog: SpreadsheetCatalog) -> PlanningResult:
    ...
```

Use deterministic cues first. Put model interaction behind an injected `candidate_builder(question, compact_catalog) -> dict`; validate every returned dataset ID, sheet, column, type, unit, operator, and limit server-side. Never accept an SQL field.

- [ ] **Step 4: Run planner tests**

Run: `pytest tests/test_spreadsheet_planner.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rag/spreadsheets/planner.py tests/test_spreadsheet_planner.py
git commit -m "feat: validate natural language spreadsheet plans"
```

### Task 4: Add the spreadsheet query coordinator and audit records

**Files:**
- Create: `rag/spreadsheets/coordinator.py`
- Modify: `rag/user_db.py`
- Test: `tests/test_spreadsheet_coordinator.py`

**Interfaces:**
- Consumes: Tasks 1-3 plus `UserDB`.
- Produces: `SpreadsheetQueryOutcome` and `SpreadsheetQueryCoordinator.query(...)`.

- [ ] **Step 1: Write failing coordinator tests**

Cover structured success, hybrid facts, clarification, unsupported requests, empty catalogs, planner rejection, executor failure, all-source citations, and audit reason codes:

```python
def test_coordinator_never_falls_back_to_vector_for_ambiguous_numeric_question(service):
    outcome = service.query("销售额合计", kb_id="kb_sales", user=service.user)
    assert outcome.kind == "clarification_required"
    assert outcome.computed_facts == ()
    assert service.semantic_retriever.calls == []
```

- [ ] **Step 2: Run coordinator tests and verify RED**

Run: `pytest tests/test_spreadsheet_coordinator.py -q`

Expected: FAIL because the coordinator and audit method do not exist.

- [ ] **Step 3: Implement the coordinator and audit boundary**

```python
@dataclass(frozen=True)
class SpreadsheetQueryOutcome:
    kind: Literal["not_applicable", "structured_result", "hybrid_result", "clarification_required", "unsupported"]
    answer_context: str = ""
    computed_facts: tuple[ComputedFact, ...] = ()
    citations: tuple[SourceRange, ...] = ()
    rows_scanned: int = 0
    clarification: Clarification | None = None

@dataclass(frozen=True)
class ComputedFact:
    label: str
    value: str

class SpreadsheetQueryCoordinator:
    def query(self, question: str, *, kb_id: str, user: dict) -> SpreadsheetQueryOutcome:
        ...
```

Add `UserDB.record_structured_query_run(...)` using the existing `structured_query_runs` table. Store normalized plan JSON, dataset IDs, status, rows scanned, duration, and reason code; never store physical paths.

- [ ] **Step 4: Run coordinator and DB tests**

Run: `pytest tests/test_spreadsheet_coordinator.py tests/test_user_db.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rag/spreadsheets/coordinator.py rag/user_db.py tests/test_spreadsheet_coordinator.py
git commit -m "feat: coordinate and audit spreadsheet queries"
```

### Task 5: Integrate structured outcomes into the streaming chat pipeline

**Files:**
- Modify: `rag/pipeline.py`
- Modify: `rag/api.py`
- Test: `tests/test_pipeline_spreadsheet_query.py`
- Test: `tests/test_pipeline_stream.py`

**Interfaces:**
- Consumes: `SpreadsheetQueryCoordinator.query` from Task 4.
- Produces: prepared routes `structured`, `hybrid`, and `clarification`, plus SSE event `structured_result`.

- [ ] **Step 1: Write failing pipeline tests**

```python
def test_stream_emits_exact_structured_result_without_vector_retrieval(pipeline):
    events = list(pipeline.stream("各品类销售额合计", kb_id="kb_sales", user=pipeline.user))
    payload = next(item for item in events if item["type"] == "structured_result")
    assert payload["facts"] == [{"label": "销售额 sum", "value": "100"}]
    assert payload["sources"][0]["sheet_name"] == "销量"
    assert pipeline.retriever.search_calls == []
```

Also verify hybrid prompts include immutable computed facts, clarification emits no generated numeric answer, and ACL scope is passed unchanged.

- [ ] **Step 2: Run integration tests and verify RED**

Run: `pytest tests/test_pipeline_spreadsheet_query.py tests/test_pipeline_stream.py -q`

Expected: FAIL because structured routes and events are not wired.

- [ ] **Step 3: Wire the coordinator before semantic retrieval**

Initialize the coordinator with the existing DB and configured structured dataset root. In `_prepare_context`, call it only when `kb_id` is present. Return clarification immediately; bypass vector retrieval for structured success; for hybrid results, prepend a delimited computed-facts block and instruct the generator not to recalculate it.

Emit an SSE-compatible object:

```python
{
    "type": "structured_result",
    "facts": [{"label": fact.label, "value": fact.value} for fact in outcome.computed_facts],
    "sources": [citation.to_public_dict() for citation in outcome.citations],
    "rows_scanned": outcome.rows_scanned,
    "clarification": outcome.clarification.model_dump() if outcome.clarification else None,
}
```

- [ ] **Step 4: Run pipeline, stream, API-security, and permission tests**

Run: `pytest tests/test_pipeline_spreadsheet_query.py tests/test_pipeline_stream.py tests/test_api_security.py tests/test_agent_permission_filter.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rag/pipeline.py rag/api.py tests/test_pipeline_spreadsheet_query.py tests/test_pipeline_stream.py
git commit -m "feat: route spreadsheet questions through structured chat"
```

### Task 6: Render computed facts, citations, and clarification in chat

**Files:**
- Modify: `frontend/src/stores/chat.ts`
- Modify: `frontend/src/components/MessageBubble.vue`
- Test: `frontend/src/stores/__tests__/chat.test.ts`
- Test: `frontend/src/components/__tests__/MessageBubble.test.ts`

**Interfaces:**
- Consumes: `structured_result` SSE event from Task 5.
- Produces: `Message.structuredResult` and user-visible exact-result provenance.

- [ ] **Step 1: Write failing frontend tests**

```typescript
it('stores structured_result events without changing canonical values', async () => {
  // Stream value "100.00", one worksheet range, and rows_scanned=42.
  await store.sendMessage('销售额合计', 'kb', 'kb_sales')
  expect(store.messages.at(-1)?.structuredResult?.facts[0].value).toBe('100.00')
  expect(store.messages.at(-1)?.structuredResult?.rowsScanned).toBe(42)
})

it('renders clarification instead of a guessed result', () => {
  const wrapper = mount(MessageBubble, { props: { message: clarificationMessage } })
  expect(wrapper.get('[data-test="structured-clarification"]').text()).toContain('元')
  expect(wrapper.find('[data-test="structured-facts"]').exists()).toBe(false)
})
```

- [ ] **Step 2: Run frontend tests and verify RED**

Run: `npm test -- --run src/stores/__tests__/chat.test.ts src/components/__tests__/MessageBubble.test.ts`

Expected: FAIL because `structuredResult` is not stored or rendered.

- [ ] **Step 3: Add typed event handling and compact rendering**

Extend `Message` with:

```typescript
interface StructuredResult {
  kind: 'structured_result' | 'hybrid_result' | 'clarification_required'
  facts: Array<{ label: string; value: string }>
  sources: Array<{ document_name: string; version_id: number; sheet_name: string; cell_range: string }>
  rowsScanned: number
  clarification?: { code: string; question: string; options: string[] }
}
```

Handle the SSE event without numeric coercion. Render canonical facts, source chips with sheet/range, scanned-row count, and a clarification panel. Keep existing document sources intact.

- [ ] **Step 4: Run targeted and full frontend verification**

Run: `npm test -- --run src/stores/__tests__/chat.test.ts src/components/__tests__/MessageBubble.test.ts`

Expected: PASS.

Run: `npm test && npm run build`

Expected: all tests and production build pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/stores/chat.ts frontend/src/components/MessageBubble.vue frontend/src/stores/__tests__/chat.test.ts frontend/src/components/__tests__/MessageBubble.test.ts
git commit -m "feat: show exact spreadsheet results in chat"
```

### Task 7: Add cache invalidation, benchmarks, and end-to-end acceptance coverage

**Files:**
- Modify: `rag/spreadsheets/coordinator.py`
- Modify: `rag/materials/worker.py`
- Modify: `rag/user_db.py`
- Create: `tests/test_spreadsheet_query_cache.py`
- Create: `tests/test_spreadsheet_query_e2e.py`
- Create: `tests/benchmarks/test_spreadsheet_query_performance.py`

**Interfaces:**
- Consumes: coordinator outcome and dataset activation flow.
- Produces: version/ACL-sensitive cache keys, invalidation hooks, and acceptance evidence.

- [ ] **Step 1: Write failing cache and acceptance tests**

Cover cache hits for identical authorized scope, invalidation after dataset activation, different authorization scopes producing different keys, multi-file exact totals, incompatible-unit clarification, current-version replacement, and no cross-KB leakage.

```python
def test_new_current_version_invalidates_cached_total(query_app):
    assert query_app.ask("销售额合计") == "100"
    query_app.activate_replacement(total="125")
    assert query_app.ask("销售额合计") == "125"

def test_department_scale_aggregate_execution(benchmark, department_dataset):
    result = benchmark(department_dataset.sum_sales)
    assert result.canonical == department_dataset.expected_total
```

- [ ] **Step 2: Run new tests and verify RED**

Run: `pytest tests/test_spreadsheet_query_cache.py tests/test_spreadsheet_query_e2e.py tests/benchmarks/test_spreadsheet_query_performance.py -q`

Expected: FAIL because cache keys and invalidation hooks are absent.

- [ ] **Step 3: Implement bounded caches and invalidation**

Key catalog and result caches by `(kb_id, authorization_fingerprint, current_dataset_versions, normalized_plan)`. Use bounded in-process caches with explicit invalidation after dataset activation and permission mutation. Correctness must not depend on cache availability.

- [ ] **Step 4: Run full backend and frontend suites**

Run: `pytest -q`

Expected: all backend tests pass.

Run: `npm test && npm run build` from `frontend/`.

Expected: all frontend tests and build pass.

- [ ] **Step 5: Perform local acceptance verification**

Upload two compatible fixture spreadsheets into one organizational KB, wait for both datasets to become ready, ask one aggregate, one Top N, one incompatible-unit question, and repeat as a user without access. Verify exact totals, all citations, clarification, and no unauthorized source names. Record measured structured execution duration from audit data; if it exceeds one second, profile catalog selection and SQLite execution separately without weakening validation.

- [ ] **Step 6: Commit**

```bash
git add rag/spreadsheets/coordinator.py rag/materials/worker.py rag/user_db.py tests/test_spreadsheet_query_cache.py tests/test_spreadsheet_query_e2e.py tests/benchmarks/test_spreadsheet_query_performance.py
git commit -m "test: verify departmental spreadsheet query workflow"
```

### Task 8: Final security and regression gate

**Files:**
- Modify only files required by failures discovered in this gate.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: verified release candidate with no unresolved numeric fallback or permission regression.

- [ ] **Step 1: Run focused security and correctness suites**

Run:

```bash
pytest tests/test_structured_query.py tests/test_spreadsheet_catalog.py tests/test_spreadsheet_planner.py tests/test_spreadsheet_coordinator.py tests/test_pipeline_spreadsheet_query.py tests/test_kb_security_boundaries.py tests/test_agent_permission_filter.py -q
```

Expected: PASS.

- [ ] **Step 2: Run all project verification**

Run: `pytest -q`

Run from `frontend/`: `npm test && npm run build`

Expected: every command exits 0 with no test failures or TypeScript/build errors.

- [ ] **Step 3: Inspect the final diff and audit the design checklist**

Run: `git diff --check` and `git status --short`.

Confirm the final implementation covers every success criterion in the design, contains no physical-path disclosure, never allows raw SQL, and leaves unrelated pre-existing worktree changes untouched.

- [ ] **Step 4: Commit gate-only fixes if needed**

If the gate requires a fix, first add a failing regression test, verify it fails, apply the minimal fix, and rerun the affected suite. Stage only those named regression-test and implementation files, then commit them with `git commit -m "fix: close spreadsheet query verification gaps"`. If the gate discovers no defect, do not create an empty commit.
