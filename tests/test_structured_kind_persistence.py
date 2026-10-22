"""R-3: `failed` 这个结构化结果种类必须真的落到 `failure_observations`。

背景：`pipeline._attempt_structured_query` 已经把 outcome 的 kind 写进 record，
`DiagnosticDetail.structured_query_kind` 也带上了它，但线上 DB 分布只有
`None` / `not_applicable` / `unsupported`，没有一条 `failed`。此前的单测只断言到
record/detail，没有任何用例穿过 `FailureRecorder` 的后台写线程去核对 SQLite 里的原始值，
所以"这一列到底有没有写进去"无法由测试回答。

本用例只用 `tmp_path` 下的临时 `evolution.db` 与真实 `FailureTraceStore` /
`FailureRecorder`（不 mock 落库），也不依赖真实模型、Qdrant 或网络。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from rag.evolution.models import DiagnosticDetail
from rag.evolution.recorder import FailureRecorder
from rag.evolution.store import FailureTraceStore
from rag.tracker import ExecutionTrace

CHUNK_HASH = "a" * 64


@pytest.fixture
def evolution_db(tmp_path) -> Iterator[tuple[Path, FailureTraceStore, FailureRecorder]]:
    """Throwaway database with a real store and recorder; both are always closed."""
    db_path = tmp_path / "evolution.db"
    store = FailureTraceStore(db_path)
    recorder = FailureRecorder(store)
    try:
        yield db_path, store, recorder
    finally:
        recorder.close()  # idempotent: also drains whatever a failing test left queued
        store.close()


def make_detail(**kw) -> DiagnosticDetail:
    """One diagnosable failure whose structured attempt died internally."""
    base = dict(
        kb_id="kb1",
        failure_stage="structured_query",
        answerability_kind="insufficient_evidence",
        confidence_level="low",
        chunk_hashes=(CHUNK_HASH,),
        best_score=0.42,
        structured_query_kind="failed",
    )
    base.update(kw)
    return DiagnosticDetail(**base)


def structured_trace(**kw) -> ExecutionTrace:
    """The trace the pipeline submits alongside the detail (question lives only here)."""
    base = dict(
        question="镜头价格合计",
        route="rag",
        answer="",
        total_ms=12.5,
        chunk_hashes=[CHUNK_HASH],
        # Same allowlisted metadata the pipeline records; the column itself must come
        # from the detail, so both are set to the value under test.
        decision_metadata={
            "structured_query_kind": "failed",
            "question_set_version": "passages-v1",
        },
    )
    base.update(kw)
    return ExecutionTrace(**base)


def read_structured_query_kinds(db_path: Path) -> list[str | None]:
    """Read the column straight from SQLite, bypassing the store's read API."""
    with closing(sqlite3.connect(db_path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT details_structured_query_kind FROM failure_observations ORDER BY id"
            )
        ]


def test_a_failed_structured_kind_is_written_to_failure_observations(evolution_db):
    """submit → 后台写线程 → SQLite：列的原始值必须是 'failed'，不是 NULL。"""
    db_path, store, recorder = evolution_db

    assert recorder.submit(structured_trace(), make_detail()) is True
    recorder.close(wait=True)  # drain the single writer thread before reading

    assert recorder.stats()["completed"] == 1
    assert recorder.stats()["dropped"] == 0
    # Direct SQL on the real file is the claim under test, not just the in-memory detail.
    assert read_structured_query_kinds(db_path) == ["failed"]

    stored = store.list_observations()
    assert len(stored) == 1
    assert stored[0]["details_structured_query_kind"] == "failed"
    assert stored[0]["stage"] == "structured_query"
    assert stored[0]["details_answerability_kind"] == "insufficient_evidence"


def test_failed_is_distinguishable_from_no_structured_attempt(evolution_db):
    """`failed` 与"根本没尝试结构化"必须落成不同的值：后者是 SQL NULL。"""
    db_path, store, recorder = evolution_db
    no_attempt = make_detail(failure_stage="retrieval", structured_query_kind=None)

    assert recorder.submit(structured_trace(), make_detail()) is True
    assert recorder.submit(structured_trace(question="保修范围"), no_attempt) is True
    recorder.close(wait=True)

    assert read_structured_query_kinds(db_path) == ["failed", None]
    assert store.count_by_status() == {"observed": 2}
