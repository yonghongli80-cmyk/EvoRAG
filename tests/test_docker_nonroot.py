from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_application_image_runs_as_dedicated_non_root_user():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "useradd -m -u 10001 rag" in dockerfile
    assert "chown -R rag:rag /app" in dockerfile
    assert "USER rag" in dockerfile
    assert dockerfile.index("USER rag") < dockerfile.index('CMD ["python", "start.py"]')


def test_application_data_uses_named_volumes_owned_by_the_image():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "rag_data:/app/data" in compose
    assert "qdrant_data:/app/qdrant_data" in compose
    assert "./data:/app/data" not in compose
    assert "./qdrant_data:/app/qdrant_data" not in compose
    assert "rag_data:" in compose
    assert "qdrant_data:" in compose


def test_container_port_matches_compose_and_healthcheck():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    start = (ROOT / "start.py").read_text(encoding="utf-8")

    assert "EXPOSE 8000" in dockerfile
    assert '"8000:8000"' in compose
    assert "localhost:8000/health" in compose
    assert '"8000" if in_container else "8080"' in start
