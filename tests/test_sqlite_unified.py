"""TDD tests: all SQLite DBs should live under data/ by default."""
import inspect
from pathlib import Path

from config import Settings


def test_config_has_memory_db_path():
    """config.py exposes memory_db_path pointing to data/memory.db"""
    expected = str(Path(__file__).resolve().parent.parent / "data" / "memory.db")
    assert Settings.model_fields["memory_db_path"].default == expected


def test_dialogue_memory_resolves_default_db_path_at_runtime():
    """The default must not capture a stale global setting during import."""
    from rag.memory import DialogueMemory

    sig = inspect.signature(DialogueMemory.__init__)
    default = sig.parameters["db_path"].default
    assert default is None


def test_execution_tracker_resolves_default_db_path_at_runtime():
    """The default must not capture a stale global setting during import."""
    from rag.tracker import ExecutionTracker

    sig = inspect.signature(ExecutionTracker.__init__)
    default = sig.parameters["db_path"].default
    assert default is None
