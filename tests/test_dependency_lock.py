from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_python_dependencies_have_a_fully_pinned_lock_file():
    lock_path = ROOT / "requirements.lock"
    assert lock_path.exists()

    requirements = [
        line.strip()
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith(("#", "--"))
    ]
    assert requirements
    assert all("==" in requirement for requirement in requirements)
    assert not any(">=" in requirement or "<" in requirement for requirement in requirements)


def test_deployment_uses_the_lock_file_and_build_cache():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "COPY requirements.lock ./" in dockerfile
    assert "-r requirements.lock" in dockerfile
    assert "pip install --no-cache" not in dockerfile

    for relative_path in ("deploy.sh", "scripts/deploy.sh", ".github/workflows/deploy.yml"):
        deployment = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "--no-cache" not in deployment
