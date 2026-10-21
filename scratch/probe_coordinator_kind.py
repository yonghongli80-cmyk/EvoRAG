"""What kind does the spreadsheet coordinator return for these questions, and why?

The pipeline only accepts structured_result / hybrid_result / clarification_required; every other
kind makes `_attempt_structured_query` return None, which looks identical to "no structured data"
from the outside.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTIONS = (
    "找一下副厂配件明细",
    "帮我找副厂配件清单",
    "r50相关价格",
    "r50电池价格",
    "遮光罩售价最高是多少？",
)
KB = "kb_accessories"

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
coordinator = pipeline._spreadsheet_coordinator  # noqa: SLF001
print("coordinator:", coordinator is not None)
try:
    for question in QUESTIONS:
        outcome = coordinator.query(question, kb_id=KB, user=user)
        clarification = getattr(outcome, "clarification", None)
        code = getattr(clarification, "code", None) if clarification else None
        print(
            f"{question:20s} kind={outcome.kind:22s} error_code={outcome.error_code!r:20s} "
            f"facts={len(outcome.computed_facts)} clar_code={code}"
        )
finally:
    user_db.close()
