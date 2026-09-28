"""Repeatable diagnosis measurement over a fixed observation set.

The phase-3 acceptance number (24/28) came from one run over 28 rows, and the
diagnosis model flips categories between runs on identical evidence, so a single
agreement number cannot tell a prompt change from noise. This runner makes the
measurement repeatable instead:

* the case set is an explicit file of ``(question, kb_id, expected_category)``
  rows, so it does not depend on cluster status, queue state or which drafts a
  previous run happened to leave behind;
* every case is looked up by question text and diagnosed ``--repeats`` times
  against the *same* evidence pack, so model variance is measured separately
  from retrieval variance;
* the report carries per-run agreement, majority-vote agreement, per-row
  stability and the contract-violation rate, which is what a before/after
  comparison needs.

Read-only against the stores, and the knowledge base is read through
``--qdrant-path``: point it at a snapshot of ``qdrant_data`` (copy it and drop
``.lock``) when another process — the 1102 service — already holds the live
directory, because the local Qdrant client takes an exclusive lock per path.

Usage::

    python scripts/run_diagnosis_devset.py \
        --cases real-case-handoff/diagnosis-devset-cases.json \
        --repeats 3 --qdrant-path %TEMP%/qdrant-snapshot \
        --label diagnosis-v3 --json-out artifacts/diagnosis-devset-v3.json

The case file is local data: it carries company questions, so it stays out of
the repository (``real-case-handoff/`` is ignored) exactly like the replay
corpus. This runner never prints or stores question text beyond the case id it
was given, and it writes nothing to ``evolution.db``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Direct script execution puts ``scripts/`` first on ``sys.path``, and this
# environment's editable install appends the project root only *after*
# site-packages — which still holds a stale ``config.py`` copy from before the
# evolution phase (no ``evolution_db_path``). A plain membership check would skip
# the reorder and that stale copy would win, so the project root is moved to the
# front before anything project-local is imported. Same fix as
# ``scripts/benchmark_jev_retrieval.py::_ensure_project_root_on_path``.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

#: Cases whose observation row is missing are reported, not silently dropped:
#: "how much of the case set is still measurable" is part of the measurement.
NO_OBSERVATION = "no_observation"


@dataclass(frozen=True)
class Case:
    """One labeled case: the question, its knowledge base, the expected category."""

    question: str
    kb_id: str
    expected_category: str
    case_id: str = ""


@dataclass
class CaseResult:
    """Every repeat of one case, plus the deterministic facts of its pack."""

    case: Case
    outcomes: list[str] = field(default_factory=list)
    reproduction_reproducible: bool | None = None
    coverage_terms: int = 0
    coverage_hits: int = 0
    elapsed_ms: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def matched(self) -> int:
        """How many repeats produced the expected category."""
        return sum(1 for outcome in self.outcomes if outcome == self.case.expected_category)

    @property
    def contract_violations(self) -> int:
        """Repeats that did not produce a contract-valid draft at all."""
        return sum(1 for outcome in self.outcomes if outcome.startswith("invalid:"))

    @property
    def stable(self) -> bool:
        """All repeats agreed on one category (a violation count is not a category)."""
        categories = [outcome for outcome in self.outcomes if not outcome.startswith("invalid:")]
        return len(categories) > 1 and len(set(categories)) == 1

    def majority(self) -> str | None:
        """The most frequent valid category, or ``None`` on an empty set or a tie."""
        categories = [outcome for outcome in self.outcomes if not outcome.startswith("invalid:")]
        if not categories:
            return None
        tally: dict[str, int] = {}
        for category in categories:
            tally[category] = tally.get(category, 0) + 1
        best = max(tally.values())
        winners = [category for category, count in tally.items() if count == best]
        return winners[0] if len(winners) == 1 else None


def load_cases(path: str | Path) -> list[Case]:
    """Read the case file; a malformed row is refused rather than skipped."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("the case file must be a JSON list")
    cases: list[Case] = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"case[{index}] is not an object")
        missing = [key for key in ("question", "kb_id", "expected_category") if not row.get(key)]
        if missing:
            raise ValueError(f"case[{index}] is missing {', '.join(missing)}")
        cases.append(
            Case(
                question=str(row["question"]),
                kb_id=str(row["kb_id"]),
                expected_category=str(row["expected_category"]),
                case_id=str(row.get("case_id") or f"case-{index + 1}"),
            )
        )
    return cases


def summarize(results: Sequence[CaseResult], *, repeats: int) -> dict:
    """The measurement: per-run agreement, majority agreement, stability, violations.

    Per-run agreement is what a single-shot production run would have scored on
    that repeat index; the majority column is the ceiling a stable model would
    reach; ``stable_rows`` is how much of the case set the model answers the same
    way twice in a row. A change that moves run-1 agreement inside the spread
    between run indices has not been shown to do anything.
    """
    compared = [result for result in results if result.outcomes]
    per_run = [
        sum(1 for result in compared if index < len(result.outcomes) and result.outcomes[index] == result.case.expected_category)
        for index in range(repeats)
    ]
    majority = sum(1 for result in compared if result.majority() == result.case.expected_category)
    violations = sum(result.contract_violations for result in compared)
    latencies = [value for result in compared for value in result.elapsed_ms]
    return {
        "cases": len(results),
        "measurable": len(compared),
        "unmeasurable": [result.case.case_id for result in results if not result.outcomes],
        "repeats": repeats,
        "agreement_by_run": per_run,
        "agreement_by_run_rate": [round(value / len(compared), 4) if compared else None for value in per_run],
        "agreement_majority": majority,
        "agreement_majority_rate": round(majority / len(compared), 4) if compared else None,
        "stable_rows": sum(1 for result in compared if result.stable),
        "contract_violations": violations,
        "contract_violation_rate": round(violations / (len(compared) * repeats), 4) if compared else None,
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
    }


def render_markdown(report: dict, results: Sequence[CaseResult]) -> str:
    """A readable table: the aggregate first, then one line per case."""
    lines = [
        "# 诊断可重复测量",
        "",
        f"- 用例 {report['cases']}，可测 {report['measurable']}，不可测 {len(report['unmeasurable'])}",
        f"- 每次重复的一致率：{report['agreement_by_run']}（共 {report['repeats']} 次）",
        f"- 多数票一致率：{report['agreement_majority']}/{report['measurable']}",
        f"- 稳定行（重复间类别一致）：{report['stable_rows']}/{report['measurable']}",
        f"- 契约违规率：{report['contract_violation_rate']}（{report['contract_violations']} 次）",
        f"- 单次耗时 P50：{report['latency_p50_ms']}ms",
        "",
        "| case | 期望 | 各次结果 | 命中 | 稳定 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        outcomes = " / ".join(result.outcomes) if result.outcomes else NO_OBSERVATION
        lines.append(
            f"| {result.case.case_id} | {result.case.expected_category} | {outcomes} "
            f"| {result.matched}/{len(result.outcomes)} | {'是' if result.stable else '否'} |"
        )
    return "\n".join(lines)


def _newest_observation_id(store, question: str) -> int | None:
    """The newest observation row for one question, or ``None`` when there is none."""
    row = store._conn.execute(
        "SELECT id FROM failure_observations WHERE question = ? ORDER BY id DESC LIMIT 1",
        (question,),
    ).fetchone()
    return None if row is None else int(row["id"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repeatable diagnosis measurement.")
    parser.add_argument("--cases", required=True, help="local case file (company data: keep it out of the repo)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--label", default="", help="name this measurement, e.g. the prompt version")
    parser.add_argument("--qdrant-path", default="", help="snapshot of qdrant_data (avoids the live lock)")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--markdown-out", default=None)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")

    from config import settings

    if args.qdrant_path:
        # Set before the vector store client is built: the local Qdrant client
        # locks its directory, and the running service holds the live one.
        settings.qdrant_path = str(args.qdrant_path)

    from rag.evolution.diagnoser import Diagnoser, DiagnosisUnavailable
    from rag.evolution.evidence import assemble_evidence
    from rag.evolution.store import FailureTraceStore, _decode_row
    from rag.evolution.worker import AdminReadOnlyRetrievalReader
    from rag.tracker import ExecutionTracker

    cases = load_cases(args.cases)
    store = FailureTraceStore(settings.evolution_db_path)
    tracker = ExecutionTracker(settings.memory_db_path)
    reader = AdminReadOnlyRetrievalReader(settings.users_db_path)
    diagnoser = Diagnoser(
        timeout_seconds=float(settings.evolution_diagnosis_timeout_seconds),
        max_tokens=int(settings.evolution_diagnosis_max_tokens),
    )

    results: list[CaseResult] = []
    try:
        for case in cases:
            result = CaseResult(case=case)
            observation_id = _newest_observation_id(store, case.question)
            if observation_id is None:
                result.error = NO_OBSERVATION
                results.append(result)
                print(f"[skip] {case.case_id}: {NO_OBSERVATION}", flush=True)
                continue
            observation = store.get_observation(observation_id)
            cluster = _decode_row(
                store._conn.execute(
                    "SELECT * FROM failure_clusters WHERE id = ?",
                    (int(observation["cluster_id"]),),
                ).fetchone()
            )
            pack = assemble_evidence(
                observation,
                cluster,
                store=store,
                tracker=tracker,
                labels_path=None,
                retrieve=reader.retrieve,
            )
            result.reproduction_reproducible = bool(pack.reproduction.reproducible)
            result.coverage_terms = len(pack.question_term_coverage)
            result.coverage_hits = sum(
                1 for record in pack.question_term_coverage if int(record.get("chunks") or 0) > 0
            )
            for _ in range(args.repeats):
                started = time.perf_counter()
                try:
                    draft = diagnoser.draft(cluster, pack)
                except DiagnosisUnavailable as error:
                    result.outcomes.append(f"{error.reason}: {error.detail}")
                else:
                    result.outcomes.append(draft.failure_category)
                result.elapsed_ms.append((time.perf_counter() - started) * 1000.0)
            results.append(result)
            print(
                f"[{case.case_id}] expected={case.expected_category} outcomes={result.outcomes}",
                flush=True,
            )
    finally:
        store.close()
        tracker.close()
        reader.close()

    report = summarize(results, repeats=args.repeats)
    report["label"] = args.label
    report["prompt_version"] = diagnoser.prompt_version
    report["model"] = diagnoser.model_name
    report["cases_detail"] = [
        {
            "case_id": result.case.case_id,
            "expected": result.case.expected_category,
            "kb_id": result.case.kb_id,
            "outcomes": result.outcomes,
            "matched": result.matched,
            "stable": result.stable,
            "majority": result.majority(),
            "reproducible": result.reproduction_reproducible,
            "coverage_terms": result.coverage_terms,
            "coverage_hits": result.coverage_hits,
            "latency_ms": [round(value, 1) for value in result.elapsed_ms],
            "error": result.error,
        }
        for result in results
    ]
    markdown = render_markdown(report, results)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
