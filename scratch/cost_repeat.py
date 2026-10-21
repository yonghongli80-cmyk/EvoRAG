"""Cost and stability of repeated passage judgments on ONE fixed candidate pool.

Measures: how long one passages call takes, and how often the decision flips across calls
with identical input (which is the variance the majority-vote fix has to remove).
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev import client as jev_client  # noqa: E402
from rag.jev.questions import build_passage_questions  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "r50电池价格"
KB = "kb_accessories"
ROUNDS = 5

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
try:
    chunks = pipeline._retrieve_visible_candidates(  # noqa: SLF001
        QUESTION, top_k=12, doc_name=None, tags=None, user=user, kb_id=KB
    )
finally:
    user_db.close()

print(f"pool={len(chunks)}")
client = jev_client.JevClient()
chunk_ids = [f"c{i}" for i in range(len(chunks))]
state = {
    "question": QUESTION,
    "query": QUESTION,
    "passages": {cid: {"text": chunk.text} for cid, chunk in zip(chunk_ids, chunks, strict=True)},
}
questions = build_passage_questions(chunk_ids)
for round_index in range(ROUNDS):
    result = client.judge_passages(state, chunk_ids)
    if not result.available:
        print(f"round {round_index}: UNAVAILABLE {result.failure_kind}")
        continue
    evidence = [round(j.answer_evidence, 2) for j in result.value]
    verdict = any(j.answer_evidence >= 0.5 for j in result.value)
    print(
        f"round {round_index}: {result.duration_ms:7.0f}ms evidence>=0.5 -> {verdict} "
        f"scores={evidence}"
    )
