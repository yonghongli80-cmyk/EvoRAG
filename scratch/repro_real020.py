"""Reproduce real-020 falling into a structured clarification instead of retrieving."""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.jev.policy import KbCapabilities  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB = "kb_accessories"

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
coordinator = getattr(pipeline, "_spreadsheet_coordinator", None)
print("coordinator present:", coordinator is not None)
try:
    fields = pipeline._structured_field_names(coordinator, KB, user)  # noqa: SLF001
    capabilities = KbCapabilities(
        has_structured_data=bool(coordinator is not None),
        structured_fields=fields,
    )
    print(f"structured_fields shown to judge: {len(fields)} e.g. {list(fields)[:8]}")
    for attempt in range(1, 4):
        route_decision = pipeline.jev.route(QUESTION, capabilities)
        print(f"\n=== attempt {attempt}: jev route={route_decision.route} reason={route_decision.reason}")
        structured = pipeline._attempt_structured_query(QUESTION, coordinator, user, KB)  # noqa: SLF001
        if structured is None:
            print("  structured attempt -> None (retrieval would run)")
            continue
        result = structured.get("structured_result") or {}
        print(f"  structured route={structured.get('route')} kind={result.get('kind')}")
        print(f"  answer preview: {str(structured.get('answer'))[:140]}")
        print(f"  is_clarification={pipeline._is_structured_clarification(structured)}")  # noqa: SLF001
finally:
    user_db.close()
