"""Is the flip caused by the LLM query rewrite?

The legacy path rewrites the query unconditionally (`_rewrite_for_retrieval`) before both
retrieval and reranking. If the rewritten text varies between calls, the reranker sees a different
query each time -- which would explain wildly different scores on an identical candidate pool.

Prints, per round: the rewritten query, the top rerank score, and the decision.
"""

from __future__ import annotations

import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB = "kb_accessories"
ROUNDS = 6

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
try:
    rewrites = Counter()
    for attempt in range(ROUNDS):
        rewritten = pipeline._rewrite_for_retrieval(QUESTION, f"rw-{attempt}")  # noqa: SLF001
        rewrites[rewritten] += 1
        candidates = pipeline._retrieve_visible_candidates(  # noqa: SLF001
            rewritten, top_k=12, doc_name=None, tags=None, user=user, kb_id=KB
        )
        ranked = pipeline.reranker.rerank(rewritten, candidates)
        top = ranked[0].rerank_score if ranked else None
        print(f"#{attempt} top={top if top is None else round(top, 3)} rewritten={rewritten!r}")
    print("\ndistinct rewrites:", len(rewrites))
    for text, count in rewrites.most_common():
        print(f"  {count}x {text!r}")
finally:
    user_db.close()
