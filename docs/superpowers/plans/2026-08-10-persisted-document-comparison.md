# Persisted Document Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable permission-first document comparisons with independently persisted, verifiable citations.

**Architecture:** The workflow produces citation locations rather than database IDs. A transaction persists a comparison aggregate, result cells, and comparison-owned citations. A narrow adapter retrieves one already-authorized document's current version at a time.

**Tech Stack:** Python, FastAPI, Pydantic v2, SQLite, existing `Retriever`, LlamaIndex Workflow, pytest, Vue 3/TypeScript.

## Global Constraints

- Authorize all documents before retrieval/tool input; inaccessible document data never appears in responses or logs.
- The authorization scope freezes every selected document's `current_version_id`. Retrieval accepts only that version and returns insufficient evidence if it is no longer current; it never falls back to an unversioned, historical, or replacement-version query.
- A `supported` cell has location-based citations. `insufficient_evidence` has no text or citations and is never completed with another document's evidence.
- Do not reuse or modify `answer_citations` or chat messages.
- Use temporary SQLite/files/vector collections in tests; never write production `data/` or `qdrant_data/`.
- Each task uses TDD: focused failing test, confirmed RED, minimal code, focused GREEN, commit.

---

## File structure

- `rag/reliability/comparison.py`: execution-draft and persisted-response contracts.
- `rag/reliability/document_tool.py`: current-version-only retrieval adapter.
- `rag/reliability/service.py`: execution-to-persistence transaction facade.
- `rag/reliability/api.py`: protected compare and read routes.
- `rag/user_db.py`: comparison aggregate schema and atomic read/write helpers.
- `rag/api.py`: cache-aware per-KB tool wiring.
- `frontend/src/api/reliability.ts`: response `comparison_id` typing.
- `tests/test_document_comparison.py`: workflow provenance behavior.
- `tests/test_comparison_persistence.py`: SQLite persistence/ownership.
- `tests/test_comparison_document_tool.py`: retrieval isolation.
- `tests/test_reliability_api.py`: wired endpoint and ACL regression.

### Task 1: Split citation drafts from response IDs

**Files:** Modify `rag/reliability/comparison.py`, `rag/reliability/llama_workflow.py`, and `tests/test_document_comparison.py`.

**Interfaces:** `ComparisonExecutionResult` carries `ComparisonCellDraft`; each supported cell carries `tuple[ComparisonCitationDraft, ...]`. `DocumentEvidence.citations` uses the same draft type. `ComparisonResult` is the persisted API representation with `comparison_id` and integer `citation_ids`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_supported_cell_carries_location_citation_draft():
    evidence = DocumentEvidence(document_id=1, text="policy", citations=(
        ComparisonCitationDraft(1, 11, 2, "Leave", "five days", 0),
    ))
    result = await compare_documents(request_for(1), scope_for(1), FakeTool({1: evidence}))
    assert result.rows[0].values[1].citations[0].document_version_id == 11

@pytest.mark.asyncio
async def test_empty_citations_make_cell_insufficient_evidence():
    result = await compare_documents(request_for(1), scope_for(1), FakeTool({
        1: DocumentEvidence(document_id=1, text="policy"),
    }))
    assert result.rows[0].values[1].status == "insufficient_evidence"
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_document_comparison.py -q`

Expected: FAIL because `ComparisonCitationDraft` and `DocumentEvidence.citations` do not exist.

- [ ] **Step 3: Implement the minimum contract**

```python
class ComparisonCitationDraft(BaseModel):
    document_id: int
    document_version_id: int
    chunk_index: int
    section_title: str
    text_preview: str
    rank: int

class ComparisonCellDraft(BaseModel):
    status: ComparisonCellStatus
    text: str = ""
    citations: tuple[ComparisonCitationDraft, ...] = ()
```

Change the workflow and `validate_comparison` to consume these drafts. A missing or malformed draft produces `insufficient_evidence`, never a fabricated integer citation.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_document_comparison.py -q`

Expected: PASS; every supported cell has complete source-location drafts.

- [ ] **Step 5: Commit**

```bash
git add rag/reliability/comparison.py rag/reliability/llama_workflow.py tests/test_document_comparison.py
git commit -m "refactor: separate comparison citation drafts"
```

### Task 2: Persist comparison aggregates atomically

**Files:** Modify `rag/user_db.py`, `rag/reliability/comparison.py`; create `tests/test_comparison_persistence.py`.

**Interfaces:** `UserDB.persist_document_comparison(owner_id, request, execution) -> ComparisonResult`; `UserDB.get_document_comparison(comparison_id, actor_id, is_admin) -> ComparisonResult | None`; read output retains referenced document IDs internally for API ACL rechecks.

- [ ] **Step 1: Write the failing persistence tests**

```python
def test_persisted_comparison_uses_independent_citation_rows(tmp_path):
    db, owner = configured_db(tmp_path)
    stored = db.persist_document_comparison(owner["id"], request, supported_execution())
    assert stored.comparison_id > 0
    assert stored.rows[0].values[1].citation_ids
    assert db.get_answer_citations(stored.comparison_id) == []

def test_only_owner_or_admin_reads_comparison(tmp_path):
    db, owner, other, admin = configured_users(tmp_path)
    stored = db.persist_document_comparison(owner["id"], request, supported_execution())
    assert db.get_document_comparison(stored.comparison_id, other["id"], False) is None
    assert db.get_document_comparison(stored.comparison_id, admin["id"], True) is not None
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_comparison_persistence.py -q`

Expected: FAIL because the comparison schema and `UserDB` methods do not exist.

- [ ] **Step 3: Implement schema and transaction**

```sql
CREATE TABLE document_comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id), question TEXT NOT NULL,
  dimensions_json TEXT NOT NULL, document_snapshot_json TEXT NOT NULL,
  partial INTEGER NOT NULL, failures_json TEXT NOT NULL,
  created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE comparison_cells (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  comparison_id INTEGER NOT NULL REFERENCES document_comparisons(id) ON DELETE CASCADE,
  dimension TEXT NOT NULL, document_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('supported', 'insufficient_evidence')),
  text TEXT NOT NULL, UNIQUE(comparison_id, dimension, document_id)
);
CREATE TABLE comparison_citations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cell_id INTEGER NOT NULL REFERENCES comparison_cells(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL, document_version_id INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL, section_title TEXT NOT NULL,
  text_preview TEXT NOT NULL, rank INTEGER NOT NULL
);
```

Within `transaction(immediate=True)`, insert parent, cells, and location drafts. Return only inserted `comparison_citations.id` values to the response. Validate the supported/insufficient-evidence invariant before any insert. Owner filtering happens in the read query unless `is_admin` is true.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_comparison_persistence.py -q`

Expected: PASS; citations never enter `answer_citations`, and non-owners cannot load the aggregate.

- [ ] **Step 5: Commit**

```bash
git add rag/user_db.py rag/reliability/comparison.py tests/test_comparison_persistence.py
git commit -m "feat: persist document comparison citations"
```

### Task 3: Create the narrow current-version adapter

**Files:** Create `rag/reliability/document_tool.py`, `tests/test_comparison_document_tool.py`.

**Interfaces:** `ComparisonScope.allowed_document_versions: Mapping[int, int]` freezes authorization-time versions. `CurrentVersionDocumentEvidenceTool(db, retrievers).query_document(document_id, question, scope) -> DocumentEvidence`; `retrievers` is keyed by `kb_id` and contains existing `Retriever` instances.

- [ ] **Step 1: Write the failing isolation tests**

```python
@pytest.mark.asyncio
async def test_tool_requests_only_current_selected_version(db):
    retriever = MagicMock()
    retriever.retrieve.return_value = [chunk(1, 11, "policy")]
    tool = CurrentVersionDocumentEvidenceTool(db, {"rag_docs": retriever})
    evidence = await tool.query_document(1, "leave", scope_for(1))
    assert evidence.citations[0].document_version_id == 11
    assert retriever.retrieve.call_args.kwargs["allowed_version_ids"] == {11}

@pytest.mark.asyncio
async def test_tool_rejects_out_of_scope_id_without_retrieval(db):
    retriever = MagicMock()
    with pytest.raises(PermissionError):
        await CurrentVersionDocumentEvidenceTool(db, {"rag_docs": retriever}).query_document(2, "leave", scope_for(1))
    retriever.retrieve.assert_not_called()

@pytest.mark.asyncio
async def test_tool_returns_no_evidence_when_current_version_changes(db):
    tool = CurrentVersionDocumentEvidenceTool(db, {"rag_docs": MagicMock()})
    scope = scope_for(1, version_id=11)
    replace_current_version(db, document_id=1, version_id=12)
    assert await tool.query_document(1, "leave", scope) == DocumentEvidence(document_id=1)
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_comparison_document_tool.py -q`

Expected: FAIL because `CurrentVersionDocumentEvidenceTool` does not exist.

- [ ] **Step 3: Implement only constrained retrieval**

```python
if document_id not in scope.allowed_document_ids:
    raise PermissionError("document comparison scope denied")
document = self._db.get_document_permission_by_id(document_id)
version_id = scope.allowed_document_versions[document_id]
if document is None or document["current_version_id"] != version_id:
    return DocumentEvidence(document_id=document_id)
chunks = self._retrievers[document["kb_id"]].retrieve(
    question, top_k=1, doc_name=document["doc_name"], allowed_version_ids={version_id},
)
```

Return empty evidence if the version is absent or returned chunks do not match the exact document/version. Map only exact chunks to citation drafts. Do not use a generator, agent, reranker, LLM, or unscoped retriever.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_comparison_document_tool.py -q`

Expected: PASS; retrieval is document- and version-bound.

- [ ] **Step 5: Commit**

```bash
git add rag/reliability/document_tool.py tests/test_comparison_document_tool.py
git commit -m "feat: add current-version comparison tool"
```

### Task 4: Wire persistence into protected API routes

**Files:** Create `rag/reliability/service.py`; modify `rag/reliability/api.py`, `rag/api.py`, `frontend/src/api/reliability.ts`, `tests/test_reliability_api.py`.

**Interfaces:** `execute_persisted_comparison(request, scope, tool, db, actor_id) -> ComparisonResult`; `POST /reliability/compare -> ComparisonResult`; `GET /reliability/comparisons/{comparison_id} -> ComparisonResult`.

- [ ] **Step 1: Write failing API tests**

```python
def test_compare_is_wired_and_returns_persisted_id(isolated_api, make_user, monkeypatch):
    owner, headers = make_user("owner")
    document_id = add_current_document(isolated_api.app.state.user_db, owner["id"])
    wire_fake_comparison_retriever(monkeypatch, isolated_api.app)
    response = isolated_api.post("/reliability/compare", headers=headers, json={
        "question": "leave", "document_ids": [document_id], "dimensions": []})
    assert response.status_code == 200
    assert response.json()["comparison_id"] > 0

def test_comparison_read_rechecks_acl_without_source_leak(isolated_api, make_user):
    owner, headers = make_user("owner")
    comparison_id = persisted_owner_comparison(isolated_api.app.state.user_db, owner)
    revoke_document_view(isolated_api.app.state.user_db, owner)
    response = isolated_api.get(f"/reliability/comparisons/{comparison_id}", headers=headers)
    assert response.status_code in {403, 404}
    assert "policy.pdf" not in response.text
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_reliability_api.py -q`

Expected: FAIL because successful output has no `comparison_id`, no default tool is wired, and no protected read route exists.

- [ ] **Step 3: Implement service and route boundary**

```python
async def execute_persisted_comparison(request, scope, tool, db, actor_id):
    execution = await compare_documents(request, scope, tool)
    return await asyncio.to_thread(db.persist_document_comparison, actor_id, request, execution)

@router.get("/comparisons/{comparison_id}")
async def get_comparison(comparison_id: int, request: Request, user: dict = Depends(require_user)):
    result = get_user_db(request).get_document_comparison(comparison_id, user["id"], bool(user.get("is_admin")))
    if result is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    recheck_comparison_documents(get_user_db(request), user, result.document_ids)
    return result
```

Build the adapter from the existing per-KB pipeline cache only after `build_comparison_scope`. Normalize backend failures to generic 503; ACL failures remain generic 403/404. Add `comparison_id: number` in the TypeScript API response without adding source-display UI.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_reliability_api.py tests/test_document_comparison.py tests/test_comparison_persistence.py tests/test_comparison_document_tool.py -q`

Expected: PASS; valid comparisons no longer return 503 merely because a tool was unwired, and later ACL loss discloses no source data.

- [ ] **Step 5: Commit**

```bash
git add rag/reliability/service.py rag/reliability/api.py rag/api.py frontend/src/api/reliability.ts tests/test_reliability_api.py
git commit -m "feat: wire persisted document comparisons"
```

### Task 5: Complete verification and record evidence

**Files:** Modify `docs/development-log-security-boundary-hardening.md`.

**Interfaces:** Consumes all completed comparison changes and produces an auditable record of actual command results.

- [ ] **Step 1: Write the failing documentation check**

Run: `rg -n "Persisted document comparison verification" docs/development-log-security-boundary-hardening.md`

Expected: FAIL because the verification section is absent.

- [ ] **Step 2: Add the exact verification record**

After Step 3 has completed, append a `Persisted document comparison verification`
heading. Under it, record each exact Step 3 command verbatim followed by its
observed exit status and test/build count. Do not record a full-suite pass
unless the full-suite command completed in this task.

- [ ] **Step 3: Run complete verification**

```bash
python -m pytest -q -p no:cacheprovider
python -m pip check
npm.cmd run test -- --run
npm.cmd run build
ruff check rag/reliability rag/api.py rag/user_db.py tests/test_document_comparison.py tests/test_comparison_persistence.py tests/test_comparison_document_tool.py tests/test_reliability_api.py
git diff --check
```

Expected: all complete successfully. Any independently confirmed pre-existing baseline failure is documented with its exact scope and is not presented as a new passing result.

- [ ] **Step 4: Commit verification evidence**

```bash
git add docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify persisted document comparisons"
```
