"""Trace the legacy path for "找一下副厂配件明细" step by step (no Jev, no HTTP)."""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.agent import route_question  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB_ID = "kb_accessories"


def main() -> int:
    user_db = UserDB(settings.users_db_path)
    user = user_db.get_user_by_id(1)
    pipeline = RAGPipeline(kb_id=KB_ID, user_db=user_db, memory_db_path=":memory:")
    try:
        print("route_question ->", route_question(QUESTION))
        rewritten = pipeline._rewrite_for_retrieval(QUESTION, "scratch")  # noqa: SLF001
        print("rewritten ->", rewritten)
        top_k = getattr(settings, "rerank_top_k", 5)
        candidates = pipeline._retrieve_visible_candidates(  # noqa: SLF001
            rewritten, top_k=top_k * 4, doc_name=None, tags=None, user=user, kb_id=KB_ID
        )
        print("candidates ->", len(candidates))
        ranked = pipeline.reranker.rerank(rewritten, candidates)
        print("ranked ->", len(ranked))
        for index, chunk in enumerate(ranked[:8]):
            print(
                f"  #{index} score={chunk.rerank_score} source={chunk.score_source} "
                f"doc_id={chunk.document_id} ver={chunk.document_version_id} "
                f"hist={chunk.historical_weight} | {' '.join(chunk.text.split())[:60]}"
            )
    finally:
        user_db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
