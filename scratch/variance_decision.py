"""Measure the per-round variance of the answerability decision on the disputed cases.

Same pipeline, same question, fresh session each round. If the decision flips between
rounds for identical input, no threshold change can fix it -- only a variance-reduction
mechanism (agreement over rounds, caching) could.
"""

from __future__ import annotations

import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

ROUNDS = 6
CASES = (
    ("r50电池价格", "kb_accessories"),
    ("r50原厂电池价格", "kb_accessories"),
    ("帮我看一下2025年招聘数据", "kb_hr"),
)

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipelines: dict[str, RAGPipeline] = {}
try:
    for question, kb in CASES:
        pipeline = pipelines.setdefault(
            kb, RAGPipeline(kb_id=kb, user_db=user_db, memory_db_path=":memory:")
        )
        kinds = []
        for round_index in range(ROUNDS):
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                question, f"var-{round_index}-{question}", None, user=user, kb_id=kb
            )
            kind = getattr((prepared or {}).get("decision"), "kind", None)
            kinds.append(str(kind))
        print(f"{question[:18]:20s} {dict(Counter(kinds))}")
finally:
    user_db.close()
