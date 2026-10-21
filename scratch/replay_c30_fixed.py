"""Replay two question sets against a FIXED candidate pool captured once, so the two
judging calls see byte-identical text (the reranker is randomised per call)."""

from __future__ import annotations

import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev import client as jev_client  # noqa: E402
from rag.jev.policy import select_passages  # noqa: E402
from rag.jev.questions import (  # noqa: E402
    PASSAGE_QUESTION_SET_VERSION,
    build_passage_questions,
    build_with_overlay,
)
from rag.models import Chunk  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB_ID = "kb_accessories"
USER_ID = 1
TARGET = "理光副厂品牌参数镜头"


def load_payload() -> dict:
    conn = sqlite3.connect(settings.evolution_db_path)
    try:
        row = conn.execute("select payload from candidates where id = 30").fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def capture_pool() -> list[Chunk]:
    user_db = UserDB(settings.users_db_path)
    pipeline = RAGPipeline(kb_id=KB_ID, user_db=None, memory_db_path=":memory:")
    pipeline._user_db = user_db  # noqa: SLF001
    try:
        user = user_db.get_user_by_id(USER_ID)
        return pipeline._retrieve_visible_candidates(  # noqa: SLF001
            QUESTION, top_k=12, doc_name=None, tags=None, user=user, kb_id=KB_ID
        )
    finally:
        user_db.close()


def judge(client, chunks, questions, version, label):
    chunk_ids = [f"c{i}" for i in range(len(chunks))]
    state = {
        "question": QUESTION,
        "query": QUESTION,
        "passages": {c: {"text": x.text} for c, x in zip(chunk_ids, chunks, strict=True)},
    }
    result = client._run(  # noqa: SLF001
        "passages",
        state,
        questions,
        lambda response, ms: client._map_passages(response, chunk_ids, ms, version),  # noqa: SLF001
    )
    if not result.available:
        print(f"{label}: UNAVAILABLE ({result.failure_kind})")
        return []
    judgments = result.value
    selection = select_passages(chunks, judgments, top_k=5)
    acceptance = [
        " ".join(chunks[int(j.chunk_id[1:])].text.split())[:50] for j in sorted(judgments, key=lambda j: j.answer_evidence, reverse=True)
    ]
    print(
        f"{label}: evidence={selection.with_evidence} has_answer_evidence={selection.has_answer_evidence} "
        f"accepted={len(selection.accepted)} ({result.duration_ms:.0f}ms)"
    )
    for rank, (chunk, j) in enumerate(zip(chunks, judgments, strict=True)):
        text = " ".join(chunk.text.split())
        hit = "TARGET" if TARGET in text else "      "
        print(f"   {hit} c{rank:02d} ev={j.answer_evidence:.2f} rel={j.relevance:.2f}")
    return acceptance


def main() -> int:
    payload = load_payload()
    chunks = capture_pool()
    target_rank = next(
        (i for i, c in enumerate(chunks) if TARGET in " ".join(c.text.split())), None
    )
    print(f"pool={len(chunks)} target_at_rank={target_rank}")
    client = jev_client.JevClient()
    ids = [f"c{i}" for i in range(len(chunks))]
    judge(client, chunks, build_passage_questions(ids), PASSAGE_QUESTION_SET_VERSION, "baseline   ")
    judge(client, chunks, build_with_overlay("passages", payload, ids), "passages-v1+candidate-30", "candidate30")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
