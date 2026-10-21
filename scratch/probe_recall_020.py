"""Show what the live retrieval actually returns for the real-020 family (BM25 only, no Jev)."""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTIONS = ("找一下副厂配件明细", "帮我找副厂配件清单")
KB_ID = "kb_accessories"
USER_ID = 1


def main() -> int:
    user_db = UserDB(settings.users_db_path)
    pipeline = RAGPipeline(kb_id=KB_ID, user_db=None, memory_db_path=":memory:")
    pipeline._user_db = user_db  # noqa: SLF001
    try:
        user = user_db.get_user_by_id(USER_ID)
        for question in QUESTIONS:
            print(f"\n=== {question} ===")
            chunks = pipeline._retrieve_visible_candidates(  # noqa: SLF001
                question, top_k=24, doc_name=None, tags=None, user=user, kb_id=KB_ID
            )
            print(f"pool={len(chunks)}")
            for index, chunk in enumerate(chunks):
                text = " ".join(chunk.text.split())
                meta = getattr(chunk, "metadata", {}) or {}
                marker = " ***" if ("LH878" in text or "LH1113" in text or "B060" in text) else ""
                print(
                    f"  #{index:02d} src={meta.get('doc_name', '?')} "
                    f"sheet={meta.get('sheet_name', '')} score={getattr(chunk, 'score', None)}{marker}"
                )
                print(f"       {text[:90]}")
    finally:
        user_db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
