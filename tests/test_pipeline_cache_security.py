from rag.pipeline import RAGPipeline


def test_prepare_context_never_reads_a_question_only_answer_cache(monkeypatch):
    import rag.agent

    pipeline = RAGPipeline.__new__(RAGPipeline)

    class SeededCache:
        def get(self, question):
            raise AssertionError("final-answer cache must not be read")

    pipeline._cache = SeededCache()
    monkeypatch.setattr(rag.agent, "route_question", lambda question: "agent")

    prepared, error = pipeline._prepare_context(
        "same question", session_id="s1", doc_name=None,
        user={"id": 22, "is_admin": False}, kb_id="kb_public",
    )

    assert error is None
    assert prepared == {
        "route": "agent",
        "question": "same question",
        "user": {"id": 22, "is_admin": False},
        "kb_id": "kb_public",
    }
