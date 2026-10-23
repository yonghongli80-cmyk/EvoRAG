"""Regression coverage for the wiki chunker's runtime Markdown parser."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parents[1]


def test_markdown_it_is_declared_and_locked_for_runtime_and_dev():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    runtime = {
        canonicalize_name(Requirement(spec).name)
        for spec in project["dependencies"]
    }

    assert "markdown-it-py" in runtime

    for filename in ("requirements.lock", "requirements-dev.lock"):
        locked = {
            canonicalize_name(line.split("==", 1)[0].strip())
            for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert "markdown-it-py" in locked, f"{filename} is missing markdown-it-py"
        assert "mdurl" in locked, f"{filename} is missing mdurl"
