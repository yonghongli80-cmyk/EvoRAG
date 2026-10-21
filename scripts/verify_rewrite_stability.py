"""Measure whether the query rewrite is stable for one question.

Why this exists: the legacy path rewrites the query unconditionally before retrieval and
reranking, and the rewrite is produced by an LLM. Measured 2026-10-21 on kb_accessories with
`找一下副厂配件明细`, six calls produced **six different sentences**, and the reranker's top score
fell into two clusters that straddle `reliability_min_score` (0.35):

    请提供副厂配件的详细明细清单。      0.703   answers
    问能否提供副厂配件明细清单？        0.672   answers
    请提供副厂配件明细。                0.583   answers
    请查询副厂配件明细。                0.531   answers
    如何查询副厂配件明细？              0.063   refuses
    如何查询或获取副厂配件明细？        0.035   refuses

So the flip is not retrieval, not the passage judge, and not the reranker's fallback: it is the
rewrite changing the question from "find this for me" into "how do I query for this".

Frees Qdrant only if nothing else holds it: **stop the service first**. Needs the rewrite model
and the rerank API (so it reads the operator .env); it is a live diagnostic, not a test.

    .\\.venv\\Scripts\\python.exe scripts\\verify_rewrite_stability.py "找一下副厂配件明细" --rounds 6
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="找一下副厂配件明细")
    parser.add_argument("--kb-id", default="kb_accessories")
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=12, help="candidate pool size to rerank")
    args = parser.parse_args()

    from config import settings
    from rag.pipeline import RAGPipeline
    from rag.user_db import UserDB

    user_db = UserDB(settings.users_db_path)
    user = user_db.get_user_by_id(args.user_id)
    if user is None:
        print(f"user {args.user_id} not found", file=sys.stderr)
        return 2

    pipeline = RAGPipeline(
        kb_id=args.kb_id, user_db=user_db, memory_db_path=":memory:"
    )
    print(f"reliability_min_score = {settings.reliability_min_score}")
    print(f"question = {args.question!r}  kb = {args.kb_id}  rounds = {args.rounds}\n")

    rewrites: Counter[str] = Counter()
    top_scores: list[float | None] = []
    try:
        for attempt in range(args.rounds):
            rewritten = pipeline._rewrite_for_retrieval(  # noqa: SLF001 - diagnostic
                args.question, f"rewrite-stability-{attempt}"
            )
            rewrites[rewritten] += 1
            candidates = pipeline._retrieve_visible_candidates(  # noqa: SLF001
                rewritten, top_k=args.top_k, doc_name=None, tags=None,
                user=user, kb_id=args.kb_id,
            )
            ranked = pipeline.reranker.rerank(rewritten, candidates)
            top = ranked[0].rerank_score if ranked else None
            top_scores.append(top)
            verdict = "answer" if top is not None and top >= settings.reliability_min_score else "refuse"
            shown = "None" if top is None else f"{top:.3f}"
            print(f"#{attempt} top={shown:>6s} -> {verdict:6s} rewritten={rewritten!r}")
    finally:
        user_db.close()

    print(f"\ndistinct rewrites: {len(rewrites)}/{args.rounds}")
    for text, count in rewrites.most_common():
        print(f"  {count}x {text!r}")

    usable = [score for score in top_scores if score is not None]
    if usable:
        spread = max(usable) - min(usable)
        crossed = len({score >= settings.reliability_min_score for score in top_scores if score is not None})
        print(
            f"\ntop score range {min(usable):.3f}..{max(usable):.3f} (spread {spread:.3f}); "
            f"crosses the {settings.reliability_min_score} threshold on both sides: "
            f"{'YES' if crossed > 1 else 'no'}"
        )
        if crossed > 1:
            print(
                "=> the same question produced both answers and refusals; "
                "the variable is the rewrite, not retrieval or the judge."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
