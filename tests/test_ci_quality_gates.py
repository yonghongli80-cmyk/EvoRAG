from pathlib import Path


def test_ci_runs_build_and_dependency_audits():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "pip-audit -r requirements.lock" in workflow
    assert "npm audit --omit=dev --audit-level=high" in workflow
    assert "npm run build" in workflow
    assert "ruff check .\n" in workflow
    assert "ruff check . --select" not in workflow
