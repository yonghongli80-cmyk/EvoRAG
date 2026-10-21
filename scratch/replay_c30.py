"""Replay candidate #30 against the case family it was designed for (real-020 family).

Read-only.  Builds the real candidate pool for "找一下副厂配件明细" on kb_accessories
(the same pipeline call the online path makes), then judges that pool twice through the
production Jev client:

* baseline   -- the live passages-v1 question set
* candidate30 -- the payload overlay of candidate #30 (build_with_overlay)

and applies the same PassageSelection policy to both, so `has_answer_evidence` is
computed exactly as the pipeline computes it.

Needs Qdrant (retrieval), so run it with the service stopped OR accept that the local
Qdrant directory is shared.  Needs the .env Jev key.
"""

from __future__ import annotations

import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev import client as jev_client  # noqa: E402
from rag.jev.policy import select_passages  # noqa: E402
from rag.jev.questions import build_with_overlay  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB_ID = "kb_accessories"
USER_ID = 1


def load_payload() -> dict:
    conn = sqlite3.connect(settings.evolution_db_path)
    try:
        row = conn.execute("select payload from candidates where id = 30").fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit("candidate 30 not found")
    return json.loads(row[0])


def retrieve_pool():
    """The exact candidate pool the online passage stage would judge."""
    from rag.pipeline import RAGPipeline
    from rag.user_db import UserDB

    user_db = UserDB(settings.users_db_path)
    pipeline = RAGPipeline(kb_id=KB_ID, user_db=None, memory_db_path=":memory:")
    pipeline._user_db = user_db  # noqa: SLF001 - the ACL path needs the same handle
    try:
        user = user_db.get_user_by_id(USER_ID)
        retrieve_top_k = getattr(settings, "jev_max_passage_candidates", 12)
        candidates = pipeline._retrieve_visible_candidates(  # noqa: SLF001
            QUESTION,
            top_k=retrieve_top_k,
            doc_name=None,
            tags=None,
            user=user,
            kb_id=KB_ID,
        )
    finally:
        user_db.close()
    return candidates


def judge(client, chunks, questions: dict, version: str, label: str) -> None:
    chunk_ids = [f"c{i}" for i in range(len(chunks))]
    state = {
        "question": QUESTION,
        "query": QUESTION,
        "passages": {cid: {"text": chunk.text} for cid, chunk in zip(chunk_ids, chunks, strict=True)},
    }
    result = client._run(  # noqa: SLF001
        "passages",
        state,
        questions,
        lambda response, ms: client._map_passages(response, chunk_ids, ms, version),  # noqa: SLF001
    )
    if not result.available:
        print(f"{label}: UNAVAILABLE ({result.failure_kind})")
        return
    judgments = result.value
    selection = select_passages(chunks, judgments, top_k=5)
    print(
        f"{label}: judged={len(judgments)} relevant={selection.relevant} "
        f"evidence={selection.with_evidence} has_answer_evidence={selection.has_answer_evidence} "
        f"({result.duration_ms:.0f}ms)"
    )
    for chunk, judgment in zip(chunks, judgments, strict=True):
        snippet = " ".join(chunk.text.split())[:60]
        flag = "E" if judgment.answer_evidence >= 0.5 else "."
        print(
            f"   [{flag}] ev={judgment.answer_evidence:.2f} rel={judgment.relevance:.2f} "
            f"| {getattr(chunk, 'metadata', {}).get('doc_name', '?')}: {snippet}"
        )


def main() -> int:
    payload = load_payload()
    candidates = retrieve_pool()
    print(f"candidate pool: {len(candidates)} chunks")
    if not candidates:
        print("EMPTY POOL -- nothing to judge")
        return 2
    client = jev_client.JevClient()
    from rag.jev.questions import PASSAGE_QUESTION_SET_VERSION, build_passage_questions

    chunk_ids = [f"c{i}" for i in range(len(candidates))]
    judge(client, candidates, build_passage_questions(chunk_ids), PASSAGE_QUESTION_SET_VERSION, "baseline   ")
    overlay = build_with_overlay("passages", payload, chunk_ids)
    judge(client, candidates, overlay, "passages-v1+candidate-30", "candidate30")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
