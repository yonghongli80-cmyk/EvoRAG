# Security Boundary Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close RAGv3's confirmed authentication, authorization, cache, upload, shared-knowledge-base, file-lifecycle, and data-source security gaps without implementing NAS or organization features.

**Architecture:** Add one focused API-security boundary around the existing `UserDB` and permission helpers, then route every affected endpoint through it. Keep file operations compensating and explicit, disable answer caching until ACL-versioned cache keys exist, and keep experimental data-source code present but inaccessible by default.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLite, Qdrant, pytest, Vue 3, Pinia, TypeScript, Vitest

## Global Constraints

- `Settings.auth_enabled` defaults to `True`; only explicit `RAG_AUTH_ENABLED=false` enables anonymous development mode.
- Anonymous development mode emits a high-priority startup warning and is treated as insecure.
- `/register` and `/login` remain public; affected business endpoints require a resolved principal.
- Same-name uploads return `409 Conflict`; this phase does not implement overwrite.
- Missing KB metadata is an inconsistent resource state; non-administrative mutation returns `409`.
- Historical documents without a permission record are admin-only.
- Protected documents cannot be overwritten, deleted, shared, or have visibility/download settings changed.
- Final-answer cache reads and writes are disabled; `RedisCache` remains in the repository.
- `Settings.data_sources_enabled` defaults to `False`; disabled `/sources` routes return `404`.
- Enabled `/sources` routes are administrator-only and never return credentials or authorization headers.
- KB-owned files live at `data/kb_files/{kb_id}/{document_id}/{safe_filename}`.
- NAS, SMB, WebDAV, DingTalk, department/position inheritance, employee import, and departure handover are outside this plan.
- Do not write tests against `data/users.db`, the shared Qdrant directory, or repository upload files.

---

## File Structure

### New files

- `rag/api_security.py` — principal resolution and the single KB/document authorization boundary.
- `rag/kb_file_store.py` — safe staging, final-path construction, atomic promotion, and physical-file cleanup.
- `tests/test_api_security.py` — authentication mode, bearer/API-key principal resolution, and helper behavior.
- `tests/test_upload_security.py` — anonymous upload, duplicate, compensation, static-file, and authenticated-download cases.
- `tests/test_kb_security_boundaries.py` — private/shared/public KB access and missing-metadata conflicts.
- `tests/test_document_security_boundaries.py` — document filtering, share level, protected state, and historical-record behavior.
- `tests/test_kb_file_lifecycle.py` — persistent KB paths and upload/delete compensation.
- `tests/test_pipeline_cache_security.py` — proof that final-answer cache cannot bypass ACL processing.
- `tests/test_data_sources_security.py` — disabled/admin-only source routes and secret redaction.
- `frontend/src/api/files.ts` — authenticated file download used by “add to knowledge base”.
- `frontend/src/api/__tests__/files.test.ts` — download helper request/response behavior.

### Modified files

- `config.py` — fail-closed authentication and data-source feature flag.
- `.env.example` — safe defaults and explicit insecure-development warning.
- `rag/api.py` — apply dependencies, remove upload static mount, compensate file/index writes, target stream queries to the requested KB, and seal source routes.
- `rag/user_db.py` — KB-document lifecycle methods, share-level persistence usage, and state/error migration.
- `rag/permissions.py` — fail closed for unowned historical documents and enforce protected records.
- `rag/pipeline.py` — remove final-answer cache read/write paths while preserving ACL filtering.
- `rag/batch_importer.py` — preserve the authenticated source document name instead of naming chunks after a temporary file.
- `frontend/src/components/ChatInput.vue` — pass the active KB identifier into the chat store.
- `frontend/src/stores/chat.ts` — carry an optional `kbId` in stream query payloads.
- `frontend/src/views/KnowledgeDetailView.vue` — bind chat to the route KB and use authenticated download for copying files.
- `tests/conftest.py` — isolated API database, upload roots, and authenticated user fixtures.
- `tests/test_code_review_fixes.py` — remove the false static-file assertion and keep only genuine regression tests.
- `tests/test_data_sources_api.py` — stop mutating the module-level production database and conform to the sealed feature.

---

### Task 1: Isolated API Test Harness and Principal Boundary

**Files:**
- Create: `rag/api_security.py`
- Create: `tests/test_api_security.py`
- Modify: `rag/api.py:295-297`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `get_user_db(request: Request) -> UserDB`
- Produces: `resolve_principal(db: UserDB, authorization: str, api_key: str | None) -> dict | None`
- Produces: `require_user(request: Request, authorization: str, api_key: str | None) -> dict`
- Produces: `require_admin(user: dict) -> dict`
- Produces fixtures: `isolated_api`, `make_user`

- [ ] **Step 1: Write failing tests for strict bearer parsing, API-key identity resolution, and database isolation**

```python
# tests/test_api_security.py
from fastapi.testclient import TestClient

from config import settings
from rag.auth import create_token
from rag.api import app
from rag.api_security import resolve_principal


def test_bearer_token_resolves_full_database_user(isolated_api, make_user):
    user, headers = make_user("alice")

    response = isolated_api.get("/me", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"id": user["id"], "username": "alice"}


def test_malformed_bearer_prefix_is_rejected(isolated_api, make_user):
    user, _ = make_user("alice")
    token = create_token({"user_id": user["id"], "username": "alice"})

    response = isolated_api.get("/me", headers={"Authorization": f"bearer {token}"})

    assert response.status_code == 401


def test_configured_api_key_must_map_to_a_real_user(isolated_api, make_user, monkeypatch):
    user, _ = make_user("service-admin", is_admin=True)
    monkeypatch.setattr(settings, "auth_keys", '{"service-admin":"key-123"}')

    response = isolated_api.get("/me", headers={"X-API-Key": "key-123"})

    assert response.status_code == 200
    assert response.json() == {"id": user["id"], "username": "service-admin"}


def test_isolated_api_does_not_use_repository_users_database(isolated_api):
    assert app.state.user_db.db_path != settings.users_db_path
```

- [ ] **Step 2: Run the tests and verify the old code fails**

Run: `python -m pytest tests/test_api_security.py -v`

Expected: FAIL because `rag.api_security`, `isolated_api`, `make_user`, and `app.state.user_db` do not exist.

- [ ] **Step 3: Add isolated fixtures that restore every module-level resource**

```python
# append to tests/conftest.py
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag.auth import create_token
from rag.user_db import UserDB


@pytest.fixture
def isolated_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import rag.api as api

    db = UserDB(str(tmp_path / "users.db"))
    upload_dir = tmp_path / "upload"
    kb_root = tmp_path / "kb_files"
    upload_dir.mkdir()
    kb_root.mkdir()

    previous_db = api.app.state.user_db if hasattr(api.app.state, "user_db") else api.user_db
    previous_upload_dir = getattr(api.app.state, "upload_dir", None)
    previous_kb_files_root = getattr(api.app.state, "kb_files_root", None)
    api.app.state.user_db = db
    monkeypatch.setattr(api, "user_db", db)
    monkeypatch.setattr(api, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(api, "KB_FILES_ROOT", kb_root, raising=False)
    api.app.state.upload_dir = upload_dir
    api.app.state.kb_files_root = kb_root

    with TestClient(api.app) as client:
        yield client

    api.app.dependency_overrides.clear()
    api.app.state.user_db = previous_db
    if previous_upload_dir is None:
        del api.app.state.upload_dir
    else:
        api.app.state.upload_dir = previous_upload_dir
    if previous_kb_files_root is None:
        del api.app.state.kb_files_root
    else:
        api.app.state.kb_files_root = previous_kb_files_root
    db.close()


@pytest.fixture
def make_user(isolated_api: TestClient) -> Callable[[str, bool], tuple[dict, dict[str, str]]]:
    from rag.api import app

    def _make(username: str, is_admin: bool = False) -> tuple[dict, dict[str, str]]:
        user_id = app.state.user_db.create_user(username, "Password-123!")
        if is_admin:
            app.state.user_db.set_user_admin(user_id, True)
        user = app.state.user_db.get_user_by_id(user_id)
        token = create_token({"user_id": user_id, "username": username})
        return user, {"Authorization": f"Bearer {token}"}

    return _make
```

- [ ] **Step 4: Implement the principal boundary**

```python
# rag/api_security.py
from __future__ import annotations

import asyncio
import hmac
import json
import logging

from fastapi import Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from config import settings
from rag.auth import decode_token
from rag.user_db import UserDB

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
logger = logging.getLogger(__name__)


def get_user_db(request: Request) -> UserDB:
    return request.app.state.user_db


def _user_for_api_key(db: UserDB, api_key: str) -> dict | None:
    try:
        configured = json.loads(settings.auth_keys)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="鉴权配置格式错误") from exc
    for principal, expected in configured.items():
        if not hmac.compare_digest(api_key, str(expected)):
            continue
        if str(principal).isdigit():
            return db.get_user_by_id(int(principal))
        return db.get_user_by_username(str(principal))
    return None


def resolve_principal(db: UserDB, authorization: str, api_key: str | None) -> dict | None:
    if api_key:
        return _user_for_api_key(db, api_key)
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme != "Bearer" or not token or " " in token:
        return None
    payload = decode_token(token)
    if not payload or "user_id" not in payload:
        return None
    try:
        user_id = int(payload["user_id"])
    except (TypeError, ValueError):
        return None
    user = db.get_user_by_id(user_id)
    if not user:
        return None
    if decode_token(token, user.get("password_changed_at")) is None:
        return None
    return user


async def require_user(
    request: Request,
    authorization: str = Header(default=""),
    api_key: str | None = Security(api_key_header),
) -> dict:
    if not settings.auth_enabled:
        return {"id": 0, "username": "anonymous-development", "is_admin": True, "auth_mode": "disabled"}
    user = await asyncio.to_thread(resolve_principal, get_user_db(request), authorization, api_key)
    if not user:
        raise HTTPException(status_code=401, detail="未提供有效认证信息")
    return user


def require_admin(user: dict) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
```

In `rag/api.py`, immediately after constructing the production database:

```python
user_db = UserDB(str(_DB_PATH))
app.state.user_db = user_db
```

Replace `/me` with `user: dict = Depends(require_user)` and return only `id` and `username`. Add `Depends` to the FastAPI import.

Use the same dependency for `/files`, `/tags`, `/index`, `/batch-import`, `/query`, `/query/stream`, and `/suggest`. `/index-all` additionally calls `require_admin(user)` because it reindexes every physical file. The only public API decorators after this task are `/health`, `/register`, `/login`, the SPA/static routes, and `/data/charts`.

- [ ] **Step 5: Run focused and existing authentication tests**

Run: `python -m pytest tests/test_api_security.py tests/test_auth.py tests/test_user_auth.py -v`

Expected: PASS; no test creates a user in repository `data/users.db`.

- [ ] **Step 6: Commit**

```bash
git add rag/api_security.py rag/api.py rag/user_db.py tests/conftest.py tests/test_api_security.py
git commit -m "refactor: centralize authenticated principals"
```

---

### Task 2: Fail-Closed Defaults and Data-Source Sealing

**Files:**
- Modify: `config.py:26-31`
- Modify: `.env.example`
- Modify: `rag/api_security.py`
- Modify: `rag/api.py:32-145,2328-2446`
- Create: `tests/test_data_sources_security.py`
- Modify: `tests/test_data_sources_api.py`

**Interfaces:**
- Consumes: `require_user(...) -> dict`, `require_admin(user: dict) -> dict`
- Produces: `require_data_sources_admin(request: Request, authorization: str, api_key: str | None) -> dict`
- Produces: `redact_source_config(value: object) -> object`
- Produces: `log_security_mode() -> None`

- [ ] **Step 1: Write failing configuration and route tests**

```python
# tests/test_data_sources_security.py
from config import Settings, settings


def test_authentication_and_source_flags_fail_closed(monkeypatch):
    monkeypatch.delenv("RAG_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("RAG_DATA_SOURCES_ENABLED", raising=False)
    isolated = Settings(_env_file=None)

    assert isolated.auth_enabled is True
    assert isolated.data_sources_enabled is False


def test_sources_are_hidden_when_feature_is_disabled(isolated_api, make_user):
    _, admin_headers = make_user("admin", is_admin=True)

    response = isolated_api.get("/sources", headers=admin_headers)

    assert response.status_code == 404


def test_anonymous_sources_are_also_hidden_when_feature_is_disabled(isolated_api):
    response = isolated_api.get("/sources")

    assert response.status_code == 404


def test_enabled_sources_reject_non_admin(isolated_api, make_user, monkeypatch):
    _, headers = make_user("alice")
    monkeypatch.setattr(settings, "data_sources_enabled", True)

    response = isolated_api.get("/sources", headers=headers)

    assert response.status_code == 403


def test_enabled_sources_redact_secrets(isolated_api, make_user, monkeypatch):
    _, headers = make_user("admin", is_admin=True)
    monkeypatch.setattr(settings, "data_sources_enabled", True)
    db = isolated_api.app.state.user_db
    db.create_data_source(
        "internal",
        "api",
        '{"url":"https://example.invalid","headers":{"Authorization":"Bearer secret"},'
        '"password":"db-secret","connection_string":"postgresql://user:pass@db/name"}',
    )

    response = isolated_api.get("/sources", headers=headers)

    assert response.status_code == 200
    config = response.json()["sources"][0]["config"]
    assert config["url"] == "https://example.invalid"
    assert config["headers"]["Authorization"] == "[REDACTED]"
    assert config["password"] == "[REDACTED]"
    assert config["connection_string"] == "[REDACTED]"
    assert "secret" not in response.text
    assert "user:pass" not in response.text


def test_explicit_anonymous_development_mode_warns_and_preserves_dev_access(
    isolated_api, monkeypatch, caplog
):
    from rag.api_security import log_security_mode

    monkeypatch.setattr(settings, "auth_enabled", False)
    with caplog.at_level("CRITICAL"):
        log_security_mode()

    response = isolated_api.get("/me")
    assert response.status_code == 200
    assert response.json()["username"] == "anonymous-development"
    assert "RAG_AUTH_ENABLED=false" in caplog.text
```

- [ ] **Step 2: Run the tests and verify fail-open behavior**

Run: `python -m pytest tests/test_data_sources_security.py -v`

Expected: FAIL because authentication defaults to false, the feature flag does not exist, normal users can reach source routes, and list responses expose configuration.

- [ ] **Step 3: Add safe settings and startup warning**

```python
# config.py
auth_enabled: bool = True
auth_keys: str = "{}"
data_sources_enabled: bool = False
```

```dotenv
# .env.example
# Authentication is enabled by default. Use false only on an isolated developer machine.
RAG_AUTH_ENABLED=true
RAG_AUTH_KEYS={}

# Experimental RSS/API/database sources stay sealed until connector hardening is complete.
RAG_DATA_SOURCES_ENABLED=false
```

At the start of `auto_index_on_startup()`:

```python
log_security_mode()
```

Implement the focused helper without touching indexing:

```python
def log_security_mode() -> None:
    if not settings.auth_enabled:
        logger.critical(
            "SECURITY WARNING: RAG_AUTH_ENABLED=false; "
            "anonymous development mode has administrator-equivalent access"
        )
```

- [ ] **Step 4: Add source gate and recursive redaction**

```python
# rag/api_security.py
_SOURCE_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "connection_string",
    "password",
    "secret",
    "token",
}


async def require_data_sources_admin(
    request: Request,
    authorization: str = Header(default=""),
    api_key: str | None = Security(api_key_header),
) -> dict:
    if not settings.data_sources_enabled:
        raise HTTPException(status_code=404, detail="资源不存在")
    user = await require_user(request, authorization, api_key)
    return require_admin(user)


def redact_source_config(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SOURCE_SECRET_KEYS else redact_source_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_source_config(item) for item in value]
    return value
```

Give every `/sources` route `user: dict = Depends(require_data_sources_admin)`. Because the feature check runs inside that dependency before principal resolution, disabled routes return `404` even without credentials. Return `redact_source_config(config)` from create/list responses. Keep `_sync_source` and all data-source classes unchanged.

- [ ] **Step 5: Rewrite legacy source tests to use the isolated fixture**

Remove module-level `TestClient(app)` and direct `user_db._conn` cleanup from `tests/test_data_sources_api.py`. Each legacy CRUD/sync test must set:

```python
monkeypatch.setattr(settings, "data_sources_enabled", True)
_, headers = make_user("source-admin", is_admin=True)
```

and send `headers=headers` with the request.

- [ ] **Step 6: Run source and configuration tests**

Run: `python -m pytest tests/test_data_sources_security.py tests/test_data_sources_api.py -v`

Expected: PASS; the disabled route is indistinguishable from an absent route and enabled non-admin access is `403`.

- [ ] **Step 7: Commit**

```bash
git add config.py .env.example rag/api.py tests/test_data_sources_security.py tests/test_data_sources_api.py
git commit -m "fix: fail closed and seal data sources"
```

---

### Task 3: Secure Default-Library Upload and Authenticated Frontend Copy

**Files:**
- Modify: `rag/api.py:731-802,882-930,2473-2480`
- Modify: `rag/batch_importer.py`
- Create: `tests/test_upload_security.py`
- Modify: `tests/test_code_review_fixes.py`
- Create: `frontend/src/api/files.ts`
- Create: `frontend/src/api/__tests__/files.test.ts`
- Modify: `frontend/src/views/KnowledgeDetailView.vue:299-320`

**Interfaces:**
- Consumes: `require_user(...) -> dict` and the existing `check_doc_permission(...)`; Task 5 replaces the latter call with `require_doc_view(...)`.
- Produces: `downloadLibraryFile(filename: string, authHeaders: Record<string, string>) -> Promise<Blob>`

- [ ] **Step 1: Write backend tests that observe disk and indexing side effects**

```python
# tests/test_upload_security.py
from pathlib import Path


def test_anonymous_upload_is_rejected_before_body_is_persisted(isolated_api):
    response = isolated_api.post(
        "/upload",
        files={"file": ("secret.txt", b"private text", "text/plain")},
    )

    assert response.status_code == 401
    assert list(Path(isolated_api.app.state.upload_dir).glob("*")) == []


def test_existing_filename_returns_conflict_without_overwrite(isolated_api, make_user):
    _, headers = make_user("alice")
    upload_dir = isolated_api.app.state.upload_dir
    existing = upload_dir / "same.txt"
    existing.write_bytes(b"old")

    response = isolated_api.post(
        "/upload",
        headers=headers,
        files={"file": ("same.txt", b"new", "text/plain")},
    )

    assert response.status_code == 409
    assert existing.read_bytes() == b"old"


def test_upload_directory_is_not_publicly_mounted(isolated_api):
    secret = isolated_api.app.state.upload_dir / "mounted-secret.txt"
    secret.write_text("must not leak", encoding="utf-8")

    response = isolated_api.get("/data/upload/mounted-secret.txt")

    assert response.status_code == 404
    assert "must not leak" not in response.text


def test_all_ingest_routes_authenticate_before_parsing(isolated_api):
    upload = isolated_api.post(
        "/upload",
        files={"file": ("secret.txt", b"private", "text/plain")},
    )
    index = isolated_api.post(
        "/index",
        files={"file": ("secret.txt", b"private", "text/plain")},
    )
    batch = isolated_api.post(
        "/batch-import",
        files={"file": ("secret.csv", b"question,answer\nq,a", "text/csv")},
    )
    index_all = isolated_api.post("/index-all")

    assert [upload.status_code, index.status_code, batch.status_code, index_all.status_code] == [
        401,
        401,
        401,
        401,
    ]


def test_index_failure_compensates_file_and_permission(
    isolated_api, make_user, monkeypatch
):
    _, headers = make_user("alice")
    monkeypatch.setattr("rag.api.load_document", lambda path: "valid")

    def fail_index(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(
        "rag.knowledge_base.KnowledgeBaseManager.add_document",
        fail_index,
    )

    response = isolated_api.post(
        "/upload",
        headers=headers,
        files={"file": ("failed.txt", b"private", "text/plain")},
    )

    assert response.status_code == 400
    assert list(isolated_api.app.state.upload_dir.glob("*")) == []
    assert (
        isolated_api.app.state.user_db.get_document_permission("failed.txt", "rag_docs")
        is None
    )
```

Store the temporary paths in the Task 1 fixture:

```python
api.app.state.upload_dir = upload_dir
api.app.state.kb_files_root = kb_root
```

- [ ] **Step 2: Run backend tests and confirm the real leaks**

Run: `python -m pytest tests/test_upload_security.py -v`

Expected: FAIL: anonymous upload reaches file handling, duplicate content is overwritten, and the real file is served from `/data/upload`.

- [ ] **Step 3: Make upload authenticate before reading and compensate every later failure**

Extract the shared `/upload` and legacy `/index` behavior into:

```python
async def _ingest_default_document(
    *,
    content: bytes,
    filename: str,
    user: dict,
    db: UserDB,
) -> dict:
```

Both routes authenticate first, read at most `MAX_FILE_SIZE + 1`, then call this helper. The helper starts with:

```python
filename = Path(filename or "upload.txt").name
suffix = Path(filename).suffix.lower()
if suffix not in SUPPORTED_EXTENSIONS:
    raise HTTPException(status_code=400, detail="不支持的文件格式")

dest = DATA_DIR / filename
if dest.exists() or db.get_document_permission(filename, "rag_docs"):
    raise HTTPException(status_code=409, detail="同名文件已存在")

if len(content) > MAX_FILE_SIZE:
    raise HTTPException(status_code=413, detail="文件超过 10 MB 限制")
```

Write a random sibling stage file, validate parsing, create permission, index, and atomically promote:

```python
stage_dir = DATA_DIR / ".staging"
stage_dir.mkdir(parents=True, exist_ok=True)
stage = stage_dir / f"{uuid.uuid4().hex}-{filename}"
perm_id: int | None = None
indexed = False
reserved = False
promoted = False
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest.touch(exist_ok=False)
    reserved = True
    stage.write_bytes(content)
    await asyncio.to_thread(load_document, str(stage))
    perm_id = await asyncio.to_thread(
        db.create_document_permission, filename, "rag_docs", user["id"], 1, False
    )
    manager = KnowledgeBaseManager()
    count = await asyncio.to_thread(
        manager.add_document,
        "rag_docs",
        str(stage),
        doc_name=filename,
        doc_permission_id=perm_id,
    )
    indexed = True
    os.replace(stage, dest)
    promoted = True
except FileExistsError as exc:
    raise HTTPException(status_code=409, detail="同名文件已存在") from exc
except Exception as exc:
    if indexed:
        try:
            await asyncio.to_thread(
                KnowledgeBaseManager().remove_document, "rag_docs", filename
            )
        except Exception:
            logger.exception("upload vector rollback failed", extra={"filename": filename})
    if perm_id is not None:
        try:
            await asyncio.to_thread(db.delete_document_permission, perm_id)
        except Exception:
            logger.exception("upload permission rollback failed", extra={"filename": filename})
    logger.exception(
        "upload failed",
        extra={"user_id": user["id"], "filename": filename},
    )
    raise HTTPException(status_code=400, detail="文件处理失败") from exc
finally:
    stage.unlink(missing_ok=True)
    if reserved and not promoted:
        dest.unlink(missing_ok=True)
    try:
        stage_dir.rmdir()
    except OSError:
        pass
```

Do not put `str(exc)`, local paths, or connector errors in the response. Log `exc_info=True` server-side with user ID and filename.

- [ ] **Step 4: Secure batch import and full reindex**

Add an optional explicit source name to `BatchImporter.parse`:

```python
def parse(
    self,
    file_path: str,
    mode: str,
    config: dict | None = None,
    doc_name: str | None = None,
) -> list[Chunk]:
    rows = self._read_csv(file_path) if Path(file_path).suffix.lower() == ".csv" else self._read_excel(file_path)
    source_name = doc_name or Path(file_path).stem
    if mode == "qa_pair":
        return self._parse_qa_pair(rows, config or {}, source_name)
    if mode == "document":
        return self._parse_document(rows, config or {}, source_name)
    if mode == "table":
        return self._parse_table(rows, source_name)
    raise ValueError("不支持的导入模式")
```

Change the three private parse helpers to accept `doc_name: str` directly. In `/batch-import`, authenticate before reading, derive `doc_name = Path(file.filename).stem`, reject an existing permission with `409`, parse with `doc_name=doc_name`, create one private permission owned by `user["id"]`, and call:

```python
add_to_collection(
    "rag_docs",
    chunks,
    embeddings,
    doc_permission_id=permission_id,
)
```

On vector failure, delete that permission and call `delete_doc("rag_docs", doc_name)` before returning the generic `400` error. Make `/index-all` require `require_admin(user)` and return generic errors.

- [ ] **Step 5: Remove the unsafe mount and replace the false regression test**

Delete these declarations and mount:

```python
DATA_UPLOAD_DIR = DATA_DIR_PATH / "upload"
if DATA_UPLOAD_DIR.is_dir():
    app.mount("/data/upload", StaticFiles(directory=str(DATA_UPLOAD_DIR)), name="data-upload")
```

Remove the old test that requested a nonexistent hard-coded filename and asserted only `status_code != 200`. Keep the real-file assertion from `test_upload_security.py`.

- [ ] **Step 6: Write the failing frontend download-helper test**

```typescript
// frontend/src/api/__tests__/files.test.ts
import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadLibraryFile } from '../files'

describe('downloadLibraryFile', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('downloads through the authenticated API route', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(['private']), { status: 200 })
    )
    vi.stubGlobal('fetch', fetchMock)

    const blob = await downloadLibraryFile('a b.txt', { Authorization: 'Bearer token' })

    expect(await blob.text()).toBe('private')
    expect(fetchMock).toHaveBeenCalledWith('/files/a%20b.txt/download', {
      headers: { Authorization: 'Bearer token' }
    })
  })

  it('throws without returning a blob when authorization fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 403 })))

    await expect(downloadLibraryFile('secret.txt', {})).rejects.toThrow('无法读取文件')
  })
})
```

- [ ] **Step 7: Run the frontend test and verify the helper is absent**

Run: `npm exec -- vitest run src/api/__tests__/files.test.ts`

Working directory: `frontend`

Expected: FAIL because `frontend/src/api/files.ts` does not exist.

- [ ] **Step 8: Implement the helper and update the KB detail view**

```typescript
// frontend/src/api/files.ts
export async function downloadLibraryFile(
  filename: string,
  authHeaders: Record<string, string>
): Promise<Blob> {
  const response = await fetch(`/files/${encodeURIComponent(filename)}/download`, {
    headers: authHeaders
  })
  if (!response.ok) {
    throw new Error('无法读取文件')
  }
  return response.blob()
}
```

In `KnowledgeDetailView.vue`, import `downloadLibraryFile` and replace the `/data/upload` fetch:

```typescript
const blob = await downloadLibraryFile(
  selectedFile.value.name,
  authStore.getAuthHeaders()
)
formData.append('file', blob, selectedFile.value.name)
```

- [ ] **Step 9: Run backend/frontend focused tests and build**

Run: `python -m pytest tests/test_upload_security.py tests/test_code_review_fixes.py -v`

Expected: PASS.

Run: `npm exec -- vitest run src/api/__tests__/files.test.ts`

Working directory: `frontend`

Expected: PASS.

Run: `npm run build`

Working directory: `frontend`

Expected: exit code 0.

- [ ] **Step 10: Commit**

```bash
git add rag/api.py rag/batch_importer.py tests/test_upload_security.py tests/test_code_review_fixes.py frontend/src/api/files.ts frontend/src/api/__tests__/files.test.ts frontend/src/views/KnowledgeDetailView.vue
git commit -m "fix: secure uploaded file access"
```

---

### Task 4: Knowledge-Base ACL Consistency

**Files:**
- Modify: `rag/api_security.py`
- Modify: `rag/permissions.py:75-136`
- Modify: `rag/api.py:1242-1669`
- Create: `tests/test_kb_security_boundaries.py`

**Interfaces:**
- Consumes: `get_user_db(request)`, `require_user(...)`
- Produces: `require_kb_view(db: UserDB, kb_id: str, user: dict) -> dict`
- Produces: `require_kb_edit(db: UserDB, kb_id: str, user: dict) -> dict`

- [ ] **Step 1: Write a permission matrix that fails against the current shared-scope checks**

```python
# tests/test_kb_security_boundaries.py
import pytest
from fastapi import HTTPException

from rag.api_security import require_kb_edit, require_kb_view


def test_shared_kb_is_visible_only_to_owner_admin_and_named_share(isolated_api, make_user):
    owner, _ = make_user("owner")
    shared, _ = make_user("shared")
    outsider, _ = make_user("outsider")
    admin, _ = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_finance", "Finance", owner["id"], "private")
    db.share_kb("kb_finance", shared["id"], owner["id"], "view")

    assert require_kb_view(db, "kb_finance", owner)["kb_id"] == "kb_finance"
    assert require_kb_view(db, "kb_finance", shared)["kb_id"] == "kb_finance"
    assert require_kb_view(db, "kb_finance", admin)["kb_id"] == "kb_finance"
    with pytest.raises(HTTPException) as exc:
        require_kb_view(db, "kb_finance", outsider)
    assert exc.value.status_code == 403


def test_view_share_cannot_edit_but_edit_share_can(isolated_api, make_user):
    owner, _ = make_user("owner")
    viewer, _ = make_user("viewer")
    editor, _ = make_user("editor")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    db.share_kb("kb_ops", viewer["id"], owner["id"], "view")
    db.share_kb("kb_ops", editor["id"], owner["id"], "edit")

    with pytest.raises(HTTPException) as exc:
        require_kb_edit(db, "kb_ops", viewer)
    assert exc.value.status_code == 403
    assert require_kb_edit(db, "kb_ops", editor)["kb_id"] == "kb_ops"


def test_missing_metadata_is_conflict_for_mutation(isolated_api, make_user):
    user, _ = make_user("alice")

    with pytest.raises(HTTPException) as exc:
        require_kb_edit(isolated_api.app.state.user_db, "orphan_collection", user)

    assert exc.value.status_code == 409
```

```python
def test_shared_scope_is_consistent_across_list_detail_and_query(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api
    from rag.knowledge_base import KnowledgeBaseInfo
    from rag.models import Chunk

    owner, _ = make_user("owner")
    shared, shared_headers = make_user("shared")
    outsider, outsider_headers = make_user("outsider")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_finance", "Finance", owner["id"], "private")
    db.share_kb("kb_finance", shared["id"], owner["id"], "view")
    db.create_document_permission("public.txt", "kb_finance", owner["id"], scope="public")

    class FakeManager:
        def list_kbs(self):
            return [KnowledgeBaseInfo("kb_finance", "Finance", 1, "")]

        def search(self, kb_id, question, top_k):
            return [Chunk("allowed", "public.txt", 0)]

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)

    assert isolated_api.get("/knowledge-bases", headers=outsider_headers).json() == []
    assert (
        isolated_api.get("/knowledge-bases/kb_finance", headers=outsider_headers).status_code
        == 403
    )
    assert (
        isolated_api.post(
            "/knowledge-bases/kb_finance/query",
            headers=outsider_headers,
            json={"question": "budget"},
        ).status_code
        == 403
    )
    assert (
        isolated_api.get("/knowledge-bases/kb_finance", headers=shared_headers).status_code
        == 200
    )
    assert (
        isolated_api.post(
            "/knowledge-bases/kb_finance/query",
            headers=shared_headers,
            json={"question": "budget"},
        ).status_code
        == 200
    )


def test_orphan_collection_delete_is_conflict(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api

    _, headers = make_user("alice")

    class FakeManager:
        def delete_kb(self, kb_id):
            raise AssertionError("authorization must fail before collection deletion")

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)
    response = isolated_api.delete("/knowledge-bases/kb_orphan", headers=headers)

    assert response.status_code == 409
```

- [ ] **Step 2: Run the matrix and verify shared and missing-metadata failures**

Run: `python -m pytest tests/test_kb_security_boundaries.py -v`

Expected: FAIL because the new helpers do not exist and current list/detail/query checks treat `shared` as public.

- [ ] **Step 3: Implement the KB helper contract**

```python
# rag/api_security.py
from rag.permissions import check_kb_permission


def require_kb_view(db: UserDB, kb_id: str, user: dict) -> dict:
    meta = db.get_kb_metadata(kb_id)
    if meta is None:
        if user.get("is_admin"):
            return {"kb_id": kb_id, "owner_id": 0, "scope": "orphaned"}
        raise HTTPException(status_code=409, detail="知识库元数据缺失")
    check_kb_permission(db, kb_id, user, "view")
    return meta


def require_kb_edit(db: UserDB, kb_id: str, user: dict) -> dict:
    meta = db.get_kb_metadata(kb_id)
    if meta is None:
        raise HTTPException(status_code=409, detail="知识库元数据缺失")
    check_kb_permission(db, kb_id, user, "edit")
    return meta
```

Make `check_kb_permission` allow view only for owner, admin, `scope == "public"`, or an actual `kb_shares` row. Editing remains owner, admin, or an actual `permission="edit"` share.

Require `scope == "shared"` for share-based view/edit. In the KB scope endpoint, reject `scope="private"` with `409` while share rows still exist; the owner must unshare users first. This prevents stale share rows from silently granting access to a private KB.

- [ ] **Step 4: Route every KB operation through the helper**

Apply this exact mapping in `rag/api.py`:

| Endpoint | Required helper |
|---|---|
| `GET /knowledge-bases` | authenticate, include only KBs where `require_kb_view` succeeds; omit `403/409` resources |
| `GET /knowledge-bases/{kb_id}` | `require_kb_view` |
| `POST /knowledge-bases/{kb_id}/query` | `require_kb_view`, then document filtering in Task 5 |
| `POST /knowledge-bases` | `require_user`; owner is `user["id"]` |
| `DELETE /knowledge-bases/{kb_id}` | `require_kb_edit`; orphan returns `409` |
| `POST /knowledge-bases/{kb_id}/documents` | `require_kb_edit` |
| `DELETE /knowledge-bases/{kb_id}/documents/{doc_name}` | `require_kb_edit` plus Task 5 document edit |
| rename/scope/share/unshare endpoints | `require_kb_edit` |
| TOC/overview update and generation endpoints | view for reads, edit for writes/generation |

Use `db: UserDB = Depends(get_user_db)` and `user: dict = Depends(require_user)` in each affected route; do not parse authorization headers inside route bodies.

The list route performs the helper call rather than reimplementing scope logic:

```python
@app.get("/knowledge-bases")
async def list_knowledge_bases(
    user: dict = Depends(require_user),
    db: UserDB = Depends(get_user_db),
):
    manager = KnowledgeBaseManager()
    result = []
    for kb in await asyncio.to_thread(manager.list_kbs):
        try:
            meta = await asyncio.to_thread(require_kb_view, db, kb.kb_id, user)
        except HTTPException as exc:
            if exc.status_code in {403, 409}:
                continue
            raise
        is_owner = meta["owner_id"] == user["id"]
        shares = db.list_kb_shared_users(kb.kb_id) if is_owner or user.get("is_admin") else []
        result.append(
            KBResponse(
                kb_id=kb.kb_id,
                name=meta.get("name", kb.name),
                doc_count=kb.doc_count,
                scope=meta["scope"],
                is_owner=is_owner,
                shared_users=[
                    {"username": share["username"], "permission": share["permission"]}
                    for share in shares
                ],
            )
        )
    return result
```

Mutation routes begin with `meta = require_kb_edit(db, kb_id, user)` before constructing `KnowledgeBaseManager` or opening SQLite. View routes begin with `meta = require_kb_view(db, kb_id, user)` before search, file reads, or LLM calls.

- [ ] **Step 5: Run KB tests**

Run: `python -m pytest tests/test_kb_security_boundaries.py tests/test_kb_permissions.py tests/test_kb_management.py -v`

Expected: PASS. Update old expectations only where they depended on anonymous or unshared access; do not weaken the new matrix.

- [ ] **Step 6: Commit**

```bash
git add rag/api_security.py rag/permissions.py rag/api.py tests/test_kb_security_boundaries.py tests/test_kb_permissions.py tests/test_kb_management.py
git commit -m "fix: enforce knowledge base ACLs consistently"
```

---

### Task 5: Document ACL, Share Levels, and Protected Records

**Files:**
- Modify: `rag/api_security.py`
- Modify: `rag/permissions.py:17-173`
- Modify: `rag/api.py:804-930,1415-1447,1473-1500,1840-1960`
- Modify: `rag/user_db.py:838-1038`
- Create: `tests/test_document_security_boundaries.py`

**Interfaces:**
- Produces: `require_doc_view(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict`
- Produces: `require_doc_edit(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict`
- Consumes: `filter_chunks_by_permission(db, kb_id, chunks, user) -> list`

- [ ] **Step 1: Write failing tests for historical, shared, protected, and retrieval behavior**

```python
# tests/test_document_security_boundaries.py
import pytest
from fastapi import HTTPException

from rag.api_security import require_doc_edit, require_doc_view
from rag.models import Chunk
from rag.permissions import filter_chunks_by_permission


def test_historical_document_without_permission_is_admin_only(isolated_api, make_user):
    user, _ = make_user("alice")
    admin, _ = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db

    with pytest.raises(HTTPException) as exc:
        require_doc_view(db, "legacy.pdf", "rag_docs", user)
    assert exc.value.status_code == 403
    assert require_doc_view(db, "legacy.pdf", "rag_docs", admin)["doc_name"] == "legacy.pdf"


def test_private_document_is_removed_from_public_kb_results(isolated_api, make_user):
    owner, _ = make_user("owner")
    reader, _ = make_user("reader")
    db = isolated_api.app.state.user_db
    db.create_document_permission("private.pdf", "kb_public", owner["id"], scope="private")
    db.create_document_permission("public.pdf", "kb_public", owner["id"], scope="public")
    chunks = [
        Chunk(text="private", doc_name="private.pdf", chunk_index=0),
        Chunk(text="public", doc_name="public.pdf", chunk_index=0),
        Chunk(text="legacy", doc_name="legacy.pdf", chunk_index=0),
    ]

    filtered = filter_chunks_by_permission(db, "kb_public", chunks, reader)

    assert [chunk.doc_name for chunk in filtered] == ["public.pdf"]


def test_document_share_persists_requested_edit_level(isolated_api, make_user):
    owner, owner_headers = make_user("owner")
    editor, _ = make_user("editor")
    db = isolated_api.app.state.user_db
    doc_id = db.create_document_permission("plan.pdf", "rag_docs", owner["id"])

    response = isolated_api.post(
        f"/documents/{doc_id}/share",
        headers=owner_headers,
        json={"user_id": editor["id"], "permission": "edit"},
    )

    assert response.status_code == 200
    assert db.is_document_shared(doc_id, editor["id"], permission="edit") is True


def test_protected_document_rejects_owner_and_admin_mutation(isolated_api, make_user):
    owner, _ = make_user("owner")
    admin, _ = make_user("admin", is_admin=True)
    db = isolated_api.app.state.user_db
    db.create_document_permission("policy.pdf", "rag_docs", owner["id"], protected=True, scope="public")

    for user in (owner, admin):
        with pytest.raises(HTTPException) as exc:
            require_doc_edit(db, "policy.pdf", "rag_docs", user)
        assert exc.value.status_code == 403
```

- [ ] **Step 2: Run tests and verify current fail-open behavior**

Run: `python -m pytest tests/test_document_security_boundaries.py -v`

Expected: FAIL because missing permission records are public, legacy chunks survive filtering, `ShareRequest.permission` is ignored, and admin can mutate protected records.

- [ ] **Step 3: Implement document helper contract and filtering**

```python
# rag/api_security.py
from rag.permissions import check_doc_permission


def require_doc_view(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict:
    doc = db.get_document_permission(doc_name, kb_id)
    if doc is None:
        if user.get("is_admin"):
            return {"id": 0, "doc_name": doc_name, "kb_id": kb_id, "historical": True}
        raise HTTPException(status_code=403, detail="文档尚未建立权限记录")
    check_doc_permission(db, doc_name, kb_id, user, "view")
    return doc


def require_doc_edit(db: UserDB, doc_name: str, kb_id: str, user: dict) -> dict:
    doc = db.get_document_permission(doc_name, kb_id)
    if doc is None:
        raise HTTPException(status_code=409, detail="文档权限记录缺失")
    if doc.get("protected"):
        raise HTTPException(status_code=403, detail="受保护文档不可修改")
    check_doc_permission(db, doc_name, kb_id, user, "edit")
    return doc
```

Change `check_doc_permission` and `filter_chunks_by_permission` so `perm is None` rejects/removes for non-admin users. Keep `public`, owner, and explicit document share behavior.

- [ ] **Step 4: Persist and return the requested share permission**

Change the API call from:

```python
db.share_document(doc_id, req.user_id, user["id"])
```

to:

```python
db.share_document(doc_id, req.user_id, user["id"], req.permission)
```

Return the saved `permission` in the endpoint response and include it in `list_shared_users`. Reject share and unshare changes when the document is protected.

Keep `scope` and share rows synchronized in `UserDB`:

```python
def share_document(self, doc_id: int, user_id: int, granted_by: int, permission: str = "view") -> int:
    with self._lock:
        cur = self._conn.execute(
            "INSERT INTO document_shares (doc_id, user_id, granted_by, permission) VALUES (?, ?, ?, ?)",
            (doc_id, user_id, granted_by, permission),
        )
        self._conn.execute(
            "UPDATE document_permissions SET scope = 'shared', is_public = 0 "
            "WHERE id = ? AND scope = 'private'",
            (doc_id,),
        )
        self._conn.commit()
        return cur.lastrowid
```

When the final share is removed, change `scope='shared'` to `scope='private'`. When visibility is toggled on, set `is_public=1, scope='public'`; when toggled off, set `scope='shared'` if share rows remain and `scope='private'` otherwise.

- [ ] **Step 5: Apply document checks to every affected path**

Use `require_doc_view` for authenticated download and document-permission reads. Use `require_doc_edit` for delete, visibility, downloadability, share/unshare, KB document removal, and any document metadata write. Check duplicate protected files before upload returns `409`; never remove or replace them.

In `/knowledge-bases/{kb_id}/query`, call:

```python
chunks = manager.search(kb_id, req.question, top_k=req.top_k * 3)
visible_chunks = filter_chunks_by_permission(db, kb_id, chunks, user)
return {
    "kb_id": kb_id,
    "results": [
        {"doc_name": chunk.doc_name, "chunk_index": chunk.chunk_index, "text": chunk.text}
        for chunk in visible_chunks[:req.top_k]
    ],
}
```

Also make `/files` hide permission-less historical files from normal users, `/tags` omit tags from inaccessible document payloads, and `/query` require the resolved `user` and pass it to `pipeline.query`. This closes the non-KB-detail retrieval paths, not only the explicit KB endpoint.

- [ ] **Step 6: Run document, file, and KB regressions**

Run: `python -m pytest tests/test_document_security_boundaries.py tests/test_document_permissions.py tests/test_upload_security.py tests/test_kb_security_boundaries.py -v`

Expected: PASS; no private or historical chunk appears for a normal user.

- [ ] **Step 7: Commit**

```bash
git add rag/api_security.py rag/permissions.py rag/api.py rag/user_db.py tests/test_document_security_boundaries.py tests/test_document_permissions.py
git commit -m "fix: enforce document ACL and protected state"
```

---

### Task 6: Remove Permission-Unsafe Final-Answer Caching

**Files:**
- Modify: `rag/pipeline.py:18,65,73-113` and all final-answer cache writes in `query`/`query_stream`
- Create: `tests/test_pipeline_cache_security.py`

**Interfaces:**
- Preserves: `RAGPipeline._prepare_context(..., user: dict | None, kb_id: str | None)`
- Preserves: `RedisCache` class for future ACL-versioned caching

- [ ] **Step 1: Write a regression test proving a preseeded answer is ignored**

```python
# tests/test_pipeline_cache_security.py
import threading
from types import SimpleNamespace

from rag.pipeline import RAGPipeline


def test_prepare_context_never_returns_question_only_cached_answer(monkeypatch):
    import rag.agent

    pipeline = RAGPipeline.__new__(RAGPipeline)
    cache_reads = []

    class SeededCache:
        def get(self, question):
            cache_reads.append(question)
            return "other user's private answer"

    pipeline._cache = SeededCache()
    pipeline._user_db = None
    pipeline._kb_id = "kb_public"
    monkeypatch.setattr(rag.agent, "route_question", lambda question: "agent")

    prepared, error = pipeline._prepare_context(
        "same question",
        session_id="s1",
        doc_name=None,
        user={"id": 22, "is_admin": False},
        kb_id="kb_public",
    )

    assert error is None
    assert cache_reads == []
    assert prepared == {"route": "agent", "question": "same question"}


def test_query_does_not_write_final_answer_cache():
    pipeline = RAGPipeline.__new__(RAGPipeline)
    cache_writes = []

    class CacheSpy:
        def set(self, question, answer):
            cache_writes.append((question, answer))

    class MemorySpy:
        def add_message(self, session_id, role, content):
            return None

        def should_summarize(self, session_id):
            return False

    pipeline._cache = CacheSpy()
    pipeline.session_id = None
    pipeline._prepare_context = lambda *args, **kwargs: (
        {"route": "agent", "question": "same question"},
        None,
    )
    pipeline.agent = SimpleNamespace(run=lambda question: "fresh answer")
    pipeline._agent_lock = threading.Lock()
    pipeline._wrap_agent_tools = lambda calls: None
    pipeline._unwrap_agent_tools = lambda: None
    pipeline.tracker = SimpleNamespace(save=lambda trace: None)
    pipeline.memory = MemorySpy()

    result = pipeline.query("same question", session_id="s1")

    assert result.answer == "fresh answer"
    assert cache_writes == []
```

- [ ] **Step 2: Run the regression test**

Run: `python -m pytest tests/test_pipeline_cache_security.py -v`

Expected: FAIL because `_prepare_context` reads `_cache.get(question)` before routing and ACL filtering.

- [ ] **Step 3: Remove final-answer reads and writes**

Delete the cache lookup branch from `_prepare_context`:

```python
cached = self._cache.get(question)
if cached:
    return {"route": "cache", "answer": cached}
```

Delete every `self._cache.set(question, answer)` in synchronous and streaming completion paths. Keep the `RedisCache` import and construction only if another non-answer use remains; otherwise remove the field from `RAGPipeline` while leaving `rag/redis_cache.py` untouched.

Do not move caching after ACL filtering in this phase: revocation still cannot invalidate a user-specific answer safely.

- [ ] **Step 4: Run pipeline and streaming tests**

Run: `python -m pytest tests/test_pipeline_cache_security.py tests/test_pipeline.py tests/test_query_stream.py tests/test_query_permission_filter.py -v`

Expected: PASS; retrieval, reranking, ACL filtering, and generation still execute.

- [ ] **Step 5: Commit**

```bash
git add rag/pipeline.py tests/test_pipeline_cache_security.py
git commit -m "fix: disable permission-unsafe answer cache"
```

---

### Task 7: Persistent KB File Lifecycle and Compensation

**Files:**
- Create: `rag/kb_file_store.py`
- Modify: `rag/user_db.py:88-99,280-340` and add KB-document methods near metadata methods
- Modify: `rag/api.py:1343-1447,1510-1775`
- Create: `tests/test_kb_file_lifecycle.py`

**Interfaces:**
- Produces: `stage_upload(root: Path, safe_filename: str, content: bytes) -> Path`
- Produces: `final_document_path(root: Path, kb_id: str, document_id: int, safe_filename: str) -> Path`
- Produces: `promote_stage(stage: Path, destination: Path) -> Path`
- Produces: `resolve_stored_path(root: Path, stored_path: str) -> Path`
- Produces DB methods:
  - `create_kb_document_pending(kb_id: str, filename: str) -> int`
  - `finish_kb_document(document_id: int, file_path: str, toc: str, summary: str, chunk_count: int) -> None`
  - `set_kb_document_state(document_id: int, status: str, last_error: str = "") -> None`
  - `get_kb_document(kb_id: str, filename: str) -> dict | None`
  - `list_kb_documents(kb_id: str) -> list[dict]`
  - `delete_kb_document(document_id: int) -> None`

- [ ] **Step 1: Write storage and endpoint lifecycle tests**

```python
# tests/test_kb_file_lifecycle.py
from pathlib import Path

import pytest

from rag.kb_file_store import (
    final_document_path,
    promote_stage,
    resolve_stored_path,
    stage_upload,
)


def test_kb_document_path_contains_kb_and_database_document_id(tmp_path):
    stage = stage_upload(tmp_path, "report.pdf", b"content")
    destination = final_document_path(tmp_path, "kb_finance", 42, "report.pdf")

    result = promote_stage(stage, destination)

    assert result == tmp_path / "kb_finance" / "42" / "report.pdf"
    assert result.read_bytes() == b"content"
    assert not stage.exists()


def test_final_path_rejects_traversal_components(tmp_path):
    with pytest.raises(ValueError):
        final_document_path(tmp_path, "../other", 2, "report.pdf")


def test_stored_path_outside_kb_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(ValueError):
        resolve_stored_path(tmp_path, str(outside))


def test_kb_upload_record_points_to_existing_file_after_request(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    monkeypatch.setattr("rag.api.load_document", lambda path: "section body")
    monkeypatch.setattr("rag.api.generate_toc", lambda text: [{"title": "section"}])
    monkeypatch.setattr("rag.api.generate_summary", lambda text: "summary")
    monkeypatch.setattr("rag.knowledge_base.KnowledgeBaseManager.add_document", lambda *args, **kwargs: 3)

    response = isolated_api.post(
        "/knowledge-bases/kb_ops/documents",
        headers=headers,
        files={"file": ("report.txt", b"section body", "text/plain")},
    )

    assert response.status_code == 200
    record = db.get_kb_document("kb_ops", "report.txt")
    assert record["status"] == "indexed"
    assert Path(record["file_path"]).read_text(encoding="utf-8") == "section body"


def test_index_failure_removes_stage_permission_and_pending_record(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")

    def fail_index(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr("rag.api.load_document", lambda path: "body")
    monkeypatch.setattr("rag.knowledge_base.KnowledgeBaseManager.add_document", fail_index)

    response = isolated_api.post(
        "/knowledge-bases/kb_ops/documents",
        headers=headers,
        files={"file": ("report.txt", b"body", "text/plain")},
    )

    assert response.status_code == 400
    assert db.get_kb_document("kb_ops", "report.txt") is None
    assert db.get_document_permission("report.txt", "kb_ops") is None
    assert list(isolated_api.app.state.kb_files_root.rglob("*.*")) == []


def test_kb_delete_removes_document_files_before_collection_metadata(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api

    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    document_id = db.create_kb_document_pending("kb_ops", "report.txt")
    stored = isolated_api.app.state.kb_files_root / "kb_ops" / str(document_id) / "report.txt"
    stored.parent.mkdir(parents=True)
    stored.write_text("body", encoding="utf-8")
    db.finish_kb_document(document_id, str(stored), "[]", "summary", 1)
    db.create_document_permission("report.txt", "kb_ops", owner["id"])
    calls = []

    class FakeManager:
        def remove_document(self, kb_id, filename):
            calls.append(("document", kb_id, filename))

        def delete_kb(self, kb_id):
            calls.append(("collection", kb_id))

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)

    response = isolated_api.delete("/knowledge-bases/kb_ops", headers=headers)

    assert response.status_code == 200
    assert calls == [
        ("document", "kb_ops", "report.txt"),
        ("collection", "kb_ops"),
    ]
    assert not stored.exists()
    assert db.get_kb_document("kb_ops", "report.txt") is None
    assert db.get_kb_metadata("kb_ops") is None
```

- [ ] **Step 2: Run tests and verify nonexistent paths**

Run: `python -m pytest tests/test_kb_file_lifecycle.py -v`

Expected: FAIL because KB uploads delete their temporary file and record `data/upload/{filename}`, which may not exist.

- [ ] **Step 3: Implement the focused file-store module**

```python
# rag/kb_file_store.py
from __future__ import annotations

import os
import uuid
from pathlib import Path


def _single_component(value: str, label: str) -> str:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return value


def stage_upload(root: Path, safe_filename: str, content: bytes) -> Path:
    filename = _single_component(safe_filename, "filename")
    staging = root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / f"{uuid.uuid4().hex}-{filename}"
    path.write_bytes(content)
    return path


def final_document_path(
    root: Path, kb_id: str, document_id: int, safe_filename: str
) -> Path:
    safe_kb = _single_component(kb_id, "kb_id")
    filename = _single_component(safe_filename, "filename")
    if document_id <= 0:
        raise ValueError("invalid document_id")
    return root / safe_kb / str(document_id) / filename


def promote_stage(stage: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=False)
    os.replace(stage, destination)
    return destination


def resolve_stored_path(root: Path, stored_path: str) -> Path:
    resolved_root = root.resolve()
    resolved = Path(stored_path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("stored path escapes kb root")
    return resolved
```

Define the production root in `rag/api.py`:

```python
KB_FILES_ROOT = Path(__file__).resolve().parent.parent / "data" / "kb_files"
```

- [ ] **Step 4: Add explicit KB-document database methods and migration**

Add `last_error TEXT DEFAULT ''` to new `kb_documents` tables and an idempotent migration for existing databases. Implement methods using parameterized SQL and `self._lock`; `finish_kb_document` performs:

```sql
UPDATE kb_documents
SET file_path = ?, toc = ?, summary = ?, chunk_count = ?, status = 'indexed', last_error = ''
WHERE id = ?
```

The migration follows the repository's existing idempotent pattern:

```python
try:
    self._conn.execute("SELECT last_error FROM kb_documents LIMIT 1")
except sqlite3.OperationalError:
    self._conn.execute(
        "ALTER TABLE kb_documents ADD COLUMN last_error TEXT DEFAULT ''"
    )
    self._conn.commit()
```

`set_kb_document_state` accepts only `{"pending", "indexed", "deleting", "delete_failed"}` and truncates `last_error` to 500 characters before storage. API responses never return `last_error`.

Use these concrete methods:

```python
def create_kb_document_pending(self, kb_id: str, filename: str) -> int:
    with self._lock:
        cur = self._conn.execute(
            "INSERT INTO kb_documents (kb_id, filename, status) VALUES (?, ?, 'pending')",
            (kb_id, filename),
        )
        self._conn.commit()
        return int(cur.lastrowid)


def finish_kb_document(
    self,
    document_id: int,
    file_path: str,
    toc: str,
    summary: str,
    chunk_count: int,
) -> None:
    with self._lock:
        self._conn.execute(
            "UPDATE kb_documents SET file_path = ?, toc = ?, summary = ?, "
            "chunk_count = ?, status = 'indexed', last_error = '' WHERE id = ?",
            (file_path, toc, summary, chunk_count, document_id),
        )
        self._conn.commit()


def set_kb_document_state(
    self, document_id: int, status: str, last_error: str = ""
) -> None:
    allowed = {"pending", "indexed", "deleting", "delete_failed"}
    if status not in allowed:
        raise ValueError("invalid kb document status")
    with self._lock:
        self._conn.execute(
            "UPDATE kb_documents SET status = ?, last_error = ? WHERE id = ?",
            (status, last_error[:500], document_id),
        )
        self._conn.commit()


def get_kb_document(self, kb_id: str, filename: str) -> dict | None:
    with self._lock:
        row = self._conn.execute(
            "SELECT id, kb_id, filename, file_path, toc, summary, chunk_count, status "
            "FROM kb_documents WHERE kb_id = ? AND filename = ?",
            (kb_id, filename),
        ).fetchone()
    return dict(row) if row else None


def list_kb_documents(self, kb_id: str) -> list[dict]:
    with self._lock:
        rows = self._conn.execute(
            "SELECT id, kb_id, filename, file_path, toc, summary, chunk_count, status "
            "FROM kb_documents WHERE kb_id = ? ORDER BY id",
            (kb_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_kb_document(self, document_id: int) -> None:
    with self._lock:
        self._conn.execute("DELETE FROM kb_documents WHERE id = ?", (document_id,))
        self._conn.commit()
```

- [ ] **Step 5: Replace KB upload with one compensating lifecycle**

Use this exact success order:

1. `require_kb_edit`.
2. Validate filename, extension, `MAX_FILE_SIZE`, and duplicate DB record.
3. `stage_upload`.
4. Parse the staged file and compute TOC/summary.
5. `create_kb_document_pending` to allocate `document_id`.
6. `create_document_permission`.
7. `KnowledgeBaseManager.add_document` using the stage path and permission ID.
8. `promote_stage` to `final_document_path`.
9. `finish_kb_document` with the real path and generated metadata.

Track `document_id`, `permission_id`, `indexed`, `stage`, and `destination`. On failure, compensate in reverse:

```python
if indexed:
    await asyncio.to_thread(KnowledgeBaseManager().remove_document, kb_id, filename)
if permission_id is not None:
    await asyncio.to_thread(db.delete_document_permission, permission_id)
if document_id is not None:
    await asyncio.to_thread(db.delete_kb_document, document_id)
stage.unlink(missing_ok=True)
if destination is not None:
    destination.unlink(missing_ok=True)
```

Log the internal exception, but return `HTTPException(400, "文档处理失败")`.

- [ ] **Step 6: Read TOC/overview content only from stored `file_path`**

Change the TOC and overview generation queries from:

```sql
SELECT filename FROM kb_documents WHERE kb_id = ? AND status = 'indexed'
```

to:

```sql
SELECT id, filename, file_path FROM kb_documents
WHERE kb_id = ? AND status = 'indexed'
```

For each row, call `resolve_stored_path(KB_FILES_ROOT, row["file_path"])` and load that path. Missing/out-of-root paths are logged and skipped; no fallback to `DATA_DIR / filename`. Build document response objects from `id`, `filename`, `toc`, `summary`, `chunk_count`, `status`, and `added_at`; never serialize `last_error` or absolute `file_path`.

- [ ] **Step 7: Implement retryable deletion state**

Deletion must:

```python
record = db.get_kb_document(kb_id, doc_name)
db.set_kb_document_state(record["id"], "deleting")
manager.remove_document(kb_id, doc_name)
db.delete_document_permission(permission["id"])
try:
    resolve_stored_path(KB_FILES_ROOT, record["file_path"]).unlink()
except OSError as exc:
    db.set_kb_document_state(record["id"], "delete_failed", type(exc).__name__)
    raise HTTPException(status_code=500, detail="物理文件删除失败，可重试")
db.delete_kb_document(record["id"])
```

If vector deletion fails, set `delete_failed` and keep metadata, permission, and physical file for retry. Protected records are rejected before state changes.

For whole-KB deletion, use `list_kb_documents(kb_id)` and run the same retryable document deletion lifecycle for every row. Delete the Qdrant collection only after all document vectors, permissions, metadata rows, and files are removed; delete `kb_metadata` last. If a document is `delete_failed`, return `500` and leave the collection plus KB metadata so the request can be retried. Never delete the collection first.

- [ ] **Step 8: Run lifecycle and metadata tests**

Run: `python -m pytest tests/test_kb_file_lifecycle.py tests/test_kb_metadata.py tests/test_kb_permissions.py -v`

Expected: PASS; all indexed KB records point to existing files under the isolated KB root.

- [ ] **Step 9: Commit**

```bash
git add rag/kb_file_store.py rag/user_db.py rag/api.py tests/test_kb_file_lifecycle.py tests/test_kb_metadata.py
git commit -m "fix: persist knowledge base files safely"
```

---

### Task 8: Bind Streaming Chat to the Selected Knowledge Base

**Files:**
- Modify: `rag/api.py:222-228,1160-1210`
- Modify: `frontend/src/stores/chat.ts:137-175`
- Modify: `frontend/src/components/ChatInput.vue:1-17`
- Modify: `frontend/src/views/KnowledgeDetailView.vue:140`
- Create: `tests/test_stream_kb_target.py`
- Modify: `frontend/src/stores/__tests__/chat.test.ts` or the existing chat-store test file

**Interfaces:**
- Extends: `StreamQueryRequest.kb_id: str | None`
- Extends: `sendMessage(question: string, mode?: ChatMode, kbId?: string) -> Promise<void>`
- Consumes: `require_kb_view(db, kb_id, user)`

- [ ] **Step 1: Write a backend test proving stream construction uses the requested KB**

```python
# tests/test_stream_kb_target.py
from collections.abc import AsyncIterator


def test_stream_query_uses_explicit_authorized_kb(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_hr", "HR", owner["id"], "private")
    constructed = []

    class FakePipeline:
        def __init__(self, kb_id, user_db):
            constructed.append((kb_id, user_db))

        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("rag.pipeline.RAGPipeline", FakePipeline)

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "leave policy", "kb_id": "kb_hr"},
    )

    assert response.status_code == 200
    assert constructed == [("kb_hr", db)]


def test_stream_query_rejects_unshared_kb_before_pipeline_creation(
    isolated_api, make_user, monkeypatch
):
    owner, _ = make_user("owner")
    outsider, headers = make_user("outsider")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_hr", "HR", owner["id"], "private")
    constructed = []
    monkeypatch.setattr(
        "rag.pipeline.RAGPipeline",
        lambda **kwargs: constructed.append(kwargs),
    )

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "leave policy", "kb_id": "kb_hr"},
    )

    assert response.status_code == 403
    assert constructed == []
```

- [ ] **Step 2: Write a frontend store assertion**

In the existing chat-store test, mock `fetch`, call:

```typescript
await store.sendMessage('leave policy', 'kb', 'kb_hr')
```

and assert the parsed request body contains:

```typescript
expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
  question: 'leave policy',
  kb_id: 'kb_hr'
})
```

- [ ] **Step 3: Run both tests and confirm current default-library behavior**

Run: `python -m pytest tests/test_stream_kb_target.py -v`

Expected: FAIL because `StreamQueryRequest` has no `kb_id` and the global `rag_docs` pipeline is always used.

Run: `npm exec -- vitest run src/stores`

Working directory: `frontend`

Expected: FAIL because `sendMessage` does not accept or send `kbId`.

- [ ] **Step 4: Implement target selection after authorization**

```python
class StreamQueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1)
    session_id: str | None = None
    conversation_id: int | None = None
    doc_name: str | None = None
    tags: list[str] | None = None
    kb_id: str | None = None
```

At the start of `query_stream`:

```python
user: dict = Depends(require_user)
db: UserDB = Depends(get_user_db)
target_kb = req.kb_id or "rag_docs"
if target_kb != "rag_docs":
    require_kb_view(db, target_kb, user)
    from rag.pipeline import RAGPipeline
    current_pipeline = await asyncio.to_thread(RAGPipeline, kb_id=target_kb, user_db=db)
else:
    with _pipeline_lock.read():
        current_pipeline = pipeline
    if current_pipeline is None:
        raise HTTPException(status_code=400, detail="尚未索引文档")
```

Pass `user=user` and `kb_id=target_kb` to `current_pipeline.query_stream`. Do not fall back to `rag_docs` when an explicit KB fails authorization or initialization.

- [ ] **Step 5: Carry the route KB from the view**

```typescript
// frontend/src/stores/chat.ts
async function sendMessage(question: string, mode?: ChatMode, kbId?: string) {
  const payload: Record<string, unknown> = {
    question,
    conversation_id: currentConvId.value,
    session_id: sessionId.value
  }
  if (mode === 'kb' && kbId) {
    payload.kb_id = kbId
  }
}
```

Extend `ChatInput.vue`:

```typescript
const props = defineProps<{
  mode?: ChatMode
  kbId?: string
}>()

async function handleSend() {
  const q = inputText.value.trim()
  if (!q || chatStore.isStreaming) return
  inputText.value = ''
  await chatStore.sendMessage(q, props.mode, props.kbId)
}
```

In `KnowledgeDetailView.vue`:

```typescript
function askSuggested(question: string) {
  chatStore.sendMessage(question, 'kb', kbId)
}
```

Bind the main input:

```vue
<ChatInput v-if="chatStore.currentConvId" mode="kb" :kb-id="kbId" />
```

- [ ] **Step 6: Run stream and frontend tests**

Run: `python -m pytest tests/test_stream_kb_target.py tests/test_query_stream.py tests/test_kb_security_boundaries.py -v`

Expected: PASS.

Run: `npm exec -- vitest run`

Working directory: `frontend`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rag/api.py frontend/src/stores/chat.ts frontend/src/components/ChatInput.vue frontend/src/views/KnowledgeDetailView.vue tests/test_stream_kb_target.py frontend/src/stores/__tests__
git commit -m "fix: bind chat queries to selected knowledge base"
```

---

### Task 9: Full Regression, Security Review, and Documentation Check

**Files:**
- Modify only files required by concrete failures found in this task.
- Review: `docs/superpowers/specs/2026-07-25-security-boundary-hardening-design.md`
- Review: `docs/superpowers/plans/2026-07-25-security-boundary-hardening.md`

**Interfaces:**
- Consumes every contract defined by Tasks 1-8.
- Produces a clean backend/frontend verification result and an objective post-implementation code review.

- [ ] **Step 1: Run targeted security tests together to detect shared-state pollution**

Run: `python -m pytest tests/test_api_security.py tests/test_data_sources_security.py tests/test_upload_security.py tests/test_kb_security_boundaries.py tests/test_document_security_boundaries.py tests/test_pipeline_cache_security.py tests/test_kb_file_lifecycle.py tests/test_stream_kb_target.py -v`

Expected: PASS with no writes under repository `data/` or `qdrant_data/`.

- [ ] **Step 2: Prove the worktree did not mutate runtime data**

Run: `git status --short data qdrant_data`

Expected: no output. If tracked fixture data is intentionally changed by an existing test, fix that test to use `tmp_path`; do not commit runtime database or vector changes.

- [ ] **Step 3: Run the complete backend suite**

Run: `python -m pytest -v`

Expected: PASS. Security expectation changes must be updated to require authentication or sharing; unrelated behavioral regressions must be fixed in implementation rather than hidden with skips.

- [ ] **Step 4: Run Python static checks**

Run: `python -m ruff check .`

Expected: PASS.

Run: `python -m ruff format --check .`

Expected: PASS.

- [ ] **Step 5: Run the complete frontend suite and production build**

Run: `npm exec -- vitest run`

Working directory: `frontend`

Expected: PASS.

Run: `npm run build`

Working directory: `frontend`

Expected: exit code 0 with `vue-tsc` and Vite build succeeding.

- [ ] **Step 6: Execute the Superpowers code-review gate**

Use `superpowers:requesting-code-review` against the implementation diff. The review must explicitly inspect:

- every `/sources` decorator for feature gate plus admin requirement;
- every KB list/detail/query/mutation path for the same ACL helper;
- every document read/write/share path for protected and historical behavior;
- all references to `/data/upload`;
- all `_cache.get`/`_cache.set` answer paths;
- every use of `kb_documents.file_path`;
- any raw `str(exc)` returned through `HTTPException`;
- tests for accidental access to repository `data/users.db`.

Resolve every Critical and Important finding with a failing regression test first. Re-run Steps 1-5 after the final review fix.

- [ ] **Step 7: Verify spec coverage manually**

Check each section of the design against the implementation:

- authentication defaults and development warning;
- unified principal and ACL boundary;
- private/shared/public KB matrix;
- admin-only historical documents;
- protected document behavior;
- upload conflict and compensation;
- persistent KB file paths and deletion state;
- authenticated frontend copy;
- no upload static mount;
- no final-answer caching;
- disabled/admin-only/redacted sources;
- explicit selected-KB stream target;
- sanitized public errors and security logs.

Expected: every item is represented by both implementation and a test.

- [ ] **Step 8: Commit final verified adjustments**

```bash
git add config.py .env.example rag/api.py rag/api_security.py rag/batch_importer.py rag/kb_file_store.py rag/permissions.py rag/pipeline.py rag/user_db.py tests frontend/src/api frontend/src/components/ChatInput.vue frontend/src/stores/chat.ts frontend/src/views/KnowledgeDetailView.vue
git commit -m "test: verify security boundary hardening"
```

Do not create an empty commit. If Step 6 produces no changes, retain the previous task commit as the final implementation commit.
