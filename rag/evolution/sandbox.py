"""Task 3: the isolated worktree sandbox for one code-patch candidate.

A code patch is the only artifact in this system that can change how the
pipeline behaves, so it is the only one that must never be validated in place.
This module owns that isolation and nothing else:

1. the layer-1 boundary (:func:`~rag.evolution.code_patches.validate_patch`) is
   re-run here, because a candidate may have travelled through a store since it
   was generated;
2. the **main working tree must be clean** (``git status --porcelain``), because
   a sandbox that cannot tell its own state apart from someone else's cannot
   claim any of its numbers;
3. ``git worktree add <tmp> --detach HEAD`` creates a *second* working tree
   inside a ``tempfile.mkdtemp`` sandbox root. The repository the service is
   serving is only ever read from here: no ``git apply``, no ``git checkout``
   and no branch switch ever runs against it;
4. ``git apply`` puts the diff into that worktree (stdin, UTF-8, bounded);
5. ``python -m pytest`` runs the candidate's own verification targets **plus the
   fixed regression net** (:data:`REGRESSION_TEST_PATHS`) with ``cwd`` and
   ``PYTHONPATH`` pointing at the worktree, ``PYTHONHASHSEED=0`` and an
   environment stripped of every ``RAG_*`` key, so the host ``.env`` cannot reach
   the target tests — and ``import rag`` resolves to the patched copy rather than
   the installed one;
6. one benchmark replay of the phase-2 corpus
   (``scripts/benchmark_jev_retrieval.py``) in the same worktree, decided with
   the layer-2 thresholds: route accuracy may not drop at all, Top-5 source hit
   may drop by at most two percentage points;
7. ``git worktree remove --force`` + ``git worktree prune`` in a ``finally``, so
   a step that dies in the middle still leaves no worktree behind.

Every child process is bounded by a timeout, and every failure is reported as a
short category (``does_not_apply``, ``main tree dirty``, ``timeout_after_600s``)
rather than as diff or log content, so a refused candidate cannot leak file text
into a gate note or an API error.

A replay that cannot run — no credentials, no vector store, a crashed runner —
is recorded as an error with ``benchmark=None`` instead of as a rejection: a
missing environment is not evidence *about* a candidate. The same rule skips the
replay entirely once the target tests have already failed, because a failing
patch does not need five more minutes of measurement to be refused.

A replay that *ran* but shows no work is no evidence either, and
:func:`benchmark_verdict` refuses it. A preparation that never happened — a
knowledge-base request answered before retrieval, in microseconds, with no
recorded failure — leaves both sides reporting identical metrics, and identical
metrics would otherwise read as the one conclusion a gate must never invent:
"no regression".

The module stops at the report. Turning one into a candidate-status move is the
caller's job, and :func:`sandbox_gate_outcome` is the seam for it: it returns the
exact ``(status, gate_detail)`` pair to write back with ``gate="sandbox"``, which
is the gate name the review endpoints and the frontend summary read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from config import settings
from rag.evolution.candidate_gates import ROUTE_DELTA_MAX_PP, TOP5_DELTA_MAX_PP
from rag.evolution.candidates import PayloadBoundaryError
from rag.evolution.code_patches import (
    GIT_APPLY_TIMEOUT_S,
    CodePatchCandidate,
    validate_patch,
)
from rag.evolution.models import EvolutionStatus

#: Prefix of the sandbox temp directory; the worktree always lives inside it.
SANDBOX_ROOT_PREFIX = "rag-patch-sandbox-"
#: The worktree directory name inside the sandbox root.
WORKTREE_DIRNAME = "worktree"
#: The second, **unpatched** worktree the regression replay needs: the patch's effect is the
#: difference between the two runs, so both must exist at the same HEAD.
BASELINE_WORKTREE_DIRNAME = "baseline"
#: Where the replay writes its report: next to the worktree, never inside it,
#: so the tree the tests ran in stays exactly the patched tree.
BENCHMARK_REPORT_NAME = "benchmark.json"
#: The clean run's report, kept apart from the patched one.
BASELINE_REPORT_NAME = "benchmark-baseline.json"
#: The phase-2 A/B runner, addressed as a *relative* path so the worktree's own
#: copy of ``scripts/`` is what the replay measures.
BENCHMARK_SCRIPT = "scripts/benchmark_jev_retrieval.py"
#: The runner's two sides: ``old`` is the legacy preparation path and ``new`` is the Jev
#: path. They are *not* baseline and patched — the patch's effect is the same side measured
#: in the clean worktree and in the patched one.
REPLAY_SIDES = (("old", "legacy"), ("new", "jev"))

#: Repository-owned verification the sandbox always runs beside the candidate's
#: own targets. Layer 1 refuses any diff that touches ``tests/**`` or a
#: ``conftest.py``, so this set cannot be trimmed, deselected or rewritten by the
#: candidate it judges: it is a fixed baseline over the shared surfaces a patch
#: can reach — Jev policy and its registry seam, pipeline assembly, retrieval,
#: reranking, chunking, structured queries, tools — and it needs no credential
#: and no network, which is exactly what a sandboxed child process has.
#:
#: It carries the weight the end-to-end replay cannot: the replay's preparation
#: path calls a routing model, a rewriter, an embedder and a reranker over the
#: network, and the sandbox never hands a child the credentials for any of them.
REGRESSION_TEST_PATHS: tuple[str, ...] = (
    "tests/test_jev_client.py",
    "tests/test_jev_passage_policy.py",
    "tests/test_jev_routing_policy.py",
    "tests/test_jev_question_registry.py",
    "tests/test_jev_questions.py",
    "tests/test_jev_shadow.py",
    "tests/test_pipeline_jev.py",
    "tests/test_pipeline.py",
    "tests/test_pipeline_clean.py",
    "tests/test_pipeline_cache_security.py",
    "tests/test_retriever.py",
    "tests/test_retriever_bm25_persistence.py",
    "tests/test_retriever_default_collection_filter.py",
    "tests/test_retriever_embed_fallback.py",
    "tests/test_reranker.py",
    "tests/test_agent.py",
    "tests/test_chunker.py",
    "tests/test_chunk_metadata.py",
    "tests/test_rrf_dedup.py",
    "tests/test_structured_query.py",
    "tests/test_tools.py",
    "tests/test_tools_overflow.py",
    "tests/test_loader.py",
    "tests/test_embedder.py",
)

#: ``git worktree add`` materializes a whole tree: bounded, not open-ended.
WORKTREE_TIMEOUT_S = 120.0
#: The replay walks the committed corpus through both preparation paths.
BENCHMARK_TIMEOUT_S = 900.0
#: ``git status`` on a large repository is still a local, read-only operation.
GIT_STATUS_TIMEOUT_S = 120.0

#: Percentage-point deltas are rounded before comparison, so a discrete corpus
#: decides on the value rather than on float noise (same rule as layer 2).
PP_PRECISION = 4
#: A verdict names at most this many case ids or test names.
MAX_LISTED = 5
#: How many failing test names a report keeps; the count still names the rest.
MAX_RECORDED_TESTS = 50

#: A replay is evidence about a candidate only when it shows the preparation path
#: actually ran. This floor is execution evidence, not an acceptance threshold:
#: the layer-2 thresholds are never moved, and a run below this floor is reported
#: as no evidence at all rather than as a pass. A real preparation on the phase-2
#: corpus takes seconds; a request answered before retrieval takes microseconds.
MIN_PREP_P50_MS = 1.0

#: Environment keys that must never reach a sandbox child process. ``RAG_*`` is
#: the host ``.env`` (``config.Settings`` reads ``env_prefix="RAG_"``); the rest
#: would change what the target tests or the replay measure.
ENV_STRIPPED_PREFIXES = ("RAG_",)
ENV_STRIPPED_KEYS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONBREAKPOINT",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_CURRENT_TEST",
)

#: pytest exit codes a sandbox can read a verdict out of. Anything else means
#: the target tests never ran, which is "inconclusive", not "failed".
_PYTEST_EXIT_MEANINGS = {
    2: "interrupted",
    3: "internal_error",
    4: "usage_error",
    5: "no_tests_collected",
}

#: First stderr line → one short category. Only the first line is read, so the
#: reason a command failed never carries the patch or the log content.
_GIT_ERROR_CATEGORIES = (
    ("patch failed", "does_not_apply"),
    ("does not apply", "does_not_apply"),
    ("already exists", "already_exists"),
    ("no such file", "missing_target"),
    ("no valid patches", "no_valid_patch"),
    ("corrupt patch", "corrupt_patch"),
    ("unrecognized input", "unrecognized_input"),
)

#: The git invocation that applies a candidate diff, on stdin, UTF-8, bounded.
_GIT_APPLY_COMMAND = ("git", "apply", "--whitespace=nowarn", "-")


@dataclass(frozen=True)
class SandboxReport:
    """What one sandbox run concluded about one candidate patch.

    ``applied`` says the diff landed in the worktree; ``tests_passed`` is
    ``None`` when they never ran (a refusal, a timeout, a collection error), so
    "did not run" never reads as "passed"; ``failed_tests`` names the failing
    pytest ids; ``benchmark`` is the replay comparison against the layer-2
    thresholds when the replay could run; ``error`` is a short category, never
    diff or log content.
    """

    applied: bool
    tests_passed: bool | None
    failed_tests: tuple[str, ...] = ()
    benchmark: dict | None = None
    duration_s: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        """The JSON-safe form a gate log, an audit note or an API stores."""
        return {
            "applied": self.applied,
            "tests_passed": self.tests_passed,
            "failed_tests": list(self.failed_tests),
            "benchmark": self.benchmark,
            "duration_s": self.duration_s,
            "error": self.error,
        }


@dataclass(frozen=True)
class CommandOutcome:
    """One child process, bounded: its codes, its captured text, why it stopped.

    ``timed_out`` and ``unavailable`` are separate from ``returncode`` so a
    caller can tell a command that failed from one that never finished.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    unavailable: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.unavailable and self.returncode == 0


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
    stdin_text: str | None = None,
) -> CommandOutcome:
    """Run one child process, capturing text as UTF-8 and never blocking forever.

    ``encoding`` is explicit: a diff with non-ASCII context decoded through a
    locale-guided default codec is corrupted on a Chinese Windows host, which
    would make a legal patch look like one that does not apply.
    """
    try:
        completed = subprocess.run(
            [str(word) for word in command],
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        return CommandOutcome(
            returncode=-1,
            stdout=_as_text(expired.stdout),
            stderr=_as_text(expired.stderr),
            timed_out=True,
        )
    except OSError:
        return CommandOutcome(returncode=-2, unavailable=True)
    return CommandOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _sandbox_env(worktree: Path) -> dict[str, str]:
    """The environment every sandbox child process runs under.

    The host environment is inherited — an interpreter, a ``PATH``, a temp
    directory — and then stripped of everything that would let the repository
    this service runs from reach into the sandbox: every ``RAG_*`` setting (that
    is the host ``.env``) and the python/pytest knobs that would change what the
    target tests or the replay measure. ``PYTHONPATH`` then points at the
    worktree so ``import rag`` and ``import config`` resolve to the patched
    copy, and UTF-8 mode keeps the child's own text on the same code page as
    the parent's.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ENV_STRIPPED_KEYS and not key.startswith(ENV_STRIPPED_PREFIXES)
    }
    env["PYTHONPATH"] = str(worktree)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _replay_env(worktree: Path, data_snapshot: str | None) -> dict[str, str]:
    """The environment for the benchmark replay alone.

    Identical to :func:`_sandbox_env` except for the read-only copies a replay needs: every
    ``RAG_*`` setting is stripped to keep the sandbox isolated, which also leaves the replay
    with no corpus and no principal — and a replay without a principal answers every request
    before retrieval, so its preparation metrics are all zero and its verdict means nothing.
    Whatever exists inside ``data_snapshot`` is therefore handed to the replay child and to
    nothing else: the target tests keep the stripped environment.

    Recognised entries: ``qdrant_data/`` (or the directory itself when it is a corpus) becomes
    ``RAG_QDRANT_PATH``; ``users.db`` and ``memory.db`` become the matching settings.
    """

    env = _sandbox_env(worktree)
    snapshot = str(data_snapshot or "").strip()
    if not snapshot or not Path(snapshot).is_dir():
        return env
    root = Path(snapshot)
    corpus = root / "qdrant_data"
    env["RAG_QDRANT_PATH"] = str(corpus if corpus.is_dir() else root)
    for filename, key in (("users.db", "RAG_USERS_DB_PATH"), ("memory.db", "RAG_MEMORY_DB_PATH")):
        candidate = root / filename
        if candidate.is_file():
            env[key] = str(candidate)
    return env


def _is_within(path: Path, parent: Path) -> bool:
    """Whether ``path`` is inside ``parent`` once both are fully resolved."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _git_category(stderr: str) -> str:
    """A short category from the first stderr line; never the command's text."""
    lines = (stderr or "").splitlines()
    first = lines[0].strip().lower() if lines else ""
    for needle, category in _GIT_ERROR_CATEGORIES:
        if needle in first:
            return category
    return "git_error"


def _failure_kind(outcome: CommandOutcome, timeout: float) -> str:
    """Why a bounded child process did not succeed, as one short phrase."""
    if outcome.timed_out:
        return f"timeout_after_{int(timeout)}s"
    if outcome.unavailable:
        return "git_unavailable"
    if outcome.returncode != 0:
        return _git_category(outcome.stderr)
    return "unknown"


# ------------------------------------------------------------------
# The target tests.
# ------------------------------------------------------------------


def _pytest_command(test_paths: Sequence[str]) -> list[str]:
    """``python -m pytest`` over the candidate's own verification targets."""
    return [
        sys.executable,
        "-m",
        "pytest",
        *test_paths,
        "-q",
        "--no-header",
        "--color=no",
        "-p",
        "no:cacheprovider",
    ]


def _failed_test_names(stdout: str) -> tuple[str, ...]:
    """The failing pytest ids in a ``-q`` short summary, in report order."""
    names: list[str] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        for prefix in ("FAILED ", "ERROR "):
            if not stripped.startswith(prefix):
                continue
            name = stripped[len(prefix) :].split(" - ", 1)[0].strip()
            if name and name not in names:
                names.append(name)
            break
    return tuple(names[:MAX_RECORDED_TESTS])


def _listed(names: Sequence[str]) -> str:
    """Names for an error line, capped so a big failure stays readable."""
    shown = ", ".join(names[:MAX_LISTED])
    hidden = len(names) - MAX_LISTED
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown


def _existing_regression_paths(worktree: Path) -> list[str]:
    """The regression net's paths that this worktree actually carries.

    The real repository carries all of them, and its own test pins that. A
    worktree that legitimately does not (the sandbox's own synthetic probe
    fixtures) runs the candidate's targets alone rather than failing to collect:
    a missing baseline file is a property of the repository, not of the patch.
    """
    return [path for path in REGRESSION_TEST_PATHS if (worktree / path).is_file()]


def _run_target_tests(
    candidate: CodePatchCandidate, *, worktree: Path, env: Mapping[str, str], timeout: float
) -> tuple[bool | None, tuple[str, ...], str]:
    """Run the candidate's verification tests plus the fixed regression net.

    Returns ``(tests_passed, failed_tests, error)``. A timeout or a pytest that
    never ran is ``tests_passed=None`` — inconclusive, which the plan records as
    ``needs_more_evidence`` rather than as a rejection.

    A candidate that names no test path is still inconclusive rather than
    verified: the fixed net runs either way, but "we ran our own baseline" is not
    the "the candidate's change is verified by the tests it claimed" the plan
    asks for.
    """
    if not candidate.test_paths:
        return None, (), "target tests skipped: the candidate names no test path"
    outcome = _run_command(
        _pytest_command([*candidate.test_paths, *_existing_regression_paths(worktree)]),
        cwd=worktree,
        env=env,
        timeout=timeout,
    )
    if outcome.timed_out:
        return None, (), f"target tests timed out after {int(timeout)}s"
    if outcome.unavailable:
        return None, (), "target tests unavailable: the interpreter could not be started"
    failed = _failed_test_names(outcome.stdout)
    if failed:
        return False, failed, f"target tests failed: {_listed(failed)}"
    if outcome.returncode == 0:
        return True, (), ""
    return None, (), (
        f"target tests did not run: "
        f"{_PYTEST_EXIT_MEANINGS.get(outcome.returncode, f'exit_{outcome.returncode}')}"
    )


# ------------------------------------------------------------------
# The benchmark replay.
# ------------------------------------------------------------------


def _benchmark_command(*, kb_id: str, json_out: Path, user_id: int | None = None) -> list[str]:
    """One replay of the committed corpus, as a relative script path.

    ``user_id`` makes the replay run the authenticated path. Without a principal every
    request is answered before retrieval, so the preparation metrics come back as zeros and
    the verdict says nothing — the runner rejects that shape on its own, but only after
    spending the run, so the principal is passed whenever one is configured.
    """
    command = [
        sys.executable,
        BENCHMARK_SCRIPT,
        "--kb-id",
        str(kb_id),
        "--json-out",
        str(json_out),
    ]
    if user_id:
        command.extend(["--user-id", str(int(user_id))])
    return command


def _number(value: object) -> float | None:
    """A JSON number, or ``None`` — a missing metric is not a zero metric."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _count(value: object) -> int:
    number = _number(value)
    return int(number) if number is not None else 0


def _pp(delta: float) -> float:
    """A rate delta in percentage points, rounded and free of negative zero."""
    rounded = round(delta * 100.0, PP_PRECISION)
    return 0.0 if rounded == 0 else rounded


def _replay_cases(payload: Mapping, *, regressed: bool) -> list[str]:
    """Case ids where the two sides disagree, from the runner's own breakdown."""
    case_ids: list[str] = []
    breakdowns = payload.get("case_breakdowns")
    if not isinstance(breakdowns, Sequence) or isinstance(breakdowns, str | bytes):
        return case_ids
    for entry in breakdowns:
        if not isinstance(entry, Mapping):
            continue
        case_id = entry.get("case_id")
        if not isinstance(case_id, str):
            continue
        old = entry.get("old") if isinstance(entry.get("old"), Mapping) else {}
        new = entry.get("new") if isinstance(entry.get("new"), Mapping) else {}
        old_hits = _count(old.get("route_correct_count"))
        new_hits = _count(new.get("route_correct_count"))
        if regressed and old_hits > 0 and new_hits == 0:
            case_ids.append(case_id)
        if not regressed and new_hits > 0 and old_hits == 0:
            case_ids.append(case_id)
    return case_ids


def _execution_evidence_reason(payload: Mapping) -> str | None:
    """Why one replay report shows no work, or ``None`` when it shows some.

    The runner counts a preparation that merely *returned* as a successful
    sample. A knowledge-base request without a principal is answered by the
    enterprise guard before retrieval — in microseconds, without a recorded
    failure — so both sides report full-sample metrics, identical to each other,
    and the layer-2 comparison would call that "no regression".

    Only the *baseline* has to prove it measured the preparation path: at least
    one case, no failed call, and a preparation latency above
    :data:`MIN_PREP_P50_MS`. A candidate that fails calls or skips preparation is
    not discarded here — that is a regression the verdict has to report, and the
    route and failure comparisons are what catch it.
    """
    cases = _count(payload.get("cases"))
    if cases <= 0:
        return f"the replay measured no samples (cases={cases})"
    baseline = payload.get("old")
    if not isinstance(baseline, Mapping):
        return "the report carries no baseline metrics"
    failed = _count(baseline.get("failed_calls"))
    if failed > 0:
        return f"the baseline side failed {failed} call(s)"
    p50 = _number(baseline.get("prep_p50_ms"))
    if p50 is None:
        return "the report carries no preparation latency for the baseline side"
    if p50 < MIN_PREP_P50_MS:
        return (
            f"the preparation path never ran on the baseline side "
            f"(prep P50 {p50:.1f}ms < {MIN_PREP_P50_MS:.1f}ms)"
        )
    return None


def patch_verdict(baseline: Mapping, patched: Mapping) -> dict | None:
    """The layer-2 comparison of the **clean** replay against the **patched** one, per side.

    ``scripts/benchmark_jev_retrieval.py`` compares two implementation paths — its own
    ``old`` is the legacy preparation and ``new`` is the Jev one — so a single report says
    nothing about a patch. The patch's effect is the *same side* measured twice: once in a
    worktree at HEAD and once in the worktree the diff was applied to. Both sides are
    compared, because a code patch can move either path, and the never-weakened layer-2
    thresholds decide (route drop ≤ 0pp, Top-5 drop ≤ 2pp, no extra failed calls). A side
    that fails any of them fails the verdict.

    A report that shows no work at all is not a verdict but ``None``: two preparations that
    never happened compare as identical, and "no regression" is exactly the claim a report of
    nothing must not be able to make. See :func:`_execution_evidence_reason`.
    """
    for payload in (baseline, patched):
        if _execution_evidence_reason(payload) is not None:
            return None

    sides: dict[str, dict] = {}
    reasons: list[str] = []
    for key, label in REPLAY_SIDES:
        base_side = baseline.get(key)
        patched_side = patched.get(key)
        if not isinstance(base_side, Mapping) or not isinstance(patched_side, Mapping):
            return None
        base_route = _number(base_side.get("route_accuracy"))
        patched_route = _number(patched_side.get("route_accuracy"))
        base_top5 = _number(base_side.get("top5_hit_rate"))
        patched_top5 = _number(patched_side.get("top5_hit_rate"))
        if None in (base_route, patched_route, base_top5, patched_top5):
            return None
        route_delta_pp = _pp(patched_route - base_route)
        top5_delta_pp = _pp(patched_top5 - base_top5)
        base_calls = _count(base_side.get("failed_calls"))
        patched_calls = _count(patched_side.get("failed_calls"))
        failed_calls_delta = patched_calls - base_calls
        passed = (
            -route_delta_pp <= ROUTE_DELTA_MAX_PP
            and -top5_delta_pp <= TOP5_DELTA_MAX_PP
            and failed_calls_delta <= 0
        )
        sides[label] = {
            "baseline": {
                "route_accuracy": base_route,
                "top5_hit_rate": base_top5,
                "failed_calls": base_calls,
                "prep_p50_ms": _number(base_side.get("prep_p50_ms")),
            },
            "patched": {
                "route_accuracy": patched_route,
                "top5_hit_rate": patched_top5,
                "failed_calls": patched_calls,
                "prep_p50_ms": _number(patched_side.get("prep_p50_ms")),
            },
            "route_delta_pp": route_delta_pp,
            "top5_delta_pp": top5_delta_pp,
            "failed_calls_delta": failed_calls_delta,
            "passed": passed,
        }
        if not passed:
            # 每条原因都带上是哪一侧：补丁可能只影响 legacy 或只影响 Jev。
            if not (-route_delta_pp <= ROUTE_DELTA_MAX_PP):
                reasons.append(
                    f"{label} side: route accuracy dropped {-route_delta_pp:.1f}pp beyond "
                    f"{-ROUTE_DELTA_MAX_PP:.1f}pp"
                )
            if not (-top5_delta_pp <= TOP5_DELTA_MAX_PP):
                reasons.append(
                    f"{label} side: top-5 hit dropped {-top5_delta_pp:.1f}pp beyond "
                    f"{-TOP5_DELTA_MAX_PP:.1f}pp"
                )
            if failed_calls_delta > 0:
                reasons.append(
                    f"{label} side: {failed_calls_delta} more failed call(s) than the clean run"
                )

    cases = _count(baseline.get("cases"))
    production = sides["jev"]
    if reasons:
        verdict = "failed: " + "; ".join(reasons)
    else:
        verdict = (
            f"passed: Jev side route delta {production['route_delta_pp']:+.1f}pp "
            f"(limit {-ROUTE_DELTA_MAX_PP:+.1f}pp), top-5 delta "
            f"{production['top5_delta_pp']:+.1f}pp (limit {-TOP5_DELTA_MAX_PP:+.1f}pp) "
            f"over {cases} cases"
        )
    return {
        "cases": cases,
        "sides": sides,
        "route_delta_pp": production["route_delta_pp"],
        "top5_delta_pp": production["top5_delta_pp"],
        "thresholds": {
            "route_drop_max_pp": ROUTE_DELTA_MAX_PP,
            "top5_drop_max_pp": TOP5_DELTA_MAX_PP,
        },
        "passed": not reasons,
        "verdict": verdict,
    }


def _run_benchmark(
    *,
    worktree: Path,
    env: Mapping[str, str],
    sandbox_root: Path,
    kb_id: str,
    timeout: float,
    user_id: int | None = None,
    report_name: str = BENCHMARK_REPORT_NAME,
) -> tuple[dict | None, str]:
    """One replay, returning the runner's raw report and an error string.

    The report file decides, not the exit code: the runner exits non-zero when its own
    thresholds fail, which is a **verdict** (the layer-2 thresholds here may still pass), so
    the report is read whenever it exists and parses. Only a missing or unreadable report
    turns the exit code into an error.

    A replay that cannot run is reported as an error with no report, never as a rejection: a
    missing environment is not evidence about a candidate. A replay that ran without measuring
    anything is reported the same way, with the reason it cannot be read as a result.
    """
    json_out = sandbox_root / report_name
    outcome = _run_command(
        _benchmark_command(kb_id=kb_id, json_out=json_out, user_id=user_id),
        cwd=worktree,
        env=env,
        timeout=timeout,
    )
    if outcome.timed_out:
        return None, f"benchmark replay timed out after {int(timeout)}s"
    if outcome.unavailable:
        return None, "benchmark replay unavailable: the interpreter could not be started"
    if not json_out.is_file():
        if outcome.returncode != 0:
            return None, f"benchmark replay did not complete: exit_{outcome.returncode}"
        return None, "benchmark replay produced no report"
    try:
        payload = json.loads(json_out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "benchmark replay report is not valid json"
    if not isinstance(payload, Mapping):
        return None, "benchmark replay report is not a json object"
    no_evidence = _execution_evidence_reason(payload)
    if no_evidence is not None:
        return None, f"benchmark replay measured nothing: {no_evidence}"
    return dict(payload), ""


# ------------------------------------------------------------------
# The run.
# ------------------------------------------------------------------


def _execute(
    candidate: CodePatchCandidate,
    *,
    root: Path,
    sandbox_root: Path,
    worktree: Path,
    started: float,
    tests_timeout: float,
    run_benchmark: bool,
    kb_id: str,
    benchmark_timeout: float,
    data_snapshot: str | None = None,
    replay_user_id: int | None = None,
) -> SandboxReport:
    """One sandbox run; the caller still owns cleanup, whatever happens here."""

    def report(
        *,
        applied: bool,
        tests_passed: bool | None,
        failed_tests: Sequence[str] = (),
        benchmark: dict | None = None,
        error: str = "",
    ) -> SandboxReport:
        return SandboxReport(
            applied=applied,
            tests_passed=tests_passed,
            failed_tests=tuple(failed_tests),
            benchmark=benchmark,
            duration_s=round(time.perf_counter() - started, 3),
            error=error,
        )

    env = _sandbox_env(worktree)

    # (1) Layer 1 is re-run: the candidate may have been through a store.
    try:
        validate_patch(candidate, repo_root=root)
    except PayloadBoundaryError as boundary_error:
        return report(applied=False, tests_passed=None, error=f"boundary: {boundary_error}")

    # (2) The main working tree must be clean, or the run is refused outright.
    status = _run_command(
        ("git", "status", "--porcelain"), cwd=root, env=env, timeout=GIT_STATUS_TIMEOUT_S
    )
    if not status.ok:
        return report(
            applied=False,
            tests_passed=None,
            error=f"git status failed: {_failure_kind(status, GIT_STATUS_TIMEOUT_S)}",
        )
    if status.stdout.strip():
        return report(applied=False, tests_passed=None, error="main tree dirty")

    # (3) A second working tree, detached at HEAD, inside the sandbox root.
    if not _is_within(worktree, sandbox_root):
        raise ValueError(f"the sandbox worktree must live inside {sandbox_root}")
    added = _run_command(
        ("git", "worktree", "add", str(worktree), "--detach", "HEAD"),
        cwd=root,
        env=env,
        timeout=WORKTREE_TIMEOUT_S,
    )
    if not added.ok:
        return report(
            applied=False,
            tests_passed=None,
            error=f"worktree add failed: {_failure_kind(added, WORKTREE_TIMEOUT_S)}",
        )

    # (4) The patch lands in the worktree — never in the main tree.
    patched = _run_command(
        _GIT_APPLY_COMMAND, cwd=worktree, env=env, timeout=GIT_APPLY_TIMEOUT_S, stdin_text=candidate.diff
    )
    if not patched.ok:
        return report(
            applied=False,
            tests_passed=None,
            error=f"git apply failed: {_failure_kind(patched, GIT_APPLY_TIMEOUT_S)}",
        )

    # (5) The candidate's own verification tests, inside the worktree.
    tests_passed, failed_tests, tests_error = _run_target_tests(
        candidate, worktree=worktree, env=env, timeout=tests_timeout
    )

    # (6) The patch's own effect: the same replay, once in a clean worktree at HEAD and
    # once here. The runner's own old/new pair is legacy-versus-Jev, so one report says
    # nothing about the diff; the difference between the two runs does.
    if run_benchmark and tests_passed is True:
        baseline_worktree = sandbox_root / BASELINE_WORKTREE_DIRNAME
        added_baseline = _run_command(
            ("git", "worktree", "add", str(baseline_worktree), "--detach", "HEAD"),
            cwd=root,
            env=env,
            timeout=WORKTREE_TIMEOUT_S,
        )
        if not added_baseline.ok:
            return report(
                applied=True,
                tests_passed=True,
                benchmark=None,
                error=(
                    "baseline worktree failed: "
                    f"{_failure_kind(added_baseline, WORKTREE_TIMEOUT_S)}"
                ),
            )
        # Each run imports its own tree: PYTHONPATH decides what ``import rag`` resolves to,
        # so the clean run must point at the clean worktree.
        baseline_payload, baseline_error = _run_benchmark(
            worktree=baseline_worktree,
            env=_replay_env(baseline_worktree, data_snapshot),
            sandbox_root=sandbox_root,
            kb_id=kb_id,
            timeout=benchmark_timeout,
            user_id=replay_user_id,
            report_name=BASELINE_REPORT_NAME,
        )
        patched_payload, patched_error = _run_benchmark(
            worktree=worktree,
            env=_replay_env(worktree, data_snapshot),
            sandbox_root=sandbox_root,
            kb_id=kb_id,
            timeout=benchmark_timeout,
            user_id=replay_user_id,
        )
        replay_error = baseline_error or patched_error
        if replay_error:
            return report(
                applied=True,
                tests_passed=True,
                failed_tests=(),
                benchmark=None,
                error=replay_error,
            )
        verdict = patch_verdict(baseline_payload or {}, patched_payload or {})
        if verdict is None:
            return report(
                applied=True,
                tests_passed=True,
                failed_tests=(),
                benchmark=None,
                error="benchmark replay report is missing the compared metrics",
            )
        return report(applied=True, tests_passed=True, benchmark=verdict)

    return report(
        applied=True,
        tests_passed=tests_passed,
        failed_tests=failed_tests,
        error=tests_error,
    )


def _release_worktree(*, root: Path, worktree: Path) -> list[str]:
    """``git worktree remove --force`` then ``git worktree prune``; never raises.

    Cleanup is best-effort by design: a cleanup problem is returned as an error
    string, but it must never replace the exception or the report the run
    already produced — that is the whole point of running it in a ``finally``.
    """
    errors: list[str] = []
    try:
        env = _sandbox_env(worktree)
        for command, label in (
            (("git", "worktree", "remove", "--force", str(worktree)), "worktree remove"),
            (("git", "worktree", "prune"), "worktree prune"),
        ):
            outcome = _run_command(command, cwd=root, env=env, timeout=WORKTREE_TIMEOUT_S)
            if not outcome.ok:
                errors.append(f"{label} failed: {_failure_kind(outcome, WORKTREE_TIMEOUT_S)}")
    except Exception as error:  # a cleanup crash is data, not a new failure
        errors.append(f"worktree release failed: {type(error).__name__}")
    return errors


def run_patch_sandbox(
    candidate: CodePatchCandidate,
    *,
    repo_root: str | Path,
    tests_timeout: float = 600.0,
    run_benchmark: bool = False,
    kb_id: str = "rag_docs",
    benchmark_timeout: float = BENCHMARK_TIMEOUT_S,
    data_snapshot: str | None = None,
    replay_user_id: int | None = None,
) -> SandboxReport:
    """Validate one code-patch candidate in an isolated worktree.

    ``repo_root`` is the repository the diff applies to and is only ever read
    from: the patched copy is a ``git worktree`` added inside a temp directory,
    the target tests and the benchmark replay run with that worktree as both
    ``cwd`` and ``PYTHONPATH``, and the worktree is removed in a ``finally``
    whatever happens in between.

    ``kb_id`` is the knowledge base the diagnosis came from and is what the
    replay replays; ``tests_timeout`` bounds the target tests (a timeout is
    inconclusive, not a rejection).

    ``run_benchmark`` defaults to **off**, and that is the honest default: the
    replay needs the model APIs (both embedding and reranking are external calls)
    while a sandbox child deliberately carries no credentials, so a replay inside
    the sandbox measures the absence of a key rather than the patch. The sandbox
    therefore proves what it can prove — the diff applies, the candidate's target
    tests and the fixed regression net pass — and the layer-2 regression A/B is
    the administrator's step after applying, run where the credentials live
    (``docs/evolution-code-patches.md`` section 5). The replay remains available
    as an experiment: ``run_benchmark=True`` plus ``data_snapshot`` and
    ``replay_user_id`` runs the same benchmark in a clean worktree and in the
    patched one and compares each side (``patch_verdict``).

    Returns a :class:`SandboxReport`. Nothing is raised for a refused candidate:
    a boundary violation, a dirty main tree, a diff that does not apply, a
    failing test run and a replay that could not run are all reports. Only a
    crash in the sandbox itself propagates — and it still cleans up.
    """
    started = time.perf_counter()
    root = Path(repo_root)
    snapshot = (
        settings.evolution_sandbox_data_snapshot if data_snapshot is None else data_snapshot
    )
    user_id = (
        settings.evolution_sandbox_replay_user_id if replay_user_id is None else replay_user_id
    )

    sandbox_root = Path(tempfile.mkdtemp(prefix=SANDBOX_ROOT_PREFIX))
    worktree = sandbox_root / WORKTREE_DIRNAME
    cleanup_errors: list[str] = []
    try:
        result = _execute(
            candidate,
            root=root,
            sandbox_root=sandbox_root,
            worktree=worktree,
            started=started,
            tests_timeout=tests_timeout,
            run_benchmark=run_benchmark,
            kb_id=kb_id,
            benchmark_timeout=benchmark_timeout,
            data_snapshot=snapshot,
            replay_user_id=user_id,
        )
    finally:
        # Whatever happened above — a report, a refusal, or an exception — the
        # second working tree leaves the repository here, and the sandbox root
        # leaves the disk.
        cleanup_errors = _release_worktree(root=root, worktree=worktree)
        baseline_worktree = sandbox_root / BASELINE_WORKTREE_DIRNAME
        if baseline_worktree.exists():
            cleanup_errors.extend(_release_worktree(root=root, worktree=baseline_worktree))
        shutil.rmtree(sandbox_root, ignore_errors=True)
        if worktree.exists():
            # The remove could not take (a locked file, a hung handle): with the
            # directory gone, one more prune clears the registration the worktree
            # left behind in the repository.
            cleanup_errors.extend(_release_worktree(root=root, worktree=worktree))
        if sandbox_root.exists():
            cleanup_errors.append(f"sandbox root {sandbox_root} could not be deleted")

    if cleanup_errors and not result.error:
        result = replace(result, error="; ".join(cleanup_errors))
    return result


#: The gate name a sandbox run records in ``candidates.gate_log``. The review
#: endpoints and the frontend summary read exactly this string, so it is a
#: constant here rather than a caller's choice.
GATE_NAME = "sandbox"


def sandbox_gate_outcome(report: SandboxReport) -> tuple[str, str]:
    """``(status, gate_detail)`` for the write-back a caller makes after a run.

    The caller writes::

        status, detail = sandbox_gate_outcome(report)
        store.advance_candidate(
            candidate_id, status, actor_id=..., note=..., gate="sandbox", gate_detail=detail
        )

    ``awaiting_verification`` means nothing in the plan's veto list failed: the
    diff applied, the target tests passed, and the replay either passed or could
    not run. ``rejected`` means one of them did — a diff that does not apply, a
    failing verification test, or a replay regression. A run that could not
    conclude (a timeout, a pytest that never started) is
    ``needs_more_evidence``, the same conservative direction the plan takes with
    a layer-2 timeout.
    """
    if not report.applied:
        return EvolutionStatus.REJECTED.value, f"sandbox refused the patch: {report.error}"
    if report.tests_passed is False:
        failing = ", ".join(report.failed_tests) or report.error
        return EvolutionStatus.REJECTED.value, f"sandbox target tests failed: {failing}"
    if report.tests_passed is None:
        return (
            EvolutionStatus.NEEDS_MORE_EVIDENCE.value,
            f"sandbox target tests inconclusive: {report.error}",
        )
    if report.benchmark is None:
        if report.error:
            return (
                EvolutionStatus.AWAITING_VERIFICATION.value,
                f"sandbox passed; replay unavailable: {report.error}",
            )
        # 沙箱不声称"无回归"：它证明的是"能应用 + 目标测试与固定回归网通过"。回归 A/B 需要
        # 模型凭据（嵌入与重排都走外部 API），而沙箱刻意不带凭据——那一步属于管理员在应用之后、
        # 在服务所在的环境里按既有 20 用例协议跑（docs/evolution-code-patches.md 第 5 节）。
        return (
            EvolutionStatus.AWAITING_VERIFICATION.value,
            "sandbox passed: the patch applied and the target tests plus the fixed regression net "
            "are green; the layer-2 regression A/B is not measured here (the sandbox carries no "
            "credentials) and belongs to the administrator after applying",
        )
    verdict = report.benchmark.get("verdict") or "no verdict"
    if not report.benchmark.get("passed"):
        return EvolutionStatus.REJECTED.value, f"sandbox benchmark replay: {verdict}"
    return EvolutionStatus.AWAITING_VERIFICATION.value, f"sandbox passed; {verdict}"


__all__ = [
    "BENCHMARK_SCRIPT",
    "BENCHMARK_TIMEOUT_S",
    "CommandOutcome",
    "GATE_NAME",
    "GIT_STATUS_TIMEOUT_S",
    "MAX_LISTED",
    "MAX_RECORDED_TESTS",
    "MIN_PREP_P50_MS",
    "REGRESSION_TEST_PATHS",
    "SANDBOX_ROOT_PREFIX",
    "SandboxReport",
    "WORKTREE_DIRNAME",
    "WORKTREE_TIMEOUT_S",
    "patch_verdict",
    "run_patch_sandbox",
    "sandbox_gate_outcome",
]
