"""Do the real planner-clarification questions lose their clarification to retrieval?

For each question: coordinator kind, whether the pipeline classified it as a clarification, how
many sources retrieval produced, and what the user finally sees. If the clarification is
classified but the answer comes from retrieval/refusal, the discard branch fired.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTIONS = ("脚架环价格合计", "佳能电池价格是多少", "各品牌遮光罩价格合计")
KB = "kb_accessories"
ATTEMPTS = 3

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
coordinator = pipeline._spreadsheet_coordinator  # noqa: SLF001
current: dict = {}

original_structured = pipeline._attempt_structured_query  # noqa: SLF001
original_clarify = pipeline._is_structured_clarification  # noqa: SLF001
original_finalize = pipeline._finalize_rag_context  # noqa: SLF001


def structured(question, coord, user_, kb):
    result = original_structured(question, coord, user_, kb)
    current["structured"] = "None" if result is None else (result.get("structured_result") or {}).get("kind")
    return result


def clarify(prepared):
    verdict = original_clarify(prepared)
    current["is_clarification"] = verdict
    return verdict


def finalize(*args, **kwargs):
    prepared = original_finalize(*args, **kwargs)
    current["sources"] = len(prepared.get("sources") or [])
    current["kind"] = getattr(prepared.get("decision"), "kind", None)
    return prepared


pipeline._attempt_structured_query = structured  # type: ignore[method-assign]
pipeline._is_structured_clarification = clarify  # type: ignore[method-assign]
pipeline._finalize_rag_context = finalize  # type: ignore[method-assign]

try:
    for question in QUESTIONS:
        print(f"\n=== {question}")
        outcome = coordinator.query(question, kb_id=KB, user=user)
        print(f"  coordinator kind={outcome.kind}")
        for attempt in range(ATTEMPTS):
            current.clear()
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                question, f"clar-{attempt}-{question}", None, user=user, kb_id=KB
            )
            answer = str((prepared or {}).get("answer") or "")
            print(
                f"  #{attempt} structured_kind={current.get('structured')} "
                f"classified_as_clarification={current.get('is_clarification')} "
                f"sources={current.get('sources')} decision={current.get('kind')} "
                f"route={(prepared or {}).get('route')} answer={answer[:46]!r}"
            )
finally:
    user_db.close()
