# Spreadsheet Structural Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe insert/delete row/column and blank-sheet creation to the existing immutable V3 spreadsheet editor.

**Architecture:** A focused workbook structure editor validates and mutates one staged `.xlsx`. A structure edit service reuses the existing version reservation, enqueue, worker activation, rollback, and audit boundaries while adding a typed structural audit row. The viewer sends one structural operation and polls the existing edit-session status.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, openpyxl, SQLite, pytest, Vue 3, TypeScript, Vitest.

## Global Constraints

- Work only in `C:\Users\Administrator\Desktop\RAGv3\RAGv3`.
- Preserve the pre-existing dirty worktree; do not stage or commit overlapping user changes.
- `.xlsx` only; one operation per request; row/column count 1–100.
- Delete non-empty content only with `confirm_nonempty=true`.
- Append blank worksheets only; no copy/rename/reorder/delete-sheet feature.
- Reject formula/merge/protection/external-link/named-range/table/chart/pivot risks for row/column changes.
- Every accepted action creates a new version and uses the existing worker; never switch pointers in the request thread.

---

### Task 1: Typed contracts and workbook structural editor

**Files:**
- Modify: `rag/spreadsheets/edit_models.py`
- Create: `rag/spreadsheets/structure_editor.py`
- Test: `tests/test_spreadsheet_structure_models.py`
- Test: `tests/test_spreadsheet_structure_editor.py`

**Interfaces:**
- Produces: `SpreadsheetStructureEditRequest` with the five actions and conditional validation.
- Produces: `apply_structure_edit(source, destination, sheet_map, request) -> StructureChange`.

- [ ] Write model tests proving action-specific required/forbidden fields and 1–100 bounds.
- [ ] Run `pytest tests/test_spreadsheet_structure_models.py -q` and verify failure because the models do not exist.
- [ ] Implement only the model contract and rerun to green.
- [ ] Write real-workbook tests for all five actions, preservation of unaffected cells/sheets, non-empty deletion confirmation, invalid names, formulas, merges, tables, charts, external links, and protection.
- [ ] Run `pytest tests/test_spreadsheet_structure_editor.py -q` and verify failure because the editor does not exist.
- [ ] Implement staging mutation, validation, reopen verification, and failure cleanup; rerun both files to green.

### Task 2: Immutable version reservation and structural audit

**Files:**
- Modify: `rag/db_migrations.py`
- Modify: `rag/user_db.py`
- Modify: `rag/spreadsheets/audit.py`
- Test: `tests/test_spreadsheet_structure_repository.py`

**Interfaces:**
- Produces: migration v5 table `spreadsheet_structure_changes`.
- Produces: `reserve_spreadsheet_structure_edit(...) -> tuple[session_id, version_id]`.
- Produces: structural fields in audit list/detail projections.

- [ ] Write a failing repository test that reserves one new version/session/structure row atomically and rejects a stale base without partial rows.
- [ ] Run the test and verify missing-table/missing-method failure.
- [ ] Add migration/baseline schema and the locked transaction method; run to green.
- [ ] Extend the failing audit API test with structure detail and affected-count assertions.
- [ ] Project structural audit without deleted values and rerun audit tests to green.

### Task 3: Structure edit service and API

**Files:**
- Create: `rag/spreadsheets/structure_service.py`
- Modify: `rag/spreadsheets/api.py`
- Test: `tests/test_spreadsheet_structure_service.py`
- Test: `tests/test_spreadsheet_structure_api.py`

**Interfaces:**
- Produces: `SpreadsheetStructureEditService.save(document_id, actor, body, meta)`.
- Produces: `POST /documents/{document_id}/spreadsheet-structure-edits`.

- [ ] Write failing service tests for edit authorization, `.xlsx`, current base, server sheet map, one immutable version, one enqueue, and compensation.
- [ ] Run and verify missing-service failure.
- [ ] Implement the minimum orchestration by reusing version path helpers and latest-job enqueue; run to green.
- [ ] Write failing API tests for 202, 401/403/404, 409, 422 safety codes, and no path/value leakage.
- [ ] Add the route/error mapping and rerun service/API tests to green.

### Task 4: Worker activation and rollback integration

**Files:**
- Modify: `rag/materials/worker.py` only if a failing integration test exposes a missing generic hook.
- Test: `tests/test_spreadsheet_structure_indexing.py`

**Interfaces:**
- Consumes: existing spreadsheet material job and edit-session activation.
- Produces: a ready structured dataset/FTS/vector version from a structural edit, with normal historical rollback.

- [ ] Write a failing end-to-end worker test: insert row, process target, assert new ready rows/current version/audit; restore the base and process again.
- [ ] Run and verify the exact missing behavior.
- [ ] Make the smallest worker/service change needed, if any, and rerun to green.
- [ ] Add deletion/current-text absence and permission-revocation assertions; keep old current on failure.

### Task 5: Viewer structural controls

**Files:**
- Modify: `frontend/src/api/spreadsheets.ts`
- Modify: `frontend/src/views/SpreadsheetViewer.vue`
- Create: `frontend/src/components/SpreadsheetStructureDialog.vue`
- Create: `frontend/src/components/__tests__/SpreadsheetStructureDialog.test.ts`
- Extend: `frontend/src/views/__tests__/SpreadsheetViewerEditing.test.ts`

**Interfaces:**
- Produces: `saveSpreadsheetStructureEdit(documentId, request)`.
- Produces: one-action dialog and reuse of existing status polling.

- [ ] Write failing API and component tests for conditional fields, maximum count, permission hiding, delete confirmation, retained form on 409, and active-version reload.
- [ ] Run targeted Vitest and verify missing API/component failures.
- [ ] Implement the API client and minimal dialog; integrate it into the viewer.
- [ ] Run targeted tests, then `npm.cmd run build`, and fix only evidenced failures.

### Task 6: Documentation and release verification

**Files:**
- Modify: `README.md`
- Modify: `docs/permission-design.md`

**Interfaces:**
- Documents exact supported actions, conservative refusals, immutable behavior, and recovery.

- [ ] Run all `test_spreadsheet_*` tests plus version, worker, audit, and cache-security regressions.
- [ ] Run full `pytest -q` and frontend Vitest with `--maxWorkers=1`.
- [ ] Run Ruff on touched Python and `git diff --check`.
- [ ] Run the production frontend build.
- [ ] Update documentation without claiming formula rewriting, sheet deletion, or real-time collaboration.

## Plan Self-Review

- Coverage: all five confirmed actions, safety refusals, versioning, indexing, rollback, audit, frontend, and verification are assigned.
- Placeholder scan: no TBD/TODO or unspecified implementation step remains.
- Type consistency: `SpreadsheetStructureEditRequest`, `StructureChange`, structure audit table, service method, route, and frontend client names are stable across tasks.
- Commit steps are intentionally suspended because this checkout contains pre-existing overlapping uncommitted work.

