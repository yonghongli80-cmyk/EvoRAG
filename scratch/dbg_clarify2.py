"""Replicate the failing pytest case exactly, using the test module's own helpers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev.policy import PassageSelection, RouteDecision  # noqa: E402
from rag.models import Chunk  # noqa: E402
from rag.pipeline import _produced_answer  # noqa: E402
from tests.test_pipeline_jev import build_pipeline, structured_clarification  # noqa: E402

settings.jev_mode = "active"
settings.jev_kb_allowlist = "rag_docs,kb1"

jev = MagicMock()
jev.route.return_value = RouteDecision(
    route="structured", should_rewrite=False, reason="structured_confident", preflight=None,
)
chunk = Chunk(
    text="镜头型号 价格 <金额>", doc_name="prices.md", chunk_index=0,
    document_id=11, document_version_id=12,
)
jev.select.return_value = PassageSelection(
    accepted=(chunk,), conflicts=(), excluded_ids=(), has_answer_evidence=False,
    judged=12, relevant=1, with_evidence=0,
)
pipeline = build_pipeline(jev, kb_id="kb1")
pipeline.retriever.retrieve.return_value = [chunk]

with patch.object(pipeline, "_attempt_structured_query", return_value=structured_clarification()):
    prepared, error = pipeline._prepare_context(
        "镜头价格合计", "dbg2", None, user={"id": 1, "is_admin": True}, kb_id="kb1"
    )
    print("prepare -> route:", (prepared or {}).get("route"))
    print("prepare -> answer:", str((prepared or {}).get("answer"))[:60])
    print("prepare -> decision:", getattr((prepared or {}).get("decision"), "kind", None))
    print("prepare -> sources:", len((prepared or {}).get("sources") or []))
    print("produced_answer:", _produced_answer(prepared or {}))
    result = pipeline.query("镜头价格合计", user={"id": 1, "is_admin": True}, kb_id="kb1")
    print("query -> answer:", str(result.answer)[:60])
