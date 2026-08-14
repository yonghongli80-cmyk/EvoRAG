# Spreadsheet Structural Editing Design

## Goal

Extend the existing V3 `.xlsx` cell editor with a safe basic structural editor: insert/delete rows, insert/delete columns, and append a blank worksheet. Every operation creates an immutable version, remains rollbackable, enters the existing indexing pipeline, and appears in management audit.

## Confirmed Scope

- One structural operation per request; structural and cell edits are never mixed.
- Actions: `insert_rows`, `delete_rows`, `insert_columns`, `delete_columns`, `add_sheet`.
- Insert/delete count is 1–100.
- Deleting non-empty rows or columns is allowed only with explicit `confirm_nonempty=true`.
- New worksheets are blank, user-named, and appended at the end. Copying, renaming, reordering, and deleting worksheets are out of scope.
- Existing optimistic concurrency, immutable versions, pending indexing, rollback, and permission rules remain authoritative.
- This is not real-time collaboration and does not promise WPS/Excel fidelity for macros, pivots, external links, charts, or structural formula rewriting.

## Safety Rules

The server resolves `sheet_key` from the current ready structured dataset; clients never choose physical paths or worksheet names. The operation is accepted only for the current `.xlsx` version and only after `edit` authorization.

For row/column operations, the first version is deliberately conservative. It rejects workbooks with workbook/sheet protection, formulas anywhere in the workbook, merged ranges on the target sheet, external links, named ranges, Excel tables, charts, or pivot tables. This avoids silent corruption because openpyxl does not comprehensively rewrite all dependent structures. `add_sheet` may run in an otherwise ordinary workbook but still rejects structure-locked workbooks and invalid/duplicate names.

The workbook is saved to staging, reopened, and compared against invariants before the version is reserved. Failures delete staging and create a value-free rejected audit record with a safe error code.

## API

`POST /documents/{document_id}/spreadsheet-structure-edits`

```json
{
  "base_version_id": 7,
  "action": "delete_rows",
  "sheet_key": "sheet_0001",
  "index": 5,
  "count": 2,
  "new_sheet_name": null,
  "confirm_nonempty": true,
  "reason": "remove obsolete products"
}
```

Conditional contract:

- Row/column actions require `sheet_key`, `index`, and `count` and forbid `new_sheet_name`.
- `add_sheet` requires `new_sheet_name` and forbids `sheet_key`, `index`, and non-default `count`.
- The response matches cell editing: `202 {status:"pending", session_id, version_id}`.
- Stale bases return 409. Validation/safety refusals return 422 with a stable code. Enqueue failures return 500 after compensation.

## Versioning and Indexing

The service copies the current immutable workbook to staging, applies one operation, computes a content hash, reserves a staging `document_versions` row plus audit rows in one transaction, promotes the file, and enqueues exactly one latest-version material job.

The existing spreadsheet worker parses the complete target workbook, builds a new immutable structured dataset, FTS text, and complete target-version vector set, rechecks permission and base version, then atomically activates version, dataset, and edit session. Row/column shifts may change row-group unit keys and therefore may re-embed more chunks than a one-cell edit; correctness takes priority over maximal reuse.

## Audit Model

`spreadsheet_edit_sessions` remains the common operation envelope with `operation='edit'`. Migration v5 adds one optional `spreadsheet_structure_changes` row per session:

- `session_id`
- `action`
- `sheet_key` and `sheet_name_snapshot`
- `index_no` and `count`
- `new_sheet_name`
- `affected_nonempty_cells`

Deletion audit records affected non-empty cell count, not deleted cell values. Management list/detail projections expose the structural operation while preserving existing admin/owner authorization and redaction boundaries. Rejected structural attempts have no version and no cell-value rows.

## Frontend

The spreadsheet viewer shows a “结构操作” control only when `can_edit=true`. A small dialog selects one action and collects its conditional fields. Destructive deletion displays the target sheet, start index, count, and a second confirmation; the server remains authoritative and may demand `confirm_nonempty`.

After a 202 response, the page uses the existing bounded status polling. Activation navigates to the new version and reloads rows/history. A 409 or failure retains the form values and leaves the old version visible.

## Testing

- Model tests cover conditional payloads and 1–100 bounds.
- Real workbook tests cover each action, name validation, non-empty confirmation, and every conservative refusal.
- Service/API tests cover authorization, stale base, immutable version reservation, audit, enqueue compensation, and safe errors.
- Worker integration proves structural versions produce ready datasets and remain rollbackable.
- Vue tests cover action forms, destructive confirmation, 202 polling, 409 retention, and permission hiding.
- Focused and full backend/frontend suites remain green.

