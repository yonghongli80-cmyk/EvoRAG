"""Is the remaining flip in "r50原厂电池价格" caused by the pool, the vote, or the gate?

For each round: capture the candidate pool (first chunk signature), then judge it three
times and report the per-call verdicts. If the ballots agree inside a round but rounds
differ, the pool is the variable; if ballots differ inside a round, the vote is not enough.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev import client as jev_client  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "r50原厂电池价格"
KB = "kb_accessories"
ROUNDS = 4

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
client = jev_client.JevClient()
try:
    for attempt in range(ROUNDS):
        chunks = pipeline._retrieve_visible_candidates(  # noqa: SLF001
            QUESTION, top_k=12, doc_name=None, tags=None, user=user, kb_id=KB
        )
        signature = [f"{c.doc_name}#{c.chunk_index}" for c in chunks]
        chunk_ids = [f"c{i}" for i in range(len(chunks))]
        state = {
            "question": QUESTION,
            "query": QUESTION,
            "passages": {cid: {"text": c.text} for cid, c in zip(chunk_ids, chunks, strict=True)},
        }
        ballots = []
        for _ in range(3):
            result = client.judge_passages(state, chunk_ids)
            if not result.available:
                ballots.append(f"FAIL:{result.failure_kind}")
                continue
            ballots.append("YES" if any(j.answer_evidence >= 0.5 for j in result.value) else "no")
        print(f"round {attempt}: pool[0]={signature[0] if signature else '-'} ballots={ballots}")
finally:
    user_db.close()
