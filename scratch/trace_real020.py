"""Trace how real-020 ends up at a structured clarification, end to end, several times."""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB = "kb_accessories"

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")

original = pipeline._is_structured_clarification  # noqa: SLF001


def traced(structured):
    verdict = original(structured)
    result = (structured or {}).get("structured_result") or {}
    print(
        f"    [structured] route={(structured or {}).get('route')} "
        f"kind={result.get('kind')} is_clarification={verdict}"
    )
    return verdict


pipeline._is_structured_clarification = traced  # type: ignore[method-assign]
try:
    for attempt in range(1, 5):
        prepared, error = pipeline._prepare_context(  # noqa: SLF001
            QUESTION, f"trace020-{attempt}", None, user=user, kb_id=KB
        )
        meta = (prepared or {}).get("decision_metadata") or {}
        print(
            f"=== attempt {attempt}: final route={(prepared or {}).get('route')} "
            f"error={error} sources={len((prepared or {}).get('sources') or [])} "
            f"jev_route={meta.get('jev_route')} reason={meta.get('route_reason')} "
            f"conf={meta.get('intent_confidence')}"
        )
        answer = str((prepared or {}).get("answer") or "")
        if answer:
            print(f"    answer: {answer[:100]}")
finally:
    user_db.close()
