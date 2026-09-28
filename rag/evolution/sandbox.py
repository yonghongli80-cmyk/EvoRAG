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
5. ``python -m pytest <test_paths>`` runs with ``cwd`` and ``PYTHONPATH``
   pointing at the worktree, ``PYTHONHASHSEED=0`` and an environment stripped of
   every ``RAG_*`` key, so the host ``.env`` cannot reach the target tests — and
   ``import rag`` resolves to the patched copy rather than the installed one;
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
#: Where the replay writes its report: next to the worktree, never inside it,
#: so the tree the tests ran in stays exactly the patched tree.
BENCHMARK_REPORT_NAME = "benchmark.json"
#: The phase-2 A/B runner, addressed as a *relative* path so the worktree's own
#: copy of ``scripts/`` is what the replay measures.
BENCHMARK_SCRIPT = "scripts/benchmark_jev_retrieval.py"

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


def _run_target_tests(
    candidate: CodePatchCandidate, *, worktree: Path, env: Mapping[str, str], timeout: float
) -> tuple[bool | None, tuple[str, ...], str]:
    """Run the candidate's verification tests inside the worktree.

    Returns ``(tests_passed, failed_tests, error)``. A timeout or a pytest that
    never ran is ``tests_passed=None`` — inconclusive, which the plan records as
    ``needs_more_evidence`` rather than as a rejection.
    """
    if not candidate.test_paths:
        return None, (), "target tests skipped: the candidate names no test path"
    outcome = _run_command(
        _pytest_command(candidate.test_paths), cwd=worktree, env=env, timeout=timeout
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


def _benchmark_command(*, kb_id: str, json_out: Path) -> list[str]:
    """One replay of the committed corpus, as a relative script path."""
    return [
        sys.executable,
        BENCHMARK_SCRIPT,
        "--kb-id",
        str(kb_id),
        "--json-out",
        str(json_out),
    ]


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


def benchmark_verdict(payload: Mapping) -> dict | None:
    """The layer-2 comparison of one replay report, or ``None`` if unreadable.

    The runner replays the legacy preparation path against the Jev path over the
    same labeled corpus and writes both sides' metrics; the sandbox reads that
    pair as baseline-versus-candidate and applies the never-weakened layer-2
    thresholds to it (route drop ≤ 0pp, Top-5 drop ≤ 2pp). A patched side that
    fails more calls than the baseline also fails the verdict: a crash inside
    the patched code is a regression, whatever the aggregate rates say.
    """
    baseline = payload.get("old")
    candidate = payload.get("new")
    if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
        return None
    baseline_route = _number(baseline.get("route_accuracy"))
    candidate_route = _number(candidate.get("route_accuracy"))
    baseline_top5 = _number(baseline.get("top5_hit_rate"))
    candidate_top5 = _number(candidate.get("top5_hit_rate"))
    if None in (baseline_route, candidate_route, baseline_top5, candidate_top5):
        return None

    route_delta_pp = _pp(candidate_route - baseline_route)
    top5_delta_pp = _pp(candidate_top5 - baseline_top5)
    failed_calls_delta = _count(candidate.get("failed_calls")) - _count(baseline.get("failed_calls"))
    regressed = _replay_cases(payload, regressed=True)
    improved = _replay_cases(payload, regressed=False)

    route_ok = -route_delta_pp <= ROUTE_DELTA_MAX_PP
    top5_ok = -top5_delta_pp <= TOP5_DELTA_MAX_PP
    calls_ok = failed_calls_delta <= 0

    reasons: list[str] = []
    if not route_ok:
        reasons.append(
            f"route accuracy dropped {-route_delta_pp:.1f}pp beyond {-ROUTE_DELTA_MAX_PP:.1f}pp "
            f"(regressed cases: {_listed(regressed)})"
        )
    if not top5_ok:
        reasons.append(
            f"top-5 hit dropped {-top5_delta_pp:.1f}pp beyond {-TOP5_DELTA_MAX_PP:.1f}pp "
            f"(regressed cases: {_listed(regressed)})"
        )
    if not calls_ok:
        reasons.append(
            f"the patched side failed {failed_calls_delta} more call(s) than the baseline"
        )

    cases = _count(payload.get("cases"))
    if reasons:
        verdict = "failed: " + "; ".join(reasons)
    else:
        verdict = (
            f"passed: route delta {route_delta_pp:+.1f}pp (limit {-ROUTE_DELTA_MAX_PP:+.1f}pp), "
            f"top-5 delta {top5_delta_pp:+.1f}pp (limit {-TOP5_DELTA_MAX_PP:+.1f}pp) "
            f"over {cases} cases"
        )
    return {
        "cases": cases,
        "baseline": {
            "route_accuracy": baseline_route,
            "top5_hit_rate": baseline_top5,
            "sample_count": _count(baseline.get("sample_count")),
        },
        "candidate": {
            "route_accuracy": candidate_route,
            "top5_hit_rate": candidate_top5,
            "sample_count": _count(candidate.get("sample_count")),
            "failed_calls": _count(candidate.get("failed_calls")),
        },
        "route_delta_pp": route_delta_pp,
        "top5_delta_pp": top5_delta_pp,
        "thresholds": {
            "route_drop_max_pp": ROUTE_DELTA_MAX_PP,
            "top5_drop_max_pp": TOP5_DELTA_MAX_PP,
        },
        "regressed_cases": regressed,
        "improved_cases": improved,
        "passed": not reasons,
        "verdict": verdict,
    }


def _run_benchmark_replay(
    *, worktree: Path, env: Mapping[str, str], sandbox_root: Path, kb_id: str, timeout: float
) -> tuple[dict | None, str]:
    """Replay the committed corpus in the worktree and decide with layer-2 thresholds.

    A replay that cannot run is reported as an error with no benchmark, never as
    a rejection: a missing environment is not evidence about a candidate.
    """
    json_out = sandbox_root / BENCHMARK_REPORT_NAME
    outcome = _run_command(
        _benchmark_command(kb_id=kb_id, json_out=json_out),
        cwd=worktree,
        env=env,
        timeout=timeout,
    )
    if outcome.timed_out:
        return None, f"benchmark replay timed out after {int(timeout)}s"
    if outcome.unavailable:
        return None, "benchmark replay unavailable: the interpreter could not be started"
    if outcome.returncode != 0:
        return None, f"benchmark replay did not complete: exit_{outcome.returncode}"
    if not json_out.is_file():
        return None, "benchmark replay produced no report"
    try:
        payload = json.loads(json_out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "benchmark replay report is not valid json"
    if not isinstance(payload, Mapping):
        return None, "benchmark replay report is not a json object"
    verdict = benchmark_verdict(payload)
    if verdict is None:
        return None, "benchmark replay report is missing the compared metrics"
    return verdict, ""


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

    # (6) One replay of the committed corpus, decided with the layer-2 thresholds.
    if run_benchmark and tests_passed is True:
        benchmark, replay_error = _run_benchmark_replay(
            worktree=worktree,
            env=env,
            sandbox_root=sandbox_root,
            kb_id=kb_id,
            timeout=benchmark_timeout,
        )
        if replay_error:
            return report(
                applied=True,
                tests_passed=True,
                failed_tests=(),
                benchmark=None,
                error=replay_error,
            )
        return report(applied=True, tests_passed=True, benchmark=benchmark)

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
    run_benchmark: bool = True,
    kb_id: str = "rag_docs",
    benchmark_timeout: float = BENCHMARK_TIMEOUT_S,
) -> SandboxReport:
    """Validate one code-patch candidate in an isolated worktree.

    ``repo_root`` is the repository the diff applies to and is only ever read
    from: the patched copy is a ``git worktree`` added inside a temp directory,
    the target tests and the benchmark replay run with that worktree as both
    ``cwd`` and ``PYTHONPATH``, and the worktree is removed in a ``finally``
    whatever happens in between.

    ``kb_id`` is the knowledge base the diagnosis came from and is what the
    replay replays; ``tests_timeout`` bounds the target tests (a timeout is
    inconclusive, not a rejection); ``run_benchmark=False`` skips the replay,
    which is what a local smoke run wants.

    Returns a :class:`SandboxReport`. Nothing is raised for a refused candidate:
    a boundary violation, a dirty main tree, a diff that does not apply, a
    failing test run and a replay that could not run are all reports. Only a
    crash in the sandbox itself propagates — and it still cleans up.
    """
    started = time.perf_counter()
    root = Path(repo_root)

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
        )
    finally:
        # Whatever happened above — a report, a refusal, or an exception — the
        # second working tree leaves the repository here, and the sandbox root
        # leaves the disk.
        cleanup_errors = _release_worktree(root=root, worktree=worktree)
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
        return (
            EvolutionStatus.AWAITING_VERIFICATION.value,
            f"sandbox passed; replay unavailable: {report.error}",
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
    "SANDBOX_ROOT_PREFIX",
    "SandboxReport",
    "WORKTREE_DIRNAME",
    "WORKTREE_TIMEOUT_S",
    "benchmark_verdict",
    "run_patch_sandbox",
    "sandbox_gate_outcome",
]
