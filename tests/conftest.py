"""Shared pytest fixtures.

F-07 (2026-10-22): the suite must not inherit the developer's ``.env`` and must not write
into the repository's production data.  The old file only pinned four variables and patched
the API-level paths, which left three holes:

* ``rag/context.py`` creates ``UserDB(settings.users_db_path)`` **at import time**, so merely
  collecting the suite opened (and migrated) the real ``data/users.db``;
* the machine's ``.env`` sets ``RAG_JEV_MODE=active``, ``RAG_EVOLUTION_ENABLED=true``,
  ``RAG_EVOLUTION_CANDIDATE_ENABLED=true`` and a diagnosis-labels path under ``benchmarks/``,
  so tests written for the defaults silently took the Jev/evolution paths;
* ``rag/auth.py`` loads (and may create) ``data/jwt_secret.txt`` at import time.

Three layers close them:

1. ``PRESEEDED_ENVIRONMENT`` — environment variables outrank ``.env`` for pydantic-settings,
   and they are assigned *before* any ``rag``/``config`` import, so even import-time
   singletons see the sandbox paths and the neutral subsystem flags.
2. ``neutralized_settings`` — patches the live ``Settings`` object for every test, covering
   the ``*_path``/``*_root``/``*_dir`` fields (``tests/test_test_isolation.py`` fails when a
   new path field is added without being covered here).
3. ``sandbox_api_storage`` — per-test private database/uploads/vector store for API tests.

Keep ``tests/test_test_isolation.py`` green when adding a setting: it is the guardrail that
stops the next ambient-configuration leak.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# ── layer 1: environment, applied before any project import ─────────────────
# Session-scoped sandbox for import-time singletons (rag.context.UserDB, rag.auth JWT secret).
_SESSION_SANDBOX = Path(tempfile.mkdtemp(prefix="ragv3-test-session-"))
atexit.register(shutil.rmtree, _SESSION_SANDBOX, ignore_errors=True)

PRESEEDED_ENVIRONMENT = {
    # Fake provider credentials let client constructors run without host .env values.
    # Provider JSON and routing fields are pinned because they can embed credentials or
    # redirect requests; TypeSafe remains blank because Jev uses empty-key as its off switch.
    "RAG_DEEPSEEK_API_KEY": "ragv3-tests-only-generation-key",
    "RAG_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "RAG_DEEPSEEK_AUTH_HEADER": "",
    "RAG_LLM_PROVIDERS_JSON": "[]",
    "RAG_LLM_DEFAULT_MODEL": "deepseek",
    "RAG_BAILIAN_API_KEY": "ragv3-tests-only-embedding-key",
    "RAG_BAILIAN_BASE_URL": "https://api.siliconflow.cn/v1",
    "RAG_RERANK_PROVIDER": "siliconflow",
    "RAG_RERANK_MODEL": "",
    "RAG_RERANK_BASE_URL": "",
    "RAG_RERANK_API_KEY": "",
    "RAG_SILICONFLOW_RERANK_URL": "https://api.siliconflow.cn/v1/rerank",
    "RAG_TYPESAFE_API_KEY": "",
    # Auth: most tests call endpoints anonymously; the auth-enabled subset opts back in.
    # tests/test_auth_enabled_smoke.py is the counterweight for this default.
    "RAG_AUTH_ENABLED": "false",
    "RAG_SELF_REGISTRATION_ENABLED": "true",
    # Deterministic JWT secret: never read or create data/jwt_secret.txt from a test run.
    "RAG_JWT_SECRET": "ragv3-tests-only-jwt-secret-not-for-deployment",
    # Optional subsystems: off unless a test turns one on explicitly.
    "RAG_JEV_MODE": "off",
    "RAG_JEV_KB_ALLOWLIST": "",
    "RAG_EVOLUTION_ENABLED": "false",
    "RAG_EVOLUTION_WORKER_ENABLED": "false",
    "RAG_EVOLUTION_CANDIDATE_ENABLED": "false",
    "RAG_EVOLUTION_CANDIDATE_CANARY_PERCENT": "0",
    "RAG_EVOLUTION_SANDBOX_ALLOW_UNISOLATED": "false",
    "RAG_EVOLUTION_DIAGNOSIS_LABELS_PATH": "",
    "RAG_EVOLUTION_SANDBOX_DATA_SNAPSHOT": "",
    "RAG_EVOLUTION_SANDBOX_REPLAY_FIXTURE": "",
    "RAG_MATERIAL_WORKER_ENABLED": "false",
    "RAG_BACKUP_ENABLED": "false",
    # Persistent state: session sandbox, never the repository.
    "RAG_USERS_DB_PATH": str(_SESSION_SANDBOX / "users.db"),
    "RAG_WIKI_DB_PATH": str(_SESSION_SANDBOX / "wiki.db"),
    "RAG_ANALYSIS_DB_PATH": str(_SESSION_SANDBOX / "analysis.db"),
    "RAG_MEMORY_DB_PATH": str(_SESSION_SANDBOX / "memory.db"),
    "RAG_BM25_DB_PATH": str(_SESSION_SANDBOX / "bm25_index.db"),
    "RAG_QDRANT_PATH": str(_SESSION_SANDBOX / "qdrant"),
    "RAG_EVOLUTION_DB_PATH": str(_SESSION_SANDBOX / "evolution.db"),
    "RAG_DOCUMENT_VERSIONS_ROOT": str(_SESSION_SANDBOX / "document_versions"),
    "RAG_STRUCTURED_DATASETS_ROOT": str(_SESSION_SANDBOX / "structured_datasets"),
    "RAG_BACKUP_ROOT": str(_SESSION_SANDBOX / "backups"),
}
# Assigned (not setdefault): a developer shell exporting RAG_AUTH_ENABLED=true must not
# change what the suite means.
for _key, _value in PRESEEDED_ENVIRONMENT.items():
    os.environ[_key] = _value
del _key, _value

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag.auth import create_token  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

#: Settings fields that hold a filesystem path but do not end with one of the scanned suffixes.
_EXTRA_PATH_FIELDS = (
    "evolution_sandbox_data_snapshot",
    "evolution_sandbox_replay_fixture",
)


def _isolated_path_fields() -> list[str]:
    from config import settings

    derived = [
        name
        for name in type(settings).model_fields
        if name.endswith(("_path", "_root", "_dir"))
    ]
    return sorted(set(derived) | set(_EXTRA_PATH_FIELDS))


@pytest.fixture(autouse=True)
def neutralized_settings(tmp_path_factory, monkeypatch):
    """Pin optional subsystems and every persistent path for the duration of one test.

    A live service may run with Jev active and the evolution canary enabled (this machine
    does); without this fixture the suite would inherit that configuration and take paths
    its tests were never written for, and any Settings path left at its repository default
    is a production write from a test run.
    """
    from config import settings

    sandbox = tmp_path_factory.mktemp("settings-sandbox")

    # Persistence first: every path-shaped field goes to a per-test temporary directory.
    # The explicit neutralisations below win over this loop (e.g. an empty labels path).
    for name in _isolated_path_fields():
        monkeypatch.setattr(settings, name, str(sandbox / name))

    # External services / optional subsystems.
    monkeypatch.setattr(settings, "jev_mode", "off")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "")
    monkeypatch.setattr(settings, "evolution_enabled", False)
    monkeypatch.setattr(settings, "evolution_worker_enabled", False)
    monkeypatch.setattr(settings, "evolution_candidate_enabled", False)
    monkeypatch.setattr(settings, "evolution_candidate_canary_percent", 0)
    monkeypatch.setattr(settings, "evolution_sandbox_allow_unisolated", False)
    monkeypatch.setattr(settings, "evolution_diagnosis_labels_path", "")
    monkeypatch.setattr(settings, "evolution_sandbox_data_snapshot", "")
    monkeypatch.setattr(settings, "evolution_sandbox_replay_fixture", "")
    monkeypatch.setattr(settings, "data_sources_enabled", False)
    monkeypatch.setattr(settings, "material_worker_enabled", False)
    monkeypatch.setattr(settings, "backup_enabled", False)
    # Explicit, not "whatever the host .env says"; isolated_api/auth smoke tests opt back in.
    monkeypatch.setattr(settings, "auth_enabled", False)


@pytest.fixture(autouse=True)
def sandbox_api_storage(tmp_path_factory, monkeypatch):
    """Force every API test away from repository databases and vector files."""
    import rag.api as api
    from config import settings

    sandbox = tmp_path_factory.mktemp("api-sandbox")
    db = UserDB(str(sandbox / "users.db"))
    db.create_material_schema()
    upload_dir = sandbox / "upload"
    kb_files_root = sandbox / "kb_files"
    versions_root = sandbox / "document_versions"
    structured_root = sandbox / "structured_datasets"

    monkeypatch.setattr(api, "user_db", db)
    monkeypatch.setattr(api, "_DB_PATH", sandbox / "users.db")
    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(api, "KB_FILES_ROOT", kb_files_root)
    monkeypatch.setattr("rag.context.user_db", db)
    monkeypatch.setattr("rag.context._DB_PATH", sandbox / "users.db")
    monkeypatch.setattr("rag.context.DATA_DIR", upload_dir)
    monkeypatch.setattr("rag.context.KB_FILES_ROOT", kb_files_root)
    monkeypatch.setattr(settings, "users_db_path", str(sandbox / "users.db"))
    monkeypatch.setattr(settings, "qdrant_path", str(sandbox / "qdrant"))
    monkeypatch.setattr(settings, "analysis_db_path", str(sandbox / "analysis.db"))
    monkeypatch.setattr(settings, "memory_db_path", str(sandbox / "memory.db"))
    # 2026-10-18：这两个默认落在仓库 data/ 里，套件此前直接写生产库（wiki.db、bm25_index.db）。
    monkeypatch.setattr(settings, "wiki_db_path", str(sandbox / "wiki.db"))
    monkeypatch.setattr(settings, "bm25_db_path", str(sandbox / "bm25_index.db"))
    monkeypatch.setattr(settings, "evolution_db_path", str(sandbox / "evolution.db"))
    monkeypatch.setattr(settings, "backup_root", str(sandbox / "backups"))
    monkeypatch.setattr(settings, "document_versions_root", str(versions_root))
    monkeypatch.setattr(settings, "structured_datasets_root", str(structured_root))
    monkeypatch.setattr(api.app.state, "user_db", db)
    monkeypatch.setattr(api.app.state, "upload_dir", upload_dir, raising=False)
    monkeypatch.setattr(api.app.state, "kb_files_root", kb_files_root)
    monkeypatch.setattr(api.app.state, "document_versions_root", versions_root)
    monkeypatch.setattr(api.app.state, "structured_datasets_root", structured_root)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def isolated_wiki_store():
    """Reset the wiki store singleton around every test.

    ``rag.wiki.store`` caches one instance bound to whatever ``settings.wiki_db_path`` was when it
    was first built, so without a reset the first test that touches it decides the database every
    later test writes to — including, before this fixture, the repository's ``data/wiki.db``.
    """
    import contextlib

    from rag.wiki.store import reset_wiki_store

    with contextlib.suppress(Exception):
        reset_wiki_store()
    yield
    with contextlib.suppress(Exception):
        reset_wiki_store()


@pytest.fixture(autouse=True)
def isolated_service_control_log(tmp_path_factory, monkeypatch):
    """Keep the suite out of the operator's ``artifacts/service-control.log``.

    Apply/rollback API tests append lines such as ``candidate 1 applied by admin`` to that file,
    and they are shaped exactly like real operations — 2026-10-12, reading it during a drill, I
    could not tell which lines were mine and which came from the suite. ``service_log_path`` is the
    one place that decides the destination, so pointing it at a temp directory covers every writer:
    the endpoints, ``spawn_restart_helper`` and the detached helper's own ``log`` entry.
    """
    import rag.evolution.api as evolution_api
    import rag.evolution.service_control as control

    sandbox = tmp_path_factory.mktemp("service-control-log")
    default_export_dir = evolution_api.PATCH_EXPORT_DIR
    monkeypatch.setattr(control, "service_log_path", lambda repo_root: sandbox / control.LOG_NAME)
    # Patch exports are addressed through PATCH_EXPORT_DIR while the copy commands retain their
    # production-relative form. Keep each test's files private so a prior approval cannot change
    # the exported flag observed by a later candidate-detail test.
    monkeypatch.setattr(evolution_api, "PATCH_EXPORT_DIR", sandbox / "patches")
    return default_export_dir


@pytest.fixture(autouse=True)
def reset_vector_store():
    import contextlib

    import rag.vector_store as vs
    with contextlib.suppress(Exception):
        vs._client.close() if vs._client is not None else None
    vs._client = None
    # Clear BM25 module-level cache to prevent test pollution
    with contextlib.suppress(Exception):
        import sys
        if "rag.retriever" in sys.modules:
            sys.modules["rag.retriever"]._bm25_cache.clear()
    yield
    with contextlib.suppress(Exception):
        vs._client.close() if vs._client is not None else None
    vs._client = None


@pytest.fixture(autouse=True)
def reset_api_pipeline():
    import rag.api
    rag.api.pipeline = None
    yield


@pytest.fixture(autouse=True)
def reset_stream_state():
    """重置流式相关全局状态，防止测试间污染。"""
    import rag.generator as gen
    yield
    gen._async_client = None
    gen._breaker._failure_count = 0
    gen._breaker.state = "closed"
    gen._breaker._probe_admitted = False


@pytest.fixture
def mock_embedding_indexing(monkeypatch):
    """Replace the embedding model and vector writes so ingest tests run offline.

    The local machine has no model API credentials, and tests must never write
    to the repository ``qdrant_data``. This keeps the versioned ingest service
    fully exercised while stubbing only the external embedding/write boundary.
    """
    import rag.embedder
    import rag.vector_store

    monkeypatch.setattr(
        rag.embedder, "embed", lambda texts: [[0.1] * 1024 for _ in texts]
    )
    monkeypatch.setattr(
        rag.vector_store, "add_to_collection", lambda *args, **kwargs: None
    )


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    """Give API tests a private database and upload root, with authentication enabled."""
    import rag.api as api
    from config import settings

    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    upload_dir = tmp_path / "upload"
    kb_files_root = tmp_path / "kb_files"
    document_versions_root = tmp_path / "document_versions"
    structured_datasets_root = tmp_path / "structured_datasets"
    previous_db = api.user_db
    previous_state_db = getattr(api.app.state, "user_db", None)
    previous_upload_dir = getattr(api.app.state, "upload_dir", None)
    previous_kb_files_root = getattr(api.app.state, "kb_files_root", None)
    previous_document_versions_root = getattr(api.app.state, "document_versions_root", None)

    monkeypatch.setattr(api, "user_db", db)
    monkeypatch.setattr(api, "_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(api, "KB_FILES_ROOT", kb_files_root, raising=False)
    monkeypatch.setattr("rag.context.user_db", db)
    monkeypatch.setattr("rag.context._DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr("rag.context.DATA_DIR", upload_dir)
    monkeypatch.setattr("rag.context.KB_FILES_ROOT", kb_files_root, raising=False)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "qdrant_path", str(tmp_path / "qdrant"))
    monkeypatch.setattr(settings, "document_versions_root", str(document_versions_root), raising=False)
    monkeypatch.setattr(settings, "structured_datasets_root", str(structured_datasets_root), raising=False)
    api.app.state.user_db = db
    api.app.state.upload_dir = upload_dir
    api.app.state.kb_files_root = kb_files_root
    api.app.state.document_versions_root = document_versions_root
    api.app.state.structured_datasets_root = structured_datasets_root

    with TestClient(api.app) as client:
        yield client

    api.user_db = previous_db
    if previous_state_db is None:
        del api.app.state.user_db
    else:
        api.app.state.user_db = previous_state_db
    if previous_upload_dir is None:
        del api.app.state.upload_dir
    else:
        api.app.state.upload_dir = previous_upload_dir
    if previous_kb_files_root is None:
        del api.app.state.kb_files_root
    else:
        api.app.state.kb_files_root = previous_kb_files_root
    if previous_document_versions_root is None:
        del api.app.state.document_versions_root
    else:
        api.app.state.document_versions_root = previous_document_versions_root
    db.close()


@pytest.fixture
def make_user(isolated_api):
    import rag.api as api

    def _make(username: str, is_admin: bool = False):
        user_id = api.app.state.user_db.create_user(username, "Password-123!")
        if is_admin:
            api.app.state.user_db.set_user_admin(user_id, True)
        user = api.app.state.user_db.get_user_by_id(user_id)
        token = create_token({
            "user_id": user_id,
            "username": username,
            "security_version": 1,
        })
        return user, {"Authorization": f"Bearer {token}"}

    return _make
