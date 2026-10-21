"""Which questions DO produce a planner clarification, and is the discard branch reachable?

The discard branch in `_prepare_context_standard` only fires when the planner returns
`clarification_required`. real-020 returns `not_applicable`, so it never reaches that branch.
This finds questions that do, so the branch's behaviour can be judged on real inputs.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTIONS = (
    "脚架环价格合计",
    "遮光罩价格",
    "价格",
    "镜头型号有哪些",
    "佳能电池价格是多少",
    "各品牌遮光罩价格合计",
)
KB = "kb_accessories"

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
coordinator = pipeline._spreadsheet_coordinator  # noqa: SLF001
try:
    for question in QUESTIONS:
        outcome = coordinator.query(question, kb_id=KB, user=user)
        clarification = getattr(outcome, "clarification", None)
        code = getattr(clarification, "code", None) if clarification else None
        text = getattr(clarification, "question", None) if clarification else None
        print(f"{question:20s} kind={outcome.kind:22s} code={code} text={str(text)[:40]}")
finally:
    user_db.close()
