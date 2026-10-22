"""Contract tests for the pip-compile lock files (offline).

``requirements.lock`` is the runtime set (Docker + ``pip-audit``);
``requirements-dev.lock`` is the superset CI installs to run tests.  The old
``tests/test_dependency_lock.py`` only checked that every line is pinned, which
is why missing direct dependencies (``typesafe-sdk``) went unnoticed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parents[1]
LOCK_FILES = ("requirements.lock", "requirements-dev.lock")


def _project_dependency_names() -> tuple[set[str], set[str]]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = {
        canonicalize_name(Requirement(specifier).name)
        for specifier in project["dependencies"]
    }
    dev = {
        canonicalize_name(Requirement(specifier).name)
        for specifier in project.get("optional-dependencies", {}).get("dev", [])
    }
    return runtime, dev


def _locked_names(filename: str) -> set[str]:
    names: set[str] = set()
    for raw_line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement = Requirement(line)
        names.add(canonicalize_name(requirement.name))
    return names


def test_runtime_lock_contains_every_direct_project_dependency():
    runtime, _dev = _project_dependency_names()

    locked = _locked_names("requirements.lock")

    assert runtime - locked == set()


def test_dev_lock_contains_every_direct_project_dependency_and_dev_extra():
    runtime, dev = _project_dependency_names()

    locked = _locked_names("requirements-dev.lock")

    assert (runtime | dev) - locked == set()


def test_typesafe_sdk_is_locked_for_both_runtime_and_ci():
    # P0-01/F-01: rag/api.py imports typesafe_sdk at module import time.
    for filename in LOCK_FILES:
        assert "typesafe-sdk" in _locked_names(filename)


def test_ci_runs_pip_check_and_api_import_smoke():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pip check" in workflow
    assert 'python -c "import rag.api"' in workflow
