"""Architecture ratchet: prevent the two known god-modules from growing again.

These are not the final targets (``api.py`` should reach <250 lines and
``pipeline.py`` should be split), but a ratchet makes every new change pay for
itself: shrink the number or keep it flat. Lower the numbers when you split.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _local_imports() -> int:
    return sum(
        1
        for path in (ROOT / "rag").rglob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("    from ", "    import ", "\tfrom ", "\timport "))
    )


def test_god_modules_do_not_grow_past_the_frozen_budget():
    assert _lines(ROOT / "rag" / "api.py") <= 830
    assert _lines(ROOT / "rag" / "pipeline.py") <= 1760
    assert _local_imports() <= 155
