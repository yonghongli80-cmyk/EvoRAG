# Trusted Knowledge Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task in the user's workbench. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add document ownership, durable versions, traceable citations, employee correction feedback, owner tasks, and a role-aware knowledge-operations page while keeping future NAS integration behind a source adapter.

**Architecture:** Keep `document_permissions` as the logical document and ACL identity, add immutable `document_versions`, and route every upload through one version service consuming a source-neutral `SourceObject`. Persist answer citations against assistant messages, derive feedback tasks through deterministic rules, and expose operations through a focused FastAPI router and Vue page.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite WAL, Qdrant, pytest, Vue 3, Pinia, TypeScript, Element Plus, Vitest

## Global Constraints

- Start from commits `6f41cd1` and `69a9a68`; the verified baseline collects 611 Python tests.
- Execute this plan inline with `superpowers:executing-plans` in the user's workbench; do not dispatch subagents unless the user changes that preference.
- New logical documents assign `owner_id` to the first uploader; overwriting never changes the owner.
- Administrators can reassign document owners and task assignees.
- Duplicate actions are exactly `reject`, `overwrite`, and `keep`; the UI labels are `跳过`, `覆盖`, and `保留`.
- Overwrite creates an immutable version and never physically overwrites or deletes the previous version.
- Identical-content overwrite returns `status="unchanged"` and creates no version.
- Current valid results are searched first. Historical or confirmed-outdated results are used only when ACL-filtered current results are fewer than `min(top_k, 3)` and receive weight `0.35`.
- `outdated` feedback creates or increments a task immediately.
- `not_useful` feedback creates a task only after 3 distinct users submit the same normalized question/document pair within 30 days.
- Task due time is 7 days. Knowledge bases with no current-version update for 180 days are stale.
- Feedback submitter identity is visible only to the submitter, assigned document owner, task assignee, and administrators.
- Physical paths, source credentials, vector IDs, and internal exception text never appear in API responses.
- Existing `/analysis` routes and data remain intact; only the third main-navigation entry changes to `/operations`.
- NAS, SMB, WebDAV, DingTalk, department/position inheritance, departure handover, and external notifications are outside this plan.
- All backend feature work follows red-green-refactor TDD and uses temporary SQLite/files/Qdrant state.
- Do not re-enable final-answer caching.

---

## File Structure

### New backend files

- `rag/document_sources/__init__.py` — exports source-neutral document types.
- `rag/document_sources/base.py` — `SourceObject` and `DocumentSource` protocol.
- `rag/document_sources/upload.py` — local browser-upload adapter.
- `rag/document_version_store.py` — stage/final paths, promotion, version-file cleanup.
- `rag/document_versions.py` — duplicate resolution and atomic version lifecycle.
- `rag/versioned_retrieval.py` — current-first retrieval and historical fallback.
- `rag/citations.py` — citation serialization, persistence, and safe response mapping.
- `rag/knowledge_feedback.py` — feedback normalization, validation, and task trigger.
- `rag/knowledge_tasks.py` — aggregation, task state machine, visibility, and metrics.
- `rag/operations_api.py` — version, owner, feedback, task, and operations routes.
- `tests/test_document_version_schema.py`
- `tests/test_document_version_store.py`
- `tests/test_document_versions.py`
- `tests/test_versioned_retrieval.py`
- `tests/test_answer_citations.py`
- `tests/test_knowledge_feedback.py`
- `tests/test_knowledge_tasks.py`
- `tests/test_operations_api.py`
- `tests/test_existing_document_version_migration.py`

### Modified backend files

- `config.py` — version root and operations thresholds.
- `rag/user_db.py` — schema migration and focused persistence methods.
- `rag/models.py` — version-aware `Chunk`.
- `rag/chunker.py` — section-title propagation.
- `rag/vector_store.py` — version payload, filters, and version-specific mutations.
- `rag/knowledge_base.py` — version-aware add/remove/search calls.
- `rag/pipeline.py` — current-first retrieval and citation-rich sources.
- `rag/api.py` — include operations router; route all upload entry points through version service; emit assistant message IDs.
- `rag/api_security.py` — owner/version authorization helpers.
- `tests/conftest.py` — isolated version root and operations fixtures.
- Existing upload, pipeline, stream, permission, and KB lifecycle tests — preserve prior guarantees.

### New frontend files

- `frontend/src/api/documents.ts` — duplicate/version/owner endpoints and types.
- `frontend/src/api/knowledgeOperations.ts` — feedback/task/summary endpoints and types.
- `frontend/src/components/DuplicateDocumentDialog.vue`
- `frontend/src/components/DocumentVersionsDialog.vue`
- `frontend/src/components/CitationList.vue`
- `frontend/src/components/KnowledgeFeedbackDialog.vue`
- `frontend/src/views/KnowledgeOperationsView.vue`
- Corresponding `*.test.ts` component/API tests.

### Modified frontend files

- `frontend/src/stores/chat.ts` — richer sources, assistant message IDs, structured feedback.
- `frontend/src/components/MessageBubble.vue` — citations and three feedback actions.
- `frontend/src/views/FileModeView.vue` — duplicate dialog and versions entry.
- `frontend/src/views/KnowledgeDetailView.vue` — duplicate dialog and versions entry.
- `frontend/src/views/MainLayout.vue` — third navigation item becomes knowledge operations.
- `frontend/src/router/index.ts` — add `/operations`, retain `/analysis`.

---

### Task 1: Schema, Persistence Types, and Isolated Fixtures

**Files:**
- Create: `tests/test_document_version_schema.py`
- Modify: `rag/user_db.py`
- Modify: `config.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `UserDB.create_document_version(document_id: int, original_filename: str, file_path: str, content_hash: str, uploaded_by: int) -> int`
- Produces: `UserDB.activate_document_version(document_id: int, version_id: int, kb_id: str, filename: str) -> None`
- Produces: `UserDB.get_current_document_version(document_id: int) -> dict | None`
- Produces: `UserDB.list_document_versions(document_id: int) -> list[dict]`
- Produces: `UserDB.set_version_validity(version_id: int, validity_status: str) -> None`
- Produces fixture state: `isolated_api.app.state.document_versions_root`

- [ ] **Step 1: Write failing schema and transaction tests**

```python
def test_new_document_version_can_be_activated_atomically(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    document_id = db.create_document_permission("policy.pdf", "rag_docs", user_id)
    version_id = db.create_document_version(
        document_id=document_id,
        original_filename="policy.pdf",
        file_path=str(tmp_path / "v1" / "policy.pdf"),
        content_hash="abc123",
        uploaded_by=user_id,
    )

    db.activate_document_version(document_id, version_id, "rag_docs", "policy.pdf")

    current = db.get_current_document_version(document_id)
    assert current["id"] == version_id
    assert current["version_no"] == 1
    assert current["status"] == "current"
    assert current["validity_status"] == "valid"
    assert db.get_document_permission_by_id(document_id)["current_version_id"] == version_id
```

```python
def test_activating_second_version_makes_first_historical(tmp_path):
    db, user_id, document_id, first_id = make_versioned_document(tmp_path)
    second_id = db.create_document_version(
        document_id, "policy.pdf", str(tmp_path / "v2.pdf"), "hash-2", user_id
    )

    db.activate_document_version(document_id, second_id, "rag_docs", "policy.pdf")

    versions = db.list_document_versions(document_id)
    assert [(item["version_no"], item["status"]) for item in versions] == [
        (2, "current"),
        (1, "historical"),
    ]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_version_schema.py -v
```

Expected: FAIL because version tables, columns, fixtures, and methods do not exist.

- [ ] **Step 3: Add idempotent schema migration and transaction methods**

Add these tables and columns inside `UserDB._create_tables()`:

```sql
CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staging', 'current', 'historical', 'failed')),
    validity_status TEXT NOT NULL CHECK(validity_status IN (
        'valid', 'reported_outdated', 'confirmed_outdated'
    )) DEFAULT 'valid',
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    superseded_at REAL,
    UNIQUE(document_id, version_no),
    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
    FOREIGN KEY(uploaded_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS answer_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_message_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    document_version_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    section_title TEXT NOT NULL,
    text_preview TEXT NOT NULL,
    rank INTEGER NOT NULL,
    is_historical INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY(assistant_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
);
```

Use the existing column-probe migration style to add:

```sql
ALTER TABLE document_permissions ADD COLUMN current_version_id INTEGER;
ALTER TABLE document_permissions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'upload';
ALTER TABLE document_permissions ADD COLUMN source_key TEXT NOT NULL DEFAULT '';
ALTER TABLE document_permissions ADD COLUMN updated_at REAL NOT NULL DEFAULT 0;
ALTER TABLE kb_documents ADD COLUMN document_id INTEGER;
ALTER TABLE kb_documents ADD COLUMN document_version_id INTEGER;
```

Implement activation under `self._lock` with explicit rollback:

```python
def activate_document_version(
    self, document_id: int, version_id: int, kb_id: str, filename: str
) -> None:
    with self._lock:
        try:
            now = time.time()
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "UPDATE document_versions SET status='historical', superseded_at=? "
                "WHERE document_id=? AND status='current'",
                (now, document_id),
            )
            activated = self._conn.execute(
                "UPDATE document_versions SET status='current', validity_status='valid' "
                "WHERE id=? AND document_id=? AND status='staging'",
                (version_id, document_id),
            )
            if activated.rowcount != 1:
                raise VersionConflict("version is no longer staging")
            self._conn.execute(
                "UPDATE document_permissions SET current_version_id=?, updated_at=? WHERE id=?",
                (version_id, now, document_id),
            )
            self._conn.execute(
                "UPDATE kb_documents SET document_id=?, document_version_id=? "
                "WHERE kb_id=? AND filename=?",
                (document_id, version_id, kb_id, filename),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
```

Add to `Settings`:

```python
document_versions_root: str = str(Path(__file__).resolve().parent / "data" / "document_versions")
feedback_cluster_days: int = 30
feedback_cluster_users: int = 3
knowledge_task_due_days: int = 7
knowledge_stale_days: int = 180
historical_result_weight: float = 0.35
```

- [ ] **Step 4: Run focused and existing database tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_version_schema.py tests\test_user_db.py tests\test_kb_file_lifecycle.py -v
```

Expected: PASS with no writes under repository `data/`.

- [ ] **Step 5: Commit**

```powershell
git add config.py rag/user_db.py tests/conftest.py tests/test_document_version_schema.py
git commit -m "feat: add document version schema"
```

---

### Task 2: Source-Neutral Upload Adapter and Version File Store

**Files:**
- Create: `rag/document_sources/__init__.py`
- Create: `rag/document_sources/base.py`
- Create: `rag/document_sources/upload.py`
- Create: `rag/document_version_store.py`
- Create: `tests/test_document_version_store.py`

**Interfaces:**
- Produces: `SourceObject`
- Produces: `UploadDocumentSource.from_bytes(filename: str, content: bytes) -> SourceObject`
- Produces: `stage_version(root: Path, source: SourceObject) -> Path`
- Produces: `final_version_path(root: Path, document_id: int, version_no: int, filename: str) -> Path`
- Produces: `promote_version(stage: Path, destination: Path) -> None`
- Produces: `resolve_version_path(root: Path, stored_path: str) -> Path`

- [ ] **Step 1: Write path-isolation and source-identity tests**

```python
def test_upload_source_has_stable_hash_and_upload_identity():
    source = UploadDocumentSource.from_bytes("../policy.pdf", b"policy-v1")

    assert source.source_type == "upload"
    assert source.display_name == "policy.pdf"
    assert source.content_hash == hashlib.sha256(b"policy-v1").hexdigest()
    assert source.source_key.startswith("upload:")
```

```python
def test_version_path_cannot_escape_root(tmp_path):
    root = tmp_path / "versions"

    with pytest.raises(ValueError):
        final_version_path(root, 4, 1, "../../secret.txt")

    with pytest.raises(ValueError):
        resolve_version_path(root, str(tmp_path / "outside.txt"))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_version_store.py -v
```

Expected: FAIL because the source and file-store modules do not exist.

- [ ] **Step 3: Implement immutable source objects and safe storage**

```python
@dataclass(frozen=True)
class SourceObject:
    source_type: str
    source_key: str
    display_name: str
    content: bytes
    content_hash: str
    observed_at: float


class DocumentSource(Protocol):
    def read(self, source_key: str) -> SourceObject:
        raise NotImplementedError
```

```python
class UploadDocumentSource:
    @staticmethod
    def from_bytes(filename: str, content: bytes) -> SourceObject:
        safe_name = Path(filename).name
        if safe_name != filename or safe_name in {"", ".", ".."}:
            raise ValueError("invalid upload filename")
        digest = hashlib.sha256(content).hexdigest()
        return SourceObject(
            source_type="upload",
            source_key=f"upload:{uuid.uuid4()}",
            display_name=safe_name,
            content=content,
            content_hash=digest,
            observed_at=time.time(),
        )
```

`promote_version` must use `Path.replace()` on the same filesystem. `resolve_version_path` must call `Path.resolve()` and verify `candidate.is_relative_to(root.resolve())`.

- [ ] **Step 4: Run store tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_version_store.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/document_sources rag/document_version_store.py tests/test_document_version_store.py
git commit -m "feat: add source-neutral version store"
```

---

### Task 3: Versioned Ingest Service and Duplicate API Contract

**Files:**
- Create: `rag/document_versions.py`
- Create: `rag/operations_api.py`
- Create: `tests/test_document_versions.py`
- Modify: `rag/api.py`
- Modify: `rag/knowledge_base.py`
- Modify: `rag/api_security.py`
- Modify: existing upload lifecycle tests

**Interfaces:**
- Consumes: `SourceObject`, version-store functions, Task 1 database methods.
- Produces: `DuplicateAction = Literal["reject", "overwrite", "keep"]`
- Produces: `IngestResult(document_id: int, version_id: int, filename: str, version_no: int, status: str, chunks: int)`
- Produces: `ingest_document_version(db: UserDB, manager: KnowledgeBaseManager, source: SourceObject, kb_id: str, actor: dict, duplicate_action: DuplicateAction, root: Path) -> IngestResult`
- Produces: `require_doc_version_edit(db: UserDB, document_id: int, user: dict) -> dict`
- Produces: a focused `APIRouter` in `rag/operations_api.py`, mounted once by `rag/api.py` and extended by Tasks 5–7.
- Produces endpoint: `GET /documents/{document_id}/versions`.

- [ ] **Step 1: Write failing duplicate and compensation tests**

```python
def test_reject_duplicate_returns_structured_conflict(isolated_api, make_user):
    _, headers = make_user("owner")
    first = isolated_api.post(
        "/upload", headers=headers, files={"file": ("policy.txt", b"v1", "text/plain")}
    )
    second = isolated_api.post(
        "/upload", headers=headers, files={"file": ("policy.txt", b"v2", "text/plain")}
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "duplicate_document"
    assert second.json()["detail"]["current_version"] == 1
```

```python
def test_overwrite_preserves_owner_and_old_version(isolated_api, make_user):
    owner, owner_headers = make_user("owner")
    isolated_api.post(
        "/upload", headers=owner_headers, files={"file": ("policy.txt", b"v1", "text/plain")}
    )

    response = isolated_api.post(
        "/upload",
        headers=owner_headers,
        data={"duplicate_action": "overwrite"},
        files={"file": ("policy.txt", b"v2", "text/plain")},
    )

    document = isolated_api.app.state.user_db.get_document_permission("policy.txt", "rag_docs")
    versions = isolated_api.app.state.user_db.list_document_versions(document["id"])
    assert response.status_code == 200
    assert document["owner_id"] == owner["id"]
    assert [item["status"] for item in versions] == ["current", "historical"]
    assert all(Path(item["file_path"]).is_file() for item in versions)
```

Also add tests for `keep`, unchanged hash, non-editor overwrite, failed Qdrant write, and two concurrent overwrite attempts.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_versions.py -v
```

Expected: FAIL because duplicate uploads only return the old plain 409 and version records are not created.

- [ ] **Step 3: Implement the single ingest service**

Core result types:

```python
DuplicateAction = Literal["reject", "overwrite", "keep"]


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    version_id: int
    filename: str
    version_no: int
    status: str
    chunks: int
```

Duplicate selection:

```python
def choose_document_name(db: UserDB, kb_id: str, filename: str, action: DuplicateAction) -> str:
    existing = db.get_document_permission(filename, kb_id)
    if existing is None:
        return filename
    if action == "reject":
        current = db.get_current_document_version(existing["id"])
        raise DuplicateDocument(existing["id"], filename, current["version_no"])
    if action == "overwrite":
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while db.get_document_permission(f"{stem} ({index}){suffix}", kb_id):
        index += 1
    return f"{stem} ({index}){suffix}"
```

All `/upload`, `/index`, and `/knowledge-bases/{kb_id}/documents` calls must construct `UploadDocumentSource.from_bytes(...)` and call the same service. Catch `DuplicateDocument` and return the structured `409` payload. Add `duplicate_action: str = Form("reject")`.

Create `rag/operations_api.py` here, not in a later task, so the dependency order is executable. Its first route is `GET /documents/{document_id}/versions`; it calls `require_doc_view`, returns logical/version metadata only, and excludes `file_path`, source credentials, vector IDs, and uploader identity for users who are neither the document owner nor an administrator. Mount this router exactly once in `rag/api.py`.

The service must reserve a `staging` row before indexing, remove only newly written vectors on failure, and call `activate_document_version` only after file promotion and successful indexing.

- [ ] **Step 4: Run version and prior security tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_document_versions.py tests\test_upload_security.py tests\test_kb_file_lifecycle.py tests\test_document_security_boundaries.py -v
```

Expected: PASS. Verify no repository `data/` changes with `git status --short data qdrant_data`.

- [ ] **Step 5: Commit**

```powershell
git add rag/document_versions.py rag/operations_api.py rag/api.py rag/knowledge_base.py rag/api_security.py tests/test_document_versions.py tests/test_upload_security.py tests/test_kb_file_lifecycle.py
git commit -m "feat: add versioned document ingest"
```

---

### Task 4: Version-Aware Vector Payload and Historical Fallback

**Files:**
- Create: `rag/versioned_retrieval.py`
- Create: `tests/test_versioned_retrieval.py`
- Modify: `rag/models.py`
- Modify: `rag/chunker.py`
- Modify: `rag/vector_store.py`
- Modify: `rag/retriever.py`
- Modify: `rag/pipeline.py`
- Modify: `rag/knowledge_base.py`

**Interfaces:**
- Produces enriched `Chunk`.
- Produces: `mark_document_version_current(collection_name: str, document_id: int, version_id: int) -> None`
- Produces: `delete_document_version(collection_name: str, version_id: int) -> None`
- Produces: `retrieve_current_then_history(retriever: Retriever, query: str, top_k: int, user: dict, user_db: UserDB, kb_id: str) -> list[Chunk]`

- [ ] **Step 1: Write failing current-first and metadata tests**

```python
def test_history_only_fills_missing_current_results():
    current = [chunk("current", version_id=2, is_current=True)]
    history = [
        chunk("old-a", version_id=1, is_current=False),
        chunk("old-b", version_id=1, is_current=False),
    ]
    retriever = FakeVersionRetriever(current=current, history=history)

    result = retrieve_current_then_history(
        retriever, "policy", top_k=3, user=USER, user_db=DB, kb_id="rag_docs"
    )

    assert [item.text for item in result] == ["current", "old-a", "old-b"]
    assert result[0].historical_weight == 1.0
    assert result[1].historical_weight == 0.35
```

```python
def test_version_payload_round_trips_through_qdrant(mock_qdrant):
    original = Chunk(
        text="Leave policy",
        doc_name="policy.md",
        chunk_index=2,
        document_id=7,
        document_version_id=11,
        version_no=3,
        is_current=True,
        validity_status="valid",
        section_title="Annual leave",
        updated_at=1000.0,
    )

    add_to_collection("kb_test", [original], [[0.1, 0.2]], doc_permission_id=7)
    result = search_collection("kb_test", [0.1, 0.2], top_k=1, current_only=True)

    assert result[0] == original
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_versioned_retrieval.py tests\test_vector_store.py -v
```

Expected: FAIL because chunks and payloads do not carry version fields and search cannot filter current versions.

- [ ] **Step 3: Add explicit version fields and filters**

Extend `Chunk`:

```python
@dataclass(frozen=True)
class Chunk:
    text: str
    doc_name: str
    chunk_index: int
    doc_permission_id: int | None = None
    document_id: int | None = None
    document_version_id: int | None = None
    version_no: int | None = None
    is_current: bool = True
    validity_status: str = "valid"
    section_title: str | None = None
    updated_at: float | None = None
    historical_weight: float = 1.0
```

The first Qdrant search filter must require:

```python
must=[
    FieldCondition(key="is_current", match=MatchValue(value=True)),
    FieldCondition(key="validity_status", match=MatchValue(value="valid")),
]
```

Only when ACL-filtered results are below `min(top_k, 3)`, issue the fallback query for `is_current=false OR validity_status=confirmed_outdated`. Reconstruct fallback chunks with `historical_weight=settings.historical_result_weight`.

Update the prompt context builder to prefix historical text with:

`[历史资料，仅供参考，内容可能过期]`

- [ ] **Step 4: Run retrieval, pipeline, and ACL tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_versioned_retrieval.py tests\test_vector_store.py tests\test_retriever.py tests\test_pipeline.py tests\test_pipeline_stream.py tests\test_document_security_boundaries.py -v
```

Expected: PASS. Current-version ACL filtering must happen before deciding how many historical results to request.

- [ ] **Step 5: Commit**

```powershell
git add rag/models.py rag/chunker.py rag/vector_store.py rag/retriever.py rag/versioned_retrieval.py rag/pipeline.py rag/knowledge_base.py tests/test_versioned_retrieval.py tests/test_vector_store.py tests/test_pipeline.py
git commit -m "feat: add version-aware retrieval"
```

---

### Task 5: Persistent Citations and Assistant Message IDs

**Files:**
- Create: `rag/citations.py`
- Create: `tests/test_answer_citations.py`
- Modify: `rag/user_db.py`
- Modify: `rag/pipeline.py`
- Modify: `rag/api.py`
- Modify: `rag/operations_api.py`
- Modify: stream and conversation tests

**Interfaces:**
- Produces: `CitationRecord`
- Produces: `UserDB.add_answer_citations(assistant_message_id: int, citations: list[CitationRecord]) -> None`
- Produces: `UserDB.get_answer_citations(assistant_message_id: int) -> list[dict]`
- Produces: `citation_response(db: UserDB, citation: dict, user: dict) -> dict`
- Produces SSE event: `{"type":"done","assistant_message_id":<int|null>}`

- [ ] **Step 1: Write failing persistence, path-redaction, and stream tests**

```python
def test_stream_done_event_returns_persisted_assistant_message_id(
    isolated_api, make_user, monkeypatch
):
    _, headers = make_user("alice")
    conversation_id = isolated_api.app.state.user_db.create_conversation(
        isolated_api.app.state.user_db.get_user_by_username("alice")["id"], "", "file"
    )
    install_fake_stream_pipeline(monkeypatch, citation_for("policy.pdf", version_id=4))

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "leave?", "conversation_id": conversation_id},
    )

    events = parse_sse(response.text)
    done = next(event for event in events if event["type"] == "done")
    assert isinstance(done["assistant_message_id"], int)
    citations = isolated_api.app.state.user_db.get_answer_citations(
        done["assistant_message_id"]
    )
    assert citations[0]["document_version_id"] == 4
```

```python
def test_citation_response_never_exposes_file_path(isolated_api, make_user):
    user, _ = make_user("alice")
    citation = persisted_citation(file_path="C:/private/version.pdf")

    response = citation_response(isolated_api.app.state.user_db, citation, user)

    assert "file_path" not in response
    assert response["download_url"].endswith("/versions/4/download")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_answer_citations.py tests\test_stream_kb_target.py -v
```

Expected: FAIL because sources are transient and the done event has no message ID.

- [ ] **Step 3: Persist citation rows in the message transaction**

Define:

```python
@dataclass(frozen=True)
class CitationRecord:
    document_id: int
    document_version_id: int
    chunk_index: int
    section_title: str
    text_preview: str
    rank: int
    is_historical: bool
```

After completing a streamed answer:

```python
assistant_message_id = None
if user and req.conversation_id is not None:
    assistant_message_id = await asyncio.to_thread(
        user_db.add_message, req.conversation_id, "assistant", answer_buffer
    )
    await asyncio.to_thread(
        user_db.add_answer_citations, assistant_message_id, citation_records
    )
yield f"data: {_json.dumps({'type': 'done', 'assistant_message_id': assistant_message_id})}\n\n"
```

Ensure the endpoint does not add the assistant message a second time in its existing post-stream persistence block.

Add:

`GET /documents/{document_id}/versions/{version_id}/download`

The route resolves the version through the logical document, calls `require_doc_view`, then returns `FileResponse`; it rejects a version belonging to another document.

- [ ] **Step 4: Run citation, conversation, and download security tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_answer_citations.py tests\test_conversations.py tests\test_stream_kb_target.py tests\test_upload_security.py -v
```

Expected: PASS with no duplicate chat messages.

- [ ] **Step 5: Commit**

```powershell
git add rag/citations.py rag/user_db.py rag/pipeline.py rag/api.py rag/operations_api.py tests/test_answer_citations.py tests/test_conversations.py tests/test_stream_kb_target.py
git commit -m "feat: persist traceable answer citations"
```

---

### Task 6: Structured Feedback and Deterministic Aggregation Keys

**Files:**
- Create: `rag/knowledge_feedback.py`
- Create: `tests/test_knowledge_feedback.py`
- Modify: `rag/user_db.py`
- Modify: `rag/operations_api.py`

**Interfaces:**
- Produces: `normalize_question(text: str) -> str`
- Produces: `question_hash(text: str) -> str`
- Produces: `submit_knowledge_feedback(db: UserDB, user: dict, assistant_message_id: int, kind: str, citation_id: int | None, comment: str, suggested_answer: str) -> dict`
- Produces endpoint: `POST /feedback/knowledge`

- [ ] **Step 1: Write failing validation and identity tests**

```python
def test_outdated_feedback_requires_selected_citation_when_answer_has_multiple(db, users):
    message_id = answer_with_two_citations(db, users.owner_id)

    with pytest.raises(FeedbackValidationError, match="select a citation"):
        submit_knowledge_feedback(
            db,
            users.reporter,
            assistant_message_id=message_id,
            kind="outdated",
            citation_id=None,
            comment="old policy",
            suggested_answer="",
        )
```

```python
def test_question_hash_normalizes_case_whitespace_and_chinese_punctuation():
    assert question_hash("  年假 怎么 申请？ ") == question_hash("年假 怎么 申请?")
```

```python
def test_feedback_submitter_is_hidden_from_unrelated_user(
    isolated_api, make_user, seeded_feedback
):
    _, outsider_headers = make_user("outsider")

    response = isolated_api.get(
        "/knowledge-operations/my-feedback", headers=outsider_headers
    )

    assert seeded_feedback.submitter_name not in response.text
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_feedback.py -v
```

Expected: FAIL because feedback has only integer value/comment and citations are not validated.

- [ ] **Step 3: Migrate feedback and implement one submission service**

Add columns using idempotent probes:

```sql
ALTER TABLE feedback ADD COLUMN kind TEXT NOT NULL DEFAULT 'not_useful';
ALTER TABLE feedback ADD COLUMN question_text TEXT NOT NULL DEFAULT '';
ALTER TABLE feedback ADD COLUMN question_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE feedback ADD COLUMN citation_id INTEGER;
ALTER TABLE feedback ADD COLUMN suggested_answer TEXT NOT NULL DEFAULT '';
ALTER TABLE feedback ADD COLUMN status TEXT NOT NULL DEFAULT 'submitted';
```

Normalize without LLM:

```python
_PUNCTUATION = str.maketrans({"？": "?", "！": "!", "，": ",", "。": "."})


def normalize_question(text: str) -> str:
    normalized = text.translate(_PUNCTUATION).casefold()
    return " ".join(normalized.split())


def question_hash(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()
```

Validate that the assistant message belongs to the submitting user and that `citation_id` belongs to that assistant message. Preserve the existing `/feedback` endpoint as a compatibility wrapper mapping positive/negative to useful/not_useful.

- [ ] **Step 4: Run feedback and existing chat tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_feedback.py tests\test_feedback.py tests\test_regenerate.py tests\test_conversations.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/knowledge_feedback.py rag/user_db.py rag/operations_api.py tests/test_knowledge_feedback.py tests/test_feedback.py
git commit -m "feat: add structured knowledge feedback"
```

---

### Task 7: Knowledge Tasks, Ownership, and Operations API

**Files:**
- Create: `rag/knowledge_tasks.py`
- Create: `tests/test_knowledge_tasks.py`
- Create: `tests/test_operations_api.py`
- Modify: `rag/user_db.py`
- Modify: `rag/knowledge_feedback.py`
- Modify: `rag/operations_api.py`
- Modify: `rag/api.py`
- Modify: `rag/api_security.py`

**Interfaces:**
- Produces: `TaskService.record_feedback(feedback_id: int) -> int | None`
- Produces: `TaskService.transition(task_id: int, actor: dict, new_status: str, note: str, expected_updated_at: float) -> dict`
- Produces: `TaskService.reassign(task_id: int, actor: dict, assignee_id: int | None) -> dict`
- Produces owner route: `PUT /documents/{document_id}/owner`
- Produces: `GET /knowledge-operations/my-feedback?page=<int>&page_size=<int>` returning `{"items": [...], "total": <int>}`.
- Produces: `GET /knowledge-operations/my-tasks?page=<int>&page_size=<int>` returning `{"items": [...], "total": <int>}`.
- Produces: `GET /knowledge-operations/summary` returning counts for open, overdue, outdated, not-useful, and stale knowledge bases.
- Produces: `GET /knowledge-operations/tasks?status=<value>&assignee_id=<id>&page=<int>&page_size=<int>` for administrators.
- Produces: `PUT /knowledge-operations/tasks/{task_id}` for state transitions.
- Produces: `PUT /knowledge-operations/tasks/{task_id}/assignee` for administrator reassignment.

- [ ] **Step 1: Write failing task-rule and authorization tests**

```python
def test_outdated_feedback_creates_one_immediate_owner_task(task_fixture):
    first_feedback = task_fixture.submit(kind="outdated", user="alice")
    second_feedback = task_fixture.submit(kind="outdated", user="bob")

    first_task = task_fixture.service.record_feedback(first_feedback)
    second_task = task_fixture.service.record_feedback(second_feedback)

    assert second_task == first_task
    task = task_fixture.db.get_knowledge_task(first_task)
    assert task["assigned_to"] == task_fixture.owner_id
    assert task["report_count"] == 2
```

```python
def test_not_useful_requires_three_distinct_users_within_thirty_days(task_fixture):
    ids = [
        task_fixture.submit(kind="not_useful", user="alice"),
        task_fixture.submit(kind="not_useful", user="bob"),
        task_fixture.submit(kind="not_useful", user="alice"),
    ]
    assert [task_fixture.service.record_feedback(item) for item in ids] == [None, None, None]

    third_user = task_fixture.submit(kind="not_useful", user="carol")
    task_id = task_fixture.service.record_feedback(third_user)

    assert isinstance(task_id, int)
```

```python
def test_unrelated_user_cannot_read_or_transition_owner_task(
    isolated_api, make_user, seeded_task
):
    _, headers = make_user("outsider")
    listing = isolated_api.get("/knowledge-operations/my-tasks", headers=headers)
    update = isolated_api.put(
        f"/knowledge-operations/tasks/{seeded_task.id}",
        headers=headers,
        json={
            "status": "resolved",
            "note": "not mine",
            "expected_updated_at": seeded_task.updated_at,
        },
    )
    assert listing.json()["items"] == []
    assert update.status_code == 403
```

```python
def test_owner_and_admin_can_see_reporter_names_but_outsider_cannot(
    isolated_api, make_user, seeded_task
):
    _, owner_headers = make_user("owner")
    _, admin_headers = make_user("admin", is_admin=True)
    _, outsider_headers = make_user("outsider")

    owner_body = isolated_api.get(
        "/knowledge-operations/my-tasks", headers=owner_headers
    ).json()
    admin_body = isolated_api.get(
        "/knowledge-operations/tasks", headers=admin_headers
    ).json()
    outsider_body = isolated_api.get(
        "/knowledge-operations/my-tasks", headers=outsider_headers
    ).json()

    assert owner_body["items"][0]["reporters"] == seeded_task.reporter_names
    assert admin_body["items"][0]["reporters"] == seeded_task.reporter_names
    assert outsider_body["items"] == []
    assert seeded_task.reporter_names[0] not in str(outsider_body)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_tasks.py tests\test_operations_api.py -v
```

Expected: FAIL because task tables, service, and routes do not exist.

- [ ] **Step 3: Implement active-key uniqueness and state transitions**

Create:

```sql
CREATE TABLE IF NOT EXISTS knowledge_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL CHECK(task_type IN ('outdated', 'not_useful')),
    cluster_key TEXT NOT NULL,
    active_key TEXT UNIQUE,
    document_id INTEGER,
    document_version_id INTEGER,
    assigned_to INTEGER,
    status TEXT NOT NULL CHECK(status IN (
        'open', 'in_progress', 'resolved', 'dismissed', 'confirmed_expired'
    )),
    report_count INTEGER NOT NULL DEFAULT 1,
    resolution_note TEXT NOT NULL DEFAULT '',
    due_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS knowledge_task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
```

Allowed transitions:

```python
_TRANSITIONS = {
    "open": {"in_progress", "resolved", "dismissed", "confirmed_expired"},
    "in_progress": {"resolved", "dismissed", "confirmed_expired"},
    "resolved": set(),
    "dismissed": set(),
    "confirmed_expired": set(),
}
```

Use optimistic concurrency:

```sql
UPDATE knowledge_tasks
SET status=?, active_key=?, resolution_note=?, updated_at=?, resolved_at=?
WHERE id=? AND updated_at=?;
```

If rowcount is zero, return `409 task_state_conflict`.

For confirmed expiration, call `set_version_validity(version_id, "confirmed_outdated")`. Owner reassignment updates `document_permissions.owner_id` only; it does not rewrite historical version uploader fields.

Extend `submit_knowledge_feedback` so it inserts the feedback and invokes `TaskService.record_feedback` in the same application service call. API serializers include a `reporters` list only when the current user is the task assignee or an administrator. `/my-feedback` returns only the current user's rows; it never acts as an owner-wide feedback listing.

- [ ] **Step 4: Run task, API, ACL, and schema tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_tasks.py tests\test_operations_api.py tests\test_api_security.py tests\test_document_security_boundaries.py tests\test_user_db.py -v
```

Expected: PASS. Inspect API responses to confirm unrelated users never receive submitter usernames.

- [ ] **Step 5: Commit**

```powershell
git add rag/knowledge_tasks.py rag/knowledge_feedback.py rag/operations_api.py rag/user_db.py rag/api.py rag/api_security.py tests/test_knowledge_tasks.py tests/test_operations_api.py
git commit -m "feat: add knowledge correction tasks"
```

---

### Task 8: Frontend Duplicate Handling, Citations, and Feedback Dialog

**Files:**
- Create: `frontend/src/api/documents.ts`
- Create: `frontend/src/api/knowledgeOperations.ts`
- Create: `frontend/src/components/DuplicateDocumentDialog.vue`
- Create: `frontend/src/components/DocumentVersionsDialog.vue`
- Create: `frontend/src/components/CitationList.vue`
- Create: `frontend/src/components/KnowledgeFeedbackDialog.vue`
- Create corresponding component tests
- Modify: `frontend/src/stores/chat.ts`
- Modify: `frontend/src/components/MessageBubble.vue`
- Modify: `frontend/src/views/FileModeView.vue`
- Modify: `frontend/src/views/KnowledgeDetailView.vue`

**Interfaces:**
- Produces TypeScript `Citation`, `DocumentVersion`, `KnowledgeFeedbackRequest`.
- Produces: `uploadWithDuplicateAction(file: File, kbId: string, action: DuplicateAction)`.
- Produces `Message.assistantMessageId`.

- [ ] **Step 1: Write failing UI behavior tests**

```typescript
it('offers skip overwrite and keep after duplicate conflict', async () => {
  mockUpload
    .mockRejectedValueOnce({ response: { status: 409, data: {
      detail: { code: 'duplicate_document', document_id: 8, current_version: 2 }
    }}})
    .mockResolvedValueOnce({ status: 'indexed', version_no: 3 })

  const wrapper = mountUploadView()
  await wrapper.find('input[type=file]').trigger('change', { files: [file('policy.txt')] })

  expect(wrapper.get('[data-test=duplicate-dialog]').text()).toContain('覆盖')
  await wrapper.get('[data-test=duplicate-overwrite]').trigger('click')
  expect(mockUpload).toHaveBeenLastCalledWith(expect.any(File), 'rag_docs', 'overwrite')
})
```

```typescript
it('requires a citation for outdated feedback with multiple sources', async () => {
  const wrapper = mountFeedbackDialog({
    kind: 'outdated',
    citations: [citation(1), citation(2)]
  })

  await wrapper.get('[data-test=submit-feedback]').trigger('click')

  expect(wrapper.text()).toContain('请选择内容过期的来源')
  expect(mockSubmit).not.toHaveBeenCalled()
})
```

```typescript
it('stores assistant message id from the done event', async () => {
  mockSse([
    { type: 'token', content: 'answer' },
    { type: 'sources', sources: [citation(1)] },
    { type: 'done', assistant_message_id: 42 },
  ])

  await store.sendMessage('question', 'file')

  expect(store.messages.at(-1)?.id).toBe(42)
})
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/components/__tests__/DuplicateDocumentDialog.test.ts src/components/__tests__/KnowledgeFeedbackDialog.test.ts src/stores/__tests__/chat.test.ts
```

Expected: FAIL because dialogs, citation types, and done-event handling do not exist.

- [ ] **Step 3: Implement typed API and role-safe dialogs**

Use exact request shape:

```typescript
export type DuplicateAction = 'reject' | 'overwrite' | 'keep'

export interface Citation {
  citation_id: number
  doc_name: string
  kb_id: string
  section_title: string
  version_no: number
  updated_at: number
  is_historical: boolean
  text_preview: string
  download_url: string
}

export interface KnowledgeFeedbackRequest {
  assistant_message_id: number
  kind: 'useful' | 'not_useful' | 'outdated'
  citation_id?: number
  comment: string
  suggested_answer: string
}
```

In `chat.ts`, parse every SSE event and assign:

```typescript
if (event.type === 'done') {
  assistantMsg.id = event.assistant_message_id ?? undefined
}
```

Replace silent feedback failure with dialog error feedback. Citation clicks must use the authenticated download helper, not direct `window.open(download_url)`.

- [ ] **Step 4: Run frontend unit tests and build**

Run:

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
```

Expected: all tests and TypeScript build pass. After build, inspect `git status --short static` and restore generated tracked static output if the repository does not intentionally version this build.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/api frontend/src/components frontend/src/stores/chat.ts frontend/src/views/FileModeView.vue frontend/src/views/KnowledgeDetailView.vue
git commit -m "feat: add version and feedback interactions"
```

---

### Task 9: Knowledge Operations Page and Third Navigation Entry

**Files:**
- Create: `frontend/src/views/KnowledgeOperationsView.vue`
- Create: `frontend/src/views/__tests__/KnowledgeOperationsView.test.ts`
- Modify: `frontend/src/views/MainLayout.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: frontend API types from Task 8

**Interfaces:**
- Consumes: `/knowledge-operations/my-feedback`, `/my-tasks`, `/summary`, `/tasks`.
- Produces route: `/operations`.
- Preserves route: `/analysis`.

- [ ] **Step 1: Write failing route and role-view tests**

```typescript
it('uses knowledge operations as the third main navigation item', async () => {
  const wrapper = mountMainLayout('/operations')

  const labels = wrapper.findAll('.nav-label').map(node => node.text())
  expect(labels).toEqual(['文件', '知识库', '知识运营'])
  expect(wrapper.get('[aria-label="知识运营"]').classes()).toContain('active')
})
```

```typescript
it('shows admin overview only to administrators', async () => {
  mockAuthUser({ id: 1, username: 'admin', is_admin: true })
  mockOperationsResponses()
  const admin = mountOperationsView()
  await flushPromises()
  expect(admin.text()).toContain('运营总览')

  mockAuthUser({ id: 2, username: 'alice', is_admin: false })
  const employee = mountOperationsView()
  await flushPromises()
  expect(employee.text()).not.toContain('运营总览')
  expect(employee.text()).toContain('我的反馈')
})
```

Also test owner task transitions, overdue markers, empty states, failed API states, assignee names, and absence of unrelated submitter names.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/views/__tests__/KnowledgeOperationsView.test.ts src/views/__tests__/MainLayout.test.ts
```

Expected: FAIL because `/operations` and the view do not exist and the third label remains `分析`.

- [ ] **Step 3: Implement one responsive page with role-gated tabs**

Router entries:

```typescript
{
  path: 'operations',
  name: 'knowledge-operations',
  component: () => import('../views/KnowledgeOperationsView.vue')
},
{
  path: 'analysis',
  name: 'analysis',
  component: () => import('../views/AnalysisModeView.vue')
}
```

Tabs:

```typescript
const tabs = computed(() => [
  { key: 'feedback', label: '我的反馈', visible: true },
  { key: 'tasks', label: '待我处理', visible: true },
  { key: 'overview', label: '运营总览', visible: authStore.user?.is_admin === true },
].filter(tab => tab.visible))
```

Do not infer administrator status from the route or browser storage. Use the authenticated `/me` user record already held by `authStore`.

- [ ] **Step 4: Run frontend tests and production build**

Run:

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
```

Expected: PASS. Direct navigation to `/analysis` remains functional, but no main-navigation button points to it.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/views/KnowledgeOperationsView.vue frontend/src/views/__tests__/KnowledgeOperationsView.test.ts frontend/src/views/MainLayout.vue frontend/src/router/index.ts
git commit -m "feat: add knowledge operations workspace"
```

---

### Task 10: Existing-Document Migration, Full Verification, and Handoff

**Files:**
- Create: `tests/test_existing_document_version_migration.py`
- Modify: `rag/document_versions.py`
- Modify: `rag/api.py`
- Modify: `.env.example`
- Modify: `docs/deployment-guide.md`
- Modify: `docs/development-log-security-boundary-hardening.md`

**Interfaces:**
- Produces: `migrate_existing_document_versions(db: UserDB, versions_root: Path, default_upload_root: Path) -> MigrationReport`
- Produces: `MigrationReport(migrated: int, missing: int, unchanged: int)`

- [ ] **Step 1: Write failing idempotent migration tests**

```python
def test_existing_document_migration_is_idempotent(tmp_path):
    db, upload_root, versions_root, document_id = legacy_document_fixture(tmp_path)

    first = migrate_existing_document_versions(db, versions_root, upload_root)
    second = migrate_existing_document_versions(db, versions_root, upload_root)

    assert first.migrated == 1
    assert second.migrated == 0
    assert second.unchanged == 1
    assert len(db.list_document_versions(document_id)) == 1
```

```python
def test_missing_legacy_source_becomes_failed_version(tmp_path):
    db, upload_root, versions_root, document_id = missing_legacy_document_fixture(tmp_path)

    report = migrate_existing_document_versions(db, versions_root, upload_root)

    assert report.missing == 1
    version = db.list_document_versions(document_id)[0]
    assert version["status"] == "failed"
    assert db.get_document_permission_by_id(document_id)["current_version_id"] is None
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_existing_document_version_migration.py -v
```

Expected: FAIL because migration does not exist.

- [ ] **Step 3: Implement startup migration without silent data claims**

The migration must:

1. Select permission records with `current_version_id IS NULL`.
2. Resolve the file from `kb_documents.file_path` first, then the configured default upload root.
3. Copy an existing source into version 1 storage; never move or delete the legacy source during this task.
4. Create a current version and update the logical document pointer.
5. Create a failed version for missing sources and log document ID/name without logging paths outside configured roots.
6. Commit each document independently so one corrupt legacy record does not roll back completed records.

Call migration at application startup before automatic indexing. Log only aggregate counts at INFO.

Document `RAG_DOCUMENT_VERSIONS_ROOT`, `RAG_FEEDBACK_CLUSTER_DAYS`, `RAG_FEEDBACK_CLUSTER_USERS`, `RAG_KNOWLEDGE_TASK_DUE_DAYS`, `RAG_KNOWLEDGE_STALE_DAYS`, and `RAG_HISTORICAL_RESULT_WEIGHT` in `.env.example` and the environment-variable table in `docs/deployment-guide.md`. Mark them optional and show the defaults from Task 1.

- [ ] **Step 4: Run complete backend and frontend verification**

Backend collection count:

```powershell
& .\.venv\Scripts\python.exe -m pytest --collect-only -q
```

Run backend in two bounded segments if the full suite exceeds 60 seconds. Both segments must exit 0 and their passed counts must equal the collected total.

Security and feature focus:

```powershell
& .\.venv\Scripts\python.exe -m pytest `
  tests\test_api_security.py `
  tests\test_upload_security.py `
  tests\test_document_security_boundaries.py `
  tests\test_document_versions.py `
  tests\test_versioned_retrieval.py `
  tests\test_answer_citations.py `
  tests\test_knowledge_feedback.py `
  tests\test_knowledge_tasks.py `
  tests\test_operations_api.py `
  tests\test_existing_document_version_migration.py -v
```

Frontend:

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
```

Static checks:

```powershell
Set-Location ..
& .\.venv\Scripts\python.exe -m ruff check `
  rag\document_sources `
  rag\document_version_store.py `
  rag\document_versions.py `
  rag\versioned_retrieval.py `
  rag\citations.py `
  rag\knowledge_feedback.py `
  rag\knowledge_tasks.py `
  rag\operations_api.py `
  tests\test_document_version_schema.py `
  tests\test_document_version_store.py `
  tests\test_document_versions.py `
  tests\test_versioned_retrieval.py `
  tests\test_answer_citations.py `
  tests\test_knowledge_feedback.py `
  tests\test_knowledge_tasks.py `
  tests\test_operations_api.py `
  tests\test_existing_document_version_migration.py
git diff --check
git status --short data qdrant_data
```

Expected: all focused checks pass, no repository data residue, and no physical path appears in API snapshots.

- [ ] **Step 5: Update the development log and commit**

Record:

- Schema version and migration counts.
- Backend collected/passed counts.
- Frontend test/build results.
- Any remaining pre-existing full-project Ruff warnings.
- Explicitly deferred NAS, organization permissions, external notifications, and scenario assistants.

Commit:

```powershell
git add .env.example docs/deployment-guide.md docs/development-log-security-boundary-hardening.md rag/document_versions.py rag/api.py tests/test_existing_document_version_migration.py
git commit -m "docs: verify trusted knowledge loop"
```

---

## Workbench Handoff

Implementation should start with:

```powershell
$project = 'C:\Users\Administrator\Desktop\RAGv3\RAGv3\.worktrees\security-boundary-hardening'
Set-Location $project
git status --short
git log -3 --oneline
```

Expected handoff commits include:

- `6f41cd1 docs: design trusted knowledge loop`
- `69a9a68 fix: resolve backend reliability defects`
- the commit containing this implementation plan

Then invoke `superpowers:executing-plans` and execute one task at a time with review checkpoints. Do not begin NAS or organization-permission work from this plan.
