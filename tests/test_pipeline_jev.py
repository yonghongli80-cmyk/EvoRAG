"""Task 6: two-stage Jev integration tests for RAGPipeline."""

from unittest.mock import MagicMock, patch

import pytest

from config import settings
from rag.jev.models import PreflightDecision
from rag.jev.policy import PassageSelection, RouteDecision
from rag.models import Chunk
from rag.pipeline import RAGPipeline
from rag.reliability import fixed_decision_copy


def rag_route(should_rewrite: bool = False) -> RouteDecision:
    return RouteDecision(
        route="rag",
        should_rewrite=should_rewrite,
        reason="retrieval_confident",
        preflight=PreflightDecision(
            intent="retrieval_qa",
            intent_confidence=0.95,
            complexity="simple",
            complexity_confidence=0.91,
            rewrite_probability=0.1,
            model="jev-latest",
            question_set_version="preflight-v1",
            duration_ms=1.0,
        ),
    )


def evidence_selection(chunks) -> PassageSelection:
    return PassageSelection(
        accepted=tuple(chunks),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=True,
    )


def no_evidence() -> PassageSelection:
    return PassageSelection(accepted=(), conflicts=(), excluded_ids=(), has_answer_evidence=False)


def conflict_selection(chunks) -> PassageSelection:
    return PassageSelection(accepted=(), conflicts=tuple(chunks), excluded_ids=(), has_answer_evidence=False)


def build_pipeline(jev_service, **kwargs):
    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls:
        retriever_cls.return_value.retrieve.return_value = []
        reranker_cls.return_value.rerank.side_effect = lambda _query, chunks: chunks
        pipeline = RAGPipeline(
            "test.txt",
            session_id="s1",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            jev_service=jev_service,
            **kwargs,
        )
    pipeline._feedback_processor.get_weight.return_value = 1.0
    pipeline._feedback_processor.get_weights.return_value = {}
    return pipeline


@pytest.fixture
def active_jev(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs,kb1")


def test_clear_rag_question_skips_deepseek_rewrite(active_jev):
    jev = MagicMock()
    jev.route.return_value = rag_route(should_rewrite=False)
    visible = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    jev.select.return_value = evidence_selection([visible])
    pipeline = build_pipeline(jev)
    pipeline.retriever.retrieve.return_value = [visible]

    with patch("rag.pipeline.rewrite_query") as mock_rewrite, \
         patch("rag.pipeline.generate", return_value="answer") as mock_generate:
        result = pipeline.query("年假有几天")

    assert result.answer == "answer"
    mock_rewrite.assert_not_called()
    mock_generate.assert_called_once()
    [chunk] = result.context
    assert chunk.text == "年假为 7 天"
    jev.route.assert_called_once()
    sent = jev.select.call_args.args[1]
    assert sent == [visible]


def test_no_evidence_rewrites_only_once(active_jev):
    jev = MagicMock()
    jev.route.return_value = rag_route(should_rewrite=False)
    jev.select.side_effect = [no_evidence(), no_evidence()]
    pipeline = build_pipeline(jev)
    pipeline.retriever.retrieve.return_value = []

    with patch("rag.pipeline.rewrite_query", return_value="rewritten") as mock_rewrite, \
         patch("rag.pipeline.generate") as mock_generate:
        result = pipeline.query("换个说法才能找到的问题")

    mock_rewrite.assert_called_once()
    assert jev.select.call_count == 2
    assert result.answer == fixed_decision_copy("insufficient_evidence")
    mock_generate.assert_not_called()


def test_hidden_chunks_never_reach_jev(active_jev):
    visible = Chunk(
        text="visible", doc_name="visible.txt", chunk_index=0,
        document_id=1, document_version_id=11,
    )
    invisible = Chunk(
        text="private", doc_name="private.txt", chunk_index=0,
        document_id=2, document_version_id=22,
    )
    jev = MagicMock()
    jev.route.return_value = rag_route()
    jev.select.return_value = evidence_selection([visible])
    user = {"id": 7, "permission_level": 1, "is_admin": False}
    user_db = MagicMock()
    user_db.get_accessible_doc_names.return_value = ["visible.txt"]
    user_db.get_document_permission.return_value = {"id": 1, "current_version_id": 11}
    user_db.list_document_versions.return_value = []
    user_db.get_document_permissions_by_names.return_value = {"visible.txt": {"id": 1}}

    pipeline = build_pipeline(jev, kb_id="kb1", user_db=user_db)
    pipeline.retriever.retrieve.return_value = [visible, invisible]

    with patch("rag.pipeline.generate", return_value="unexpected generated answer"), \
         patch("rag.versioned_retrieval.retrieve_current_then_history", return_value=[visible, invisible]) as versioned:
        pipeline.query("内部制度", user=user, kb_id="kb1")

    sent = jev.select.call_args.args[1]
    assert [chunk.doc_name for chunk in sent] == ["visible.txt"]
    assert versioned.called


def test_structured_route_uses_coordinator(active_jev):
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="structured", should_rewrite=False, reason="structured_confident", preflight=None,
    )
    pipeline = build_pipeline(jev)
    structured = {
        "route": "structured",
        "answer": "总额：100",
        "context": [],
        "sources": [],
        "structured_result": {"kind": "structured_result", "facts": [], "sources": [], "rows_scanned": 1},
    }

    with patch.object(pipeline, "_attempt_structured_query", return_value=structured) as attempt:
        result = pipeline.query("上个月销售额是多少")

    attempt.assert_called_once()
    assert result.answer == "总额：100"


def test_clarification_route_returns_fixed_copy(active_jev):
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="clarification", should_rewrite=False, reason="clarification_confident", preflight=None,
    )
    pipeline = build_pipeline(jev)

    with patch("rag.pipeline.generate") as mock_generate:
        result = pipeline.query("那个事儿怎么办")

    assert result.answer == fixed_decision_copy("clarification_required")
    mock_generate.assert_not_called()


def test_preflight_failure_falls_back_to_legacy_router(active_jev):
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="legacy", should_rewrite=False, reason="preflight_unavailable:timeout", preflight=None,
    )
    pipeline = build_pipeline(jev)
    visible = Chunk(text="chunk1", doc_name="test.txt", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [visible]

    with patch("rag.agent.route_question", return_value="rag") as mock_route, \
         patch("rag.pipeline.rewrite_query", return_value="rewritten") as mock_rewrite, \
         patch("rag.pipeline.generate", return_value="legacy answer"):
        result = pipeline.query("年假有几天")

    assert result.answer == "legacy answer"
    mock_route.assert_called_once()
    mock_rewrite.assert_called_once()
    jev.select.assert_not_called()


def test_passage_failure_falls_back_to_legacy_reranker(active_jev):
    jev = MagicMock()
    jev.route.return_value = rag_route()
    jev.select.return_value = None
    pipeline = build_pipeline(jev)
    visible = Chunk(text="chunk1", doc_name="test.txt", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [visible]

    with patch("rag.pipeline.generate", return_value="fallback answer"):
        result = pipeline.query("年假有几天")

    assert result.answer == "fallback answer"
    pipeline.reranker.rerank.assert_called_once()
    assert len(result.context) == 1


def test_allowlist_mismatch_uses_legacy_path(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "other_kb")
    jev = MagicMock()
    pipeline = build_pipeline(jev)
    visible = Chunk(text="chunk1", doc_name="test.txt", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [visible]

    with patch("rag.agent.route_question", return_value="rag"), \
         patch("rag.pipeline.rewrite_query", return_value="rewritten"), \
         patch("rag.pipeline.generate", return_value="legacy answer"):
        result = pipeline.query("年假有几天")

    assert result.answer == "legacy answer"
    jev.route.assert_not_called()
    jev.select.assert_not_called()


def test_jev_mode_off_uses_legacy_path(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "off")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    jev = MagicMock()
    pipeline = build_pipeline(jev)
    visible = Chunk(text="chunk1", doc_name="test.txt", chunk_index=0)
    pipeline.retriever.retrieve.return_value = [visible]

    with patch("rag.agent.route_question", return_value="rag"), \
         patch("rag.pipeline.rewrite_query", return_value="rewritten"), \
         patch("rag.pipeline.generate", return_value="legacy answer"):
        result = pipeline.query("年假有几天")

    assert result.answer == "legacy answer"
    jev.route.assert_not_called()


def test_tool_route_in_enterprise_returns_fixed_clarification(active_jev):
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="tool", should_rewrite=False, reason="tool_confident", preflight=None,
    )
    pipeline = build_pipeline(jev, kb_id="kb1", user_db=MagicMock())

    with patch.object(pipeline.agent, "run") as agent_run, \
         patch("rag.pipeline.generate") as mock_generate:
        result = pipeline.query("帮我计算增长率", user={"id": 7, "permission_level": 1}, kb_id="kb1")

    assert result.answer == fixed_decision_copy("clarification_required")
    agent_run.assert_not_called()
    mock_generate.assert_not_called()


def test_tool_route_outside_enterprise_uses_agent(active_jev):
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="tool", should_rewrite=False, reason="tool_confident", preflight=None,
    )
    pipeline = build_pipeline(jev)

    with patch.object(pipeline.agent, "run", return_value="agent answer") as agent_run:
        result = pipeline.query("计算一下")

    assert result.answer == "agent answer"
    agent_run.assert_called_once()


def test_conflicting_evidence_requests_clarification(active_jev):
    jev = MagicMock()
    jev.route.return_value = rag_route()
    conflict = Chunk(text="矛盾资料", doc_name="old.md", chunk_index=1)
    jev.select.return_value = conflict_selection([conflict])
    pipeline = build_pipeline(jev, kb_id="kb1", user_db=MagicMock())

    with patch("rag.pipeline.rewrite_query", return_value="rewritten"), \
         patch("rag.pipeline.generate") as mock_generate:
        result = pipeline.query("年假是几天", user={"id": 7, "permission_level": 1}, kb_id="kb1")

    assert result.answer == fixed_decision_copy("clarification_required")
    mock_generate.assert_not_called()


def test_decision_metadata_carries_both_stage_versions(active_jev):
    """A response names the preflight version and the passages version that served it.

    A candidate question set may target either stage. Preflight already wrote its
    version into ``decision_metadata``; the passages stage has to do the same, or
    a passages canary's traffic cannot be attributed to the candidate.
    """
    jev = MagicMock()
    jev.route.return_value = rag_route()
    visible = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    jev.select.return_value = PassageSelection(
        accepted=(visible,),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=True,
        question_set_version="passages-v1+candidate-3",
    )
    pipeline = build_pipeline(jev)
    pipeline.retriever.retrieve.return_value = [visible]
    pipeline.tracker = MagicMock()

    with patch("rag.pipeline.generate", return_value="answer"):
        pipeline.query("年假有几天")

    trace = pipeline.tracker.save.call_args.args[0]
    assert trace.decision_metadata["question_set_version"] == "preflight-v1"
    assert trace.decision_metadata["passages_question_set_version"] == "passages-v1+candidate-3"


def test_streaming_parity_for_jev_path(active_jev):
    import asyncio
    import json

    jev = MagicMock()
    jev.route.return_value = rag_route()
    visible = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    jev.select.return_value = evidence_selection([visible])
    pipeline = build_pipeline(jev)
    pipeline.retriever.retrieve.return_value = [visible]

    async def fake_stream(_messages):
        yield "答"
        yield "案"

    with patch("rag.pipeline.generate_stream", return_value=fake_stream([])):
        events = []

        async def collect():
            async for event in pipeline.query_stream("年假有几天", session_id="s1"):
                events.append(json.loads(event.removeprefix("data: ")))

        asyncio.run(collect())

    kinds = [event["type"] for event in events]
    assert kinds == ["token", "token", "sources", "done"]
    assert "".join(event["content"] for event in events if event["type"] == "token") == "答案"
