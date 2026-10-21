"""Why does real-020 refuse on the live endpoint while the in-process trace says answerable?

Prints every SSE event type, the reliability payload, and repeats with fresh sessions.
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
QUESTION = "找一下副厂配件明细"
KB = "kb_accessories"
ROUNDS = 3


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


def stream(question: str, kb_id: str, bearer: str, session_id: str | None) -> list[dict]:
    body = {"question": question, "kb_id": kb_id}
    if session_id:
        body["session_id"] = session_id
    request = urllib.request.Request(
        BASE + "/query/stream",
        data=json.dumps(body).encode("utf-8"),
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
    for round_index in range(ROUNDS):
        events = stream(QUESTION, KB, bearer, f"live020-{round_index}")
        kinds: dict[str, int] = {}
        for event in events:
            kinds[event.get("type", "?")] = kinds.get(event.get("type", "?"), 0) + 1
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        sources = next((e for e in events if e.get("type") == "sources"), {})
        done = next((e for e in events if e.get("type") == "done"), {})
        print(f"\n=== round {round_index} events={kinds}")
        print(f"  sources={len(sources.get('sources') or [])} reliability={done.get('reliability')}")
        print(f"  answer: {answer[:170]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
