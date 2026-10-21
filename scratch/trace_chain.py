"""Trace the structured-clarification decision chain, end to end.

Records, per attempt:
  * what `_attempt_structured_query` returned (None / structured result + its kind)
  * whether it was classified as a clarification
  * how many sources retrieval produced
  * the final route / answerability

Purpose: confirm whether "a usable structured result is dropped" actually happens, instead of
assuming it from reading the code.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

CASES = (
    ("找一下副厂配件明细", "kb_accessories"),
    ("r50相关价格", "kb_accessories"),
    ("遮光罩售价最高是多少？", "kb_accessories"),
    ("r50电池价格", "kb_accessories"),
    ("帮我找副厂配件清单", "kb_accessories"),
)
ATTEMPTS = 3

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipelines: dict[str, RAGPipeline] = {}
current: dict = {}


def instrument(pipeline: RAGPipeline) -> None:
    """Wrap the three decision points once; every call appends to ``current``."""
    original_structured = pipeline._attempt_structured_query  # noqa: SLF001
    original_clarify = pipeline._is_structured_clarification  # noqa: SLF001
    original_finalize = pipeline._finalize_rag_context  # noqa: SLF001

    def structured(question, coordinator, user_, kb):
        result = original_structured(question, coordinator, user_, kb)
        if result is None:
            current["structured"] = "None"
        else:
            body = result.get("structured_result") or {}
            current["structured"] = f"route={result.get('route')} kind={body.get('kind')}"
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
    for question, kb in CASES:
        if kb not in pipelines:
            pipelines[kb] = RAGPipeline(kb_id=kb, user_db=user_db, memory_db_path=":memory:")
            instrument(pipelines[kb])
        pipeline = pipelines[kb]
        print(f"\n=== {question} [{kb}]")
        for attempt in range(ATTEMPTS):
            current.clear()
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                question, f"chain-{attempt}-{question}", None, user=user, kb_id=kb
            )
            meta = (prepared or {}).get("decision_metadata") or {}
            print(
                f"  #{attempt} structured={current.get('structured')} "
                f"clarification={current.get('is_clarification')} "
                f"sources={current.get('sources')} kind={current.get('kind')} "
                f"final_route={(prepared or {}).get('route')} "
                f"reason={meta.get('route_reason')} err={error}"
            )
finally:
    user_db.close()
