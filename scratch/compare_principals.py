"""Compare the in-process decision for real-020 across the two principals.

The live endpoint authenticates as `evolab-admin`; the earlier in-process traces used user id 1.
If the ACL differs, the candidate pool differs, and so does the verdict.
"""

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
try:
    for name in ("evolab-admin",):
        record = user_db.get_user_by_username(name)
        print(f"{name}: id={record['id'] if record else None} admin={record.get('is_admin') if record else None}")
    id_one = user_db.get_user_by_id(1)
    print(f"user id 1: {id_one.get('username') if id_one else None} admin={id_one.get('is_admin') if id_one else None}")

    principals = []
    if id_one:
        principals.append(("id1", id_one))
    admin = user_db.get_user_by_username("evolab-admin")
    if admin:
        principals.append(("evolab-admin", admin))

    pipeline = RAGPipeline(kb_id=KB, user_db=user_db, memory_db_path=":memory:")
    for label, user in principals:
        for attempt in range(2):
            prepared, error = pipeline._prepare_context(  # noqa: SLF001
                QUESTION, f"cmp-{label}-{attempt}", None, user=user, kb_id=KB
            )
            decision = getattr((prepared or {}).get("decision"), "kind", None)
            print(
                f"{label:14s} #{attempt} route={(prepared or {}).get('route'):10s} "
                f"decision={decision} sources={len((prepared or {}).get('sources') or [])}"
            )
finally:
    user_db.close()
