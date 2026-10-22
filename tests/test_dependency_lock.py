import re
import tomllib
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


def _normalise(name: str) -> str:
    """PEP 503 normalisation: ``PyJWT`` and ``pyjwt`` are the same distribution."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_distributions(*, include_dev: bool) -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    requirements = list(project.get("dependencies", []))
    if include_dev:
        for extras in project.get("optional-dependencies", {}).values():
            requirements.extend(extras)
    declared = set()
    for requirement in requirements:
        # "qdrant-client>=1.18,<2" -> "qdrant-client"; "pkg[extra]" -> "pkg".
        name = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0]
        declared.add(_normalise(name))
    return declared


def _locked_distributions(filename: str) -> set[str]:
    locked = set()
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "--", "-")):
            continue
        locked.add(_normalise(stripped.split("==", 1)[0]))
    return locked


def test_every_declared_dependency_is_pinned_in_both_locks():
    """``pip install`` inside the image only proves the lock, not ``pyproject``.

    Declaring a dependency in ``pyproject.toml`` without regenerating the locks is how
    F-01 happened (the clean environment could not import ``rag.api``).  The runtime lock
    must carry every runtime dependency; the dev lock must carry runtime + dev extras.
    """
    runtime = _declared_distributions(include_dev=False)
    everything = _declared_distributions(include_dev=True)
    assert runtime
    assert everything - runtime  # the dev extras exist

    missing_runtime = sorted(runtime - _locked_distributions("requirements.lock"))
    assert missing_runtime == [], f"requirements.lock is missing: {missing_runtime}"

    missing_dev = sorted(everything - _locked_distributions("requirements-dev.lock"))
    assert missing_dev == [], f"requirements-dev.lock is missing: {missing_dev}"


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
