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


def test_deployment_templates_require_an_external_redis_secret():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")

    assert "REDIS_PASSWORD=" in env_example
    assert "key: RAG_REDIS_URL" in render
    assert "sync: false" in render.split("key: RAG_REDIS_URL", 1)[1]
