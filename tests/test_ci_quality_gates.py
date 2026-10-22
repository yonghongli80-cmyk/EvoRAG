"""F-08: CI must not drift from what actually ships.

The 2026-10-22 review found the workflow installed only the dev lock, never built the image,
and had no timeouts or pinned actions — while the image is built from ``requirements.lock``.
These tests pin the parts of ``.github/workflows/`` that keep CI honest, plus the documented
fact that this repository's ``origin`` is Gitee, so GitHub Actions does not run by default.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
DEPLOY = WORKFLOWS / "deploy.yml"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"


def _workflow(name: Path) -> str:
    return name.read_text(encoding="utf-8")


def _load(name: Path) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(_workflow(name))


def test_ci_runs_build_and_dependency_audits():
    workflow = _workflow(CI)

    assert "pip-audit -r requirements.lock" in workflow
    assert "npm audit --omit=dev --audit-level=high" in workflow
    assert "npm run build" in workflow
    assert "ruff check .\n" in workflow
    assert "ruff check . --select" not in workflow


def test_every_action_is_pinned_to_a_commit_sha():
    """A moved tag is a supply-chain change; the SHA is the reviewed version."""
    pattern = re.compile(r"uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)")
    problems: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        text = _workflow(workflow)
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start : line_end if line_end != -1 else len(text)]
            number = text[: match.start()].count("\n") + 1
            if line.lstrip().startswith("#"):
                continue  # documentation example inside a comment
            ref = match.group("ref")
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                problems.append(f"{workflow.name}:{number} {match.group('action')}@{ref} is not a SHA")
            elif "#" not in line[line.index(ref) + len(ref) :]:
                problems.append(f"{workflow.name}:{number} {match.group('action')} has no version comment")
    assert problems == [], problems


def test_actions_are_not_referenced_by_a_floating_major_tag():
    text = _workflow(CI)

    for action in ("actions/checkout", "actions/setup-python", "actions/setup-node"):
        assert f"{action}@v" not in text
        assert f"{action}@main" not in text


def test_ci_has_timeouts_and_read_only_permissions():
    workflow = _load(CI)

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"], "CI declares no jobs"
    for name, job in workflow["jobs"].items():
        assert job.get("timeout-minutes"), f"job {name} has no timeout-minutes"


def test_ci_installs_the_runtime_lock_the_image_is_built_from():
    workflow = _workflow(CI)

    assert "pip install -r requirements.lock" in workflow
    assert "pip install -r requirements-dev.lock" in workflow
    # Both a dev environment and a runtime-only environment must import the app.
    assert workflow.count('python -c "import rag.api"') >= 2
    assert workflow.count("pip check") >= 2


def test_ci_runs_the_lockfile_contract_tests():
    assert "tests/test_dependency_lock.py" in _workflow(CI)


def test_ci_docker_build_smoke_is_explicit_and_skippable():
    workflow = _workflow(CI)

    assert "docker build" in workflow
    assert "docker info" in workflow  # runtime skip with ::warning, not a silent pass
    assert "vars.RUN_DOCKER_BUILD" in workflow


def test_deploy_workflow_also_pins_actions_and_bounds_its_runtime():
    workflow = _load(DEPLOY)

    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert job.get("timeout-minutes"), f"job {name} has no timeout-minutes"


def test_docs_state_that_origin_is_gitee_and_github_actions_does_not_run_by_default():
    docs = DEPLOYMENT_DOC.read_text(encoding="utf-8")

    assert "Gitee" in docs
    assert "gitee.com" in docs
    assert "GitHub Actions" in docs
    assert "requirements.lock" in docs  # local equivalent of the lock gate
    assert "import rag.api" in docs
