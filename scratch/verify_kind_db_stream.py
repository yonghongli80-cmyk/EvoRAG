"""Catch a refusal that also attempted structured query, and check the column is written.

`找一下副厂配件明细` goes through the legacy path (Jev confidence below threshold), which always
attempts the structured coordinator, and it refuses roughly one run in three. Drive it through
`query_stream` (the path that submits observations) until a refusal lands.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.evolution.recorder import FailureRecorder  # noqa: E402
from rag.evolution.store import FailureTraceStore  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB = "kb_accessories"
ATTEMPTS = 8

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
recorder = FailureRecorder(FailureTraceStore(settings.evolution_db_path), max_queue=50)
conn = sqlite3.connect(settings.evolution_db_path)
conn.row_factory = sqlite3.Row
before = conn.execute("select coalesce(max(id), 0) from failure_observations").fetchone()[0]
conn.close()

pipeline = RAGPipeline(
    kb_id=KB, user_db=user_db, memory_db_path=":memory:", failure_recorder=recorder
)


async def run_once(session: str) -> tuple[str | None, dict]:
    events = []
    async for event in pipeline.query_stream(
        QUESTION, top_k=8, session_id=session, user=user, kb_id=KB
    ):
        events.append(event)
    done = {}
    for event in events:
        if '"type": "done"' in event:
            try:
                done = json.loads(event.removeprefix("data: ").strip())
            except Exception:
                done = {}
    return (done.get("reliability") or {}).get("answerability_kind"), done


try:
    for attempt in range(ATTEMPTS):
        kind, _done = asyncio.run(run_once(f"obsdb-{attempt}-{time.time():.0f}"))
        print(f"attempt {attempt}: answerability={kind}")
        if kind == "insufficient_evidence":
            break
finally:
    user_db.close()
    recorder.close()

conn = sqlite3.connect(settings.evolution_db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select id, question, details_answerability_kind kind, details_structured_query_kind sque, "
    "details_decision_metadata meta from failure_observations where id > ? order by id",
    (before,),
).fetchall()
print(f"\nnew rows: {len(rows)}")
for row in rows:
    meta = json.loads(row["meta"] or "{}")
    print(
        f"  #{row['id']} kind={row['kind']} structured_query_kind={row['sque']!r} "
        f"meta_kind={meta.get('structured_query_kind')!r}"
    )
filled = conn.execute(
    "select count(*) from failure_observations where details_structured_query_kind is not null"
).fetchone()[0]
total = conn.execute("select count(*) from failure_observations").fetchone()[0]
print(f"non-null structured_query_kind: {filled}/{total}")
conn.close()
