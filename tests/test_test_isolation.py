"""F-07: the suite must not inherit a developer's ``.env`` or touch repository data.

Two failure modes are pinned here:

1. **Ambient configuration**: this machine's ``.env`` runs Jev in ``active`` mode, enables
   evolution *and* its candidate/canary stage, and points the diagnosis labels at a file in
   ``benchmarks/``.  Tests written for the defaults then take the Jev/evolution paths and
   either fail or write into production databases.
2. **Leaked writes**: any ``Settings`` path field left at its repository default (``data/*.db``,
   ``qdrant_data/``, ``data/embedding_cache.sqlite3`` …) is a production write from a test run.

The fixtures in ``tests/conftest.py`` own the isolation; these tests prove it holds and that
new settings fields cannot silently escape it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Path-shaped settings that are *not* covered by the ``_path``/``_root``/``_dir`` suffix scan.
EXTRA_PATH_FIELDS = (
    "evolution_sandbox_data_snapshot",
    "evolution_sandbox_replay_fixture",
)


def _path_fields() -> list[str]:
    from config import settings

    derived = [
        name
        for name in type(settings).model_fields
        if name.endswith(("_path", "_root", "_dir"))
    ]
    return sorted(set(derived) | set(EXTRA_PATH_FIELDS))


def test_api_tests_never_use_repository_data_paths():
    import rag.api as api
    from config import settings

    repository = Path(__file__).resolve().parents[1]
    assert not Path(api.user_db.db_path).resolve().is_relative_to(repository)
    assert not Path(settings.qdrant_path).resolve().is_relative_to(repository)
    assert not Path(api.DATA_DIR).resolve().is_relative_to(repository)


def test_every_persistent_settings_path_is_redirected_outside_the_repository():
    """A new ``*_path`` field with a repo default fails here until it is isolated."""
    from config import settings

    leaked = []
    for name in _path_fields():
        value = getattr(settings, name)
        if not value:
            continue  # empty = disabled subsystem
        resolved = Path(value).resolve()
        if resolved.is_relative_to(ROOT):
            leaked.append(f"{name}={value}")
    assert leaked == [], (
        "these Settings paths still point into the repository during tests; "
        f"add them to the sandbox fixture in tests/conftest.py: {leaked}"
    )


def test_optional_subsystems_are_neutralised_for_every_test():
    from config import settings

    # Jev is an external paid service; an ambient .env must not route tests through it.
    assert settings.jev_mode == "off"
    assert settings.jev_kb_allowlist == ""
    # Evolution writes observations/candidates to SQLite and can call models.
    assert settings.evolution_enabled is False
    assert settings.evolution_worker_enabled is False
    assert settings.evolution_candidate_enabled is False
    assert settings.evolution_candidate_canary_percent == 0
    assert settings.evolution_sandbox_allow_unisolated is False
    assert settings.evolution_diagnosis_labels_path == ""
    # Background helpers that would otherwise touch the network/disk on lifespan startup.
    assert settings.material_worker_enabled is False
    assert settings.backup_enabled is False


def test_auth_is_disabled_explicitly_rather_than_by_a_developer_dotenv():
    """Most tests call endpoints without a token; that must not depend on the host .env.

    ``tests/test_auth_enabled_smoke.py`` covers the other direction (auth on).
    """
    from config import settings

    assert settings.auth_enabled is False
    assert os.environ["RAG_AUTH_ENABLED"] == "false"


def test_suite_uses_a_fixed_jwt_secret_instead_of_the_repository_secret_file():
    """``rag.auth`` loads the secret at import; reading ``data/jwt_secret.txt`` is a leak."""
    import rag.auth

    assert os.environ.get("RAG_JWT_SECRET")
    assert os.environ["RAG_JWT_SECRET"] == rag.auth._JWT_SECRET


def test_model_clients_use_isolated_credentials_and_provider_configuration():
    """A poisoned clean subprocess must still construct test clients and use test endpoints."""
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    child_env.update({
        "RAG_DEEPSEEK_API_KEY": "host-simulated-generation-sentinel",
        "RAG_DEEPSEEK_BASE_URL": "https://host.invalid/generation",
        "RAG_DEEPSEEK_AUTH_HEADER": "x-host-auth",
        "RAG_LLM_PROVIDERS_JSON": (
            '[{"provider_id":"host-poison","name":"Host Poison",'
            '"model":"poison-model","base_url":"https://host.invalid/v1",'
            '"api_key":"host-simulated-provider-sentinel"}]'
        ),
        "RAG_LLM_DEFAULT_MODEL": "host-poison",
        "RAG_BAILIAN_API_KEY": "host-simulated-embedding-sentinel",
        "RAG_BAILIAN_BASE_URL": "https://host.invalid/embedding",
        "RAG_RERANK_PROVIDER": "host-poison",
        "RAG_RERANK_BASE_URL": "https://host.invalid/rerank",
        "RAG_RERANK_API_KEY": "host-simulated-rerank-sentinel",
        "RAG_SILICONFLOW_RERANK_URL": "https://host.invalid/provider-rerank",
        "RAG_TYPESAFE_API_KEY": "host-simulated-typesafe-sentinel",
    })
    script = r'''
import runpy

runpy.run_path("tests/conftest.py")

from config import settings
from langchain_openai import ChatOpenAI
from rag.llm_pool.models import load_provider_configs_from_settings
from rag.reranker import Reranker

generation_key = "ragv3-tests-only-generation-key"
embedding_key = "ragv3-tests-only-embedding-key"
assert settings.deepseek_api_key == generation_key
assert settings.deepseek_base_url == "https://api.deepseek.com"
assert settings.deepseek_auth_header == ""
assert settings.bailian_api_key == embedding_key
assert settings.bailian_base_url == "https://api.siliconflow.cn/v1"
assert settings.llm_providers_json == "[]"
assert settings.llm_default_model == "deepseek"
assert settings.rerank_provider == "siliconflow"
assert settings.rerank_base_url == ""
assert settings.rerank_api_key == ""
assert settings.typesafe_api_key == ""

generation_client = ChatOpenAI(
    model=settings.deepseek_model,
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
)
assert generation_client.root_client.api_key == generation_key
assert str(generation_client.root_client.base_url).startswith("https://api.deepseek.com")

import rag.embedder as embedder
assert embedder.embed([]) == []
assert embedder.client.api_key == embedding_key
assert str(embedder.client.base_url).startswith("https://api.siliconflow.cn/v1")

providers = load_provider_configs_from_settings(settings)
assert [(provider.provider_id, provider.api_key) for provider in providers] == [
    ("deepseek", generation_key)
]

reranker = Reranker()
assert reranker.provider == "siliconflow"
assert reranker.api_key == embedding_key
assert reranker.url == "https://api.siliconflow.cn/v1/rerank"
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f"isolated client probe failed:\n{result.stdout}\n{result.stderr}"


def test_tests_do_not_mutate_global_settings_directly():
    """Use ``monkeypatch.setattr(settings, ...)``: direct writes silence later tests.

    The guarantee comes from monkeypatch's automatic restore; a plain assignment inside a
    test body (even one with try/finally) is invisible to pytest's fixture teardown and
    leaks into whatever runs next.
    """
    assignment = re.compile(r"^\s*settings\.[a-z_]+\s*=", re.MULTILINE)
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from config import settings" not in text and "import config" not in text:
            continue  # a local fixture/SimpleNamespace named ``settings`` is fine
        for match in assignment.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}")
    assert offenders == [], (
        "tests must not assign to the global settings object directly; "
        f"use monkeypatch.setattr instead: {offenders}"
    )


def test_conftest_neutralises_the_subsystem_flags_before_importing_the_app():
    """The env pre-seed must run before the first ``rag``/``config`` import.

    ``rag.context`` opens ``UserDB(settings.users_db_path)`` at import time and ``rag.auth``
    loads the JWT secret at import time, so a pre-seed that runs later cannot protect them.
    """
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    anchors = [
        index
        for index in (
            conftest.find("import rag"),
            conftest.find("from rag."),
            conftest.find("from config import"),
            conftest.find("import config"),
        )
        if index != -1
    ]
    assert anchors, "conftest should import the application somewhere"
    first_project_import = min(anchors)

    for key in (
        "RAG_AUTH_ENABLED",
        "RAG_JEV_MODE",
        "RAG_EVOLUTION_ENABLED",
        "RAG_EVOLUTION_CANDIDATE_ENABLED",
        "RAG_EVOLUTION_CANDIDATE_CANARY_PERCENT",
        "RAG_JWT_SECRET",
        "RAG_MATERIAL_WORKER_ENABLED",
        "RAG_BACKUP_ENABLED",
        "RAG_USERS_DB_PATH",
        "RAG_QDRANT_PATH",
        "RAG_EVOLUTION_DB_PATH",
    ):
        assert key in conftest[:first_project_import], (
            f"{key} must be pre-seeded before the first project import"
        )
