"""Keep Windows-only transitive dependencies out of Linux lock installs."""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parents[1]


def test_pywin32_is_selected_only_on_windows_in_both_locks():
    for filename in ("requirements.lock", "requirements-dev.lock"):
        requirements = [
            Requirement(line.strip())
            for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        pywin32 = next(req for req in requirements if canonicalize_name(req.name) == "pywin32")

        assert pywin32.marker is not None, f"{filename} must guard pywin32 with a platform marker"
        assert pywin32.marker.evaluate({"sys_platform": "win32"})
        assert not pywin32.marker.evaluate({"sys_platform": "linux"})
