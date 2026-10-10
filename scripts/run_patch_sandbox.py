"""Run the sandbox for one stored code patch and write the result back onto it.

The sandbox conclusion had no production writer: :func:`sandbox_gate_outcome` was only ever
called by tests, so ``candidates.gate_log`` never carried a real sandbox entry and the review
panel had nothing numeric to show. This script is the supported writer. It is a *manual* step on
purpose — a sandbox needs a clean main tree and isolates itself in two worktrees, which is not
something a background round should start — and it records status, detail and the structured
metrics through :func:`record_sandbox_outcome`.

    python scripts/run_patch_sandbox.py --candidate-id 3 \
        --data-snapshot D:/tmp/corpus-copy --fixture real-case-handoff/evolab/replay-fixture.json

Without ``--fixture`` the replay runs with the sandbox's normal credential-free environment and
reports "replay unavailable"; with a fixture it replays the recorded seams, and a patch that
touches one of those modules is refused as stale rather than measured against the wrong code.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402


def _candidate_row(db_path: str, candidate_id: int) -> tuple[int, dict]:
    columns = (
        "id, artifact_type, layer, diff, test_paths, rationale, expected_effect, "
        "rollback_ref, confidence, status"
    )
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(f"SELECT {columns} FROM candidates WHERE id = ?", (int(candidate_id),)).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"candidate {candidate_id} not found")
    fields = dict(row)
    for key, empty in (("test_paths", []), ("rollback_ref", {})):
        value = fields.get(key)
        if isinstance(value, str):
            fields[key] = json.loads(value) if value else empty
    return int(fields.pop("id")), fields


def _head_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", type=int, required=True)
    parser.add_argument("--data-snapshot", default="", help="语料副本目录（回放读它）")
    parser.add_argument("--fixture", default="", help="黄金 fixture（不触 seam 的补丁才有意义）")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--actor-id", default="admin:cli")
    parser.add_argument("--note", default="沙箱回放由管理员手动执行")
    parser.add_argument("--dry-run", action="store_true", help="只跑沙箱，不写回轨迹")
    args = parser.parse_args(argv)

    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.code_patches import CodePatchCandidate
    from rag.evolution.sandbox import record_sandbox_outcome, run_patch_sandbox, sandbox_gate_metrics
    from rag.evolution.store import FailureTraceStore

    candidate_id, fields = _candidate_row(settings.evolution_db_path, args.candidate_id)
    diff = str(fields.get("diff") or "")
    if not diff:
        raise SystemExit(f"candidate {candidate_id} has no stored diff")
    candidate = CodePatchCandidate(
        artifact_type=str(fields.get("artifact_type") or "code_patch"),
        layer=str(fields.get("layer") or "code"),
        paths=tuple(line[6:].strip() for line in diff.splitlines() if line.startswith("+++ b/")),
        diff=diff,
        test_paths=tuple(fields.get("test_paths") or ()),
        rationale=str(fields.get("rationale") or ""),
        expected_effect=str(fields.get("expected_effect") or ""),
        rollback_ref=dict(fields.get("rollback_ref") or {"base_commit": _head_commit()}),
        confidence=str(fields.get("confidence") or "low"),
    )
    print(
        f"candidate #{candidate_id}: status={fields.get('status')} "
        f"{len(diff.splitlines())} diff lines",
        flush=True,
    )

    report = run_patch_sandbox(
        candidate,
        repo_root=ROOT,
        run_benchmark=True,
        data_snapshot=args.data_snapshot or None,
        replay_user_id=args.user_id,
        replay_fixture=args.fixture or None,
    )
    metrics = sandbox_gate_metrics(report)
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        return 0 if report.applied else 1

    store = FailureTraceStore(settings.evolution_db_path)
    try:
        try:
            row = record_sandbox_outcome(
                CandidateStore(store),
                candidate_id,
                report,
                actor_id=args.actor_id,
                note=args.note,
            )
        except ValueError as error:
            # 状态机不允许这次移动（例如候选已被决策过）：如实说出来，别假装写了轨迹。
            print(f"轨迹未写入：{error}", flush=True)
            return 1
    finally:
        store.close()
    latest = (row.get("gate_log") or [])[-1] if row.get("gate_log") else {}
    print(
        json.dumps(
            {
                "written": True,
                "status": row.get("status"),
                "gate": latest.get("gate"),
                "result": latest.get("result"),
                "detail": latest.get("detail"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
