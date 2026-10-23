"""Compatibility checks for the pipeline facade while preparation is extracted."""

import hashlib
import inspect
import json
from unittest.mock import MagicMock, patch

import rag.pipeline as pipeline_module
from rag.pipeline import QueryResult, RAGPipeline

# Captured from the BASE8294753 facade with inspect.signature. This catches an
# accidental parameter/default/annotation change across every existing method.
_BASE_SIGNATURES_SHA256 = "050224b42ffbbe0cf318a9f2548f242d721cfa1633384a9ad426341d557f2236"


def _signature_fingerprint() -> str:
    signatures = {
        name: str(inspect.signature(value))
        for name, value in RAGPipeline.__dict__.items()
        if callable(value) and name != "__class__"
    }
    memory_path = inspect.signature(RAGPipeline.__init__).parameters["memory_db_path"].default
    signatures["__init__"] = signatures["__init__"].replace(repr(memory_path), "<memory_db_path>")
    encoded = json.dumps(signatures, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _constructed_pipeline() -> RAGPipeline:
    with (
        patch("rag.pipeline.load", return_value="synthetic text"),
        patch("rag.pipeline.clean_document", return_value=("synthetic text", {})),
        patch("rag.pipeline.chunk", return_value=[]),
        patch("rag.pipeline.deduplicate_chunks", return_value=[]),
        patch("rag.pipeline.embed", return_value=[]),
        patch("rag.pipeline.delete_doc"),
        patch("rag.pipeline.add"),
        patch("rag.pipeline.Retriever"),
        patch("rag.pipeline.Reranker"),
    ):
        return RAGPipeline(
            "synthetic.txt",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )


def test_pipeline_facade_keeps_base_method_signatures_and_result_identity():
    assert _signature_fingerprint() == _BASE_SIGNATURES_SHA256
    assert QueryResult.__module__ == "rag.pipeline"
    assert QueryResult._fields == ("answer", "context", "sources", "decision", "confidence")
    assert RAGPipeline._prepare_context_legacy is RAGPipeline._prepare_context_standard


def test_rewrite_patch_after_construction_is_used_by_prepare_method():
    pipeline = _constructed_pipeline()

    try:
        with patch.object(pipeline_module.settings, "query_rewrite_history_turns", 0), patch.object(
            pipeline_module.settings, "query_rewrite_cache_enabled", False
        ), patch.object(pipeline_module, "rewrite_query", return_value="patched after construction") as rewrite:
            assert pipeline._rewrite_for_retrieval("question", "session") == "patched after construction"

        rewrite.assert_called_once_with("question")
    finally:
        pipeline.close()


def test_query_dispatches_through_an_instance_prepare_override():
    pipeline = _constructed_pipeline()
    calls = []

    def replacement(question, session_id, doc_name, top_k, *, tags, user, kb_id):
        calls.append((question, session_id, doc_name, top_k, tags, user, kb_id))
        return None, "blocked by instance override"

    try:
        pipeline._prepare_context = replacement
        result = pipeline.query("question", session_id="original", tags=["tag"], user={"id": 7}, kb_id="kb")

        assert result.answer == "blocked by instance override"
        assert calls == [("question", "u7:original", None, 8, ["tag"], {"id": 7}, "kb")]
    finally:
        pipeline.close()
