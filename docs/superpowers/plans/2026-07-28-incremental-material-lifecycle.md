# Incremental Material Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add permission-safe incremental file replacement, multi-format extraction, archive confirmation, 30-day recycle recovery, material location/download, and confirmed natural-language organization for roughly 300 employees.

**Architecture:** Extend the trusted knowledge loop rather than creating a second file system. A single format-capability registry controls every upload/index entry point; streamed uploads create immutable versions and persistent jobs; workers build versioned derived artifacts and atomically switch SQLite current-version pointers. Metadata/full-text/vector location and organization proposals operate only on ACL-filtered logical documents.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite WAL/FTS5, Qdrant, OpenDataLoader PDF, PyMuPDF, RapidOCR, python-pptx, python-docx, openpyxl, xlrd, py7zr, rarfile, Vue 3, Pinia, TypeScript, Element Plus, pytest, Vitest

## Global Constraints

- Prerequisite: complete and verify `docs/superpowers/plans/2026-07-27-trusted-knowledge-loop.md` before this plan.
- Use one logical document/ACL identity and immutable `document_versions`; do not create a parallel permission model.
- SQLite `document_permissions.current_version_id` is the only current-version truth. Qdrant `is_current` remains repairable metadata.
- Identical file hashes create no version, extraction, OCR, embedding, or index job.
- Deleted documents are denied synchronously and remain restorable for exactly 30 days.
- The 30-day recycle policy does not purge normal superseded versions. Historical-version retention remains governed by the trusted knowledge loop until a later governance retention policy is approved.
- Ordinary files default to 50 MB. Archives default to 200 MB compressed, 1 GB expanded, 1,000 entries, nesting depth 2, and ratio 100:1.
- Never load an entire Office document or archive into HTTP-process memory.
- Extension, declared MIME, and file signature must agree with the format registry.
- Never execute macros, formula recalculation, embedded objects, external links, archive executables, or arbitrary converter commands.
- Image files are metadata-only archives with thumbnails. Do not OCR, embed, caption, or send images to a multimodal model.
- Scanned PDFs use local OCR only. Handwritten signatures never determine identity, ownership, ACL, or subject employee.
- Archive entries inherit the container ACL and can never expand access.
- Models propose organization changes but never construct SQL, physical paths, arbitrary URLs, or file IDs outside a backend-provided candidate set.
- Every destructive/metadata batch requires preview, permission recheck, idempotency, audit, optimistic concurrency, and conflict-safe rollback.
- Git repositories, NAS connectors, voice input, multimodal image understanding, and production payroll/attendance enablement remain outside this plan.
- Follow red-green-refactor TDD. Tests use temporary SQLite/files and fake Qdrant/OCR/converter/archive backends; they never touch production `data/` or `qdrant_data/`.

---

## File Structure

### New backend files

- `rag/materials/__init__.py` — public material-lifecycle interfaces.
- `rag/materials/models.py` — frozen Pydantic/dataclass contracts shared by all tasks.
- `rag/materials/formats.py` — one capability registry and signature/MIME resolution.
- `rag/materials/staging.py` — streamed, size-limited, hashed quarantine writes.
- `rag/materials/jobs.py` — persistent job enqueue/lease/retry/cancel operations.
- `rag/materials/extraction.py` — extractor registry and normalized extraction units.
- `rag/materials/pdf_ocr.py` — text-first PDF extraction and local OCR fallback.
- `rag/materials/presentation.py` — page-aware `.pptx` extraction.
- `rag/materials/legacy_office.py` — disabled-by-default isolated legacy conversion.
- `rag/materials/archives.py` — archive manifest validation, confirmation, and child diff.
- `rag/materials/embedding_cache.py` — separate bounded embedding-cache database.
- `rag/materials/incremental.py` — fingerprints, changed-unit planning, indexing, activation.
- `rag/materials/worker.py` — bounded persistent job worker and stale-job cancellation.
- `rag/materials/recycle.py` — tombstone, restore, purge, and audit-minimum retention.
- `rag/materials/locator.py` — ACL-first metadata/FTS/vector material location.
- `rag/materials/organizer.py` — typed preview, confirm, batch audit, and rollback.
- `rag/materials/api.py` — focused upload/job/archive/recycle/location/organization router.

### New tests

- `tests/test_material_schema.py`
- `tests/test_format_registry.py`
- `tests/test_streamed_staging.py`
- `tests/test_material_jobs.py`
- `tests/test_material_extraction.py`
- `tests/test_pdf_ocr.py`
- `tests/test_presentation_extraction.py`
- `tests/test_legacy_office.py`
- `tests/test_archive_import.py`
- `tests/test_incremental_material_index.py`
- `tests/test_material_recycle.py`
- `tests/test_material_locator.py`
- `tests/test_material_organizer.py`
- `tests/test_material_api.py`
- `tests/test_material_migration.py`

### Modified files

- `pyproject.toml` — pinned multi-format dependencies.
- `config.py` and `.env.example` — limits, worker/OCR/cache/converter settings.
- `rag/user_db.py` — schema migrations and focused persistence methods.
- `rag/api.py` — include the material router and remove duplicate format/upload logic.
- `rag/loader.py` — compatibility wrapper over the extractor registry.
- `rag/folder_indexer.py`, `rag/batch_importer.py`, `rag/knowledge_base.py` — consume the same registry/lifecycle service.
- `rag/vector_store.py`, `rag/models.py` — unit fingerprint and version filters.
- `rag/api_security.py`, `rag/permissions.py` — tombstone-aware checks and permission-version invalidation.
- `frontend/src/api/materials.ts` — typed material endpoints.
- `frontend/src/stores/files.ts` — upload/job/recycle/organization state.
- `frontend/src/views/FileModeView.vue`, `frontend/src/views/KnowledgeDetailView.vue` — queue, archive preview, recycle, locator, organization UI.
- `frontend/src/components/ArchiveManifestDialog.vue`
- `frontend/src/components/MaterialJobStatus.vue`
- `frontend/src/components/RecycleBinDialog.vue`
- `frontend/src/components/OrganizationPreviewDialog.vue`
- `frontend/src/components/__tests__/ArchiveManifestDialog.test.ts`
- `frontend/src/components/__tests__/MaterialJobStatus.test.ts`
- `frontend/src/components/__tests__/RecycleBinDialog.test.ts`
- `frontend/src/components/__tests__/OrganizationPreviewDialog.test.ts`
- `docs/deployment-guide.md`, `docs/development-log-security-boundary-hardening.md`.

---

### Task 1: Schema Migration and Unified Format Capabilities

**Files:**
- Create: `rag/materials/__init__.py`
- Create: `rag/materials/models.py`
- Create: `rag/materials/formats.py`
- Create: `tests/test_material_schema.py`
- Create: `tests/test_format_registry.py`
- Modify: `pyproject.toml`
- Modify: `rag/user_db.py`
- Modify: `config.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: trusted-loop `document_permissions`, `document_versions`, `UserDB.transaction(immediate=True)`.
- Produces: `FormatCapability`, `FormatResolution`, `MaterialLimits`, `MaterialState`, `IndexJobType`, `CitationLocation`.
- Produces: `build_default_format_registry() -> FormatCapabilityRegistry`.
- Produces: `FormatCapabilityRegistry.resolve(filename: str, declared_mime: str | None, signature: bytes) -> FormatResolution`.
- Produces: `UserDB.create_material_schema() -> None`.

- [ ] **Step 1: Write failing schema and registry tests**

```python
def test_material_schema_rebuilds_version_check_and_preserves_rows(legacy_version_db):
    legacy_version_db.seed_current_version(version_id=4)
    legacy_version_db.create_material_schema()
    assert legacy_version_db.get_version(4)["status"] == "current"
    legacy_version_db.set_version_status(4, "recycle_pending")
    assert legacy_version_db.foreign_key_check() == []


def test_all_entry_points_share_one_format_registry(default_format_registry):
    pptx = default_format_registry.resolve(
        "policy.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        pptx_signature(),
    )
    assert pptx.capability.key == "pptx"
    assert pptx.capability.extract_text is True
    assert pptx.capability.citation_kind == "slide"


def test_extension_mime_signature_mismatch_is_quarantined(default_format_registry):
    result = default_format_registry.resolve("invoice.pdf", "application/pdf", exe_signature())
    assert result.state == "format_mismatch"
    assert result.can_index is False
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_schema.py tests\test_format_registry.py -v
```

Expected: FAIL because material schema and registry do not exist.

- [ ] **Step 3: Add exact schema migration and registry contracts**

Add frozen models:

```python
class FormatCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str
    extensions: frozenset[str]
    mime_types: frozenset[str]
    magic_families: frozenset[str]
    archive: bool = True
    preview: bool = False
    extract_text: bool = False
    local_ocr: bool = False
    structured_query: bool = False
    semantic_index: bool = False
    archive_container: bool = False
    citation_kind: Literal["paragraph", "page", "slide", "cell_range", "metadata", "archive_entry"]


class FormatResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["supported", "archive_only", "temporarily_unavailable", "format_mismatch", "rejected"]
    capability: FormatCapability | None
    detected_mime: str
    reason_code: str
    can_index: bool
```

Register exactly:

- `txt`, `md`, `pdf`, `docx`, `xlsx`, `csv`, `pptx`.
- legacy `doc`, `xls`, `ppt`.
- metadata-only `jpg`, `jpeg`, `png`, `tif`, `tiff`, `bmp`.
- containers `zip`, `rar`, `7z`.

Add the pure-Python signature helper:

```toml
"filetype>=1.2,<2",
```

Add `document_permissions.logical_path`, `document_type`, `tags_json`, `subject_user_id`, `metadata_version`, `deleted_at`, `deleted_by`, `restore_until`, `container_document_id`, and `container_relative_path`.

Rebuild the SQLite `document_versions.status` CHECK to allow:

`staging | indexing | current | historical | recycle_pending | failed | purged`.

Before `BEGIN IMMEDIATE`, set `PRAGMA foreign_keys=OFF`; snapshot indexes/triggers; create the replacement table; copy named columns; compare row counts; recreate indexes/triggers; run `foreign_key_check`; commit only when empty; restore foreign keys in `finally`.

Create:

- `document_index_jobs` with unique active key, lease owner/expiry, attempt, available time, payload hash, and safe error code.
- `material_archive_manifests` and `material_archive_entries`.
- `material_organization_proposals/items/batches/batch_items`.
- FTS5 `material_search_fts(document_id UNINDEXED, version_id UNINDEXED, filename, logical_path, document_type, tags, extracted_text)`.

- [ ] **Step 4: Run schema/registry and existing migration tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_schema.py tests\test_format_registry.py tests\test_document_version_schema.py tests\test_existing_document_version_migration.py -v
```

Expected: PASS with preserved rows, indexes, triggers, and foreign keys.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml rag/materials/__init__.py rag/materials/models.py rag/materials/formats.py rag/user_db.py config.py tests/conftest.py tests/test_material_schema.py tests/test_format_registry.py
git commit -m "feat: define material format and schema contracts"
```

---

### Task 2: Streamed Staging and Persistent Debounced Jobs

**Files:**
- Create: `rag/materials/staging.py`
- Create: `rag/materials/jobs.py`
- Create: `tests/test_streamed_staging.py`
- Create: `tests/test_material_jobs.py`
- Modify: `rag/user_db.py`
- Modify: `config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `FormatCapabilityRegistry`, `MaterialLimits`, trusted `DocumentVersionStore`.
- Produces: `StagedMaterial`, `IndexJob`.
- Produces: `stream_upload_to_stage(upload: AsyncIterator[bytes], filename: str, declared_mime: str | None, root: Path, registry: FormatCapabilityRegistry, limits: MaterialLimits) -> StagedMaterial`.
- Produces: `enqueue_latest_index_job(db: UserDB, document_id: int, version_id: int, content_hash: str, not_before: float) -> int`.
- Produces: `claim_material_jobs(db: UserDB, worker_id: str, now: float, limit: int, lease_seconds: int) -> list[IndexJob]`.

- [ ] **Step 1: Write failing streaming, limit, dedupe, and lease tests**

```python
@pytest.mark.asyncio
async def test_streamed_stage_hashes_without_unbounded_read(tmp_path, registry):
    source = CountingAsyncChunks([b"a" * 1024] * 128)
    staged = await stream_upload_to_stage(
        source,
        "report.pdf",
        "application/pdf",
        tmp_path,
        registry,
        MaterialLimits(file_bytes=256 * 1024),
    )
    assert source.max_requested_chunk <= 64 * 1024
    assert staged.byte_size == 128 * 1024
    assert staged.sha256 == hashlib.sha256(b"a" * (128 * 1024)).hexdigest()


@pytest.mark.asyncio
async def test_oversize_stage_removes_only_its_temporary_file(tmp_path, registry):
    with pytest.raises(MaterialTooLarge):
        await stream_upload_to_stage(
            async_chunks([b"x" * 60_000]),
            "a.docx",
            DOCX_MIME,
            tmp_path,
            registry,
            MaterialLimits(file_bytes=50_000),
        )
    assert list(tmp_path.rglob("*")) == []


def test_repeated_replacement_debounces_to_latest_version(job_db):
    first = enqueue_latest_index_job(job_db, 7, 10, "a", not_before=100)
    second = enqueue_latest_index_job(job_db, 7, 11, "b", not_before=101)
    assert job_db.get_job(first)["status"] == "cancelled"
    assert job_db.get_job(second)["version_id"] == 11


def test_expired_lease_can_be_reclaimed_once(job_db):
    job_db.enqueue_job(document_id=7, version_id=11)
    first = claim_material_jobs(job_db, "worker-a", now=100, limit=1, lease_seconds=30)
    assert claim_material_jobs(job_db, "worker-b", now=110, limit=1, lease_seconds=30) == []
    second = claim_material_jobs(job_db, "worker-b", now=131, limit=1, lease_seconds=30)
    assert second[0].id == first[0].id
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_streamed_staging.py tests\test_material_jobs.py -v
```

Expected: FAIL because streaming and persistent jobs are absent.

- [ ] **Step 3: Implement bounded writes and compare-and-set leases**

Read at most 64 KiB per iteration, write to a unique stage path under the configured root, update SHA-256 and size, `fsync`, then resolve signature/MIME. On failure close and delete only that stage file.

Configure:

```python
material_file_max_bytes: int = 50 * 1024 * 1024
material_archive_max_bytes: int = 200 * 1024 * 1024
material_debounce_seconds: int = 300
material_worker_lease_seconds: int = 120
material_job_max_attempts: int = 5
material_job_queue_limit: int = 10_000
```

Enqueue/cancel in one `BEGIN IMMEDIATE`. Claim with one compare-and-set update:

```sql
UPDATE document_index_jobs
SET status='running', lease_owner=?, lease_expires_at=?, attempt=attempt+1
WHERE id=? AND status IN ('queued','retry')
  AND available_at<=? AND (lease_expires_at IS NULL OR lease_expires_at<?);
```

Do not delete stale jobs; retain `cancelled` rows for audit.

- [ ] **Step 4: Run focused and upload-security tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_streamed_staging.py tests\test_material_jobs.py tests\test_upload_security.py -v
```

Expected: PASS and no stage files remain after rejected uploads.

- [ ] **Step 5: Commit**

```powershell
git add rag/materials/staging.py rag/materials/jobs.py rag/user_db.py config.py .env.example tests/test_streamed_staging.py tests/test_material_jobs.py
git commit -m "feat: stream uploads into persistent material jobs"
```

---

### Task 3: Normalized Extraction, Local PDF OCR, and PPTX Pages

**Files:**
- Create: `rag/materials/extraction.py`
- Create: `rag/materials/pdf_ocr.py`
- Create: `rag/materials/presentation.py`
- Create: `tests/test_material_extraction.py`
- Create: `tests/test_pdf_ocr.py`
- Create: `tests/test_presentation_extraction.py`
- Modify: `pyproject.toml`
- Modify: `rag/loader.py`

**Interfaces:**
- Consumes: `FormatResolution`.
- Produces: `ExtractedMaterial`, `ExtractedUnit`, `LocalOcrEngine`, `MaterialExtractorRegistry`.
- Produces: `extract_material(path: Path, version_id: int, resolution: FormatResolution) -> ExtractedMaterial`.
- Produces compatibility: `rag.loader.load(file_path: str) -> str`.

- [ ] **Step 1: Write failing extraction and no-image-understanding tests**

```python
def test_pptx_extracts_slide_text_notes_and_table_with_page_citations(pptx_fixture):
    result = extract_presentation(pptx_fixture.path, version_id=8)
    assert [(u.location.kind, u.location.value) for u in result.units] == [
        ("slide", "1"),
        ("slide", "2"),
    ]
    assert "年度制度" in result.units[0].text
    assert "讲者备注" in result.units[0].text
    assert "部门 | 负责人" in result.units[1].text


def test_pptx_does_not_ocr_or_caption_embedded_image(pptx_fixture, forbidden_vision_client):
    result = extract_presentation(pptx_fixture.with_image_only_slide(), version_id=8)
    assert forbidden_vision_client.calls == []
    assert result.units[-1].text == ""


def test_pdf_uses_text_layer_before_local_ocr(pdf_fixture, fake_ocr):
    result = extract_pdf(pdf_fixture.text_pdf(), version_id=9, ocr=fake_ocr)
    assert "劳动合同" in result.units[0].text
    assert fake_ocr.calls == []


def test_scanned_pdf_uses_local_ocr_and_marks_uncertainty(pdf_fixture, fake_ocr):
    fake_ocr.result = OcrPage(text="姓名 张三", confidence=0.83, boxes=((10, 20, 50, 40),))
    result = extract_pdf(pdf_fixture.scan_pdf(), version_id=9, ocr=fake_ocr)
    assert result.units[0].source == "local_ocr"
    assert result.units[0].confidence == 0.83
    assert result.units[0].location.kind == "page_region"
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_extraction.py tests\test_pdf_ocr.py tests\test_presentation_extraction.py -v
```

Expected: FAIL because normalized units and extractors do not exist.

- [ ] **Step 3: Add pinned dependencies and exact extractor behavior**

Add:

```toml
"python-pptx>=1.0,<2",
"PyMuPDF>=1.26,<2",
"rapidocr-onnxruntime>=1.4,<2",
```

Define:

```python
@dataclass(frozen=True)
class ExtractedUnit:
    unit_key: str
    text: str
    location: CitationLocation
    source: Literal["native_text", "local_ocr", "metadata"]
    confidence: float
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class ExtractedMaterial:
    version_id: int
    parser_schema_version: str
    units: tuple[ExtractedUnit, ...]
    warnings: tuple[str, ...]
```

PDF flow:

1. Extract text per page.
2. OCR only pages whose normalized text has fewer than 20 non-whitespace characters.
3. Render at bounded 200 DPI; reject pages exceeding configured pixel count.
4. Run RapidOCR locally; never call the LLM/embedding client from OCR.
5. Store OCR confidence and bounding boxes, but never infer a subject from handwriting.

PPTX flow extracts slide title/body, speaker notes, and table cells in slide order. It records image count as metadata only.

Make `rag.loader.load()` join normalized units for legacy callers; remove its separate `SUPPORTED_EXTENSIONS`.

- [ ] **Step 4: Run extraction and legacy loader tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_extraction.py tests\test_pdf_ocr.py tests\test_presentation_extraction.py tests\test_loader.py -v
```

Expected: PASS; image-only PPTX slides produce no invented text.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml rag/materials/extraction.py rag/materials/pdf_ocr.py rag/materials/presentation.py rag/loader.py tests/test_material_extraction.py tests/test_pdf_ocr.py tests/test_presentation_extraction.py
git commit -m "feat: extract scanned PDFs and presentations locally"
```

---

### Task 4: Disabled-by-Default Legacy Office Conversion

**Files:**
- Create: `rag/materials/legacy_office.py`
- Create: `tests/test_legacy_office.py`
- Modify: `pyproject.toml`
- Modify: `rag/materials/formats.py`
- Modify: `rag/materials/extraction.py`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `docs/deployment-guide.md`

**Interfaces:**
- Consumes: `ExtractedMaterial`, extractor registry.
- Produces: `LegacyOfficeConverter.convert(source: Path, expected_kind: Literal["docx","xlsx","pptx"], work_root: Path) -> ConvertedArtifact`.
- Produces: `legacy_conversion_capability(settings) -> Literal["available","archive_only"]`.

- [ ] **Step 1: Write failing disabled, fixed-command, and failure-fallback tests**

```python
def test_legacy_conversion_is_archive_only_without_isolated_launcher(settings):
    settings.legacy_office_enabled = False
    assert legacy_conversion_capability(settings) == "archive_only"


def test_converter_uses_fixed_argv_and_unique_readonly_input(converter_fixture):
    artifact = converter_fixture.convert(Path("policy.ppt"), expected_kind="pptx")
    call = converter_fixture.runner.calls[0]
    assert call.shell is False
    assert call.network_disabled is True
    assert "--convert-to" in call.argv
    assert artifact.original_version_id == converter_fixture.version_id


def test_conversion_failure_keeps_original_archive_only(converter_fixture):
    converter_fixture.runner.fail(exit_code=2)
    result = converter_fixture.run()
    assert result.state == "archive_only"
    assert result.original_path.exists()
    assert result.derived_files == ()
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_legacy_office.py -v
```

Expected: FAIL because legacy conversion is absent.

- [ ] **Step 3: Implement an explicit isolation contract**

Defaults:

```python
legacy_office_enabled: bool = False
legacy_office_launcher: str = ""
legacy_office_timeout_seconds: int = 60
legacy_office_max_memory_mb: int = 512
```

The configured launcher is a single administrator-controlled executable, not a command string. Pass fixed argv, a unique read-only input copy, unique output directory, timeout, CPU/memory caps, and `network_disabled=True`. Reject launchers that cannot attest network isolation. Never use `shell=True`, user filenames in command options, shared LibreOffice profiles, or production directories.

For `.xls`, add `xlrd>=2.0,<3` and try non-executing cell extraction first. Only converter fallback may create `.xlsx`. Derived artifacts store source version ID, source hash, converter binary hash/version, parser version, and output hash. On any conversion/parse failure return an explicit archive-only result.

- [ ] **Step 4: Run converter, registry, and upload tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_legacy_office.py tests\test_format_registry.py tests\test_upload_security.py -v
```

Expected: PASS; a missing/unsafe launcher never prevents original-file archival.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml rag/materials/legacy_office.py rag/materials/formats.py rag/materials/extraction.py config.py .env.example tests/test_legacy_office.py docs/deployment-guide.md
git commit -m "feat: isolate legacy office conversion"
```

---

### Task 5: Safe Archive Manifest, Confirmation, and Child Version Diff

**Files:**
- Create: `rag/materials/archives.py`
- Create: `tests/test_archive_import.py`
- Modify: `pyproject.toml`
- Modify: `rag/materials/formats.py`
- Modify: `rag/materials/jobs.py`
- Modify: `rag/user_db.py`
- Modify: `config.py`

**Interfaces:**
- Consumes: format registry, streamed staging, immutable version service, job store.
- Produces: `ArchiveLimits`, `ArchiveManifest`, `ArchiveEntry`, `ArchiveImportResult`.
- Produces: `inspect_archive(path: Path, capability: FormatCapability, limits: ArchiveLimits) -> ArchiveManifest`.
- Produces: `confirm_archive_import(manifest_id: int, actor: dict, selected_entry_ids: tuple[int, ...], expected_version: int, idempotency_key: str) -> ArchiveImportResult`.

- [ ] **Step 1: Write failing traversal/bomb/confirmation/diff tests**

```python
@pytest.mark.parametrize("name", ["../secret.txt", "/absolute.txt", r"C:\evil.txt"])
def test_archive_rejects_escaping_paths(archive_fixture, name):
    manifest = inspect_archive(archive_fixture.zip_with(name, b"x"), ZIP_CAPABILITY, LIMITS)
    assert manifest.entries[0].state == "rejected"
    assert manifest.entries[0].reason_code == "unsafe_path"


def test_archive_limits_expansion_ratio_and_nested_depth(archive_fixture):
    manifest = inspect_archive(archive_fixture.ratio_bomb(101), ZIP_CAPABILITY, LIMITS)
    assert manifest.state == "rejected"
    assert manifest.reason_code == "compression_ratio_exceeded"


def test_archive_has_no_searchable_children_before_confirmation(archive_service):
    manifest = archive_service.upload("onboarding.zip", {"张三/合同.pdf": pdf_bytes()})
    assert archive_service.search("张三 合同") == []
    archive_service.confirm(manifest.id, selected=manifest.supported_ids, idempotency_key="arc-1")
    assert [item.relative_path for item in archive_service.search("张三 合同")] == ["张三/合同.pdf"]


def test_archive_replacement_diffs_children_atomically(archive_service):
    first = archive_service.confirm_upload({"a.pdf": b"A", "b.pdf": b"B"})
    archive_service.worker.fail_before_batch_activation()
    with pytest.raises(IndexingFailed):
        archive_service.confirm_replacement({"a.pdf": b"A", "b.pdf": b"B2", "c.pdf": b"C"})
    assert archive_service.current_children() == first.children
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_archive_import.py -v
```

Expected: FAIL because archive manifests and child lifecycle are absent.

- [ ] **Step 3: Implement streaming inspection and transactional batch activation**

Add:

```toml
"py7zr>=1.0,<2",
"rarfile>=4.2,<5",
```

Use `zipfile` for ZIP, `py7zr` for 7z, and `rarfile` only when its backend passes a startup capability check. Unsupported RAR backends produce `temporarily_unavailable`, never partial extraction.

Validate normalized Unicode relative paths, entry count, nesting, compressed/uncompressed bytes, ratio, symlinks, device files, encryption, magic/extension, and allowlisted inner formats before extraction. Never call `extractall`; stream each approved regular file to a unique stage child and reapply per-file limits/signature checks.

Persist manifest and entries before returning preview. Confirmation rechecks container edit permission, manifest expiry/status/version, selected entry IDs, and container current version. Use a unique `(manifest_id, idempotency_key)` result.

Each child gets a stable key:

```python
child_source_key = sha256(f"{container_document_id}:{normalized_relative_path}".encode()).hexdigest()
```

Replacement compares stable key + content hash. Stage all new child versions and vectors, then switch container and every changed child pointer in one `BEGIN IMMEDIATE`. New rows remain invisible until commit. Enqueue compensating cleanup for pre-commit artifacts; never hide the old container on failure.

- [ ] **Step 4: Run archive, version, ACL, and path tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_archive_import.py tests\test_document_versions.py tests\test_upload_security.py tests\test_document_security_boundaries.py tests\test_kb_file_lifecycle.py -v
```

Expected: PASS for ZIP/7z fakes and unavailable-RAR behavior.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml rag/materials/archives.py rag/materials/formats.py rag/materials/jobs.py rag/user_db.py config.py tests/test_archive_import.py
git commit -m "feat: confirm safe archive child imports"
```

---

### Task 6: Incremental Units, Bounded Embedding Cache, and Atomic Worker

**Files:**
- Create: `rag/materials/embedding_cache.py`
- Create: `rag/materials/incremental.py`
- Create: `rag/materials/worker.py`
- Create: `tests/test_incremental_material_index.py`
- Modify: `rag/models.py`
- Modify: `rag/vector_store.py`
- Modify: `rag/knowledge_base.py`
- Modify: `rag/user_db.py`
- Modify: `config.py`

**Interfaces:**
- Consumes: `ExtractedMaterial`, `IndexJob`, trusted `_activate_document_version_in_tx`.
- Produces: `UnitFingerprint`, `IncrementalIndexPlan`, `WorkerServices`, `JobResult`, `MaterialWorker`.
- Produces: `plan_incremental_index(previous: ExtractedMaterial | None, current: ExtractedMaterial, parser_schema_version: str) -> IncrementalIndexPlan`.
- Produces: `EmbeddingCache.get_or_embed(fingerprints: tuple[str, ...], texts: tuple[str, ...], model_key: str, dimension: int) -> list[list[float]]`.
- Produces: `process_index_job(job: IndexJob, services: WorkerServices) -> JobResult`.

- [ ] **Step 1: Write failing changed-unit, cache, stale-job, and atomicity tests**

```python
def test_one_changed_unit_embeds_only_that_unit(incremental_fixture):
    previous = extracted(["unchanged", "old"])
    current = extracted(["unchanged", "new"])
    result = incremental_fixture.index(previous, current)
    assert incremental_fixture.embedder.texts == ["new"]
    assert result.reused_units == 1
    assert result.embedded_units == 1


def test_cache_key_includes_model_dimension_and_parser(cache_fixture):
    first = cache_fixture.embed("same", model="bge-m3", dimension=1024, parser="v1")
    second = cache_fixture.embed("same", model="bge-m3", dimension=1024, parser="v1")
    third = cache_fixture.embed("same", model="bge-m3", dimension=1024, parser="v2")
    assert first == second
    assert cache_fixture.embedder.calls == 2
    assert third != first


def test_stale_job_cannot_activate_over_newer_version(worker_fixture):
    worker_fixture.db.set_desired_version(document_id=7, version_id=12)
    result = worker_fixture.process(job(version_id=11))
    assert result.status == "cancelled"
    assert worker_fixture.db.current_version_id(7) != 11


def test_sqlite_failure_removes_only_new_points(worker_fixture):
    worker_fixture.db.fail_activation()
    with pytest.raises(ActivationFailed):
        worker_fixture.process(job(version_id=12))
    assert worker_fixture.vector_store.version_ids() == {11}
    assert worker_fixture.db.current_version_id(7) == 11
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_incremental_material_index.py -v
```

Expected: FAIL because unit planning/cache/worker do not exist.

- [ ] **Step 3: Implement deterministic fingerprints and worker ordering**

Fingerprint:

```python
@dataclass(frozen=True)
class UnitFingerprint:
    unit_key: str
    digest: str
    unchanged_from_version_id: int | None = None


def unit_fingerprint(text: str, parser_schema_version: str) -> str:
    normalized = normalize_unit_text(text)
    return hashlib.sha256(f"{parser_schema_version}\0{normalized}".encode("utf-8")).hexdigest()
```

Use a separate configured `embedding_cache.sqlite3` with primary key `(fingerprint, model_key, dimension)`, vector BLOB, created/last-used time, hit count, and byte size. Store no ACL, source path, filename, question, or display text. Apply a configured 10 GB LRU cap during low-priority cleanup.

Worker order:

1. Claim lease and verify job version is still desired.
2. Extract and fingerprint.
3. Resolve cached/new embeddings with max embedding concurrency 4.
4. Write all new Qdrant points with the target `document_version_id`.
5. Write/update current-version FTS rows inside the activation transaction.
6. Recheck desired version and call `_activate_document_version_in_tx`.
7. Commit, then repair advisory Qdrant flags.
8. On pre-commit failure delete only target-version points/FTS/artifacts.
9. On post-commit repair failure enqueue `repair_vector_flags`.

Heartbeat the lease; after five retryable failures mark `failed` with a safe code. Never persist exception text containing physical paths or document content.

- [ ] **Step 4: Run incremental, retrieval, Qdrant, and pipeline tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_incremental_material_index.py tests\test_versioned_retrieval.py tests\test_vector_store.py tests\test_pipeline.py tests\test_concurrency.py -v
```

Expected: PASS; stale `is_current` flags cannot change SQLite-selected results.

- [ ] **Step 5: Commit**

```powershell
git add rag/materials/embedding_cache.py rag/materials/incremental.py rag/materials/worker.py rag/models.py rag/vector_store.py rag/knowledge_base.py rag/user_db.py config.py tests/test_incremental_material_index.py
git commit -m "feat: index only changed material units"
```

---

### Task 7: Tombstone, Restore, and Idempotent Purge

**Files:**
- Create: `rag/materials/recycle.py`
- Create: `tests/test_material_recycle.py`
- Modify: `rag/materials/jobs.py`
- Modify: `rag/user_db.py`
- Modify: `rag/api_security.py`
- Modify: `rag/permissions.py`
- Modify: `rag/vector_store.py`

**Interfaces:**
- Consumes: job store, version store, permission-version invalidation.
- Produces: `delete_to_recycle(document_id: int, actor: dict, now: float) -> RecycleRecord`.
- Produces: `restore_from_recycle(document_id: int, actor: dict, expected_metadata_version: int, now: float) -> RestoreResult`.
- Produces: `purge_document(document_id: int, now: float) -> PurgeResult`.

- [ ] **Step 1: Write failing immediate-denial, restore, and purge tests**

```python
def test_delete_denies_search_open_download_and_notification_immediately(recycle_fixture):
    recycle_fixture.delete(document_id=7, actor="owner", now=100)
    assert recycle_fixture.search("contract") == []
    assert recycle_fixture.open(7).status_code == 404
    assert recycle_fixture.download(7).status_code == 404
    assert recycle_fixture.deliver_notification(7).created == 0


def test_restore_before_thirty_days_preserves_version_and_acl(recycle_fixture):
    before = recycle_fixture.snapshot(7)
    recycle_fixture.delete(7, now=100)
    recycle_fixture.restore(7, now=100 + 29 * 86400)
    assert recycle_fixture.current_version_id(7) == before.current_version_id
    assert recycle_fixture.acl(7) == before.acl


def test_purge_is_idempotent_and_keeps_minimal_citation_audit(recycle_fixture):
    recycle_fixture.delete(7, now=100)
    first = recycle_fixture.purge(7, now=100 + 31 * 86400)
    second = recycle_fixture.purge(7, now=100 + 32 * 86400)
    assert first.removed_bytes > 0
    assert second.removed_bytes == 0
    assert recycle_fixture.citation_state(7) == "purged_by_retention_policy"
    assert recycle_fixture.version_file_exists(7) is False
    assert recycle_fixture.vector_points(7) == []
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_recycle.py -v
```

Expected: FAIL because current deletion physically removes records and has no recycle state.

- [ ] **Step 3: Implement tombstone-first lifecycle**

`delete_to_recycle` performs one transaction:

- require edit/delete permission;
- compare-and-set `metadata_version`;
- set `deleted_at`, `deleted_by`, `restore_until=now+30*86400`;
- set current version `recycle_pending`;
- bump affected users' permission versions;
- enqueue tombstone/purge jobs;
- append an identifier-only audit event.

Every `require_doc_view/edit`, retrieval candidate, download, citation opening, notification delivery, and organization candidate query must synchronously reject `deleted_at IS NOT NULL`.

Restore allows only deleter, owner, or admin before deadline, clears tombstone fields, restores the prior pointer/status, bumps permission versions, and enqueues repair/reindex only when artifacts are missing.

Purge verifies tombstone/deadline again, removes derived files/structured stores/points/current FTS, then immutable version files, and records `purged`. Preserve document/version IDs, uploader, timestamps, hashes, audit, and citation state—never the purged text preview.

- [ ] **Step 4: Run recycle and security regression tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_recycle.py tests\test_document_security_boundaries.py tests\test_upload_security.py tests\test_answer_citations.py -v
```

Expected: PASS with synchronous denial before asynchronous vector cleanup.

- [ ] **Step 5: Commit**

```powershell
git add rag/materials/recycle.py rag/materials/jobs.py rag/user_db.py rag/api_security.py rag/permissions.py rag/vector_store.py tests/test_material_recycle.py
git commit -m "feat: add restorable material deletion"
```

---

### Task 8: ACL-First Material Location and Authorized Downloads

**Files:**
- Create: `rag/materials/locator.py`
- Create: `tests/test_material_locator.py`
- Modify: `rag/user_db.py`
- Modify: `rag/versioned_retrieval.py`
- Modify: `rag/pipeline.py`
- Modify: `rag/citations.py`

**Interfaces:**
- Consumes: current version IDs, ACL helpers, FTS rows, semantic retriever.
- Produces: `MaterialLocationQuery`, `MaterialLocationResult`, `MaterialLocator`.
- Produces: `MaterialLocator.locate(query: MaterialLocationQuery, user: dict) -> list[MaterialLocationResult]`.
- Produces: `material_result_response(result: MaterialLocationResult, user: dict) -> dict`.

- [ ] **Step 1: Write failing metadata/semantic/ACL/download tests**

```python
def test_locate_my_onboarding_material_combines_exact_and_semantic(locator_fixture):
    locator_fixture.seed(
        filename="劳动合同扫描件.pdf",
        subject_user_id=locator_fixture.alice.id,
        document_type="onboarding",
        logical_path="我的人事资料/入职材料",
        ocr_text="保密义务",
    )
    result = locator_fixture.locate("我入职时签过的保密资料", user="alice")
    assert result[0].logical_path == "我的人事资料/入职材料"
    assert result[0].citation.location.kind == "page_region"


def test_invisible_filename_never_reaches_locator_model(locator_fixture):
    locator_fixture.seed(filename="张三工资调整.pdf", visible_to=["hr"])
    locator_fixture.locate("张三资料", user="alice")
    assert "张三工资调整.pdf" not in locator_fixture.model_input


def test_download_action_is_backend_generated_and_rechecks_acl(locator_fixture):
    result = locator_fixture.locate("我的合同", user="alice")[0]
    response = locator_fixture.serialize(result, user="alice")
    assert response["download_url"].startswith("/documents/")
    locator_fixture.revoke(result.document_id, "alice")
    assert locator_fixture.get(response["download_url"], user="alice").status_code == 403
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_locator.py -v
```

Expected: FAIL because the locator does not exist.

- [ ] **Step 3: Implement ACL-first hybrid location**

Define:

```python
class MaterialLocationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: Annotated[str, Field(min_length=1, max_length=500)]
    subject: Literal["self"] | int | None = None
    document_types: tuple[str, ...] = ()
    uploaded_after: datetime | None = None
    logical_path_prefix: str | None = None
    top_k: Annotated[int, Field(ge=1, le=50)] = 10
```

Resolve the user's visible, non-deleted current document/version ID set first. Apply exact subject/type/path/date filters and FTS only within that set, then semantic retrieval for the same allowed version IDs. Reciprocal-rank merge exact/FTS/semantic results, but exact employee ID/name and document type outrank similarity. A low-confidence OCR-extracted subject is a candidate label only.

The model may summarize already-authorized result metadata; backend serialization constructs version open/download routes. Responses include logical path, filename, type, updated time, current version, OCR warning, and citation—never physical paths.

- [ ] **Step 4: Run locator, pipeline, citation, and ACL tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_locator.py tests\test_versioned_retrieval.py tests\test_pipeline.py tests\test_answer_citations.py tests\test_document_security_boundaries.py -v
```

Expected: PASS and model-input assertions contain no invisible metadata.

- [ ] **Step 5: Commit**

```powershell
git add rag/materials/locator.py rag/user_db.py rag/versioned_retrieval.py rag/pipeline.py rag/citations.py tests/test_material_locator.py
git commit -m "feat: locate authorized enterprise materials"
```

---

### Task 9: Typed Organization Preview, Confirmation, and Rollback

**Files:**
- Create: `rag/materials/organizer.py`
- Create: `tests/test_material_organizer.py`
- Modify: `rag/user_db.py`
- Modify: `rag/generator.py`

**Interfaces:**
- Consumes: locator candidate IDs, ACL helpers, logical metadata versions.
- Produces: `OrganizationInstruction`, `OrganizationProposal`, `OrganizationItem`, `OrganizationBatchResult`.
- Produces: `generate_structured(messages: list[dict], response_model: type[T], temperature: float = 0.0) -> T`.
- Produces: `create_organization_preview(instruction: OrganizationInstruction, actor: dict) -> OrganizationProposal`.
- Produces: `confirm_organization(proposal_id: int, selected_item_ids: tuple[int, ...], actor: dict, expected_version: int, idempotency_key: str) -> OrganizationBatchResult`.
- Produces: `rollback_organization(batch_id: int, actor: dict, idempotency_key: str) -> OrganizationBatchResult`.

- [ ] **Step 1: Write failing bounded-output/idempotency/concurrency tests**

```python
def test_model_cannot_add_file_outside_backend_candidates(organizer_fixture):
    organizer_fixture.model.return_value = {
        "items": [{"document_id": 999, "target_path": "我的人事资料"}]
    }
    preview = organizer_fixture.preview("把合同放入入职材料", user="alice")
    assert preview.executable_items == ()
    assert preview.rejected_items[0].reason_code == "unknown_candidate"


def test_preview_does_not_change_metadata(organizer_fixture):
    before = organizer_fixture.snapshot()
    organizer_fixture.preview("把今年合同放到入职材料", user="alice")
    assert organizer_fixture.snapshot() == before


def test_confirmation_is_idempotent_and_rechecks_acl(organizer_fixture):
    proposal = organizer_fixture.preview("整理合同", user="alice")
    first = organizer_fixture.confirm(proposal.id, key="org-1")
    second = organizer_fixture.confirm(proposal.id, key="org-1")
    assert first.batch_id == second.batch_id
    organizer_fixture.revoke_edit(proposal.items[0].document_id, "alice")
    conflict = organizer_fixture.confirm(proposal.id, key="org-2")
    assert conflict.items[0].status == "permission_conflict"


def test_rollback_does_not_overwrite_later_manual_change(organizer_fixture):
    batch = organizer_fixture.confirm_preview(target_path="入职材料")
    organizer_fixture.manual_move(batch.document_id, "已归档")
    result = organizer_fixture.rollback(batch.id, key="undo-1")
    assert result.items[0].status == "metadata_conflict"
    assert organizer_fixture.path(batch.document_id) == "已归档"
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_organizer.py -v
```

Expected: FAIL because organization contracts and persistence do not exist.

- [ ] **Step 3: Implement typed bounded proposals and compare-and-set writes**

Use `ConfigDict(extra="forbid", frozen=True)` for every model-facing request/result. `OrganizationInstruction` contains text plus optional user-selected document IDs; the backend supplies only authorized candidate IDs and allowed target directories/types/tags.

The LLM output contains only candidate ID, target logical path, add/remove tags, document type, reason, and confidence. Reject unknown IDs, illegal paths, unknown types/tags, ACL failures, deleted/current-version changes, and confidence below 0.70. Store canonical request/output hashes. Preview expires after 15 minutes.

`generate_structured` sends a JSON-only instruction, requests JSON-object output, removes no arbitrary prose, parses exactly one JSON object, and validates it with the provided Pydantic model. Validation failure returns `structured_output_invalid`; it never evaluates Python, repairs JSON with `eval`, or accepts unknown fields.

Confirmation checks idempotency first, then proposal actor/status/expiry/version, then per-item edit permission and metadata version. Each executable item updates logical metadata with:

```sql
UPDATE document_permissions
SET logical_path=?, tags_json=?, document_type=?, metadata_version=metadata_version+1
WHERE id=? AND metadata_version=? AND deleted_at IS NULL;
```

Persist before/after values and audit in the same item transaction. Partial item conflicts do not roll back unrelated successful items. Rollback uses the batch's recorded `after_metadata_version`; changed fields return conflict and are not overwritten. Neither operation changes ACL, historical versions, or physical files.

- [ ] **Step 4: Run organizer, permission, and audit tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_organizer.py tests\test_document_security_boundaries.py tests\test_user_db.py -v
```

Expected: PASS with no unconfirmed mutation.

- [ ] **Step 5: Commit**

```powershell
git add rag/materials/organizer.py rag/user_db.py rag/generator.py tests/test_material_organizer.py
git commit -m "feat: preview and confirm material organization"
```

---

### Task 10: Material API and User Interface

**Files:**
- Create: `rag/materials/api.py`
- Create: `tests/test_material_api.py`
- Create: `frontend/src/api/materials.ts`
- Create: `frontend/src/components/ArchiveManifestDialog.vue`
- Create: `frontend/src/components/MaterialJobStatus.vue`
- Create: `frontend/src/components/RecycleBinDialog.vue`
- Create: `frontend/src/components/OrganizationPreviewDialog.vue`
- Create: `frontend/src/components/__tests__/ArchiveManifestDialog.test.ts`
- Create: `frontend/src/components/__tests__/MaterialJobStatus.test.ts`
- Create: `frontend/src/components/__tests__/RecycleBinDialog.test.ts`
- Create: `frontend/src/components/__tests__/OrganizationPreviewDialog.test.ts`
- Modify: `rag/api.py`
- Modify: `rag/folder_indexer.py`
- Modify: `rag/batch_importer.py`
- Modify: `frontend/src/stores/files.ts`
- Modify: `frontend/src/views/FileModeView.vue`
- Modify: `frontend/src/views/KnowledgeDetailView.vue`
- Modify: `frontend/src/api/__tests__/files.test.ts`
- Modify: `frontend/src/stores/__tests__/files.test.ts`

**Interfaces:**
- Consumes: Tasks 1–9 services.
- Produces: upload/job/archive/recycle/location/organization routes and typed UI.

- [ ] **Step 1: Write failing API permission and frontend interaction tests**

```python
def test_upload_returns_queued_version_without_blocking_for_index(isolated_api, user_headers):
    response = isolated_api.post(
        "/materials/upload",
        headers=user_headers,
        files={"file": ("policy.pptx", pptx_bytes(), PPTX_MIME)},
    )
    assert response.status_code == 202
    assert response.json()["status"] in {"queued", "waiting_for_capacity"}
    assert response.json()["version_id"]


def test_archive_confirm_requires_owner_or_editor(isolated_api, viewer_headers, archive_manifest):
    response = isolated_api.post(
        f"/materials/archives/{archive_manifest.id}/confirm",
        headers=viewer_headers,
        json={"selected_entry_ids": archive_manifest.supported_ids, "expected_version": 1, "idempotency_key": "x"},
    )
    assert response.status_code == 403


def test_location_response_never_contains_physical_path(isolated_api, user_headers):
    response = isolated_api.post("/materials/locate", headers=user_headers, json={"text": "我的合同"})
    assert response.status_code == 200
    assert "file_path" not in response.text
    assert "qdrant" not in response.text.lower()
```

```typescript
it('shows archive manifest before confirmation', async () => {
  api.upload.mockResolvedValue({ status: 'awaiting_archive_confirmation', manifestId: 7 })
  render(FileModeView)
  await uploadFile('onboarding.zip')
  expect(screen.getByText('确认压缩包内容')).toBeTruthy()
  expect(api.confirmArchive).not.toHaveBeenCalled()
})

it('shows organization preview and only mutates after confirm', async () => {
  render(FileModeView)
  await typeInstruction('把今年合同放到入职材料')
  expect(screen.getByText('将修改以下资料')).toBeTruthy()
  expect(api.confirmOrganization).not.toHaveBeenCalled()
  await fireEvent.click(screen.getByRole('button', { name: '确认整理' }))
  expect(api.confirmOrganization).toHaveBeenCalledTimes(1)
})
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_api.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src\components\__tests__ src\views\__tests__\FileModeView.test.ts
Set-Location ..
```

Expected: FAIL because routes and components are absent.

- [ ] **Step 3: Add focused routes and UI states**

Routes:

- `GET /materials/capabilities`
- `POST /materials/upload` (`202`)
- `GET /materials/jobs/{job_id}`
- `GET /materials/archives/{manifest_id}`
- `POST /materials/archives/{manifest_id}/confirm`
- `DELETE /materials/{document_id}` (recycle)
- `POST /materials/{document_id}/restore`
- `GET /materials/recycle`
- `POST /materials/locate`
- `POST /materials/organization/preview`
- `POST /materials/organization/{proposal_id}/confirm`
- `POST /materials/organization/batches/{batch_id}/rollback`

Every request model forbids extra fields. The router authenticates first, caps request size by streamed staging, delegates authorization to services, and maps stable errors to `400/403/404/409/413/422/429/503`. Do not return exception strings.

Remove `SUPPORTED_EXTENSIONS` and direct `await file.read(MAX_FILE_SIZE + 1)` from every API upload route. Compatibility endpoints call the same material upload service. Folder indexing and batch import query the shared registry.

Frontend:

- shows capability/size guidance from backend;
- reports queued/running/failed/archive-only state;
- archive dialog displays selected/rejected entries and reasons;
- recycle dialog displays restore deadline;
- locator displays logical path and backend-provided open/download action;
- organizer displays item-level reason/confidence/conflicts and rollback status;
- never renders physical paths.

- [ ] **Step 4: Run API/frontend/security/build verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_api.py tests\test_upload_security.py tests\test_api_security.py tests\test_kb_security_boundaries.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
```

Expected: PASS with a successful frontend production build.

- [ ] **Step 5: Commit**

```powershell
git add rag/materials/api.py rag/api.py rag/folder_indexer.py rag/batch_importer.py tests/test_material_api.py frontend/src/api/materials.ts frontend/src/components/ArchiveManifestDialog.vue frontend/src/components/MaterialJobStatus.vue frontend/src/components/RecycleBinDialog.vue frontend/src/components/OrganizationPreviewDialog.vue frontend/src/components/__tests__/ArchiveManifestDialog.test.ts frontend/src/components/__tests__/MaterialJobStatus.test.ts frontend/src/components/__tests__/RecycleBinDialog.test.ts frontend/src/components/__tests__/OrganizationPreviewDialog.test.ts frontend/src/stores/files.ts frontend/src/views/FileModeView.vue frontend/src/views/KnowledgeDetailView.vue frontend/src/api/__tests__/files.test.ts frontend/src/stores/__tests__/files.test.ts
git commit -m "feat: expose incremental material workflows"
```

---

### Task 11: Migration, Operations, Full Verification, and Handoff

**Files:**
- Create: `tests/test_material_migration.py`
- Modify: `rag/materials/worker.py`
- Modify: `rag/api.py`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `docs/deployment-guide.md`
- Modify: `docs/development-log-security-boundary-hardening.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: `migrate_material_metadata(db: UserDB) -> MaterialMigrationReport`.
- Produces: bounded worker startup/shutdown and deployment/rollback instructions.

- [ ] **Step 1: Write failing idempotent migration and worker-shutdown tests**

```python
def test_material_migration_is_idempotent_and_does_not_reembed(legacy_material_fixture):
    first = migrate_material_metadata(legacy_material_fixture.db)
    second = migrate_material_metadata(legacy_material_fixture.db)
    assert first.migrated_documents == 1
    assert second.migrated_documents == 0
    assert legacy_material_fixture.embedder.calls == []
    assert legacy_material_fixture.document()["logical_path"] == ""


def test_shutdown_releases_leases_without_losing_jobs(worker_fixture):
    worker_fixture.start_one_job()
    worker_fixture.shutdown(timeout=5)
    job = worker_fixture.db.get_job(worker_fixture.job_id)
    assert job["status"] in {"queued", "retry"}
    assert job["lease_owner"] is None
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_material_migration.py -v
```

Expected: FAIL because migration/startup/shutdown are absent.

- [ ] **Step 3: Add startup migration, bounded workers, and deployment controls**

Migration:

- initializes missing logical metadata without changing ACL/owner/current version;
- indexes only metadata into FTS and does not re-embed existing documents;
- detects and records format capability from signature where the source exists;
- marks missing/unsupported sources explicitly without deleting records;
- commits each logical document independently and reports counts only.

Startup order:

1. Complete trusted-loop/material schema migrations.
2. Validate configured roots are safe children.
3. Build capability registry and report unavailable optional backends.
4. Open job/cache databases.
5. Start two parser/OCR workers and four bounded embedding slots.
6. Do not auto-enable legacy conversion without the isolated launcher contract.

Shutdown stops claims, heartbeats in-flight work, waits up to the configured timeout, then atomically returns unfinished jobs to retry and releases leases.

Deployment guide must include dependency installation, local OCR model cache preparation, legacy converter isolation requirements, archive limits, worker sizing, backup coverage for material/job/cache metadata, staged rollout, metrics, and rollback that disables new uploads while preserving already stored versions.

- [ ] **Step 4: Run the full verification matrix**

Backend:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check `
  rag\materials `
  rag\api.py `
  rag\loader.py `
  rag\folder_indexer.py `
  rag\batch_importer.py `
  rag\knowledge_base.py `
  rag\user_db.py `
  rag\api_security.py `
  rag\permissions.py `
  rag\vector_store.py `
  tests\test_material_schema.py `
  tests\test_format_registry.py `
  tests\test_streamed_staging.py `
  tests\test_material_jobs.py `
  tests\test_material_extraction.py `
  tests\test_pdf_ocr.py `
  tests\test_presentation_extraction.py `
  tests\test_legacy_office.py `
  tests\test_archive_import.py `
  tests\test_incremental_material_index.py `
  tests\test_material_recycle.py `
  tests\test_material_locator.py `
  tests\test_material_organizer.py `
  tests\test_material_api.py `
  tests\test_material_migration.py
```

Frontend:

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
```

Repository hygiene:

```powershell
git diff --check
git status --short data qdrant_data
```

Expected: backend tests, focused Ruff, frontend tests/build, and diff check pass; production data directories have no test residue.

- [ ] **Step 5: Record evidence and commit**

Record exact dependency versions, schema migration counts, backend pass count, frontend pass count/build result, unavailable optional backends, worker limits, and any pre-existing out-of-scope warnings in the development log.

```powershell
git add config.py .env.example rag/materials/worker.py rag/api.py tests/test_material_migration.py docs/deployment-guide.md docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify incremental material lifecycle"
```

---

## Execution Checkpoints

- After Task 2: review schema transaction ownership, streaming cleanup, job lease/idempotency, and queue backpressure before adding parsers.
- After Task 5: review archive path/ratio/encryption handling and container/child atomicity before enabling archives in UI.
- After Task 7: review synchronous tombstone enforcement across every read path before running purge.
- After Task 9: review model-output allowlists, ACL rechecks, optimistic concurrency, audit, and rollback before exposing organization confirmation.
- After Task 11: stop and review the full evidence log before advancing to answer reliability.

## Deferred Follow-ups

- Git repository ingestion and symbol-aware code search require a separate approved design.
- NAS/SMB/WebDAV/DingTalk source adapters consume the material-source boundary in a later design.
- Multimodal image understanding and voice input remain disabled.
