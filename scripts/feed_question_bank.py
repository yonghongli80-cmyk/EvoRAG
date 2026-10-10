"""把题库里**尚未跑过**的问题走一遍线上同一条路径，让真实失败落成观测。

为什么用进程内而不是打 HTTP：这个部署 `RAG_AUTH_ENABLED=true`，`/login` 需要密码（脚本不该猜，
也不该改你的账号密码），`RAG_AUTH_KEYS` 又未配置 ⇒ HTTP 需要改 `.env` 并重启服务。进程内调用的是
**线上同一个构造函数** `rag.api._make_pipeline(kb_id=...)`——它注入 `failure_recorder`、feedback、
gap analyzer 与灰度控制器——所以记录下来的观测与线上同源。

**与线上唯一的两处偏差，如实写在这里**：
1. 语料从**副本**读（`RAG_QDRANT_PATH`）。服务以本地模式持有 Qdrant 目录锁，两个进程不能同读一个
   目录；副本内容与线上分片一致，且"失败与否"由 pipeline 自己计算，与读哪份副本无关。
2. 线上 `/query` 把 `kb_id="rag_docs"` 写死（历史遗留），这里传**该问题自己的 kb_id**（与 pipeline
   的构造一致）。这处差异本身是个值得记录的发现。

**实测结论（2026-10-10，先别急着重跑）**：题库 50 条里 18 条早已进过回路；剩下 **32 条全跑一遍，
零失败**（`answerable` 24 / `clarification_required` 5 / 被 guard 拦下 3，**没有一条
`insufficient_evidence`**）⇒ **这份题库作为供给已经用尽**，真实供给只能来自线上用户流量。
记录链路本身用一条注定答不出的问题在**临时进化库**上验证过（`insufficient_evidence` → 落成观测；
生产库保持 158 不变）——即"零失败"不是链路坏了，而是**系统确实答得上**。

用法::

    # 先干跑看会跑哪些（不加载 pipeline、不写任何东西）
    python scripts/feed_question_bank.py --dry-run
    # 真跑：小批验证 → 全量
    python scripts/feed_question_bank.py --limit 2 --qdrant-copy <副本目录>
    python scripts/feed_question_bank.py --qdrant-copy <副本目录> --json-out ...json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BANK = "real-case-handoff/question-bank/questions-verified.json"


def _load_bank(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else (data.get("questions") or data.get("cases") or [])
    return [row for row in rows if isinstance(row, dict) and row.get("question")]


def _already_run(db_path: str) -> set[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {row[0] for row in con.execute("SELECT DISTINCT question FROM failure_observations")}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", default=DEFAULT_BANK)
    parser.add_argument("--qdrant-copy", default="", help="语料副本目录（服务持锁时必填）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全部）")
    parser.add_argument("--dry-run", action="store_true", help="只列出要跑哪些")
    parser.add_argument("--user-id", type=int, default=1, help="以哪个主体跑（默认 1 = bench-admin）")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    from config import settings

    bank = _load_bank(args.bank)
    seen = _already_run(settings.evolution_db_path)
    pending = [row for row in bank if row["question"] not in seen]
    if args.limit:
        pending = pending[: args.limit]
    print(
        json.dumps(
            {
                "bank": len(bank),
                "already_ran": len(bank) - len([r for r in bank if r["question"] not in seen]),
                "pending": len(pending),
                "kb_mix": dict(Counter(row["kb_id"] for row in pending)),
                "family_mix": dict(Counter(row.get("family", "?") for row in pending)),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.dry_run:
        for row in pending[:10]:
            print(f"  {row.get('case_id','?')} [{row['kb_id']}] {row['question'][:46]}")
        return 0

    if args.qdrant_copy:
        settings.qdrant_path = args.qdrant_copy
    settings.evolution_enabled = True

    from rag.user_db import UserDB

    user = UserDB(settings.users_db_path).get_user_by_id(args.user_id)
    if user is None:
        raise SystemExit(f"主体 {args.user_id} 不存在")
    print(f"主体: id={user['id']} username={user.get('username')} admin={user.get('is_admin')}", flush=True)

    import rag.api as api

    user_db = UserDB(settings.users_db_path)

    def observations() -> set[str]:
        return _already_run(settings.evolution_db_path)

    results: list[dict] = []
    pipelines: dict[str, object] = {}
    for index, row in enumerate(pending, start=1):
        kb_id = str(row["kb_id"])
        entry: dict = {"case_id": row.get("case_id"), "kb_id": kb_id, "question": row["question"]}
        before = observations()
        started = time.perf_counter()
        try:
            pipeline = pipelines.get(kb_id)
            if pipeline is None:
                pipeline = api._make_pipeline(kb_id=kb_id, user_db=user_db)
                pipelines[kb_id] = pipeline
            result = pipeline.query(
                row["question"], top_k=5, session_id=None, user=user, kb_id=kb_id
            )
            decision = getattr(result, "decision", None)
            entry["answerability"] = getattr(decision, "kind", None)
            entry["confidence"] = getattr(getattr(result, "confidence", None), "level", None)
            entry["answered"] = bool(str(getattr(result, "answer", "") or "").strip())
        except Exception as error:  # noqa: BLE001 - 一条失败不能中断整批
            entry["exception"] = f"{type(error).__name__}: {str(error)[:120]}"
        entry["elapsed_s"] = round(time.perf_counter() - started, 1)
        after = observations()
        entry["recorded"] = bool(after - before)
        entry["recorded_question"] = sorted(after - before)[:1]
        results.append(entry)
        print(
            f"[{index}/{len(pending)}] {entry['case_id']} [{kb_id}] "
            f"{entry.get('answerability') or entry.get('exception','?')} "
            f"{'→ 已落成观测 ✓' if entry['recorded'] else '→ 未落观测'} {entry['elapsed_s']}s",
            flush=True,
        )

    summary = {
        "ran": len(results),
        "recorded": sum(1 for entry in results if entry["recorded"]),
        "by_answerability": dict(Counter(str(entry.get("answerability")) for entry in results)),
        "exceptions": sum(1 for entry in results if entry.get("exception")),
        "wall_s": round(sum(entry["elapsed_s"] for entry in results), 1),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
