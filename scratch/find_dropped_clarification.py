"""Search for the worst case: planner asks for a clarification, retrieval finds sources,
but the evidence gate refuses. That combination drops the clarification AND gives no answer.

The discard branch is gated on `not prepared["sources"]`, and sources include the Jev citation
fallback (relevant chunks with no answer evidence). So a refusal *with* sources silently
replaces the clarification with "现有可见资料不足".
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

CANDIDATES = {
    "kb_accessories": (
        "脚架环价格合计", "各品牌遮光罩价格合计", "遮光罩价格合计", "镜头价格合计",
        "电池价格合计", "各型号电池价格合计", "脚架环数量合计",
    ),
    "kb_hr": (
        "员工人数合计", "各岗位人数合计", "招聘数量合计", "盘点人数合计",
    ),
    "kb_evolab": (
        "产品数量合计", "各产品价格合计",
    ),
}

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipelines: dict[str, RAGPipeline] = {}
current: dict = {}


def instrument(pipeline: RAGPipeline) -> None:
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
    for kb, questions in CANDIDATES.items():
        pipelines[kb] = RAGPipeline(kb_id=kb, user_db=user_db, memory_db_path=":memory:")
        instrument(pipelines[kb])
        pipeline = pipelines[kb]
        coordinator = pipeline._spreadsheet_coordinator  # noqa: SLF001
        for question in questions:
            outcome = coordinator.query(question, kb_id=kb, user=user)
            if outcome.kind != "clarification_required":
                continue
            current.clear()
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                question, f"worst-{question}", None, user=user, kb_id=kb
            )
            answer = str((prepared or {}).get("answer") or "")
            route = (prepared or {}).get("route")
            # 修复后的判据：澄清在"检索没给出答案"时胜出。所以 route=structured 且答案就是
            # 澄清文案 ⇒ 正确；route=rag/fixed 且 decision 非 answerable ⇒ 澄清又被丢了（坏）。
            got_clarification = route == "structured"
            answered = current.get("kind") in ("answerable", "answerable_with_warning")
            if answered:
                verdict = "answer wins (ok)"
            elif got_clarification:
                verdict = "clarification returned (ok)"
            else:
                verdict = "CLARIFICATION STILL DROPPED"
            print(
                f"[{kb}] {question:22s} planner={outcome.kind} "
                f"clarified={current.get('is_clarification')} sources={current.get('sources')} "
                f"decision={current.get('kind')} route={route} "
                f"answer={answer[:30]!r} -> {verdict}"
            )
finally:
    user_db.close()
