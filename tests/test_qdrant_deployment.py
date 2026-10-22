"""Phase 1: Qdrant server deployment wiring in Compose and the env template."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_defines_qdrant_service_with_persistent_storage():
    compose = _compose()
    qdrant = compose["services"]["qdrant"]

    assert qdrant["image"].startswith("qdrant/qdrant")
    assert "qdrant_server_data:/qdrant/storage" in qdrant["volumes"]
    assert "qdrant_server_data" in compose["volumes"]
    assert qdrant["restart"] == "unless-stopped"


def test_qdrant_is_reachable_only_from_the_compose_network():
    qdrant = _compose()["services"]["qdrant"]

    # 不发布宿主端口：只有同一 Compose 网络内的服务能连。
    assert "ports" not in qdrant
    assert qdrant.get("expose") == ["6333"]


def test_qdrant_healthcheck_works_without_curl_in_the_image():
    healthcheck = _compose()["services"]["qdrant"]["healthcheck"]
    probe = " ".join(healthcheck["test"])

    # 官方镜像不带 curl/wget（安全考虑）；用 bash 的 /dev/tcp 做 TCP 探活。
    assert "bash" in probe
    assert "/dev/tcp/127.0.0.1/6333" in probe
    assert "curl" not in probe
    assert healthcheck["interval"] and healthcheck["timeout"] and healthcheck["retries"]


def test_qdrant_service_does_not_inject_an_empty_api_key():
    """空字符串会被 Qdrant 当成"已配置的空 key"（qdrant#8856），默认必须完全不设置。"""
    qdrant = _compose()["services"]["qdrant"]

    assert "QDRANT__SERVICE__API_KEY" not in (qdrant.get("environment") or {})
    # 文档化的启用方式仍在文件里（注释行），不是悄悄留一个空默认值。
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "QDRANT__SERVICE__API_KEY" in compose_text


def test_app_waits_for_qdrant_and_reads_phase1_settings():
    compose = _compose()
    app_service = compose["services"]["rag-app"]
    environment = app_service["environment"]

    assert app_service["depends_on"]["qdrant"]["condition"] == "service_healthy"
    assert "RAG_QDRANT_URL=${RAG_QDRANT_URL:-}" in environment
    assert "RAG_QDRANT_API_KEY=${QDRANT_API_KEY:-}" in environment


def test_local_path_volume_is_kept_for_rollback():
    compose = _compose()
    volumes = compose["services"]["rag-app"]["volumes"]

    assert "qdrant_data:/app/qdrant_data" in volumes
    assert "qdrant_data" in compose["volumes"]


def test_env_example_documents_qdrant_settings():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "RAG_QDRANT_URL=" in text
    assert "RAG_QDRANT_API_KEY=" in text
    assert "QDRANT_API_KEY=" in text
    assert "http://qdrant:6333" in text
