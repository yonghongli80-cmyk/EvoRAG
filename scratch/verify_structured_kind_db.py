"""Does details_structured_query_kind actually reach the database now?

Drives real questions through the pipeline (in-process), lets the recorder drain, then reads the
observation rows. Before this fix the column was NULL for every row ever recorded.
"""

from __future__ import annotations

import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.evolution.candidate_store import CandidateStore  # noqa: E402,F401  (ensures schema)
from rag.evolution.recorder import FailureRecorder  # noqa: E402
from rag.evolution.store import FailureTraceStore  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

CASES = (
    ("镜头价格合计", "kb_accessories"),
    ("r50电池价格", "kb_accessories"),
    ("脚架环价格合计", "kb_accessories"),
)
KB_DEFAULT = "kb_accessories"

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
store = FailureTraceStore(settings.evolution_db_path)
recorder = FailureRecorder(store, max_queue=50)
pipelines: dict[str, RAGPipeline] = {}
try:
    for question, kb in CASES:
        if kb not in pipelines:
            pipelines[kb] = RAGPipeline(
                kb_id=kb,
                user_db=user_db,
                memory_db_path=":memory:",
                failure_recorder=recorder,
            )
        pipeline = pipelines[kb]
        for attempt in range(2):
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                question, f"obs-{attempt}-{question}", None, user=user, kb_id=kb
            )
            meta = (prepared or {}).get("decision_metadata") or {}
            kind = getattr((prepared or {}).get("decision"), "kind", None)
            print(
                f"{question:16s} #{attempt} decision={kind} "
                f"structured_query_kind={meta.get('structured_query_kind')}"
            )
finally:
    user_db.close()

# 让记录器把队列刷进库（close(wait=True) 会等队列排空）
recorder.close()

conn = sqlite3.connect(settings.evolution_db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select id, question, details_answerability_kind kind, details_structured_query_kind sque "
    "from failure_observations order by id desc limit 8"
).fetchall()
print("\nlatest observation rows:")
for row in rows:
    print(f"  #{row['id']} kind={row['kind']} structured_query_kind={row['sque']} | {row['question'][:24]}")
filled = conn.execute(
    "select count(*) from failure_observations where details_structured_query_kind is not null"
).fetchone()[0]
total = conn.execute("select count(*) from failure_observations").fetchone()[0]
print(f"\nnon-null structured_query_kind: {filled}/{total}")
conn.close()
