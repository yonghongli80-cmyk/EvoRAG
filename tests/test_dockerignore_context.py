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


def test_docker_context_excludes_runtime_uploads_but_keeps_the_repo_bootstrap():
    """The image bootstrap needs .repo_files + the three fixtures, never user uploads."""
    lines = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "data/upload/**" in lines
    assert "!data/upload/.repo_files" in lines
    assert "!data/upload/压测.docx" in lines
    assert "!data/upload/压测.md" in lines
    assert "!data/upload/压测.pdf" in lines
    assert "data/document_versions/**" in lines
    assert "data/kb_files/**" in lines
