from pathlib import Path

import pytest

from scripts.create_backup_smoke_fixture import create_smoke_fixture

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_SH = ROOT / "deploy.sh"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"


def test_compose_uses_a_separate_backup_volume():
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "backup_data:/app/backups" in compose
    assert "RAG_BACKUP_ROOT=/app/backups" in compose
    assert "backup_data:" in compose


def test_image_prepares_backup_directory_for_nonroot_user():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "mkdir -p /app/data/upload /app/qdrant_data /app/backups" in dockerfile
    assert dockerfile.index("/app/backups") < dockerfile.index("USER rag")


def test_deploy_scripts_gate_up_on_backup():
    for path in (DEPLOY_SH, DEPLOY_WORKFLOW):
        text = path.read_text(encoding="utf-8")
        backup = text.index(
            "python scripts/backup.py create-if-due --trigger deploy"
        )
        up = text.index("docker compose up -d")
        assert backup < up
        if path == DEPLOY_WORKFLOW:
            assert text.index("set -euo pipefail") < backup


def test_backup_payloads_are_excluded_from_git_and_image_context():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for ignored in ("/backups/", "/RAGv3-backups/"):
        assert ignored in gitignore
    for ignored in ("backups/", "RAGv3-backups/"):
        assert ignored in dockerignore


def test_deployment_docs_explain_backup_and_isolated_restore():
    docs = DEPLOYMENT_DOC.read_text(encoding="utf-8")

    assert "每 7 天" in docs
    assert "保留最近 8 份" in docs
    assert "python scripts/backup.py verify" in docs
    assert "python scripts/backup.py restore-test" in docs
    assert "不包含 Qdrant" in docs
    assert "重新生成向量索引" in docs


def test_smoke_fixture_refuses_repository_target():
    with pytest.raises(ValueError, match="smoke_target_must_be_temporary"):
        create_smoke_fixture(PROJECT_ROOT)
