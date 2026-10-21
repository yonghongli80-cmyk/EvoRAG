"""Live check of the two behaviours this round changed, over /query/stream.

1. `镜头价格合计` — the planner asks which field; retrieval finds sources but the evidence gate
   refuses. Before the fix the user saw "现有可见资料不足"; the clarification must win now.
2. `找一下副厂配件明细` — real-020, which the earlier rounds were about. It must answer.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8080"
CASES = (
    ("镜头价格合计", "kb_accessories"),
    ("各品牌遮光罩价格合计", "kb_accessories"),
    ("脚架环价格合计", "kb_accessories"),
    ("找一下副厂配件明细", "kb_accessories"),
)


def token() -> str:
    from rag.auth import create_token
    from rag.org_auth.repository import OrgRepository
    from rag.user_db import UserDB

    db = UserDB(str(ROOT / "data" / "users.db"))
    try:
        user = db.get_user_by_username("evolab-admin")
        person = OrgRepository(db).get_person_for_user(int(user["id"]))
        payload = {"user_id": int(user["id"]), "username": user["username"], "is_admin": True}
        if person is not None:
            payload["security_version"] = int(person["security_version"])
        return create_token(payload)
    finally:
        db.close()


def stream(question: str, kb_id: str, bearer: str) -> list[dict]:
    request = urllib.request.Request(
        BASE + "/query/stream",
        data=json.dumps({"question": question, "kb_id": kb_id}).encode("utf-8"),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=300) as response:
        for raw in response:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    continue
    return events


def main() -> int:
    bearer = token()
    for question, kb in CASES:
        events = stream(question, kb, bearer)
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        done = next((e for e in events if e.get("type") == "done"), {})
        structured = next((e for e in events if e.get("type") == "structured_result"), None)
        reliability = done.get("reliability") or {}
        print(f"\n=== {question}")
        print(f"  structured_event={bool(structured)} kind={(structured or {}).get('kind')}")
        print(f"  answerability={reliability.get('answerability_kind')} sources={len(done.get('sources') or [])}")
        print(f"  answer: {answer[:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
