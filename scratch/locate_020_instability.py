"""Where does real-020's instability actually come from?

Separates three candidate causes for one question, on the live configuration (service stopped so
Qdrant is free and the dense path is available):

  A. the candidate pool differs between calls (retrieval is not deterministic)
  B. the pool is stable but the judge flips (already mitigated by voting)
  C. the gate flips on a stable pool and stable judgments (threshold/aggregation)

Prints, per call: pool signature (doc#chunk of the top chunks), the evidence counts, and the
final decision.
"""

from __future__ import annotations

import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from rag.pipeline import RAGPipeline  # noqa: E402
from rag.user_db import UserDB  # noqa: E402

QUESTION = "找一下副厂配件明细"
KB = "kb_accessories"
ROUNDS = 6

user_db = UserDB(settings.users_db_path)
user = user_db.get_user_by_id(1)
pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
try:
    signatures = Counter()
    for attempt in range(ROUNDS):
        chunks = pipeline._retrieve_visible_candidates(  # noqa: SLF001
            QUESTION, top_k=12, doc_name=None, tags=None, user=user, kb_id=KB
        )
        signature = tuple(f"{c.doc_name}#{c.chunk_index}" for c in chunks[:5])
        signatures[signature] += 1
        prepared, error = pipeline._prepare_context(  # noqa: SLF001
            QUESTION, f"src-{attempt}", None, user=user, kb_id=KB
        )
        meta = (prepared or {}).get("decision_metadata") or {}
        kind = getattr((prepared or {}).get("decision"), "kind", None)
        print(
            f"#{attempt} kind={kind!s:22s} judged={meta.get('passage_judged')} "
            f"evidence={meta.get('passage_evidence')} pool[0]={signature[0] if signature else '-'}"
        )
    print("\ndistinct top-5 signatures:", len(signatures))
    for signature, count in signatures.most_common():
        print(f"  {count}x {signature}")
finally:
    user_db.close()
