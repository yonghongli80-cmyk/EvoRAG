"""The document comparison workflow must not pull in LlamaIndex or NLTK."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_comparison_workflow_import_does_not_load_llama_index_or_nltk() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    script = """
import sys
import rag.reliability.llama_workflow

loaded = sorted(
    name for name in sys.modules
    if name == 'llama_index' or name.startswith('llama_index.')
    or name == 'nltk' or name.startswith('nltk.')
)
assert not loaded, f'unexpected heavy workflow dependencies loaded: {loaded}'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
