"""Layer-2 benchmark runner: the live Jev question set versus one candidate set.

Replays the fixed A/B corpus (``benchmarks/jev_retrieval_cases.json``, plus
``benchmarks/real-cases-v1.json`` when an operator has confirmed its labels)
through the in-process Jev preparation path of
``scripts/benchmark_jev_retrieval.py`` — the same cached pipelines, the same
seam counting — once with the live question set and once with the candidate
overlay applied. The verdict is not decided here: both trajectories are handed
to :func:`rag.evolution.candidate_gates.evaluate_on_benchmark` and its
``GateResult`` is reported as JSON/Markdown.

The candidate question set is **injected**, never implemented here:

* ``--provider module:attr`` names a callable ``(target, payload) -> builder``
  returning a ``build_preflight_questions()``-shaped builder (no arguments) or
  a ``build_passage_questions(chunk_ids)``-shaped one, with the payload text
  overlaid on the default set. Task 5's ``question_set_registry`` is the
  production provider; the recommended signature it exposes is

      build_candidate_questions(target, payload, *, base_builder=None) -> builder

  so the registry owns the overlay semantics (and the version) while this
  runner only supplies ``target`` and ``payload``.
* without a provider the runner overlays the payload text itself
  (``_smoke_overlay``) so a手工 candidate can be replayed before the registry
  lands. TODO(Task 5): delete the smoke overlay and require the provider.
* with no payload at all the candidate side is the live set verbatim — the
  "equivalent candidate" smoke that only exercises the plumbing.

The runner is read-only by default. ``--apply`` runs a gate round over every
candidate still in ``candidate_testing`` without a ``benchmark`` gate entry and
writes the outcomes back through
:func:`rag.evolution.candidate_gates.run_benchmark_gate`
(``awaiting_verification`` / ``rejected`` + ``gate_log`` + audit). That write-back
takes the comparison **three times by default** and only a stable verdict settles
a candidate (``--repeats``): measured 2026-10-13, the same candidate scored
``passed``, ``-10.0pp``, ``-15.0pp`` and ``route -2.3pp`` across four identical
runs, so a single run must not decide a terminal status. Cost is linear in
``--repeats`` — a round is minutes per candidate per repeat.

Usage:
    python scripts/benchmark_candidate_questions.py --candidate-id 12
    python scripts/benchmark_candidate_questions.py \\
        --target preflight --payload-json '{"intent": {"instructions": "..."}}'
    python scripts/benchmark_candidate_questions.py --apply --limit 5 \\
        --provider rag.jev.questions:candidate_question_builder

Secrets: the report carries case ids, routes, versions and counts only —
question text is redacted unless ``--include-questions`` is supplied, and this
path never produces chunk or answer text.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

DEFAULT_CASES = "benchmarks/jev_retrieval_cases.json"
DEFAULT_EXTRA_CASES = "benchmarks/real-cases-v1.json"

# ``build_preflight_questions()``-shaped or ``build_passage_questions(chunk_ids)``-shaped.
QuestionSetBuilder = Callable[..., dict]

# The Task 5 seam: (target, payload) -> builder with the payload already applied.
QuestionSetProvider = Callable[[str, Mapping[str, Mapping]], QuestionSetBuilder]

_OVERRIDE_KEYS = ("instructions", "criteria")

_EXIT_OK = 0
_EXIT_GATE_FAILED = 1
_EXIT_USAGE = 2


def _ensure_project_root_on_path() -> None:
    """Direct script execution must resolve the project's own packages.

    Mirrors ``scripts/benchmark_jev_retrieval.py``: with
    ``python scripts/benchmark_candidate_questions.py`` the script directory
    comes first on ``sys.path`` and ``config``/``rag`` would otherwise resolve
    to the editable-install copies in site-packages, whose relative data paths
    point outside the repository.
    """
    root = str(Path(__file__).resolve().parents[1])
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)


# ------------------------------------------------------------------
# Candidate question sets: the provider seam and the smoke overlay.
# ------------------------------------------------------------------


def _apply_overrides(questions: dict, payload: Mapping[str, Mapping]) -> dict:
    """Replace ``instructions``/``criteria`` text on a default question set.

    Structural identity is preserved by construction: ids, count and primitive
    type come from the default builder (``model_copy`` for question objects,
    a dict merge for the dict form the SDK also accepts), so only text changes.
    """
    overlaid = dict(questions)
    for question_id, entry in payload.items():
        if question_id not in overlaid:
            raise KeyError(f"candidate payload names an unknown question id: {question_id}")
        updates = {key: entry[key] for key in _OVERRIDE_KEYS if key in entry}
        if not updates:
            continue
        question = overlaid[question_id]
        if hasattr(question, "model_copy"):
            overlaid[question_id] = question.model_copy(update=updates)
        else:
            merged = dict(question)
            merged.update(updates)
            overlaid[question_id] = merged
    return overlaid


def _smoke_overlay(target: str, payload: Mapping[str, Mapping]) -> QuestionSetBuilder:
    """Overlay payload text on the default builders (pre-registry smoke only).

    TODO(Task 5): replace with the ``question_set_registry`` provider fed through
    ``--provider``; this local overlay exists so a手工 candidate can be replayed
    before the registry lands and must not become a second overlay semantics.
    """
    from rag.jev.questions import build_passage_questions, build_preflight_questions

    if target == "preflight":

        def builder() -> dict:
            return _apply_overrides(build_preflight_questions(), payload)

        return builder
    if target == "passages":

        def builder(chunk_ids: Sequence[str]) -> dict:
            return _apply_overrides(build_passage_questions(chunk_ids), payload)

        return builder
    raise ValueError(f"unknown candidate target: {target!r}")


def _load_question_set_provider(
    spec: str | None, target: str, payload: Mapping[str, Mapping]
) -> QuestionSetBuilder | None:
    """Resolve the candidate question-set builder; ``None`` = the live set itself.

    ``spec`` is ``module:attr`` naming a ``(target, payload) -> builder``
    callable (the Task 5 registry provider). Without a spec but with a payload,
    the local smoke overlay stands in; without a payload the candidate side is
    the live set verbatim and the run is an "equivalent candidate" smoke.
    """
    if spec:
        module_name, _, attribute = spec.partition(":")
        if not module_name or not attribute:
            raise ValueError(f"--provider must be 'module:attr', got {spec!r}")
        provider = getattr(importlib.import_module(module_name), attribute)
        return provider(target, payload)
    if payload:
        return _smoke_overlay(target, payload)
    return None


@contextmanager
def _patched_question_set(target: str, builder: QuestionSetBuilder):
    """Serve the candidate builder from ``rag.jev.client`` for the call duration.

    ``JevClient`` resolves both builders as module globals at call time, so the
    patch scopes exactly one candidate route call — the same in-process seam
    style the phase-2 benchmark uses for its counters.
    """
    import rag.jev.client as jev_client

    attribute = "build_preflight_questions" if target == "preflight" else "build_passage_questions"
    with patch.object(jev_client, attribute, builder):
        yield


# ------------------------------------------------------------------
# Corpus, candidate resolution, and the route callables.
# ------------------------------------------------------------------


def _load_cases(args: argparse.Namespace) -> list:
    """Load the fixed corpus, appending the confirmed real-case set when present."""
    from scripts.benchmark_jev_retrieval import load_cases

    paths = [args.cases]
    if not args.skip_extra_cases:
        if Path(args.extra_cases).exists():
            paths.append(args.extra_cases)
        else:
            print(
                f"note: {args.extra_cases} not found; using {args.cases} only",
                file=sys.stderr,
            )
    cases: list = []
    seen: set[str] = set()
    for path in paths:
        for case in load_cases(path):
            if case.id in seen:
                raise ValueError(f"duplicate benchmark case id across corpus files: {case.id}")
            seen.add(case.id)
            cases.append(case)
    return cases


def _check_payload_shape(payload: object) -> dict[str, dict]:
    """Light shape check for an inline payload; layer 1 owns the full boundary."""
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("--payload-json must be a non-empty mapping of question id -> override")
    for question_id, entry in payload.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"payload entry {question_id} must be a mapping")
        unsupported = sorted(set(entry) - set(_OVERRIDE_KEYS))
        if unsupported:
            raise ValueError(
                f"payload entry {question_id} may only override {list(_OVERRIDE_KEYS)}, got {unsupported}"
            )
        for key in _OVERRIDE_KEYS:
            if key in entry and not isinstance(entry[key], str):
                raise ValueError(f"payload entry {question_id}: {key} must be a string")
    return {str(question_id): dict(entry) for question_id, entry in payload.items()}


def _resolve_candidate_spec(args: argparse.Namespace) -> SimpleNamespace:
    """Resolve the one candidate to replay: a stored row or an inline payload.

    Returns a namespace exposing ``candidate`` (for the gate's identity stats),
    ``target`` and ``payload``.
    """
    if args.candidate_id is not None:
        from rag.evolution.candidate_gates import candidate_from_row
        from rag.evolution.candidate_store import CandidateStore

        store = CandidateStore(db_path=args.db)
        row = store.get_candidate(args.candidate_id)
        if row is None:
            raise KeyError(f"candidate not found: {args.candidate_id}")
        return SimpleNamespace(
            candidate=candidate_from_row(row),
            target=row["target"],
            payload=row["payload"] or {},
            source=f"store:{args.candidate_id}",
        )

    if not args.target or not args.payload_json:
        raise ValueError("pass --candidate-id, or --target together with --payload-json")
    payload = _check_payload_shape(json.loads(args.payload_json))
    return SimpleNamespace(
        candidate=SimpleNamespace(
            artifact_type="jev_question_set",
            target=args.target,
            payload=payload,
            rollback_ref={},
            confidence="",
        ),
        target=args.target,
        payload=payload,
        source="inline",
    )


def _cases_by_question(cases: Sequence) -> dict[str, Any]:
    """Index the corpus by question text; duplicates would misroute the gate."""
    index: dict[str, Any] = {}
    for case in cases:
        if case.question in index:
            raise ValueError(f"duplicate benchmark question for {case.id}; questions must be unique")
        index[case.question] = case
    return index


#: A knowledge-base gate needs a principal, and this is not a nicety: without one
#: the pipeline answers every request in its enterprise guard *before* retrieval.
#: Measured on the committed corpus: ``user_id=None`` gives ``route=clarification``,
#: ``sources=[]``, ``external_calls=0`` and ~0.05ms for every case, so both sides
#: compare identical empty runs and the gate can only ever report "no regression".
#: (The phase-4 e2e result "route +0.0pp, Top-5 +0.0pp, 20 cases" was such a run.)
MISSING_PRINCIPAL_HINT = (
    "the layer-2 gate needs a principal (--user-id): without one every request is "
    "answered before retrieval, and the two sides would compare identical empty runs"
)


def build_gate_callables(
    cases: Sequence,
    *,
    kb_id: str,
    memory_db_path: str,
    user_id: int | None,
    user_db_path: str | None,
    provider_spec: str | None,
) -> tuple[Callable[[str, str], Mapping[str, Any]], Callable[[str, Mapping[str, Mapping]], Callable]]:
    """Build ``(baseline_route, candidate_route_for)`` for the gate.

    Both sides run the Jev path of one cached pipeline per knowledge base — the
    legacy preparation path of the phase-2 benchmark is **not** the baseline
    here: layer 2 compares the live question set against the candidate set, so
    both trajectories go through the same ``_prepare_context``. The candidate
    side additionally serves its builder from ``rag.jev.client`` for the
    duration of each call, which is why it is a factory over ``(target, payload)``.
    """
    if user_id is None:
        raise ValueError(MISSING_PRINCIPAL_HINT)
    _ensure_project_root_on_path()
    from scripts.benchmark_jev_retrieval import build_live_preparations

    _, jev_prepare = build_live_preparations(
        kb_id=kb_id,
        memory_db_path=memory_db_path,
        user_id=user_id,
        user_db_path=user_db_path,
    )
    index = _cases_by_question(cases)

    def route(question: str, kb_id_: str) -> Mapping[str, Any]:
        outcome = jev_prepare(index[question])
        answerability = outcome.answerability
        if answerability is None and outcome.route == "structured":
            # 结构化澄清是"追问"：没有 decision 可读，但设计上就没有给出答案。
            # 交给门禁一个明确的未作答，而不是让它把读不到的形态当成"答了"。
            answerability = "clarification_required"
        return {
            "route": outcome.route,
            "sources": list(outcome.source_names),
            "external_calls": outcome.external_calls,
            "duration_ms": outcome.preparation_ms,
            # 层 2 的拒答护栏要读它：候选是否把基线答得出来的问题变成了拒答。
            # 这里传的是判定层自己的结论（``decision.kind``），不是从 route 猜的。
            "answerability": answerability,
        }

    def baseline_route(question: str, kb_id_: str) -> Mapping[str, Any]:
        return route(question, kb_id_)

    def candidate_route_for(target: str, payload: Mapping[str, Mapping]) -> Callable[[str, str], Mapping[str, Any]]:
        builder = _load_question_set_provider(provider_spec, target, payload)
        if builder is None:
            return route  # identical-set smoke: no overlay to apply

        def candidate_route(question: str, kb_id_: str) -> Mapping[str, Any]:
            with _patched_question_set(target, builder):
                return route(question, kb_id_)

        return candidate_route

    return baseline_route, candidate_route_for


# ------------------------------------------------------------------
# Reporting.
# ------------------------------------------------------------------


def _escape_markdown(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _result_block(
    result,
    *,
    questions: Mapping[str, str],
    include_questions: bool,
    candidate_source: str,
) -> dict:
    """One gate result as a report block; question text only when asked for."""
    per_case = []
    for entry in result.per_case:
        block = dict(entry)
        if include_questions:
            block["question"] = questions.get(entry["case_id"], "")
        per_case.append(block)
    return {
        "gate": result.gate,
        "passed": result.passed,
        "verdict": result.verdict,
        "stats": result.stats,
        "per_case": per_case,
        "candidate_source": candidate_source,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report; question text appears only when it was included."""
    lines = [
        "# Candidate Question-Set Benchmark Gate",
        "",
        f"- gate: {report['gate']}",
        f"- cases: {report['cases']}",
        f"- candidate question set: {report['candidate_question_set']}",
        f"- applied to the store: {'yes' if report['applied'] else 'no'}",
        f"- thresholds: route drop <= {report['thresholds']['route_drop_max_pp']}pp, "
        f"top-5 drop <= {report['thresholds']['top5_drop_max_pp']}pp",
        "",
    ]
    for index, block in enumerate(report["results"], start=1):
        stats = block["stats"]
        lines.extend(
            [
                f"## Result {index} ({block['candidate_source']}) — "
                f"{'PASS' if block['passed'] else 'FAIL'}",
                "",
                f"- verdict: {block['verdict']}",
                f"- route: baseline {stats['baseline']['route_hits']}/{stats['cases']} "
                f"({stats['baseline']['route_accuracy']:.3f}) -> "
                f"candidate {stats['candidate']['route_hits']}/{stats['cases']} "
                f"({stats['candidate']['route_accuracy']:.3f}), delta "
                f"{stats['route_delta_pp']:+.1f}pp",
                f"- top-5: baseline {stats['baseline']['top5_hits']}/{stats['baseline']['top5_labeled']} "
                f"({stats['baseline']['top5_hit_rate']:.3f}) -> "
                f"candidate {stats['candidate']['top5_hits']}/{stats['candidate']['top5_labeled']} "
                f"({stats['candidate']['top5_hit_rate']:.3f}), delta "
                f"{stats['top5_delta_pp']:+.1f}pp",
                f"- regressions: route {len(stats['route_regressions'])}, "
                f"top-5 {len(stats['top5_regressions'])}, "
                f"improvements {len(stats['improvements'])}",
                "",
                "| case | expected | baseline route | candidate route | baseline top-5 | candidate top-5 | changed |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in block["per_case"]:
            lines.append(
                f"| {_escape_markdown(entry['case_id'])} "
                f"| {_escape_markdown(entry['expected_route'])} "
                f"| {_escape_markdown(entry['baseline']['route'])}"
                f"{'' if entry['baseline']['route_correct'] else ' (miss)'} "
                f"| {_escape_markdown(entry['candidate']['route'])}"
                f"{'' if entry['candidate']['route_correct'] else ' (miss)'} "
                f"| {entry['baseline']['top5_hit']} | {entry['candidate']['top5_hit']} "
                f"| {'yes' if entry['route_changed'] else 'no'} |"
            )
        lines.append("")
    return "\n".join(lines)


def _write_outputs(report: Mapping[str, Any], args: argparse.Namespace) -> None:
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(render_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(report), encoding="utf-8")


# ------------------------------------------------------------------
# Entry points.
# ------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the live Jev question set against a candidate question set."
    )
    parser.add_argument("--cases", default=DEFAULT_CASES, help="fixed A/B case set")
    parser.add_argument(
        "--extra-cases",
        default=DEFAULT_EXTRA_CASES,
        help="confirmed real-case set appended when it exists (draft files are ignored)",
    )
    parser.add_argument(
        "--skip-extra-cases", action="store_true", help="run --cases only, even if the extra set exists"
    )
    parser.add_argument("--candidate-id", type=int, default=None, help="replay one stored candidate")
    parser.add_argument("--db", default=None, help="candidate store database (default: settings.evolution_db_path)")
    parser.add_argument("--target", default=None, choices=["preflight", "passages"], help="inline candidate target")
    parser.add_argument("--payload-json", default=None, help="inline candidate payload as JSON")
    parser.add_argument(
        "--provider",
        default=None,
        help="Task 5 seam: 'module:attr' naming (target, payload) -> question-set builder",
    )
    parser.add_argument("--kb-id", default="rag_docs")
    parser.add_argument("--memory-db-path", default=":memory:")
    parser.add_argument("--user-id", type=int, default=None, help="run the enterprise path with this principal")
    parser.add_argument("--user-db", default=None, help="user database path for --user-id")
    parser.add_argument("--apply", action="store_true", help="write gate outcomes back for every pending candidate")
    parser.add_argument("--limit", type=int, default=5, help="pending candidates per --apply round (default: 5)")
    parser.add_argument("--actor-id", default="evolver/benchmark-gate", help="audit actor for --apply")
    parser.add_argument(
        "--repeats",
        type=int,
        default=None,
        help=(
            "how many times to take the same comparison; only a stable verdict settles a candidate "
            "(default: 3 with --apply, 1 for a read-only replay)"
        ),
    )
    parser.add_argument(
        "--max-total-runs",
        type=int,
        default=25,
        help="safety ceiling on candidate_limit * repeats for --apply rounds (default: 25)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="override the safety ceiling on total replay runs",
    )
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--markdown-out", default=None)
    parser.add_argument(
        "--include-questions", action="store_true", help="include question text in the report"
    )
    args = parser.parse_args(argv)
    if args.repeats is None:
        args.repeats = 3 if args.apply else 1
    if args.repeats < 1 or (args.repeats > 1 and args.repeats % 2 == 0):
        # 多数票需要一个裁决者：偶数次可以平票，而"平票"在这条路径上必须是一种**未决**，
        # 与其发明平票语义，不如要求奇数。
        parser.error("--repeats must be 1 or an odd number >= 3")
    if args.apply:
        if args.candidate_id is not None or args.payload_json is not None:
            parser.error("--apply runs the pending round; drop --candidate-id/--payload-json")
        total_planned_runs = args.limit * args.repeats
        if not args.force and total_planned_runs > args.max_total_runs:
            parser.error(
                f"--apply total planned runs ({args.limit} candidates * {args.repeats} repeats = "
                f"{total_planned_runs}) exceeds safety limit ({args.max_total_runs}). "
                "Reduce --limit/--repeats, raise --max-total-runs, or pass --force."
            )
    elif (args.candidate_id is not None) == (args.payload_json is not None):
        parser.error("pass exactly one of --candidate-id or --payload-json (with --target)")
    elif args.payload_json is not None and not args.target:
        parser.error("--payload-json requires --target")
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.user_id is None:
        # Refused here as well as in build_gate_callables, so an operator gets one
        # clear line instead of a traceback from deep inside the gate.
        parser.error(MISSING_PRINCIPAL_HINT)
    return args


def _run_single(args: argparse.Namespace, cases: Sequence) -> int:
    from rag.evolution.candidate_gates import GATE_NAME, evaluate_repeatable_on_benchmark

    spec = _resolve_candidate_spec(args)
    baseline_route, candidate_route_for = build_gate_callables(
        cases,
        kb_id=args.kb_id,
        memory_db_path=args.memory_db_path,
        user_id=args.user_id,
        user_db_path=args.user_db,
        provider_spec=args.provider,
    )
    result = evaluate_repeatable_on_benchmark(
        spec.candidate,
        baseline_route_fn=baseline_route,
        candidate_route_fn=candidate_route_for(spec.target, spec.payload),
        cases=cases,
        repeats=args.repeats,
    )
    overlay = "provider overlay" if args.provider else ("smoke overlay" if spec.payload else "identical (no overlay)")
    report = {
        "gate": GATE_NAME,
        "cases": len(cases),
        "thresholds": result.stats["thresholds"],
        "applied": False,
        "repeats": int(args.repeats),
        "candidate_question_set": overlay,
        "results": [
            _result_block(
                result,
                questions={case.id: case.question for case in cases},
                include_questions=args.include_questions,
                candidate_source=spec.source,
            )
        ],
    }
    _write_outputs(report, args)
    return _EXIT_OK if result.passed else _EXIT_GATE_FAILED


def _run_applied(args: argparse.Namespace, cases: Sequence) -> int:
    from rag.evolution.candidate_gates import (
        GATE_NAME,
        ROUTE_DELTA_MAX_PP,
        TOP5_DELTA_MAX_PP,
        run_benchmark_gate,
    )
    from rag.evolution.candidate_store import CandidateStore

    store = CandidateStore(db_path=args.db)
    baseline_route, candidate_route_for = build_gate_callables(
        cases,
        kb_id=args.kb_id,
        memory_db_path=args.memory_db_path,
        user_id=args.user_id,
        user_db_path=args.user_db,
        provider_spec=args.provider,
    )
    results = run_benchmark_gate(
        store,
        actor_id=args.actor_id,
        baseline_route_fn=baseline_route,
        candidate_route_fn_factory=lambda row: candidate_route_for(row["target"], row["payload"] or {}),
        cases=cases,
        limit=args.limit,
        repeats=args.repeats,
    )
    overlay = "provider overlay" if args.provider else "smoke overlay (payload) / live set (no payload)"
    report = {
        "gate": GATE_NAME,
        "cases": len(cases),
        "thresholds": {
            "route_drop_max_pp": ROUTE_DELTA_MAX_PP,
            "top5_drop_max_pp": TOP5_DELTA_MAX_PP,
        },
        "applied": True,
        "repeats": int(args.repeats),
        "candidate_question_set": overlay,
        "results": [
            _result_block(
                result,
                questions={case.id: case.question for case in cases},
                include_questions=args.include_questions,
                candidate_source="store:pending-round",
            )
            for result in results
        ],
    }
    _write_outputs(report, args)
    return _EXIT_OK if all(result.passed for result in results) else _EXIT_GATE_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_project_root_on_path()
    args = _parse_args(argv)
    try:
        cases = _load_cases(args)
        return _run_applied(args, cases) if args.apply else _run_single(args, cases)
    except (ValueError, KeyError, FileNotFoundError) as error:
        print(f"benchmark_candidate_questions: {type(error).__name__}: {error}", file=sys.stderr)
        return _EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
