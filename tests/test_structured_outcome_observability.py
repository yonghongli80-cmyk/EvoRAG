"""结构化尝试的结果必须可观测：这一列此前从来没有写入点。

`details_structured_query_kind` 只有建表、模型定义与读取端（`rag/evolution/api.py`），
没有任何生产者，于是永远为 NULL。后果不是"少一列统计"：结构化澄清与结构化**执行失败**
（`failed`）从来没进过自演化观测，所以"结构化意图被降级成文档检索"这类缺陷无法被发现。

约束：
* 只记**种类**（not_applicable / structured_result / clarification_required / unsupported /
  failed），不记问题原文、不记单元格内容；
* `failed` 必须能被区分出来——它此前和"没有表格数据"一样都表现为 `None`；
* 观测只在原本就会产生观测的路径上附带这个信号，不新增观测。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rag.evolution.models import DiagnosticDetail
from rag.pipeline import RAGPipeline
from rag.spreadsheets.coordinator import SpreadsheetQueryOutcome
from rag.tracker import _sanitize_decision_metadata


def build_pipeline(**kwargs) -> RAGPipeline:
    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline(
            kb_id="kb1",
            user_db=MagicMock(),
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            **kwargs,
        )
    pipeline._feedback_processor.get_weight.return_value = 1.0
    pipeline._feedback_processor.get_weights.return_value = {}
    return pipeline


def coordinator_returning(outcome: SpreadsheetQueryOutcome) -> MagicMock:
    coordinator = MagicMock()
    coordinator.has_datasets.return_value = True
    coordinator.query.return_value = outcome
    return coordinator


def test_the_structured_outcome_kind_is_recorded_for_every_kind():
    """每一种 outcome 都要留下种类，包括"协调器不管"和"执行失败"。"""
    cases = {
        "not_applicable": SpreadsheetQueryOutcome("not_applicable"),
        "unsupported": SpreadsheetQueryOutcome("unsupported"),
        "failed": SpreadsheetQueryOutcome("failed", error_code="dataset_read_failed"),
    }
    pipeline = build_pipeline()
    user = {"id": 1, "is_admin": True}
    try:
        for expected, outcome in cases.items():
            record: dict = {}
            pipeline._attempt_structured_query(
                "镜头价格合计",
                coordinator_returning(outcome),
                user,
                "kb1",
                record=record,
            )
            assert record.get("structured_query_kind") == expected
    finally:
        pipeline.close()


def test_a_successful_structured_result_is_also_recorded():
    outcome = SpreadsheetQueryOutcome("structured_result")
    pipeline = build_pipeline()
    try:
        record: dict = {}
        prepared = pipeline._attempt_structured_query(
            "遮光罩售价最高是多少？",
            coordinator_returning(outcome),
            {"id": 1, "is_admin": True},
            "kb1",
            record=record,
        )
        assert prepared is not None
        assert record["structured_query_kind"] == "structured_result"
    finally:
        pipeline.close()


def test_the_kind_reaches_the_observation_detail():
    """观测明细要带上它，否则这一列仍然是 NULL。"""
    pipeline = build_pipeline()
    try:
        prepared = {
            "decision_metadata": {"structured_query_kind": "failed"},
            "decision": MagicMock(kind="insufficient_evidence"),
            "confidence": MagicMock(level="low"),
            "chunk_hashes": [],
        }
        detail = pipeline._evolution_submission("镜头价格合计", prepared)
        assert isinstance(detail, DiagnosticDetail)
        assert detail.structured_query_kind == "failed"
    finally:
        pipeline.close()


def test_the_kind_survives_the_metadata_whitelist():
    """决策元数据走白名单：不在名单里的键会被静默丢掉。"""
    sanitized = _sanitize_decision_metadata({"structured_query_kind": "not_applicable"})
    assert sanitized["structured_query_kind"] == "not_applicable"


def test_no_kind_is_invented_when_there_was_no_structured_attempt():
    pipeline = build_pipeline()
    try:
        prepared = {
            "decision_metadata": {},
            "decision": MagicMock(kind="insufficient_evidence"),
            "confidence": MagicMock(level="low"),
            "chunk_hashes": [],
        }
        detail = pipeline._evolution_submission("保修范围", prepared)
        assert detail.structured_query_kind is None
    finally:
        pipeline.close()


def test_an_outcome_without_a_kind_is_not_recorded_as_a_guess():
    """协调器返回非 outcome 对象时不能编一个种类出来。"""
    pipeline = build_pipeline()
    coordinator = MagicMock()
    coordinator.has_datasets.return_value = True
    coordinator.query.return_value = object()  # 没有 kind 属性
    try:
        record: dict = {}
        with pytest.raises(AttributeError):
            pipeline._attempt_structured_query(
                "镜头价格合计", coordinator, {"id": 1, "is_admin": True}, "kb1", record=record
            )
        assert record == {}
    finally:
        pipeline.close()
