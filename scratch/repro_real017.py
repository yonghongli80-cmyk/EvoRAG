"""Reproduce real-017 (/018) answering: what evidence cleared the answerability gate?

Prints the decision, its reasons, and the rerank scores of the accepted context, so the
question "why is this answerable when the price cells are redacted" is answered by data.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTIONS = ("r50电池价格", "r50原厂电池价格")

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id="kb_accessories", user_db=user_db, memory_db_path=":memory:")
print("reliability_min_score =", settings.reliability_min_score)
try:
    for question in QUESTIONS:
        for attempt in range(1, 3):
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                question, f"repro17-{attempt}-{question}", None, user=user, kb_id="kb_accessories"
            )
            if error:
                print(f"{question}: BLOCKED {error}")
                continue
            decision = prepared.get("decision")
            print(f"\n=== {question} (attempt {attempt})")
            print(f"  route={prepared.get('route')} kind={getattr(decision, 'kind', None)}")
            print(f"  reasons={getattr(decision, 'reasons', None)}")
            scores = [c.rerank_score for c in prepared.get("context") or []]
            print(f"  context chunks={len(scores)} scores={[round(s, 4) if s else s for s in scores]}")
            for chunk in prepared.get("context") or []:
                text = " ".join(chunk.text.split())
                print(f"    score={chunk.rerank_score} src={chunk.score_source} | {text[:100]}")
finally:
    user_db.close()
