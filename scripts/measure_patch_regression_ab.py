"""§5.1's regression A/B, run where the credentials are.

The sandbox cannot measure a patch: it strips every RAG_* setting, and both the embedding and
the reranking calls are external, so a replay inside it measures the absence of a key. The
procedure that replaces it is the administrator's own: apply the exported patch to a scratch
worktree, run the target tests and the fixed regression net, then replay the same benchmark
once in a clean worktree and once in the patched one — from here, where the key exists — and
compare the two runs side by side with the layer's thresholds.

    python scripts/measure_patch_regression_ab.py --candidate-id 10 \
        --snapshot <read-only data copy> [--kb-id kb_accessories] [--user-id 1]

It creates both worktrees under one temporary root and removes them in a finally block. Nothing
is applied to the main tree, and nothing is merged: the report, the apply command and the
revert command go to the operator. This is the scripted form of
``docs/evolution-code-patches.md`` section 5.1: the sandbox cannot run it (it strips every
RAG_* setting, credentials included, while both embedding and reranking are external calls),
so the regression measurement lives here, where the key does.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(r"D:\Desktop\RAGv3")
OUT = ROOT / "real-case-handoff" / "evolab"  # 报告写在本地留档目录（不入库）
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

settings.jev_mode = "active"
settings.jev_kb_allowlist = "rag_docs,kb_hr,kb_accessories,kb_after_sales,kb_evolab"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *args), cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-id", type=int, required=True)
    parser.add_argument("--snapshot", required=True, help="只读 qdrant 副本目录")
    parser.add_argument("--kb-id", default="kb_accessories")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--runs", type=int, default=2, help="每侧重复次数（降次数=更快更糙）")
    parser.add_argument("--warmups", type=int, default=0)
    args = parser.parse_args()

    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.code_patches import CodePatchCandidate
    from rag.evolution.sandbox import _replay_env, _run_benchmark, patch_verdict
    from rag.evolution.store import FailureTraceStore

    trace = FailureTraceStore(settings.evolution_db_path)
    store = CandidateStore(trace)
    root = Path(tempfile.mkdtemp(prefix="rag-patch-ab-"))
    report: dict = {"candidate_id": args.candidate_id, "kb_id": args.kb_id}
    try:
        row = store.get_candidate(args.candidate_id)
        diff = str(row["diff"] or "")
        paths = tuple(
            dict.fromkeys(
                line[6:].strip().removeprefix("b/")
                for line in diff.splitlines()
                if line.startswith("+++ ") and "/dev/null" not in line
            )
        )
        candidate = CodePatchCandidate(
            artifact_type=str(row["artifact_type"]),
            layer=str(row["layer"]),
            paths=paths,
            test_paths=tuple(row["test_paths"] or ()),
            rationale=str(row["rationale"] or ""),
            expected_effect=str(row["expected_effect"] or ""),
            rollback_ref=dict(row["rollback_ref"] or {}),
            confidence=str(row["confidence"]),
            diff=diff,
        )
        base = str(candidate.rollback_ref.get("base_commit") or "")
        report.update({"paths": list(paths), "test_paths": list(candidate.test_paths), "base": base[:8]})

        clean = root / "clean"
        patched = root / "patched"
        for worktree in (clean, patched):
            added = _git("worktree", "add", str(worktree), "--detach", base or "HEAD", cwd=ROOT)
            if added.returncode != 0:
                report["error"] = f"worktree add failed: {added.stderr.strip()[:200]}"
                print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
                return 1
            # 工作树里没有 .env / data（git-ignored）：被测代码只读仓库内的文件与快照。
            for name in (".env",):
                source = ROOT / name
                if source.is_file():
                    shutil.copy2(source, worktree / name)
        applied = subprocess.run(
            ("git", "apply", "--whitespace=nowarn", "-"),
            cwd=str(patched), input=diff, capture_output=True, text=True, encoding="utf-8",
        )
        report["applied"] = applied.returncode == 0
        if applied.returncode != 0:
            report["error"] = f"git apply failed: {applied.stderr.strip()[:200]}"
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            return 1

        started = time.perf_counter()
        baseline, baseline_error = _run_benchmark(
            worktree=clean,
            env=_replay_env(clean, args.snapshot),
            sandbox_root=root,
            kb_id=args.kb_id,
            timeout=args.timeout,
            user_id=args.user_id,
            report_name="benchmark-baseline.json",
            runs=args.runs,
            warmups=args.warmups,
        )
        patched_payload, patched_error = _run_benchmark(
            worktree=patched,
            env=_replay_env(patched, args.snapshot),
            sandbox_root=root,
            kb_id=args.kb_id,
            timeout=args.timeout,
            user_id=args.user_id,
            runs=args.runs,
            warmups=args.warmups,
        )
        report["runs"] = args.runs
        report["elapsed_s"] = round(time.perf_counter() - started, 1)
        error = baseline_error or patched_error
        if error:
            report["replay_error"] = error
        else:
            report["verdict"] = patch_verdict(baseline or {}, patched_payload or {})
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        (OUT / f"regression-ab-candidate-{args.candidate_id}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0
    finally:
        for worktree in (root / "clean", root / "patched"):
            if worktree.exists():
                _git("worktree", "remove", "--force", str(worktree), cwd=ROOT)
        _git("worktree", "prune", cwd=ROOT)
        shutil.rmtree(root, ignore_errors=True)
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
