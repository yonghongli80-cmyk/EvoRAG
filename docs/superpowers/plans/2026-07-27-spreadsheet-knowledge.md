# Spreadsheet Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Excel/CSV into versioned, browser-viewable knowledge with exact structured queries and row-range vector citations.

**Architecture:** Keep the immutable source file, create one read-only SQLite dataset per document version, and write version-aware workbook/schema/row-group chunks to Qdrant. A typed query gateway performs filtering and Decimal aggregation; the LLM selects a query mode and explains only validated results.

**Tech Stack:** Python 3.12, FastAPI, SQLite, openpyxl 3.1.x, chardet 5.2.x, Qdrant, pytest, Vue 3, Element Plus, TypeScript, Vitest

## Global Constraints

- Execute after Trusted Knowledge Loop Tasks 1–5 and Answer Reliability Tasks 1–3.
- Use `superpowers:executing-plans`; do not dispatch subagents unless the user changes that preference.
- Supported formats are exactly `.xlsx` and `.csv`.
- First release is read-only; no cell editing or write-back.
- Original file, dataset SQLite, and Qdrant points share one `document_version_id`.
- Each spreadsheet version stores its structured copy at `data/structured_datasets/{document_id}/{document_version_id}/dataset.sqlite3`.
- A new spreadsheet version becomes current only after all three layers validate.
- Empty, numeric zero, and boolean false remain distinct.
- Currency and precision-sensitive aggregation uses `decimal.Decimal`, not float.
- The model never executes raw SQL, Python, Pandas, formulas, or macros.
- Default limits: 50MB, 50 sheets, 200,000 data rows, 500 columns, 32,000 characters per cell.
- Viewer row page default is 100 and maximum is 500.
- Row-group target is at most 1,200 tokens and at most 50 rows.
- Real payroll/attendance remains production-disabled pending supervisor approval.
- System administrator status alone does not grant payroll content access.

---

## File Structure

### New backend

- `rag/spreadsheets/__init__.py`
- `rag/spreadsheets/models.py`
- `rag/spreadsheets/parser.py`
- `rag/spreadsheets/store.py`
- `rag/spreadsheets/extractor.py`
- `rag/spreadsheets/chunker.py`
- `rag/spreadsheets/query.py`
- `rag/spreadsheets/citations.py`
- `rag/spreadsheets/api.py`
- `rag/spreadsheets/migration.py`
- `tests/test_spreadsheet_schema.py`
- `tests/test_spreadsheet_parser.py`
- `tests/test_spreadsheet_store.py`
- `tests/test_spreadsheet_extraction.py`
- `tests/test_spreadsheet_chunks.py`
- `tests/test_structured_query.py`
- `tests/test_spreadsheet_api.py`
- `tests/test_spreadsheet_migration.py`

### Modified backend

- `config.py`
- `rag/user_db.py`
- `rag/models.py`
- `rag/vector_store.py`
- `rag/document_versions.py`
- `rag/api.py`
- `tests/conftest.py`

### New frontend

- `frontend/src/api/spreadsheets.ts`
- `frontend/src/views/SpreadsheetViewer.vue`
- `frontend/src/components/SpreadsheetGrid.vue`
- `frontend/src/components/SpreadsheetFilterDialog.vue`
- `frontend/src/components/SpreadsheetAskSelection.vue`
- Corresponding `*.test.ts`.

### Modified frontend

- `frontend/src/router/index.ts`
- `frontend/src/views/FileModeView.vue`
- `frontend/src/components/CitationList.vue`

---

### Task 1: Dataset Metadata and Isolated Paths

**Files:**
- Create: `rag/spreadsheets/models.py`
- Test: `tests/test_spreadsheet_schema.py`
- Modify: `rag/user_db.py`
- Modify: `config.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `DatasetStatus`, `SemanticIndexMode`, `SheetVisibility`, `ColumnType`.
- Produces: `UserDB.create_structured_dataset(document_id: int, document_version_id: int, fmt: str, semantic_index_mode: str) -> int`.
- Produces: `UserDB.activate_structured_dataset(dataset_id: int) -> None`.
- Produces: `UserDB.get_structured_dataset_for_version(version_id: int) -> dict | None`.
- Produces: `settings.structured_datasets_root`.

- [ ] **Step 1: Write failing schema and uniqueness tests**

```python
def test_one_dataset_per_document_version(tmp_path):
    db, version_id = versioned_spreadsheet_fixture(tmp_path)
    first = db.create_structured_dataset(7, version_id, "xlsx", "row_groups")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_structured_dataset(7, version_id, "xlsx", "row_groups")
    assert db.get_structured_dataset_for_version(version_id)["id"] == first
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_schema.py -v
```

Expected: FAIL because tables and methods are missing.

- [ ] **Step 3: Add metadata tables and config**

Create `structured_datasets`, `structured_sheets`, `structured_columns`, and `structured_query_runs` with the exact fields and status enums from the spec. Add a unique constraint on `document_version_id`.

```python
structured_datasets_root: str = str(
    Path(__file__).resolve().parent / "data" / "structured_datasets"
)
spreadsheet_max_bytes: int = 50 * 1024 * 1024
spreadsheet_max_sheets: int = 50
spreadsheet_max_rows: int = 200_000
spreadsheet_max_columns: int = 500
spreadsheet_max_cell_chars: int = 32_000
sensitive_structured_query_enabled: bool = False
```

```sql
CREATE TABLE structured_datasets (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    document_version_id INTEGER NOT NULL UNIQUE,
    format TEXT NOT NULL CHECK(format IN ('xlsx', 'csv')),
    status TEXT NOT NULL CHECK(status IN ('staging', 'ready', 'failed', 'historical')),
    storage_key TEXT NOT NULL,
    semantic_index_mode TEXT NOT NULL CHECK(semantic_index_mode IN ('schema_only', 'row_groups'))
);
```

Fixtures must point roots to `tmp_path`; no test may use repository `data/`.

- [ ] **Step 4: Run schema and existing DB tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_schema.py tests\test_user_db.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add config.py rag/user_db.py rag/spreadsheets/models.py tests/conftest.py tests/test_spreadsheet_schema.py
git commit -m "feat: add spreadsheet dataset metadata"
```

---

### Task 2: XLSX/CSV Parser and Version SQLite Store

**Files:**
- Create: `rag/spreadsheets/parser.py`
- Create: `rag/spreadsheets/store.py`
- Test: `tests/test_spreadsheet_parser.py`
- Test: `tests/test_spreadsheet_store.py`

**Interfaces:**
- Produces: `parse_spreadsheet(path: Path, fmt: str, mapping: ImportMapping | None) -> ParsedWorkbook`.
- Produces: `write_dataset(db_path: Path, workbook: ParsedWorkbook) -> DatasetManifest`.
- Produces: `resolve_dataset_path(root: Path, document_id: int, version_id: int) -> Path`.

- [ ] **Step 1: Write failing value-preservation tests**

```python
def test_xlsx_preserves_zero_false_formula_and_cached_metadata(tmp_path):
    path = workbook_with_values(tmp_path, values=[0, False, None], formula="=A2+1")
    parsed = parse_spreadsheet(path, "xlsx", None)
    row = parsed.sheets[0].rows[0]
    assert row.cells[0].value == 0
    assert row.cells[1].value is False
    assert row.cells[2].value is None
    assert parsed.sheets[0].formula_cells[0].formula == "=A2+1"
```

```python
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8", "gb18030"])
def test_csv_encodings_round_trip(tmp_path, encoding):
    path = write_csv(tmp_path, "姓名,工资\n张三,0\n", encoding)
    parsed = parse_spreadsheet(path, "csv", None)
    assert parsed.sheets[0].rows[0].cells[1].value == 0
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_parser.py tests\test_spreadsheet_store.py -v
```

Expected: FAIL because parser/store do not exist.

- [ ] **Step 3: Implement parser and typed storage**

Load XLSX twice when formulas exist: `data_only=False` for formula text and `data_only=True` for cached values. Never execute formulas. CSV attempts UTF-8 BOM, UTF-8, then GB18030 and uses `csv.Sniffer` only over `,`, tab, `;`, and `|`.

```python
def canonical_cell(value: object, inferred: ColumnType) -> object:
    if value is None:
        return None
    if inferred == ColumnType.BOOLEAN:
        return 1 if value is True else 0
    if inferred == ColumnType.DECIMAL:
        return format(Decimal(str(value)), "f")
    if inferred in {ColumnType.DATE, ColumnType.DATETIME}:
        return value.isoformat()
    return value
```

Use internal table names `sheet_0001`; store `__row_number` and `__row_hash`. Run `PRAGMA integrity_check` after writing, then atomically rename the staged SQLite file.

- [ ] **Step 4: Run parser/store tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_parser.py tests\test_spreadsheet_store.py -v
```

Expected: PASS for multi-sheet, hidden sheet, merged header, duplicate header, formulas, encodings, delimiter detection, and limits.

- [ ] **Step 5: Commit**

```powershell
git add rag/spreadsheets/parser.py rag/spreadsheets/store.py tests/test_spreadsheet_parser.py tests/test_spreadsheet_store.py
git commit -m "feat: parse spreadsheets into version stores"
```

---

### Task 3: Three-Layer Extraction Lifecycle

**Files:**
- Create: `rag/spreadsheets/extractor.py`
- Test: `tests/test_spreadsheet_extraction.py`
- Modify: `rag/document_versions.py`
- Modify: `rag/user_db.py`

**Interfaces:**
- Produces: `extract_spreadsheet_version(context: ExtractionContext) -> ExtractionResult`.
- Consumes callbacks: `write_vectors(chunks)`, `delete_version_vectors(version_id)`.

- [ ] **Step 1: Write failing compensation test**

```python
def test_vector_failure_keeps_old_spreadsheet_current(spreadsheet_context):
    old = spreadsheet_context.seed_current_version()
    spreadsheet_context.vector_writer.fail_with(RuntimeError("qdrant down"))
    with pytest.raises(RuntimeError, match="qdrant down"):
        extract_spreadsheet_version(spreadsheet_context.new_version())
    assert spreadsheet_context.db.current_version_id() == old.version_id
    assert spreadsheet_context.db.current_dataset_id() == old.dataset_id
    assert not spreadsheet_context.new_dataset_path.exists()
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_extraction.py -v
```

Expected: FAIL because extraction service is missing.

- [ ] **Step 3: Implement staging and atomic activation**

Order is source stage, version row, dataset stage, integrity check, chunks, vectors with `is_current=false`, file promotion, and one SQLite activation transaction. On failure delete only new vectors/files and mark the new dataset/version failed.

```python
with db.activation_transaction():
    db.mark_old_dataset_historical(document_id)
    db.activate_document_version(document_id, version_id, kb_id, filename)
    db.activate_structured_dataset(dataset_id)
vector_store.mark_document_version_current(collection, document_id, version_id)
```

If the post-transaction vector flag update fails, enqueue a repair record and keep retrieval filtering by current version ID from SQLite until repair succeeds.

- [ ] **Step 4: Run lifecycle and version tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_extraction.py tests\test_document_versions.py tests\test_kb_file_lifecycle.py -v
```

Expected: PASS including identical hash and concurrent overwrite cases.

- [ ] **Step 5: Commit**

```powershell
git add rag/spreadsheets/extractor.py rag/document_versions.py rag/user_db.py tests/test_spreadsheet_extraction.py
git commit -m "feat: add atomic spreadsheet extraction"
```

---

### Task 4: Spreadsheet Chunks and Qdrant Payload

**Files:**
- Create: `rag/spreadsheets/chunker.py`
- Test: `tests/test_spreadsheet_chunks.py`
- Modify: `rag/models.py`
- Modify: `rag/vector_store.py`

**Interfaces:**
- Produces: `chunk_workbook(workbook: ParsedWorkbook, metadata: DatasetMetadata) -> list[Chunk]`.
- Adds Chunk fields: `dataset_id`, `sheet_id`, `sheet_key`, `sheet_name`, `chunk_kind`, `row_start`, `row_end`, `column_keys`.

- [ ] **Step 1: Write failing chunk-boundary test**

```python
def test_row_groups_repeat_headers_and_preserve_ranges():
    chunks = chunk_workbook(workbook(rows=120), metadata(dataset_id=9, version_id=4))
    row_groups = [item for item in chunks if item.chunk_kind == "row_group"]
    assert all(item.text.startswith("姓名\t月份\t工资") for item in row_groups)
    assert all(item.row_end - item.row_start + 1 <= 50 for item in row_groups)
    assert [(item.row_start, item.row_end) for item in row_groups] == [(2, 51), (52, 101), (102, 121)]
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_chunks.py -v
```

Expected: FAIL because chunk types/payload are absent.

- [ ] **Step 3: Implement four chunk kinds**

Emit workbook summary, one schema chunk per sheet, adaptive row groups capped by both token estimate and 50 rows, and one chunk per QA pair only when import mode is explicit. Add exact payload fields from the spec and round-trip them in Qdrant search.

```python
def row_group_chunk(sheet: ParsedSheet, rows: list[ParsedRow]) -> Chunk:
    return Chunk(
        text=render_rows(sheet.columns, rows),
        doc_name=sheet.document_name,
        chunk_index=rows[0].row_number,
        chunk_kind="row_group",
        sheet_key=sheet.key,
        row_start=rows[0].row_number,
        row_end=rows[-1].row_number,
        column_keys=tuple(column.key for column in sheet.columns),
    )
```

- [ ] **Step 4: Run chunk/vector tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_chunks.py tests\test_vector_store.py tests\test_versioned_retrieval.py -v
```

Expected: PASS; ACL and current-version filters precede result reconstruction.

- [ ] **Step 5: Commit**

```powershell
git add rag/spreadsheets/chunker.py rag/models.py rag/vector_store.py tests/test_spreadsheet_chunks.py tests/test_vector_store.py
git commit -m "feat: index spreadsheet row groups"
```

---

### Task 5: Typed Structured Query Gateway

**Files:**
- Create: `rag/spreadsheets/query.py`
- Create: `rag/spreadsheets/citations.py`
- Test: `tests/test_structured_query.py`
- Modify: `rag/user_db.py`

**Interfaces:**
- Produces: `StructuredQueryPlan`, `FilterClause`, `Metric`, `StructuredQueryResult`, `QueryScope`.
- Produces: `execute_structured_query(plan: StructuredQueryPlan, scope: QueryScope) -> StructuredQueryResult`.

- [ ] **Step 1: Write failing SQL-rejection and Decimal tests**

```python
def test_plan_schema_rejects_raw_sql():
    with pytest.raises(ValidationError):
        StructuredQueryPlan.model_validate({"dataset_id": 1, "sql": "SELECT * FROM sheet_0001"})


def test_decimal_average_is_exact(query_fixture):
    result = query_fixture.execute(metric=("工资", "average"), values=["0.1", "0.2"])
    assert result.values[0].canonical == "0.15"
    assert result.citations[0].cell_range == "工资!B2:B3"
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_structured_query.py -v
```

Expected: FAIL because typed plan and gateway are absent.

- [ ] **Step 3: Implement allowlisted filtering and aggregation**

Allowed operations: `lookup`, `aggregate`, `compare`. Allowed filters: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `between`, `in`. Allowed metrics: `count`, `distinct_count`, `sum`, `average`, `min`, `max`.

Resolve all column keys through metadata, generate parameterized filtering SQL, cap rows/time, and aggregate Decimal columns in Python. Persist only plan/field/filter summaries and result hash.

```python
def aggregate_decimal(values: Iterable[str], function: str) -> Decimal:
    items = [Decimal(value) for value in values]
    if function == "sum":
        return sum(items, Decimal("0"))
    if function == "average":
        return sum(items, Decimal("0")) / Decimal(len(items))
    raise UnsupportedMetric(function)
```

- [ ] **Step 4: Run query and permission tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_structured_query.py tests\test_document_security_boundaries.py -v
```

Expected: PASS for unknown column, write SQL, unit conflict, timeout, row limit, ACL, and production-disabled payroll fixtures.

- [ ] **Step 5: Commit**

```powershell
git add rag/spreadsheets/query.py rag/spreadsheets/citations.py rag/user_db.py tests/test_structured_query.py
git commit -m "feat: query spreadsheets deterministically"
```

---

### Task 6: Spreadsheet Viewer API

**Files:**
- Create: `rag/spreadsheets/api.py`
- Test: `tests/test_spreadsheet_api.py`
- Modify: `rag/api.py`

**Interfaces:**
- Produces version-scoped spreadsheet/sheet/row/filter/selection routes from the spec.

- [ ] **Step 1: Write failing range and path-redaction test**

```python
def test_rows_endpoint_returns_cells_without_storage_path(isolated_api, payroll_sheet, hr_headers):
    response = isolated_api.get(
        f"/documents/{payroll_sheet.document_id}/versions/{payroll_sheet.version_id}/"
        f"sheets/{payroll_sheet.sheet_key}/rows?after_row=0&limit=100",
        headers=hr_headers,
    )
    assert response.status_code == 200
    assert response.json()["rows"][0]["row_number"] == 2
    assert "storage" not in response.text.lower()
    assert "sqlite" not in response.text.lower()
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_api.py -v
```

Expected: 404.

- [ ] **Step 3: Implement routes**

Every route resolves version through logical document, calls `require_doc_view`, validates sheet ownership, and opens the resolved dataset read-only. `limit` is constrained to 1–500. Filters consume `column_key` and operator enums only.

```python
@router.get("/documents/{document_id}/versions/{version_id}/sheets/{sheet_key}/rows")
def rows(document_id: int, version_id: int, sheet_key: str, after_row: int = 0, limit: int = 100):
    require_doc_view(user_db, document_id, current_user())
    dataset = require_dataset_version(user_db, document_id, version_id)
    return store.read_rows(dataset, sheet_key, after_row=after_row, limit=min(limit, 500))
```

- [ ] **Step 4: Run API/security tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_api.py tests\test_upload_security.py tests\test_document_security_boundaries.py -v
```

Expected: PASS including historical version, revoked ACL, wrong-document version, and hidden sheet cases.

- [ ] **Step 5: Commit**

```powershell
git add rag/spreadsheets/api.py rag/api.py tests/test_spreadsheet_api.py
git commit -m "feat: expose read-only spreadsheet API"
```

---

### Task 7: Browser Spreadsheet Viewer

**Files:**
- Create: `frontend/src/api/spreadsheets.ts`
- Create: `frontend/src/views/SpreadsheetViewer.vue`
- Create: `frontend/src/components/SpreadsheetGrid.vue`
- Create: `frontend/src/components/SpreadsheetFilterDialog.vue`
- Create: `frontend/src/components/SpreadsheetAskSelection.vue`
- Create: corresponding tests.
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/views/FileModeView.vue`
- Modify: `frontend/src/components/CitationList.vue`

**Interfaces:**
- Produces route `/documents/:documentId/versions/:versionId/spreadsheet`.

- [ ] **Step 1: Write failing viewer test**

```typescript
it('opens cited rows and offers no edit action', async () => {
  mockSpreadsheet({ sheets: [{ key: 's1', name: '考勤' }], rows: [{ row_number: 12, values: ['张三', '3'] }] })
  const wrapper = mountSpreadsheetViewer('/documents/7/versions/4/spreadsheet?sheet=s1&range=A12:B12')
  await flushPromises()
  expect(wrapper.text()).toContain('考勤')
  expect(wrapper.get('[data-row="12"]').classes()).toContain('cited')
  expect(wrapper.find('[aria-label="保存"]').exists()).toBe(false)
})
```

- [ ] **Step 2: Run RED**

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/views/SpreadsheetViewer.test.ts
Set-Location ..
```

Expected: module resolution failure.

- [ ] **Step 3: Implement virtualized read-only UI**

Load schema first, then 100-row pages. Selection stores server coordinates, and “针对所选范围提问” posts IDs/ranges rather than browser-provided cell text. Citation links include version, sheet, and range query parameters.

```typescript
const askSelection = () => api.post(
  `/documents/${documentId}/versions/${versionId}/spreadsheet-selection`,
  { sheet_key: activeSheet.value, ...selection.value },
)
```

- [ ] **Step 4: Run frontend tests/build**

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
```

Expected: PASS for sheet switch, paging, filter, selection, historical version, revoked ACL, and no-edit UI.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/api/spreadsheets.ts frontend/src/views/SpreadsheetViewer.vue frontend/src/components frontend/src/router/index.ts frontend/src/views/FileModeView.vue
git commit -m "feat: add spreadsheet viewer"
```

---

### Task 8: Existing Spreadsheet Migration and Verification

**Files:**
- Create: `rag/spreadsheets/migration.py`
- Test: `tests/test_spreadsheet_migration.py`
- Modify: `rag/api.py`
- Modify: `.env.example`
- Modify: `docs/deployment-guide.md`
- Modify: `docs/development-log-security-boundary-hardening.md`

**Interfaces:**
- Produces: `migrate_spreadsheet_versions(db: UserDB, versions_root: Path, datasets_root: Path) -> SpreadsheetMigrationReport`.

- [ ] **Step 1: Write failing idempotency test**

```python
def test_spreadsheet_migration_replaces_single_table_chunk_once(migration_fixture):
    first = migrate_spreadsheet_versions(**migration_fixture.args)
    second = migrate_spreadsheet_versions(**migration_fixture.args)
    assert first.migrated == 1
    assert second.unchanged == 1
    assert migration_fixture.vector_store.count_kind("row_group") > 0
    assert migration_fixture.vector_store.count_legacy_single_chunk() == 0
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_spreadsheet_migration.py -v
```

Expected: FAIL because migration is absent.

- [ ] **Step 3: Implement idempotent extraction-version migration**

Use `(document_version_id, extraction_version)` uniqueness. Migrate only `.xlsx`/`.csv` versions with accessible immutable sources. Failure preserves ordinary document availability and records `spreadsheet_extraction_failed` for operations.

```python
def needs_migration(db: UserDB, version_id: int, extraction_version: int) -> bool:
    dataset = db.get_structured_dataset_for_version(version_id)
    return dataset is None or dataset["extraction_version"] < extraction_version
```

- [ ] **Step 4: Run full verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/spreadsheets tests/test_spreadsheet_schema.py tests/test_spreadsheet_parser.py tests/test_spreadsheet_store.py tests/test_spreadsheet_extraction.py tests/test_spreadsheet_chunks.py tests/test_structured_query.py tests/test_spreadsheet_api.py tests/test_spreadsheet_migration.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
git status --short data qdrant_data
```

Expected: all checks pass and repository data directories remain unchanged.

- [ ] **Step 5: Document settings and commit**

Document all limits and the disabled sensitive-data flag, then:

```powershell
git add rag/spreadsheets/migration.py rag/api.py tests/test_spreadsheet_migration.py .env.example docs/deployment-guide.md docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify spreadsheet knowledge"
```
