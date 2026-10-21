"""Live check for the real-020 family ("找一下副厂配件明细") after the evidence-wording fix.

Prints the answer, the sources and the passage-judging counters from decision_metadata so
the change can be read off the real endpoint rather than inferred.
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


def stream(question: str, kb_id: str, bearer: str):
    request = urllib.request.Request(
        BASE + "/query/stream",
        data=json.dumps({"question": question, "kb_id": kb_id}).encode("utf-8"),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        method="POST",
    )
    events = []
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
    for question in ("找一下副厂配件明细", "帮我找副厂配件清单"):
        events = stream(question, "kb_accessories", bearer)
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        done = next((e for e in events if e.get("type") == "done"), {})
        sources = done.get("sources") or []
        print(f"\n=== {question} ===")
        print(f"route={done.get('route')} sources={len(sources)}")
        print(f"answer: {answer[:300]}")
        meta = done.get("decision_metadata") or {}
        interesting = {
            k: v
            for k, v in meta.items()
            if any(s in k for s in ("passage_", "evidence", "route", "question_set"))
        }
        print("decision_metadata:", json.dumps(interesting, ensure_ascii=False)[:600])
        for source in sources[:5]:
            print("  source:", source.get("doc_name") or source.get("source"), str(source)[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
