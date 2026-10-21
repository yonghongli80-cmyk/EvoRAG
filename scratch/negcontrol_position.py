"""Negative control for the answer-position scoring on the real benchmark path.

Runs the real benchmark (same pipeline, same cases) with the Jev path forced OFF, i.e. the
legacy behaviour where real-020 used to answer. The cases labeled ``expected_position`` must
then disagree with the live Jev path, and the ``refusal`` criterion must FAIL. If it still
passes, the criterion is decorative and proves nothing.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402
from scripts.benchmark_jev_retrieval import (  # noqa: E402
    evaluate_acceptance,
    load_cases,
    run_benchmark,
)

CASES = "benchmarks/real-cases-v1.json"
LABELED = {"real-012", "real-017", "real-018", "real-020"}


def main() -> int:
    cases = tuple(c for c in load_cases(CASES) if c.id in LABELED)
    print("cases:", [c.id for c in cases])
    from scripts.benchmark_jev_retrieval import build_live_preparations

    old, new = build_live_preparations(kb_id="rag_docs", user_id=1)

    # Force the legacy path on the "new" side: same cases, same pipeline, Jev off.
    original_mode = settings.jev_mode
    settings.jev_mode = "off"
    try:
        report = run_benchmark(
            cases,
            lambda case: old(case, session_id=f"neg-old-{case.id}"),
            lambda case: new(case, session_id=f"neg-new-{case.id}"),
            runs=1,
            warmups=0,
        )
    finally:
        settings.jev_mode = original_mode

    for sample in report.case_breakdowns:
        print(f"  {sample.case_id}: expected={sample.new.expected_route}")
    print()
    print("old side:", report.old.refusal_case_count, report.old.refusal_escapes, report.old.refusal_misses)
    print("new side:", report.new.refusal_case_count, report.new.refusal_escapes, report.new.refusal_misses)
    check = next(c for c in report.verdict.checks if c.name == "refusal")
    print("refusal check:", check.passed, "|", check.detail)
    verdict = evaluate_acceptance(report.old, report.new)
    print("refusal in failures:", "refusal" in verdict.failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
