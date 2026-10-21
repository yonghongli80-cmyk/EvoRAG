"""Why does the structured clarification not win over the refusal in the Jev path?

Reproduces the unit test's setup outside pytest and prints the decision chain.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev.policy import PassageSelection, RouteDecision  # noqa: E402
from rag.models import Chunk  # noqa: E402
from rag.pipeline import RAGPipeline, _produced_answer  # noqa: E402

settings.jev_mode = "active"
settings.jev_kb_allowlist = "kb1"

chunk = Chunk(
    text="镜头型号 价格 <金额>", doc_name="prices.md", chunk_index=0,
    document_id=11, document_version_id=12,
)
jev = MagicMock()
jev.route.return_value = RouteDecision(
    route="structured", should_rewrite=False, reason="structured_confident", preflight=None,
)
jev.select.return_value = PassageSelection(
    accepted=(chunk,), conflicts=(), excluded_ids=(), has_answer_evidence=False,
    judged=12, relevant=1, with_evidence=0,
)

clarification = {
    "route": "structured",
    "answer": "请指明要统计的字段",
    "context": [],
    "sources": [],
    "structured_result": {"kind": "clarification_required", "facts": [], "sources": [], "rows_scanned": 0},
}

with patch("rag.pipeline.Retriever") as retriever_cls, patch("rag.pipeline.Reranker") as reranker_cls:
    retriever_cls.return_value.retrieve.return_value = [chunk]
    reranker_cls.return_value.rerank.side_effect = lambda _q, chunks: chunks
    pipeline = RAGPipeline(
        kb_id="kb1", user_db=MagicMock(), memory_db_path=":memory:",
        feedback_processor=MagicMock(), gap_analyzer=MagicMock(), jev_service=jev,
    )
    pipeline._feedback_processor.get_weight.return_value = 1.0
    pipeline._feedback_processor.get_weights.return_value = {}

    with patch.object(pipeline, "_attempt_structured_query", return_value=clarification):
        print("is_clarification ->", pipeline._is_structured_clarification(clarification))
        prepared, error = pipeline._prepare_context(
            "镜头价格合计", "dbg", None, user={"id": 1, "is_admin": True}, kb_id="kb1"
        )
        decision = (prepared or {}).get("decision")
        print("route ->", (prepared or {}).get("route"))
        print("decision ->", getattr(decision, "kind", None))
        print("sources ->", len((prepared or {}).get("sources") or []))
        print("produced_answer ->", _produced_answer(prepared or {}))
        print("answer ->", str((prepared or {}).get("answer"))[:60])
