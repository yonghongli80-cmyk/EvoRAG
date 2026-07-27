# Answer Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-aware refusal, explainable confidence, conflict detection, and permission-safe cross-document comparison to RAGv3.

**Architecture:** Build a reliability package around the existing retriever and citation pipeline. The package emits a typed evidence bundle before generation, applies deterministic answerability/confidence rules, persists candidate conflicts, and uses a narrow LlamaIndex Workflow adapter only for comparison orchestration.

**Tech Stack:** Python 3.12, FastAPI, SQLite WAL, Qdrant, llama-index-core 0.14.x, pytest, Vue 3, TypeScript, Vitest

## Global Constraints

- Execute after `docs/superpowers/plans/2026-07-27-trusted-knowledge-loop.md`.
- Use `superpowers:executing-plans` in the user's workbench; do not dispatch subagents unless the user changes that preference.
- Add `llama-index-core>=0.14.23,<0.15`; do not add the deprecated QueryPipeline API.
- Confidence is exactly `high | medium | low | unavailable`; never emit a percentage.
- No visible current evidence means refusal, not a low-confidence generated answer.
- ACL filtering happens before evidence counts, conflict detection, confidence, and comparison.
- Historical evidence never raises confidence and always carries a warning.
- LlamaIndex receives tool contracts and ACL-filtered results, never file paths, SQLite connections, or Qdrant clients.
- Spreadsheet calculation is implemented by `2026-07-27-spreadsheet-knowledge.md`, not duplicated here.
- Every backend task follows red-green-refactor and uses temporary databases/collections.
- Do not re-enable final-answer caching.

---

## File Structure

### New backend

- `rag/reliability/__init__.py` — public reliability interfaces.
- `rag/reliability/models.py` — evidence, decision, confidence, and comparison types.
- `rag/reliability/answerability.py` — refusal and clarification gate.
- `rag/reliability/confidence.py` — deterministic confidence evaluator.
- `rag/reliability/conflicts.py` — candidate normalization, persistence, and resolution.
- `rag/reliability/comparison.py` — comparison service interface.
- `rag/reliability/llama_workflow.py` — LlamaIndex Workflow adapter.
- `rag/reliability/api.py` — reliability and comparison router.
- `tests/test_answerability.py`
- `tests/test_confidence.py`
- `tests/test_conflicts.py`
- `tests/test_document_comparison.py`
- `tests/test_reliability_api.py`

### Modified backend

- `pyproject.toml`
- `config.py`
- `rag/models.py`
- `rag/retriever.py`
- `rag/pipeline.py`
- `rag/user_db.py`
- `rag/api.py`
- `tests/conftest.py`

### New frontend

- `frontend/src/api/reliability.ts`
- `frontend/src/components/ReliabilityBadge.vue`
- `frontend/src/components/ConflictCard.vue`
- `frontend/src/components/DocumentCompareDialog.vue`
- Component tests beside existing frontend tests.

### Modified frontend

- `frontend/src/stores/chat.ts`
- `frontend/src/components/MessageBubble.vue`
- `frontend/src/views/FileModeView.vue`

---

### Task 1: Evidence Types and Dependency Boundary

**Files:**
- Create: `rag/reliability/__init__.py`
- Create: `rag/reliability/models.py`
- Test: `tests/test_reliability_models.py`
- Modify: `pyproject.toml`
- Modify: `config.py`

**Interfaces:**
- Produces: `EvidenceRef`, `RetrievalMetrics`, `EvidenceBundle`, `AnswerabilityDecision`, `ConfidenceAssessment`, `ComparisonScope`.
- Produces: `settings.reliability_min_score: float = 0.35`.
- Produces: `settings.comparison_max_documents: int = 5`.

- [ ] **Step 1: Write the failing model test**

```python
def test_evidence_bundle_rejects_acl_unchecked_evidence():
    with pytest.raises(ValueError, match="acl_checked"):
        EvidenceBundle(
            question="年假是多少",
            current_evidence=[EvidenceRef.fake(acl_checked=False)],
            historical_evidence=[],
            structured_results=[],
            conflicts=[],
            retrieval_metrics=RetrievalMetrics(visible_candidates=1, top_score=0.8),
        )
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_reliability_models.py -v
```

Expected: collection fails because `rag.reliability.models` does not exist.

- [ ] **Step 3: Implement immutable Pydantic models**

```python
class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    document_id: int
    document_version_id: int
    citation_id: int | None = None
    score: float
    is_current: bool
    validity_status: Literal["valid", "reported_outdated", "confirmed_outdated"]
    acl_checked: bool
    authority_level: Literal["authoritative", "supporting", "unknown"] = "unknown"


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str
    current_evidence: tuple[EvidenceRef, ...]
    historical_evidence: tuple[EvidenceRef, ...]
    structured_results: tuple[dict, ...]
    conflicts: tuple[dict, ...]
    retrieval_metrics: RetrievalMetrics

    @model_validator(mode="after")
    def require_acl_checks(self):
        if any(not item.acl_checked for item in self.current_evidence + self.historical_evidence):
            raise ValueError("acl_checked evidence required")
        return self
```

Add the exact dependency range and settings from Global Constraints.

- [ ] **Step 4: Run model tests and dependency validation**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_reliability_models.py -v
& .\.venv\Scripts\python.exe -m pip check
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml config.py rag/reliability tests/test_reliability_models.py
git commit -m "feat: add reliability evidence types"
```

---

### Task 2: Answerability Gate

**Files:**
- Create: `rag/reliability/answerability.py`
- Test: `tests/test_answerability.py`
- Modify: `rag/pipeline.py`

**Interfaces:**
- Consumes: `EvidenceBundle`.
- Produces: `decide_answerability(bundle: EvidenceBundle) -> AnswerabilityDecision`.
- Produces decision kinds: `answerable | answerable_with_warning | clarification_required | insufficient_evidence | permission_limited`.

- [ ] **Step 1: Write failing refusal tests**

```python
@pytest.mark.parametrize(
    ("bundle", "kind"),
    [
        (bundle(current=[]), "insufficient_evidence"),
        (bundle(current=[evidence(score=0.20)]), "insufficient_evidence"),
        (bundle(current=[], historical=[evidence(is_current=False)]), "answerable_with_warning"),
        (bundle(current=[evidence(score=0.8)], conflicts=[conflict()]), "clarification_required"),
    ],
)
def test_answerability_decisions(bundle, kind):
    assert decide_answerability(bundle).kind == kind
```

- [ ] **Step 2: Run and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_answerability.py -v
```

Expected: FAIL because the gate is missing.

- [ ] **Step 3: Implement ordered deterministic rules**

```python
def decide_answerability(bundle: EvidenceBundle) -> AnswerabilityDecision:
    if bundle.retrieval_metrics.permission_limited and not bundle.current_evidence:
        return AnswerabilityDecision(kind="permission_limited", reasons=("visible evidence is insufficient",))
    if bundle.conflicts:
        return AnswerabilityDecision(kind="clarification_required", reasons=("unresolved evidence conflict",))
    qualified = [item for item in bundle.current_evidence if item.score >= settings.reliability_min_score]
    if qualified:
        return AnswerabilityDecision(kind="answerable", reasons=("current evidence available",))
    if bundle.historical_evidence:
        return AnswerabilityDecision(kind="answerable_with_warning", reasons=("historical evidence only",))
    return AnswerabilityDecision(kind="insufficient_evidence", reasons=("no qualifying evidence",))
```

Integrate the decision before prompt generation. Refusal responses use fixed backend copy and persist the decision; they do not call the generator.

- [ ] **Step 4: Run gate and pipeline tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_answerability.py tests\test_pipeline.py tests\test_pipeline_stream.py -v
```

Expected: PASS; refusal tests assert the generator mock has zero calls.

- [ ] **Step 5: Commit**

```powershell
git add rag/reliability/answerability.py rag/pipeline.py tests/test_answerability.py tests/test_pipeline.py
git commit -m "feat: refuse answers without evidence"
```

---

### Task 3: Explainable Confidence

**Files:**
- Create: `rag/reliability/confidence.py`
- Test: `tests/test_confidence.py`
- Modify: `rag/pipeline.py`
- Modify: `rag/user_db.py`

**Interfaces:**
- Produces: `assess_confidence(bundle: EvidenceBundle, decision: AnswerabilityDecision) -> ConfidenceAssessment`.
- Persists `confidence_level` and `confidence_reasons_json` on assistant messages.

- [ ] **Step 1: Write failing rule tests**

```python
def test_historical_evidence_cannot_produce_high_confidence():
    assessment = assess_confidence(
        bundle(current=[], historical=[evidence(score=0.99, is_current=False)]),
        AnswerabilityDecision(kind="answerable_with_warning", reasons=("historical evidence only",)),
    )
    assert assessment.level == "low"
    assert "historical" in assessment.reasons[0]


def test_refusal_has_unavailable_confidence():
    assert assess_confidence(bundle(current=[]), refusal()).level == "unavailable"
```

- [ ] **Step 2: Run and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_confidence.py -v
```

Expected: FAIL because evaluator and message columns are absent.

- [ ] **Step 3: Implement rules and idempotent message migration**

High requires qualifying current evidence, no conflict, and either two consistent sources or one direct authoritative source. Medium requires one qualifying current source. Low is warning-only. Unavailable is refusal.

Add columns using existing column probes:

```sql
ALTER TABLE chat_messages ADD COLUMN confidence_level TEXT NOT NULL DEFAULT 'unavailable';
ALTER TABLE chat_messages ADD COLUMN confidence_reasons_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_messages ADD COLUMN answerability_kind TEXT NOT NULL DEFAULT 'answerable';
```

- [ ] **Step 4: Run confidence and conversation tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_confidence.py tests\test_conversations.py tests\test_user_db.py -v
```

Expected: PASS and serialized responses contain no percentage field.

- [ ] **Step 5: Commit**

```powershell
git add rag/reliability/confidence.py rag/pipeline.py rag/user_db.py tests/test_confidence.py tests/test_conversations.py
git commit -m "feat: add explainable answer confidence"
```

---

### Task 4: Conflict Candidates and Owner Resolution

**Files:**
- Create: `rag/reliability/conflicts.py`
- Test: `tests/test_conflicts.py`
- Modify: `rag/user_db.py`

**Interfaces:**
- Produces: `ConflictClaim(subject, attribute, value, unit, scope, effective_at, citation_id)`.
- Produces: `detect_conflicts(claims: Sequence[ConflictClaim]) -> list[ConflictCandidate]`.
- Produces: `resolve_conflict(conflict_id: int, actor: dict, resolution: str, note: str) -> dict`.

- [ ] **Step 1: Write failing normalization test**

```python
def test_overlapping_current_claims_with_different_values_conflict():
    claims = [
        claim(value="5", unit="days", scope="all", citation_id=1),
        claim(value="7", unit="days", scope="all", citation_id=2),
    ]
    result = detect_conflicts(claims)
    assert len(result) == 1
    assert result[0].citation_ids == (1, 2)
```

- [ ] **Step 2: Run and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_conflicts.py -v
```

Expected: FAIL because conflict storage and detector are absent.

- [ ] **Step 3: Add conflict tables and deterministic matching**

Create `knowledge_conflicts` and `knowledge_conflict_events`. Use a unique active key from normalized subject, attribute, unit, scope, and sorted document version IDs. Only owner/admin resolution changes status; model extraction can only create candidates.

```python
def claims_conflict(left: ConflictClaim, right: ConflictClaim) -> bool:
    return (
        left.subject_key == right.subject_key
        and left.attribute_key == right.attribute_key
        and left.unit_key == right.unit_key
        and scopes_overlap(left.scope_key, right.scope_key)
        and left.normalized_value != right.normalized_value
    )
```

- [ ] **Step 4: Run conflict and ACL tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_conflicts.py tests\test_document_security_boundaries.py -v
```

Expected: PASS; the test fixture includes an invisible contradictory claim and asserts it is absent.

- [ ] **Step 5: Commit**

```powershell
git add rag/reliability/conflicts.py rag/user_db.py tests/test_conflicts.py
git commit -m "feat: detect evidence conflicts"
```

---

### Task 5: Permission-Safe LlamaIndex Comparison Workflow

**Files:**
- Create: `rag/reliability/comparison.py`
- Create: `rag/reliability/llama_workflow.py`
- Test: `tests/test_document_comparison.py`

**Interfaces:**
- Produces: `ComparisonRequest(question: str, document_ids: tuple[int, ...], dimensions: tuple[str, ...])`.
- Produces: `ComparisonResult(rows: tuple[ComparisonRow, ...], partial: bool, failures: tuple[str, ...])`.
- Consumes tool: `query_document(document_id: int, question: str, context: ComparisonScope) -> DocumentEvidence`.

- [ ] **Step 1: Write failing partial comparison test**

```python
async def test_comparison_never_fills_missing_side(fake_document_tool):
    fake_document_tool.responses = {1: evidence_text("A", citation_id=11), 2: ToolTimeout()}
    result = await compare_documents(request(document_ids=(1, 2)), comparison_scope(), fake_document_tool)
    assert result.partial is True
    assert result.rows[0].values[2].status == "insufficient_evidence"
    assert result.rows[0].values[2].text == ""
```

- [ ] **Step 2: Run and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_comparison.py -v
```

Expected: FAIL because workflow types are missing.

- [ ] **Step 3: Implement bounded workflow**

Use LlamaIndex `Workflow`, `StartEvent`, `StopEvent`, and `@step`. Reject more than 5 documents before invoking the workflow. Each document tool receives the immutable `ComparisonScope`; synthesis validates every non-empty cell has citations.

```python
def validate_comparison(result: ComparisonResult) -> ComparisonResult:
    for row in result.rows:
        for value in row.values.values():
            if value.status == "supported" and not value.citation_ids:
                raise ComparisonValidationError("supported comparison cell requires citation")
    return result
```

- [ ] **Step 4: Run workflow, timeout, and dependency tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_comparison.py -v
& .\.venv\Scripts\python.exe -m pip check
```

Expected: PASS; fake tools assert no path/DB/Qdrant object enters workflow events.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml rag/reliability/comparison.py rag/reliability/llama_workflow.py tests/test_document_comparison.py
git commit -m "feat: add controlled document comparison"
```

---

### Task 6: Reliability API and Frontend

**Files:**
- Create: `rag/reliability/api.py`
- Test: `tests/test_reliability_api.py`
- Create: `frontend/src/api/reliability.ts`
- Create: `frontend/src/components/ReliabilityBadge.vue`
- Create: `frontend/src/components/ConflictCard.vue`
- Create: `frontend/src/components/DocumentCompareDialog.vue`
- Create: corresponding `*.test.ts`
- Modify: `rag/api.py`
- Modify: `frontend/src/components/MessageBubble.vue`
- Modify: `frontend/src/views/FileModeView.vue`

**Interfaces:**
- Produces: `POST /reliability/compare`.
- Produces: `GET /answers/{message_id}/reliability`.
- Produces: `PUT /knowledge-operations/conflicts/{conflict_id}/resolve`.

- [ ] **Step 1: Write failing API and component tests**

```python
def test_compare_rejects_invisible_document(isolated_api, user_headers, invisible_document):
    response = isolated_api.post(
        "/reliability/compare",
        headers=user_headers,
        json={"question": "比较制度", "document_ids": [visible_id(), invisible_document], "dimensions": []},
    )
    assert response.status_code == 403
    assert "filename" not in response.text
```

```typescript
it('renders confidence reasons without percentages', () => {
  const wrapper = mount(ReliabilityBadge, { props: { level: 'medium', reasons: ['单一当前来源'] } })
  expect(wrapper.text()).toContain('中')
  expect(wrapper.text()).toContain('单一当前来源')
  expect(wrapper.text()).not.toContain('%')
})
```

- [ ] **Step 2: Run and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_reliability_api.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/components/ReliabilityBadge.test.ts src/components/DocumentCompareDialog.test.ts
Set-Location ..
```

Expected: backend 404 and frontend module resolution failures.

- [ ] **Step 3: Implement typed routes and components**

Mount one router in `rag/api.py`. The compare dialog loads only visible logical documents, caps selection at five, and renders partial cells as “资料不足”. `MessageBubble` consumes persisted reliability fields from chat responses.

```python
router = APIRouter(prefix="/reliability")


@router.post("/compare", response_model=ComparisonResponse)
async def compare(req: ComparisonRequest, user: dict = Depends(require_user)):
    scope = build_comparison_scope(user_db, user, req.document_ids)
    return await comparison_service.compare(req, scope)
```

- [ ] **Step 4: Run focused and frontend tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_reliability_api.py tests\test_answer_citations.py tests\test_document_security_boundaries.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/reliability/api.py rag/api.py tests/test_reliability_api.py frontend/src
git commit -m "feat: expose answer reliability"
```

---

### Task 7: Operations Linkage and Full Verification

**Files:**
- Modify: `rag/knowledge_tasks.py`
- Modify: `rag/reliability/conflicts.py`
- Modify: `rag/user_db.py`
- Modify: `docs/development-log-security-boundary-hardening.md`
- Test: `tests/test_reliability_operations.py`
- Test: `tests/test_knowledge_task_type_migration.py`

**Interfaces:**
- Produces aggregated task types `insufficient_evidence` and `evidence_conflict`.

- [ ] **Step 1: Write failing aggregation test**

```python
def test_repeated_refusals_create_one_owner_task(task_fixture):
    for user in ("alice", "bob", "carol"):
        task_fixture.record_refusal(user=user, question="年假怎么申请", document_id=7)
    tasks = task_fixture.db.list_tasks(task_type="insufficient_evidence")
    assert len(tasks) == 1
    assert tasks[0]["report_count"] == 3


def test_task_type_migration_preserves_rows_and_foreign_keys(legacy_task_db):
    legacy_task_db.seed_task(task_type="outdated")
    migrate_knowledge_task_types(legacy_task_db.connection)
    assert legacy_task_db.list_tasks()[0]["task_type"] == "outdated"
    assert legacy_task_db.foreign_key_check() == []
    legacy_task_db.create_task(task_type="insufficient_evidence")
    legacy_task_db.create_task(task_type="evidence_conflict")
```

- [ ] **Step 2: Run and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_reliability_operations.py tests\test_knowledge_task_type_migration.py -v
```

Expected: FAIL because reliability task types are unknown.

- [ ] **Step 3: Add deterministic aggregation keys**

First inspect `sqlite_master.sql`. If the installed table still has the two-value CHECK, snapshot the table's index and trigger definitions, set `PRAGMA foreign_keys=OFF` before `BEGIN IMMEDIATE`, create `knowledge_tasks_new` with all four values, copy every named column, compare row counts, drop old, rename new, recreate indexes/triggers, and run `PRAGMA foreign_key_check` before commit. Commit only when the check is empty, then restore `PRAGMA foreign_keys=ON` in `finally`. SQL errors, row-count mismatch, or foreign-key errors roll the transaction back and leave the old table intact; a process crash is covered by SQLite transaction recovery. Fresh databases already use the four-value CHECK from the trusted-loop plan.

```python
TASK_TYPES = ("outdated", "not_useful", "insufficient_evidence", "evidence_conflict")


def refusal_active_key(question: str, document_id: int | None) -> str:
    raw = f"insufficient:{normalize_question(question)}:{document_id or 0}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run complete verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/reliability tests/test_answerability.py tests/test_confidence.py tests/test_conflicts.py tests/test_document_comparison.py tests/test_reliability_api.py tests/test_knowledge_task_type_migration.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
git status --short data qdrant_data
```

Expected: all tests/build pass and no data residue.

- [ ] **Step 5: Document and commit**

```powershell
git add rag/knowledge_tasks.py rag/user_db.py rag/reliability tests/test_reliability_operations.py tests/test_knowledge_task_type_migration.py docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify answer reliability"
```
