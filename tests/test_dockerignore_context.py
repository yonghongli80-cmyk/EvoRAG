from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_docker_context_excludes_development_only_directories():
    patterns = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".worktrees",
        "tests",
        "docs",
        "benchmarks",
        "tmp",
        ".venv",
        ".ruff_cache",
    } <= patterns
