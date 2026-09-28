"""The layer-2 gate must be able to fail: it refuses to run without a principal.

The gate compares the live question set against a candidate one over a corpus of
knowledge-base questions. Without a principal the pipeline answers every one of
them in its enterprise guard *before* retrieval — measured: ``route=clarification``,
no sources, zero external calls, ~0.05ms — so the two sides compare identical
empty runs and the gate's "no regression" would mean nothing. That is exactly how
the phase-4 e2e reported "route +0.0pp, Top-5 +0.0pp over 20 cases".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings  # noqa: F401 - imported for the script's settings use
from rag.user_db import UserDB
from scripts.benchmark_candidate_questions import (
    MISSING_PRINCIPAL_HINT,
    build_gate_callables,
)


def case(question: str = "年假有几天", kb_id: str = "rag_docs") -> SimpleNamespace:
    return SimpleNamespace(id="case-1", question=question, kb_id=kb_id,
                           expected_route="rag", expected_source_names=(), category="ordinary")


def test_the_gate_refuses_to_run_without_a_principal():
    """没有主体就只能测到空转：宁可拒绝，也不产出一个"零回归"。"""
    with pytest.raises(ValueError) as excinfo:
        build_gate_callables(
            [case()],
            kb_id="rag_docs",
            memory_db_path=":memory:",
            user_id=None,
            user_db_path=None,
            provider_spec=None,
        )

    assert "--user-id" in str(excinfo.value)
    assert str(excinfo.value) == MISSING_PRINCIPAL_HINT


def test_the_gate_builds_callables_for_a_real_principal(tmp_path):
    """给了主体就正常构造（构造期不触网；只有真正调用才跑管线）。"""
    db = UserDB(str(tmp_path / "users.db"))
    try:
        user_id = db.create_user("gate-user", "Password-123!")
        baseline, candidate_for = build_gate_callables(
            [case()],
            kb_id="rag_docs",
            memory_db_path=":memory:",
            user_id=user_id,
            user_db_path=str(tmp_path / "users.db"),
            provider_spec=None,
        )
    finally:
        db.close()

    assert callable(baseline)
    assert callable(candidate_for)
    # The identical-set smoke keeps the baseline callable; an overlay builds another.
    assert callable(candidate_for("passages", {}))
