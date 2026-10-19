"""Live end-to-end structured check over the endpoint users actually chat through (/query/stream)."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def stream(question: str, kb_id: str, bearer: str) -> tuple[int, list[dict]]:
    request = urllib.request.Request(
        BASE + "/query/stream",
        data=json.dumps({"question": question, "kb_id": kb_id}).encode("utf-8"),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        method="POST",
    )
    events: list[dict] = []
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            status = response.status
            for raw in response:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    continue
    except urllib.error.HTTPError as error:
        return error.code, [{"detail": error.read().decode("utf-8", "ignore")[:200]}]
    return status, events


def main() -> int:
    bearer = token()
    for kb_id, question in (
        ("kb_accessories", "遮光罩售价最高是多少？"),
        ("kb_accessories", "佳能 EOS R8 的有效像素是多少？"),
    ):
        started = time.perf_counter()
        status, events = stream(question, kb_id, bearer)
        elapsed = (time.perf_counter() - started) * 1000
        kinds: dict[str, int] = {}
        for event in events:
            kinds[event.get("type", "?")] = kinds.get(event.get("type", "?"), 0) + 1
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        done = next((e for e in events if e.get("type") == "done"), {})
        print(f"\n[{kb_id}] {question}")
        print(f"  HTTP {status} in {elapsed:.0f}ms events={kinds}")
        print(f"  answer: {answer[:160]!r}")
        for key in ("route", "structured_result", "reliability", "sources"):
            if key in done:
                print(f"  done.{key}: {str(done[key])[:200]}")
        structured = [e for e in events if "structured" in str(e.get("type", ""))]
        for event in structured[:2]:
            print(f"  structured event: {str(event)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
