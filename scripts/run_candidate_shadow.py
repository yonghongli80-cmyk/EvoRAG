"""Layer 3 (shadow) producer: compare a candidate question set on real cases.

The canary guard requires at least one recorded ``shadow`` comparison. The
evaluator existed but had no production caller, so an administrator could not
satisfy that guard without hand-writing a gate entry. This script is the
operator-facing producer: it runs the same ``CandidateShadowEvaluator`` the
unit tests exercise, against the benchmark cases, and records one shadow gate
entry with the measured sample count and agreement.

It is deliberately a script rather than an HTTP endpoint: the comparison calls
the real Jev SDK, so it needs the operator's credentials and should be an
explicit, auditable action (same pattern as ``measure_patch_regression_ab.py``).

    python scripts/run_candidate_shadow.py --candidate-id 30 --dry-run
    python scripts/run_candidate_shadow.py --candidate-id 30 \
        --cases benchmarks/jev_retrieval_cases.json --actor admin

The script never edits candidate status, never activates a rollout and never
prints question text; the gate entry contains only counts and agreement rates.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from rag.evolution.candidate_store import CandidateStore  # noqa: E402
from rag.evolution.candidates import ARTIFACT_TYPE  # noqa: E402
from rag.evolution.rollout import candidate_question_set_version  # noqa: E402
from rag.jev.candidate_shadow import CandidateShadowEvaluator  # noqa: E402
from rag.jev.client import JevClient  # noqa: E402
from rag.jev.questions import (  # noqa: E402
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
    build_with_overlay,
)
from rag.jev.service import JevDecisionService  # noqa: E402


def _load_cases(path: Path, limit: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases file must contain a non-empty 'cases' list")
    cleaned = []
    for case in cases[: max(1, limit)]:
        if not isinstance(case, dict) or not case.get("question"):
            continue
        cleaned.append(
            {
                "id": str(case.get("id") or ""),
                "question": str(case["question"]),
                "kb_id": str(case.get("kb_id") or "rag_docs"),
            }
        )
    if not cleaned:
        raise ValueError("no usable cases (each needs at least a question)")
    return cleaned


def _candidate_service(candidate: dict) -> tuple[JevDecisionService, str]:
    target = str(candidate.get("target") or "")
    payload = candidate.get("payload") or {}
    base_version = (
        PREFLIGHT_QUESTION_SET_VERSION
        if target == "preflight"
        else PASSAGE_QUESTION_SET_VERSION
    )
    version = candidate_question_set_version(base_version, int(candidate["id"]))

    def provider(stage: str, chunk_ids=None):
        if stage != target:
            if stage == "preflight":
                return build_preflight_questions(), PREFLIGHT_QUESTION_SET_VERSION
            return build_passage_questions(chunk_ids or ()), PASSAGE_QUESTION_SET_VERSION
        return build_with_overlay(target, payload, chunk_ids), version

    return JevDecisionService(JevClient(question_set_provider=provider)), version


def run_shadow(
    candidate: dict,
    cases: list[dict],
    *,
    store,
    actor: str,
    dry_run: bool = False,
    min_route_agreement: float = 0.9,
) -> dict:
    """Run the comparisons and (unless dry-run) append one shadow gate entry."""
    if str(candidate.get("artifact_type") or "") != ARTIFACT_TYPE:
        raise ValueError("candidate is not a jev_question_set")
    target = str(candidate.get("target") or "")
    if target not in ("preflight", "passages"):
        raise ValueError(f"unsupported candidate target: {target!r}")

    current = JevDecisionService(JevClient())
    candidate_service, version = _candidate_service(candidate)
    evaluator = CandidateShadowEvaluator(
        current,
        candidate_service,
        sample_rate=1.0,
        queue_limit=max(1, len(cases)),
    )
    try:
        submitted = 0
        for case in cases:
            if evaluator.submit(
                case["question"],
                kb_id=case["kb_id"],
                query=case["question"],
            ):
                submitted += 1
    finally:
        evaluator.close(wait=True)
    summary = evaluator.summary()
    samples = int(summary.get("samples") or 0)
    route_agreement = summary.get("route_agreement")
    if samples < 1:
        result = "inconclusive"
    elif route_agreement is None or route_agreement < min_route_agreement:
        result = "failed"
    else:
        result = "passed"
    metrics = {
        "samples": samples,
        "submitted": submitted,
        "route_agreement": route_agreement,
        "candidate_version": version,
    }
    detail = (
        f"shadow comparison on {samples} case(s), "
        f"route agreement={route_agreement!r}, candidate version={version}"
    )
    if not dry_run:
        store.record_gate_entry(
            int(candidate["id"]),
            actor_id=actor,
            note=detail,
            gate="shadow",
            result=result,
            gate_detail=detail,
            gate_metrics=metrics,
        )
    return {"result": result, "recorded": not dry_run, **metrics}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", type=int, required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "benchmarks" / "jev_retrieval_cases.json",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--actor", default="shadow-cli")
    parser.add_argument("--min-route-agreement", type=float, default=0.9)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db-path", type=Path, default=None)
    args = parser.parse_args(argv)

    cases = _load_cases(args.cases, args.limit)
    store = CandidateStore(db_path=str(args.db_path or settings.evolution_db_path))
    candidate = store.get_candidate(args.candidate_id)
    if candidate is None:
        print(json.dumps({"error": "candidate_not_found"}), file=sys.stderr)
        return 2
    try:
        report = run_shadow(
            candidate,
            cases,
            store=store,
            actor=args.actor,
            dry_run=args.dry_run,
            min_route_agreement=args.min_route_agreement,
        )
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
