from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_SH = ROOT / "deploy.sh"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


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
