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
    _parse_args,
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


def test_apply_takes_the_comparison_three_times_unless_told_otherwise():
    """写回路径**默认不接受单次判定**：2026-10-13 实测同一候选四次得到四种结论。

    代价随之线性增长，所以只在写回时默认 3 次；只读回放保持 1 次（原行为）。
    """
    applied = _parse_args(["--apply", "--user-id", "1"])
    assert applied.repeats == 3

    replay = _parse_args(["--candidate-id", "15", "--user-id", "1"])
    assert replay.repeats == 1

    explicit = _parse_args(["--apply", "--user-id", "1", "--repeats", "5"])
    assert explicit.repeats == 5
    # 只读回放也允许主动多测几次（想看清它稳不稳）
    assert _parse_args(["--candidate-id", "15", "--user-id", "1", "--repeats", "3"]).repeats == 3


@pytest.mark.parametrize("repeats", ["0", "-1", "2", "4"])
def test_repeat_counts_must_be_one_or_an_odd_number(repeats):
    """偶数次会平票；平票在这条路径上必须是"未决"，与其发明平票语义不如要求奇数。"""
    with pytest.raises(SystemExit):
        _parse_args(["--apply", "--user-id", "1", "--repeats", repeats])
