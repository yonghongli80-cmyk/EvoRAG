from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_python_dependencies_have_fully_pinned_lock_files():
    for filename in ("requirements.lock", "requirements-dev.lock"):
        lock_path = ROOT / filename
        assert lock_path.exists()

        requirements = [
            line.strip()
            for line in lock_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith(("#", "--"))
        ]
        assert requirements
        assert all("==" in requirement for requirement in requirements)
        assert not any(
            ">=" in requirement or "<" in requirement for requirement in requirements
        )


def test_jwt_dependency_does_not_include_vulnerable_ecdsa_chain():
    for filename in ("requirements.lock", "requirements-dev.lock"):
        locked = (ROOT / filename).read_text(encoding="utf-8").casefold()
        assert "pyjwt==" in locked
        assert "python-jose==" not in locked
        assert "ecdsa==" not in locked


def test_deployment_uses_the_lock_file_and_build_cache():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert "COPY requirements.lock ./" in dockerfile
    assert "-r requirements.lock" in dockerfile
    assert "pip install --no-cache" not in dockerfile

    for relative_path in ("deploy.sh", ".github/workflows/deploy.yml"):
        deployment = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "--no-cache" not in deployment


def test_ci_uses_the_development_lock_file():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "pip install -r requirements-dev.lock" in workflow
