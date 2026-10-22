"""Deployment templates must force an external Redis secret.

F-09 (2026-10-22): ``render.yaml`` was deleted.  It was a single free web service with a
1 GB disk, **no ``qdrant_data`` volume** (I48 in the 2026-06-26 review: every Render deploy
lost the vector index) and no Redis password wiring — it could not express the real
topology (Compose: ``rag-app`` + ``redis`` + local Qdrant volume).  Keeping it around made
it look like a supported deployment target, so the template is gone and the reason is in
``docs/deployment.md``.  These tests keep the remaining single source of truth honest.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compose_redis_is_internal_and_password_protected():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    redis_service = compose.split("  rag-app:", 1)[0]

    assert '"6379:6379"' not in redis_service
    assert "--requirepass" in redis_service
    assert "REDISCLI_AUTH" in redis_service
    assert "${REDIS_PASSWORD:?" in compose
    assert "RAG_REDIS_URL=redis://:${REDIS_PASSWORD" in compose


def test_env_template_requires_an_external_redis_secret():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "REDIS_PASSWORD=" in env_example


def test_render_manifest_is_gone_and_the_reason_is_documented():
    assert not (ROOT / "render.yaml").exists()

    docs = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "render.yaml" in docs
    assert "qdrant_data" in docs
