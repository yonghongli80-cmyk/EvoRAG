"""End-to-end: does details_structured_query_kind land in the database via the real endpoint?

Sends questions over /query/stream (the path that actually submits observations), then reads the
newest observation rows. Before this change the column had no writer at all.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8080"
CASES = (
    ("找一下副厂配件明细", "kb_accessories"),
    ("帮我找副厂配件清单", "kb_accessories"),
    ("镜头价格合计", "kb_accessories"),
    ("公司明年的营收目标是多少亿元?", "rag_docs"),
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


def stream(question: str, kb_id: str, bearer: str, session_id: str) -> list[dict]:
    request = urllib.request.Request(
        BASE + "/query/stream",
        data=json.dumps({"question": question, "kb_id": kb_id, "session_id": session_id}).encode("utf-8"),
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
    from config import settings

    conn = sqlite3.connect(settings.evolution_db_path)
    conn.row_factory = sqlite3.Row
    before = conn.execute("select coalesce(max(id), 0) from failure_observations").fetchone()[0]
    conn.close()

    bearer = token()
    for question, kb in CASES:
        events = stream(question, kb, bearer, f"obs-{time.time():.0f}-{question[:6]}")
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "token")
        done = next((e for e in events if e.get("type") == "done"), {})
        kind = (done.get("reliability") or {}).get("answerability_kind")
        print(f"{question[:18]:20s} answerability={kind} answer={answer[:40]!r}")

    # 记录器是异步的：给它一点时间落库
    time.sleep(8)
    conn = sqlite3.connect(settings.evolution_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select id, question, details_answerability_kind kind, details_structured_query_kind sque, "
        "details_decision_metadata meta from failure_observations where id > ? order by id",
        (before,),
    ).fetchall()
    print(f"\nnew observation rows: {len(rows)}")
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        print(
            f"  #{row['id']} kind={row['kind']} structured_query_kind={row['sque']} "
            f"meta_kind={meta.get('structured_query_kind')} | {row['question'][:20]}"
        )
    filled = conn.execute(
        "select count(*) from failure_observations where details_structured_query_kind is not null"
    ).fetchone()[0]
    total = conn.execute("select count(*) from failure_observations").fetchone()[0]
    print(f"\nnon-null structured_query_kind: {filled}/{total}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
