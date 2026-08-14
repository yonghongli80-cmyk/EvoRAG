# V3 Spreadsheet Online Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cell-level `.xlsx` editing, immutable save-as-new-version, restore-as-new-version, complete incremental indexing, and administrator-visible change auditing to RAGv3.

**Architecture:** The browser sends typed cell patches against an explicit base version. A focused spreadsheet edit service validates and applies patches to a staged workbook, reserves an immutable version plus audit rows, and sends the version through the persistent spreadsheet/material indexing lifecycle; SQLite current pointers switch only after the structured dataset, FTS, and a complete new-version vector set are ready. Restore uses the same service with a historical workbook as input, while the admin UI reads permission-filtered audit projections.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, openpyxl, SQLite, Qdrant, pytest, Vue 3, TypeScript, Element Plus, Vitest

## Global Constraints

- Modify only `C:\Users\Administrator\Desktop\RAGv3\RAGv3`.
- Support `.xlsx` only; reject `.xls`, `.xlsm`, formulas, protected cells, row/column/sheet mutations, and merge mutations.
- Accept at most 500 unique A1 cell patches per save.
- A save or restore always creates an immutable version; never overwrite a historical file or move the current pointer backward.
- `base_version_id` is mandatory and must equal the current version both before staging and before activation.
- Existing `edit` permission authorizes edit/clear/restore; `view` and `download` alone do not.
- The current version remains authoritative on every failure; physical paths and sensitive values never appear in API errors or logs.
- Unchanged spreadsheet units do not call the embedder, but every activated version must own a complete set of vector points for all current units.
- Public spreadsheet edits and restores appear in the management audit page; sensitive before/after values are permission-filtered.
- Final-answer cache remains disabled.
- Preserve unrelated dirty-worktree changes and generated user data.
- Treat the current uncommitted material-recovery work as required baseline behavior, not disposable changes. In particular, preserve `UserDB.settle_material_job`, `UserDB.renew_material_job`, worker lease renewal, owner-only job settlement, staging-version downloads, and the `version_id` file projection.
- Implement in the current `fix/review-2026-08-07` checkout so spreadsheet indexing is integrated with that baseline. Do not reset, replace, or recreate overlapping files from committed `HEAD`.
- The per-task commit commands below are suspended while pre-existing changes remain uncommitted. Run every test checkpoint, but do not stage or commit overlapping files unless the user separately approves ownership of the combined diff.

## Existing Dirty-Worktree Integration Gate

Before Task 1, run:

`pytest tests/test_material_jobs.py tests/test_material_api.py tests/test_pipeline_lifecycle.py -q`

Expected: PASS. If this baseline fails, stop and repair or obtain direction before adding spreadsheet behavior. Before modifying an overlapping file, inspect its current `git diff` and patch around the existing changes. After every change to `rag/user_db.py` or `rag/materials/worker.py`, rerun the same recovery suite in addition to the task-focused tests.

The spreadsheet worker integration belongs inside the existing `process_index_job` lifecycle. `MaterialWorker.run` remains responsible for lease renewal and exactly-once settlement; spreadsheet-specific code must return a `JobResult` and must not settle, requeue, or release the lease independently.

## File Structure

- Create `rag/spreadsheets/edit_models.py`: typed patch, session, response, and restore models.
- Create `rag/spreadsheets/workbook_editor.py`: safe openpyxl patching and post-save invariants.
- Create `rag/spreadsheets/edit_service.py`: optimistic versioning, staging, audit reservation, restore orchestration.
- Create `rag/spreadsheets/audit.py`: audit query projections, risk flags, and value redaction.
- Modify `rag/db_migrations.py`: edit session/cell change tables and version provenance columns.
- Modify `rag/user_db.py`: lock-safe audit/version reservation and activation methods.
- Modify `rag/spreadsheets/api.py`: edit, restore, edit-status, and audit routes.
- Modify `rag/spreadsheets/extractor.py`: complete new-version vector materialization contract.
- Modify `rag/materials/worker.py`: route edited spreadsheets through structured dataset activation and complete vector writes.
- Modify `rag/materials/incremental.py`: expose reusable current-unit plan including removed units.
- Modify `frontend/src/api/spreadsheets.ts`: typed read/edit/restore/audit API.
- Modify `frontend/src/views/SpreadsheetViewer.vue`: editable cells, dirty state, save status, conflict handling, and restore controls.
- Create `frontend/src/views/SpreadsheetChangesView.vue`: administrator audit list/detail.
- Modify `frontend/src/router/index.ts`: audit route.
- Modify `frontend/src/views/MainLayout.vue`: management navigation entry.
- Create focused tests listed per task below.

---

### Task 1: Persist edit sessions, cell diffs, and restore provenance

**Files:**
- Modify: `rag/db_migrations.py`
- Modify: `rag/user_db.py`
- Create: `rag/spreadsheets/edit_models.py`
- Test: `tests/test_spreadsheet_edit_schema.py`
- Test: `tests/test_spreadsheet_edit_repository.py`

**Interfaces:**
- Produces: `CellValueType = Literal["text", "number", "date", "boolean", "blank"]`.
- Produces: `CellPatch(sheet_key: str, cell: str, value_type: CellValueType, value: object | None)`.
- Produces: `SpreadsheetEditSession(id, document_id, base_version_id, new_version_id, actor_user_id, operation, status, change_count, restored_from_version_id)`.
- Produces: `UserDB.reserve_spreadsheet_edit(...) -> tuple[int, int]`, returning `(session_id, version_id)` under one `BEGIN IMMEDIATE`.
- Produces: `UserDB.set_spreadsheet_edit_status(session_id, status, *, activated_at=None, error_code="")`.

- [ ] **Step 1: Write the failing migration test**

```python
def test_spreadsheet_edit_schema_is_idempotent(tmp_path):
    path = tmp_path / "users.db"
    db = UserDB(str(path)); db.close()
    db = UserDB(str(path))
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"spreadsheet_edit_sessions", "spreadsheet_cell_changes"} <= tables
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(document_versions)")}
    assert "restored_from_version_id" in columns
```

- [ ] **Step 2: Run it to verify failure**

Run: `pytest tests/test_spreadsheet_edit_schema.py -q`  
Expected: FAIL because the tables/column do not exist.

- [ ] **Step 3: Add constrained idempotent migrations**

Create `spreadsheet_edit_sessions` with nullable `new_version_id`, operation check (`edit/rollback`), status check (`pending/active/failed/cancelled/rejected`), display-name snapshot, reason, IP, user agent, risk JSON, timestamps, and error code. Create `spreadsheet_cell_changes` with session FK cascade, sheet/cell/value fields and a unique `(session_id, sheet_key, cell_address)` constraint. Add indexes on `(created_at,id)`, actor, document, status, and risk-bearing sessions. Add nullable self-FK `document_versions.restored_from_version_id`.

- [ ] **Step 4: Write repository tests before implementation**

```python
def test_reservation_checks_base_version_and_writes_session_atomically(db, current_xlsx):
    session_id, version_id = db.reserve_spreadsheet_edit(
        document_id=current_xlsx.document_id,
        base_version_id=current_xlsx.version_id,
        actor_user_id=current_xlsx.user_id,
        original_filename="prices.xlsx",
        staged_path="staging/pending.xlsx",
        content_hash="new-hash",
        operation="edit",
        reason="price update",
        restored_from_version_id=None,
        changes=({"sheet_key": "sheet_0001", "cell_address": "B12",
                  "old_value_type": "number", "old_value_json": "6800",
                  "new_value_type": "number", "new_value_json": "6500",
                  "sheet_name_snapshot": "Prices", "sensitive": 0},),
    )
    assert db.get_spreadsheet_edit_session(session_id)["new_version_id"] == version_id
```

Also assert a stale base raises `VersionConflict` with no session/version rows, duplicate cell rows roll back the whole reservation, and status transitions reject `active -> pending`.

- [ ] **Step 5: Implement lock-safe repository methods**

All multi-row writes use `with self._lock, self.transaction(immediate=True):`; `_reserve_*_in_tx` helpers never acquire locks. Store value JSON with deterministic `json.dumps(..., sort_keys=True, ensure_ascii=False)` and never interpolate SQL.

- [ ] **Step 6: Run schema and repository tests**

Run: `pytest tests/test_spreadsheet_edit_schema.py tests/test_spreadsheet_edit_repository.py tests/test_db_migrations.py tests/test_concurrency.py -q`  
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rag/db_migrations.py rag/user_db.py rag/spreadsheets/edit_models.py tests/test_spreadsheet_edit_schema.py tests/test_spreadsheet_edit_repository.py
git commit -m "feat: persist spreadsheet edit audit sessions"
```

### Task 2: Validate and apply typed cell patches without workbook drift

**Files:**
- Create: `rag/spreadsheets/workbook_editor.py`
- Test: `tests/test_spreadsheet_workbook_editor.py`

**Interfaces:**
- Consumes: `CellPatch` from Task 1.
- Produces: `WorkbookEditError(reason_code: str)`.
- Produces: `CellChange(sheet_key, sheet_name, cell_address, old_value_type, old_value, new_value_type, new_value)`.
- Produces: `apply_cell_patches(source: Path, destination: Path, sheet_map: Mapping[str, str], patches: tuple[CellPatch, ...]) -> tuple[CellChange, ...]`.

- [ ] **Step 1: Write failing happy-path tests**

Create an `.xlsx` fixture with styles, formulas, merged cells, dates, booleans, and workbook protection. Apply text/number/date/boolean/blank patches across two sheets, reopen the result, and assert exact values plus unchanged formulas, style IDs, sheet names/order, merged ranges, hidden state, freeze panes, and dimensions.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_spreadsheet_workbook_editor.py -q`  
Expected: FAIL because `workbook_editor` does not exist.

- [ ] **Step 3: Implement strict patch validation**

Use `openpyxl.load_workbook(source, data_only=False, read_only=False, keep_links=True)`. Validate `.xlsx`, 1–500 unique patches, `sheet_key` in the server map, and `cell` with `openpyxl.utils.cell.coordinate_from_string`; reject ranges, row/column zero, unknown types, non-finite numbers, overlong text, timezone-ambiguous dates, formulas (`cell.data_type == "f"`), protected cells, and non-anchor merged cells.

- [ ] **Step 4: Implement deterministic conversion and post-save invariants**

Convert ISO dates with `date.fromisoformat`/`datetime.fromisoformat`; reject strings beginning with `=` for all types. Snapshot sheet order, formula coordinates/expressions, merged ranges, protection flags, and non-target style IDs; save destination, reopen, then compare snapshots and target values. Delete destination and raise `WorkbookEditError("workbook_validation_failed")` on mismatch.

- [ ] **Step 5: Add rejection tests**

Test formula cells, protected cells, merged non-anchor cells, duplicate addresses, 501 patches, illegal A1 strings, missing sheets, formula injection strings, NaN/Infinity, `.xlsm`, and a monkeypatched post-save drift. Assert reason codes contain no cell values.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_spreadsheet_workbook_editor.py tests/test_spreadsheet_parser.py -q`  
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rag/spreadsheets/workbook_editor.py tests/test_spreadsheet_workbook_editor.py
git commit -m "feat: safely apply spreadsheet cell patches"
```

### Task 3: Save edits as optimistic immutable versions

**Files:**
- Create: `rag/spreadsheets/edit_service.py`
- Modify: `rag/spreadsheets/api.py`
- Modify: `rag/spreadsheets/__init__.py`
- Test: `tests/test_spreadsheet_edit_service.py`
- Test: `tests/test_spreadsheet_edit_api.py`

**Interfaces:**
- Consumes: Task 1 repository, Task 2 `apply_cell_patches`, existing `require_doc_edit`, `final_version_path`, and `enqueue_latest_index_job`.
- Produces: `SpreadsheetEditService.save(document_id: int, actor: dict, request: SpreadsheetEditRequest, request_meta: RequestMeta) -> SpreadsheetEditAccepted`.
- Produces: `POST /documents/{document_id}/spreadsheet-edits` and `GET /documents/{document_id}/spreadsheet-edits/{session_id}`.

- [ ] **Step 1: Write failing service tests**

Test edit permission, `.xlsx` only, current/base match, server-owned sheet map, old values read from the source, unchanged hash returns `status="unchanged"` with no version/session, accepted edit creates staging version + pending session + cell changes, moves the file under `data/document_versions/{document}/{version}/`, and enqueues exactly one latest index job.

- [ ] **Step 2: Run service tests to verify failure**

Run: `pytest tests/test_spreadsheet_edit_service.py -q`  
Expected: FAIL because `SpreadsheetEditService` does not exist.

- [ ] **Step 3: Implement staging without holding the DB lock**

Resolve the base path with `resolve_version_path`; load the ready structured dataset manifest to build `sheet_key -> sheet_name`; apply patches in a UUID temp directory under the version staging root; compute SHA-256; then call `reserve_spreadsheet_edit`. If reservation raises `VersionConflict`, delete staging and return a service error mapped to HTTP 409.

- [ ] **Step 4: Promote the staged file and enqueue indexing with compensation**

After reservation, move to `final_version_path`, call `set_version_file_path`, set edit session `pending`, and `enqueue_latest_index_job`. If promotion/enqueue fails, mark session/version failed and delete only this new file. Do not change current version in the request thread.

- [ ] **Step 5: Write failing API tests**

Use FastAPI client tests for 401, non-editor 403, missing document 404, invalid patch 422, stale base 409, unchanged 200, accepted 202, 500-cell acceptance, 501 rejection, and status access limited to actor/owner/admin. Assert responses never contain filesystem paths or old/new sensitive values.

- [ ] **Step 6: Implement thin Pydantic routes**

Pydantic models use `extra="forbid"`, discriminated value validation, reason max length, and tuple changes. Resolve user with existing dependencies; delegate all mutation to the service through `asyncio.to_thread`.

- [ ] **Step 7: Run service/API and security tests**

Run: `pytest tests/test_spreadsheet_edit_service.py tests/test_spreadsheet_edit_api.py tests/test_api_security.py tests/test_document_security_boundaries.py -q`  
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add rag/spreadsheets/edit_service.py rag/spreadsheets/api.py rag/spreadsheets/__init__.py tests/test_spreadsheet_edit_service.py tests/test_spreadsheet_edit_api.py
git commit -m "feat: save spreadsheet edits as pending versions"
```

### Task 4: Restore a historical workbook as a new audited version

**Files:**
- Modify: `rag/spreadsheets/edit_service.py`
- Modify: `rag/spreadsheets/api.py`
- Modify: `rag/user_db.py`
- Test: `tests/test_spreadsheet_restore.py`

**Interfaces:**
- Produces: `SpreadsheetEditService.restore(document_id: int, source_version_id: int, actor: dict, request: SpreadsheetRestoreRequest, request_meta: RequestMeta) -> SpreadsheetEditAccepted`.
- Produces: `POST /documents/{document_id}/versions/{source_version_id}/restore`.

- [ ] **Step 1: Write failing restore tests**

Given v1/v2/v3, restoring v1 while base is v3 must create v4, retain all earlier rows/files, set `restored_from_version_id=v1`, create operation `rollback`, and enqueue indexing. Verify a subsequent restore of v3 can create v5. Test edit permission, cross-document source version rejection, stale base 409, failed copy compensation, and source historical file missing.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_spreadsheet_restore.py -q`  
Expected: FAIL because restore is not implemented.

- [ ] **Step 3: Reuse the save pipeline with a restore source**

Do not synthesize cell patches for restore. Copy the selected immutable `.xlsx`, reopen/parse to validate, compute hash, reserve a new version with operation `rollback`, nullable zero cell changes, `restored_from_version_id`, and risk flag `non_previous_restore` when source is not the immediate predecessor.

- [ ] **Step 4: Add the route and version serialization**

Return 202 pending response. Extend version responses in `rag/operations_api.py` to include `restored_from_version_id` without physical paths.

- [ ] **Step 5: Run restore and version tests**

Run: `pytest tests/test_spreadsheet_restore.py tests/test_document_versions.py tests/test_document_version_schema.py -q`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rag/spreadsheets/edit_service.py rag/spreadsheets/api.py rag/user_db.py rag/operations_api.py tests/test_spreadsheet_restore.py
git commit -m "feat: restore spreadsheet history as new versions"
```

### Task 5: Build complete new-version spreadsheet datasets, FTS, and vectors

**Files:**
- Modify: `rag/materials/incremental.py`
- Modify: `rag/materials/worker.py`
- Modify: `rag/spreadsheets/extractor.py`
- Modify: `rag/vector_store.py`
- Modify: `rag/user_db.py`
- Test: `tests/test_spreadsheet_edit_indexing.py`
- Extend: `tests/test_incremental_material_index.py`
- Extend: `tests/test_spreadsheet_extraction.py`

**Interfaces:**
- Produces: `IncrementalIndexPlan(previous_version_id, unchanged, changed, removed)`.
- Produces: `DefaultVectorStore.copy_units_to_version(kb_id, source_version_id, target_version_id, document_id, version_no, unit_keys)`.
- Produces: edited `.xlsx` jobs create a staging structured dataset and atomically activate document version + dataset + FTS after a complete vector set exists.

- [ ] **Step 1: Write the regression that exposes the current gap**

Create previous units `unchanged, old`, current units `unchanged, new`; process the new version and assert: embedder receives only `new`, target version Qdrant points contain `unchanged` and `new`, target does not contain `old`, and current-version retrieval returns both target units. This must fail against the current worker because unchanged points are not materialized for the target version.

- [ ] **Step 2: Add removed-unit and empty-sheet tests**

Assert removed fingerprints are explicit, clearing the final populated cell yields a valid complete target version with no stale vector/FTS text, and a workbook with no semantic chunks still activates a ready structured dataset.

- [ ] **Step 3: Run to verify failures**

Run: `pytest tests/test_spreadsheet_edit_indexing.py tests/test_incremental_material_index.py -q`  
Expected: FAIL on missing unchanged target points/removed plan.

- [ ] **Step 4: Extend the incremental plan**

Return `removed = previous keys - current keys`. Keep digest equality keyed by stable spreadsheet unit key. Expose the source version for unchanged units; never infer reuse only from chunk position.

- [ ] **Step 5: Implement vector rematerialization**

For unchanged units, scroll the exact source version + document + unit-key points, clone vector and safe payload, replace `document_version_id/version_no/is_current`, and assign new point IDs. Verify copied count equals requested unit count before activation. If Qdrant cannot return vectors or counts differ, fail the target version; never silently omit unchanged content.

- [ ] **Step 6: Route edited XLSX jobs through structured extraction**

Inside `process_index_job`, detect `.xlsx`, call `parse_spreadsheet`, reserve `structured_datasets`, write immutable dataset, build row-group chunks, embed changed units, copy unchanged vectors, replace document FTS with full current text inside the activation transaction, and use `activate_document_and_dataset`. Mark the edit session active in the same transaction; on failure mark session/dataset/version failed and delete only target-version artifacts. Return a `JobResult` to the existing `MaterialWorker.run` wrapper and do not call `settle_material_job` from spreadsheet-specific code.

- [ ] **Step 7: Recheck permission and optimistic base at activation**

Before activation, load session actor and require current edit permission; ensure session base still equals current and target remains latest expected. Revocation or conflict cancels activation and preserves the old current version.

- [ ] **Step 8: Run indexing/lifecycle tests**

Run: `pytest tests/test_spreadsheet_edit_indexing.py tests/test_incremental_material_index.py tests/test_spreadsheet_extraction.py tests/test_material_jobs.py tests/test_material_api.py tests/test_pipeline_lifecycle.py tests/test_versioned_retrieval.py tests/test_spreadsheet_chunks.py -q`  
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add rag/materials/incremental.py rag/materials/worker.py rag/spreadsheets/extractor.py rag/vector_store.py rag/user_db.py tests/test_spreadsheet_edit_indexing.py tests/test_incremental_material_index.py tests/test_spreadsheet_extraction.py
git commit -m "fix: materialize complete spreadsheet version indexes"
```

### Task 6: Add an editable SpreadsheetViewer with conflict-safe save UX

**Files:**
- Modify: `frontend/src/api/spreadsheets.ts`
- Modify: `frontend/src/views/SpreadsheetViewer.vue`
- Create: `frontend/src/views/__tests__/SpreadsheetViewerEditing.test.ts`
- Create: `frontend/src/api/__tests__/spreadsheets.test.ts`

**Interfaces:**
- Produces: `SpreadsheetCellPatch`, `SpreadsheetEditRequest`, `SpreadsheetEditAccepted`, `saveSpreadsheetEdits`, `getSpreadsheetEditStatus`.
- Consumes: row response extended with column keys/display names, current version, can_edit, cell types, formula/protected/merged metadata needed to disable cells.

- [ ] **Step 1: Write API client tests**

```typescript
it('sends typed patches against the base version', async () => {
  await saveSpreadsheetEdits(12, { base_version_id: 7, changes: [
    { sheet_key: 'sheet_0001', cell: 'B12', value_type: 'number', value: 6500 }
  ], reason: 'price update' })
  expect(api.post).toHaveBeenCalledWith('/documents/12/spreadsheet-edits', expect.any(Object))
})
```

- [ ] **Step 2: Write failing component tests**

Test edit controls only when `can_edit`, local dirty highlighting, text/number/date/boolean/blank patch typing, save disabled when clean, discard restore, 202 polling until active, unchanged reset, 409 conflict message with no local silent overwrite, failed status retaining editable changes, and 500-change UI guard.

- [ ] **Step 3: Run to verify failure**

Run: `cd frontend && npm test -- --run src/api/__tests__/spreadsheets.test.ts src/views/__tests__/SpreadsheetViewerEditing.test.ts`  
Expected: FAIL because edit APIs/UI do not exist.

- [ ] **Step 4: Extend the read API projection**

Modify backend `rows` response to include server-calculated column indices/keys, `can_edit`, `current_version_id`, cell value types, and non-editable flags without exposing paths or formulas. Keep pagination stable and require view permission.

- [ ] **Step 5: Implement local patch state and save polling**

Key patches by `${sheet_key}:${a1}`; preserve original typed values for discard/diff; use Element Plus inputs based on value type; send only changed cells; poll status with bounded backoff; on active replace route version ID and reload; on conflict keep local edits and show refresh/reapply guidance.

- [ ] **Step 6: Run frontend and backend viewer tests**

Run: `pytest tests/test_spreadsheet_api.py tests/test_spreadsheet_edit_api.py -q`  
Run: `cd frontend && npm test -- --run src/api/__tests__/spreadsheets.test.ts src/views/__tests__/SpreadsheetViewerEditing.test.ts`  
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rag/spreadsheets/api.py frontend/src/api/spreadsheets.ts frontend/src/views/SpreadsheetViewer.vue frontend/src/views/__tests__/SpreadsheetViewerEditing.test.ts frontend/src/api/__tests__/spreadsheets.test.ts
git commit -m "feat: edit spreadsheet cells in the viewer"
```

### Task 7: Add version history and restore controls to the viewer

**Files:**
- Modify: `frontend/src/api/spreadsheets.ts`
- Modify: `frontend/src/api/documents.ts`
- Modify: `frontend/src/views/SpreadsheetViewer.vue`
- Create: `frontend/src/components/SpreadsheetVersionHistory.vue`
- Create: `frontend/src/components/__tests__/SpreadsheetVersionHistory.test.ts`

**Interfaces:**
- Produces: `restoreSpreadsheetVersion(documentId, sourceVersionId, {base_version_id, reason})`.
- Consumes: existing `/documents/{document_id}/versions` plus `restored_from_version_id` from Task 4.

- [ ] **Step 1: Write failing component tests**

Render v1-v4 with v4 current and v4 restored from v1. Assert historical versions remain visible, restore is shown only with `can_edit`, confirmation names source/current versions, restore sends current base, 409 is surfaced, pending restore is polled, and successful v5 reloads without removing v2-v4.

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && npm test -- --run src/components/__tests__/SpreadsheetVersionHistory.test.ts`  
Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement history and restore UI**

Use a drawer/dialog, show version number/status/uploader/time/provenance, require an explicit confirmation and optional reason, and reuse Task 6 polling/status handling. Never call a pointer-switch endpoint.

- [ ] **Step 4: Run tests**

Run: `cd frontend && npm test -- --run src/components/__tests__/SpreadsheetVersionHistory.test.ts src/views/__tests__/SpreadsheetViewerEditing.test.ts`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/spreadsheets.ts frontend/src/api/documents.ts frontend/src/views/SpreadsheetViewer.vue frontend/src/components/SpreadsheetVersionHistory.vue frontend/src/components/__tests__/SpreadsheetVersionHistory.test.ts
git commit -m "feat: restore spreadsheet versions from the viewer"
```

### Task 8: Expose permission-filtered spreadsheet change auditing

**Files:**
- Create: `rag/spreadsheets/audit.py`
- Modify: `rag/spreadsheets/api.py`
- Modify: `rag/user_db.py`
- Modify: `frontend/src/api/spreadsheets.ts`
- Create: `frontend/src/views/SpreadsheetChangesView.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/views/MainLayout.vue`
- Test: `tests/test_spreadsheet_audit_api.py`
- Test: `tests/test_spreadsheet_audit_risks.py`
- Create: `frontend/src/views/__tests__/SpreadsheetChangesView.test.ts`

**Interfaces:**
- Produces: `GET /admin/spreadsheet-changes` and `GET /admin/spreadsheet-changes/{session_id}`.
- Produces: `compute_risk_flags(operation, changes, actor_history, document) -> tuple[str, ...]`.
- Produces: administrator/owner-safe list and detail projections; shared editors cannot read management audit unless separately authorized.

- [ ] **Step 1: Write failing risk-rule tests**

Test flags: public document edit, >100 cell changes, >20 non-empty clears, non-previous restore, >5 same-user/document saves in the configured short window, and rejected formula/protected-cell attempts. Assert deterministic sorted flags.

- [ ] **Step 2: Write failing audit API tests**

Test admin sees all permitted sessions; document owner sees owned-document sessions; shared editor gets 403; filters for actor/document/kb/time/operation/status/risk; stable cursor pagination; details group cell changes by sheet. For a sensitive/redacted fixture, assert list omits title/preview and detail omits sheet/cell/old/new while retaining actor/time/count/status/risk.

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_spreadsheet_audit_risks.py tests/test_spreadsheet_audit_api.py -q`  
Expected: FAIL because audit service/routes do not exist.

- [ ] **Step 4: Implement risk calculation at reservation/rejection**

Successful sessions calculate flags from server-read old/new values. Rejected formula/protection attempts create `status="rejected"`, `new_version_id=NULL`, no sensitive cell-value rows, and a safe error code/risk flag. Keep audit rows immutable; expose no delete endpoint.

- [ ] **Step 5: Implement permission-filtered list/detail queries**

Use parameterized SQL with bounded limit and `(created_at,id)` cursor. Centralize `can_view_audit_values(user, document)`; current V3 grants full values to admin or document owner who can view the document, while future action ACL can replace the adapter. Return generic 404 for inaccessible session details.

- [ ] **Step 6: Write and implement the frontend page**

Test filters, risk badges, pending/active/failed states, redacted detail, grouped diffs, version links, and empty/error states. Add `/operations/spreadsheet-changes` route and a management navigation entry shown to admin/eligible owners.

- [ ] **Step 7: Run backend/frontend audit tests**

Run: `pytest tests/test_spreadsheet_audit_risks.py tests/test_spreadsheet_audit_api.py tests/test_document_security_boundaries.py -q`  
Run: `cd frontend && npm test -- --run src/views/__tests__/SpreadsheetChangesView.test.ts`  
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add rag/spreadsheets/audit.py rag/spreadsheets/api.py rag/user_db.py frontend/src/api/spreadsheets.ts frontend/src/views/SpreadsheetChangesView.vue frontend/src/router/index.ts frontend/src/views/MainLayout.vue tests/test_spreadsheet_audit_api.py tests/test_spreadsheet_audit_risks.py frontend/src/views/__tests__/SpreadsheetChangesView.test.ts
git commit -m "feat: audit spreadsheet changes in management"
```

### Task 9: End-to-end verification, performance bounds, and documentation

**Files:**
- Create: `tests/test_spreadsheet_edit_e2e.py`
- Create: `tests/test_spreadsheet_edit_concurrency.py`
- Modify: `tests/test_pipeline_cache_security.py`
- Modify: `docs/permission-design.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a verified edit → index → query → audit → restore loop with final-answer cache still disabled.

- [ ] **Step 1: Write the end-to-end scenario**

Upload an `.xlsx`, edit text/number/date/boolean/blank across sheets, wait for active, assert v2 structured rows/FTS/vector retrieval and audit diff, restore v1 into v3, assert v2 remains downloadable/history-visible, then restore v2 into v4. Assert removed text is absent from current retrieval and unchanged rows remain retrievable without extra embed calls.

- [ ] **Step 2: Write concurrency and compensation scenarios**

Run two saves from the same base and assert exactly one can activate; revoke edit between reservation and activation; inject workbook, dataset, embedder, Qdrant, FTS, and activation failures; assert the old version stays current, new artifacts are cleaned, audit status is safe, and no filesystem path/cell value leaks in errors.

- [ ] **Step 3: Add bounded performance assertions**

For a workbook within configured spreadsheet limits, assert a one-cell edit embeds only changed semantic units, vector-copy work is bounded by current unit count, API patch validation rejects >500 before file IO, and audit endpoints enforce maximum page size. Use call counts and deterministic fixtures, not wall-clock thresholds.

- [ ] **Step 4: Verify answer cache remains disabled**

Extend `test_pipeline_cache_security.py` so edit/restore and ACL/version changes do not enable final-answer cache reads or writes.

- [ ] **Step 5: Run focused and full verification**

Run: `pytest tests/test_spreadsheet_* tests/test_incremental_material_index.py tests/test_document_versions.py tests/test_material_jobs.py tests/test_pipeline_cache_security.py -q`  
Run: `pytest tests/test_material_api.py tests/test_pipeline_lifecycle.py -q`  
Run: `pytest -q`  
Run: `cd frontend && npm test -- --run`  
Run: `cd frontend && npm run build`  
Expected: all commands PASS.

- [ ] **Step 6: Update documentation**

Document supported value types, 500-cell limit, immutable save/restore semantics, edit/view/download permissions, conflict response, pending indexing, admin audit/redaction, rejected features, and recovery behavior. Do not claim Word/PDF editing or real-time collaboration.

- [ ] **Step 7: Commit**

```bash
git add tests/test_spreadsheet_edit_e2e.py tests/test_spreadsheet_edit_concurrency.py tests/test_pipeline_cache_security.py docs/permission-design.md README.md
git commit -m "test: verify spreadsheet editing lifecycle"
```

## Execution Order and Checkpoints

Pass the existing dirty-worktree integration gate first. Then execute Tasks 1–5 as the backend/versioning checkpoint, review schema, failure atomicity, worker lease renewal, and exactly-once settlement; execute Tasks 6–7 as the editor checkpoint, Task 8 as the management audit checkpoint, and Task 9 as the release gate. Do not start a later checkpoint while the preceding focused suite is failing.
