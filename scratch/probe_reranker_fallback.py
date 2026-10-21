"""Is the flip caused by the reranker falling back to score=None?

`decide_answerability` only counts an evidence item when `score is not None and score >= 0.35`.
`Reranker.rerank` returns `rerank_score=None, score_source="unavailable"` whenever the rerank API
fails or its circuit breaker is open -- so a fallback turns every chunk into unqualified evidence
and the request refuses, with no change to retrieval or to the judge.

Prints, per round: the score_source of the accepted context, its scores, and the decision.
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
print("reliability_min_score =", settings.reliability_min_score)
try:
    sources = Counter()
    for attempt in range(ROUNDS):
        prepared, error = pipeline._prepare_context(  # noqa: SLF001
            QUESTION, f"rk-{attempt}", None, user=user, kb_id=KB
        )
        context = (prepared or {}).get("context") or []
        kind = getattr((prepared or {}).get("decision"), "kind", None)
        score_sources = Counter(getattr(c, "score_source", None) for c in context)
        scores = [round(c.rerank_score, 3) if c.rerank_score is not None else None for c in context]
        sources.update(score_sources)
        print(f"#{attempt} kind={kind!s:22s} n={len(context)} sources={dict(score_sources)} scores={scores}")
    print("\nscore_source totals:", dict(sources))
finally:
    user_db.close()
