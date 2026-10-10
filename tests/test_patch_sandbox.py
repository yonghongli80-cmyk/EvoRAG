"""Task 3: the isolated worktree sandbox for code-patch candidates.

The sandbox is the only component that may *apply* a candidate diff, so these
tests pin both halves of its contract:

* **it really works** — a small real diff over a real repository applies in a
  ``git worktree``, its target tests run inside that worktree against the
  patched copy, and the numbers come out the way the plan's thresholds say;
* **it really isolates** — the main working tree is never touched (the patch
  only ever lands in a second working tree), no host ``.env``/``RAG_*`` setting
  reaches a child process, the worktree always lives inside the sandbox temp
  root, and ``git worktree list`` is identical before and after a run —
  including a run that dies in the middle of it.

Two repository fixtures are used:

* ``sandbox_repo`` — a tiny purpose-built git repository. Fast, hermetic, and in
  full control of clean/dirty state, so every refusal branch and the benchmark
  replay are exercised deterministically.
* ``cloned_repo`` — a throwaway ``git clone`` of this repository at ``HEAD``:
  real code, a real ``tests/test_reranker.py``, and its own ``.git``, so the
  green path runs against the real thing without depending on the shared working
  tree being clean (two other lanes are editing it while this is written).

The benchmark replay is injected through the sandbox's child-process seam, so no
test ever calls a model, a vector store, or the live pipeline.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from rag.evolution import sandbox as sandbox_module
from rag.evolution.candidate_gates import ROUTE_DELTA_MAX_PP, TOP5_DELTA_MAX_PP
from rag.evolution.code_patches import CodePatchCandidate
from rag.evolution.sandbox import (
    GATE_NAME,
    REGRESSION_TEST_PATHS,
    SandboxReport,
    run_patch_sandbox,
    sandbox_gate_outcome,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "a" * 40
PROBE_COMMENT = "# sandbox probe (comment only)"

#: The real repository's lightweight, fully offline test module.
REAL_TARGET = "rag/reranker.py"
REAL_TESTS = ("tests/test_reranker.py",)
#: The synthetic repository's own probe module (inside the reranking layer glob).
PROBE_TARGET = "rag/reranker.py"
PROBE_TESTS = ("tests/test_probe.py",)


# ------------------------------------------------------------------
# Diff fixtures.
# ------------------------------------------------------------------


def comment_diff(repo_root: Path, path: str) -> str:
    """A real unified diff: one comment line appended to an existing file."""
    target = Path(repo_root) / path
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    edited = [*lines, f"{PROBE_COMMENT}\n"]
    return "".join(
        difflib.unified_diff(lines, edited, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


def breaking_diff(repo_root: Path, path: str, *, needle: str, replacement: str) -> str:
    """A real unified diff that changes one line of an existing file."""
    target = Path(repo_root) / path
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    edited = "".join(lines).replace(needle, replacement, 1).splitlines(keepends=True)
    assert "".join(edited) != "".join(lines), "the breaking diff must change the file"
    return "".join(
        difflib.unified_diff(lines, edited, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


def non_applying_diff(path: str) -> str:
    """A well-formed diff whose preimage is not in the file."""
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,3 +1,4 @@\n"
        ' """preimage line that is not in the file"""\n'
        ' """second preimage line that is not in the file"""\n'
        ' """third preimage line that is not in the file"""\n'
        "+# line the patch tries to add\n"
    )


def make_candidate(repo_root: Path, **kw) -> CodePatchCandidate:
    """A valid reranking candidate whose diff points at ``repo_root``."""
    data = dict(
        artifact_type="code_patch",
        layer="reranking",
        paths=(PROBE_TARGET,),
        test_paths=PROBE_TESTS,
        rationale="检索排序在撞名词面上区分度低，沙箱回归可复现",
        expected_effect="Top-5 命中回升，路线不变",
        rollback_ref={"base_commit": BASE_COMMIT},
        confidence="medium",
    )
    data.update(kw)
    if "diff" not in data:
        data["diff"] = comment_diff(repo_root, data["paths"][0])
    return CodePatchCandidate(**data)


# ------------------------------------------------------------------
# Git helpers and repositories.
# ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=300,
    )


def worktree_list(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "worktree", "list"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def is_clean(repo_root: Path) -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return completed.stdout.strip() == ""


def _init_probe_repo(repo: Path) -> None:
    """A minimal git repository with one reranking-layer module and its test.

    ``rag/__init__.py`` matters: a worktree's ``rag/`` must be a *regular*
    package, otherwise the installed copy on ``sys.path`` wins the import and
    the target tests would exercise the unpatched code.
    """
    (repo / "rag").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "rag" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "rag" / "reranker.py").write_text(
        '"""Probe reranker module (sandbox fixture)."""\n'
        "\n"
        "\n"
        "def rank(items):\n"
        '    """Baseline probe behaviour: the given order is preserved."""\n'
        "    return list(items)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_probe.py").write_text(
        "from rag.reranker import rank\n"
        "\n"
        "\n"
        "def test_probe_rank_keeps_order():\n"
        '    assert rank(["a", "b", "c"]) == ["a", "b", "c"]\n',
        encoding="utf-8",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "sandbox@example.invalid")
    _git(repo, "config", "user.name", "Sandbox Probe")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "probe baseline")


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    """A fresh, clean, throwaway git repository for the sandbox to work on."""
    repo = tmp_path / "probe-repo"
    repo.mkdir()
    _init_probe_repo(repo)
    return repo


@pytest.fixture(scope="module")
def cloned_repo(tmp_path_factory) -> Path:
    """A throwaway clone of this repository at HEAD: real code, private ``.git``."""
    repo = tmp_path_factory.mktemp("cloned-repo") / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(REPO_ROOT), str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=600,
    )
    _git(repo, "config", "user.email", "sandbox@example.invalid")
    _git(repo, "config", "user.name", "Sandbox Probe")
    return repo


# ------------------------------------------------------------------
# The injected runner: records every child process, never leaves the test.
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedCall:
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] | None
    timeout: float
    stdin_text: str | None


def _outcome(returncode: int, stdout: str = "", stderr: str = "") -> object:
    return sandbox_module.CommandOutcome(
        returncode=returncode, stdout=stdout, stderr=stderr
    )


def _benchmark_payload(
    *,
    cases: int = 4,
    route_accuracy: float = 0.75,
    candidate_route_accuracy: float | None = None,
    top5_hit_rate: float = 0.75,
    candidate_top5_hit_rate: float | None = None,
    prep_p50_ms: float = 800.0,
    candidate_prep_p50_ms: float | None = None,
    baseline_failed_calls: int = 0,
    candidate_failed_calls: int = 0,
    regressed_cases: Sequence[int] = (),
) -> dict:
    """The shape ``scripts/benchmark_jev_retrieval.py`` writes with ``--json-out``.

    ``prep_p50_ms`` belongs to that shape: a report whose preparation latency is
    zero says the preparation path never ran, which is not evidence about a
    candidate.
    """
    return {
        "cases": cases,
        "sample_count": cases * 2,
        "old": {
            "sample_count": cases,
            "route_accuracy": route_accuracy,
            "top5_hit_rate": top5_hit_rate,
            "prep_p50_ms": prep_p50_ms,
            "failed_calls": baseline_failed_calls,
        },
        "new": {
            "sample_count": cases,
            "route_accuracy": (
                route_accuracy if candidate_route_accuracy is None else candidate_route_accuracy
            ),
            "top5_hit_rate": (
                top5_hit_rate if candidate_top5_hit_rate is None else candidate_top5_hit_rate
            ),
            "prep_p50_ms": prep_p50_ms if candidate_prep_p50_ms is None else candidate_prep_p50_ms,
            "failed_calls": candidate_failed_calls,
        },
        "acceptance": {"passed": True, "failures": [], "checks": []},
        "case_breakdowns": [
            {
                "case_id": f"rag-{index:03d}",
                "old": {"route_correct_count": 1, "run_count": 1},
                "new": {
                    "route_correct_count": 0 if index in regressed_cases else 1,
                    "run_count": 1,
                },
            }
            for index in range(cases)
        ],
    }


class FakeRunner:
    """A recording stand-in for the sandbox's child-process seam.

    ``benchmark`` is written to the ``--json-out`` path the sandbox passes, so
    the parsing and the threshold comparison stay the real ones. ``raise_on``
    makes one step blow up, which is how the cleanup path is pinned.
    """

    def __init__(
        self,
        *,
        benchmark: dict | None = None,
        benchmark_returncode: int = 0,
        baseline_benchmark: dict | None = None,
        pytest_returncode: int = 0,
        pytest_stdout: str = "1 passed in 0.01s\n",
        apply_returncode: int = 0,
        status_stdout: str = "",
        pytest_timed_out: bool = False,
        raise_on: Callable[[Sequence[str]], bool] | None = None,
    ) -> None:
        self._benchmark = benchmark
        self._benchmark_returncode = benchmark_returncode
        # 干净工作树那一侧的报告；缺省与 patched 相同 ⇒ 零差异（补丁无影响）
        self._baseline_benchmark = (
            benchmark if baseline_benchmark is None else baseline_benchmark
        )
        self._pytest_returncode = pytest_returncode
        self._pytest_stdout = pytest_stdout
        self._apply_returncode = apply_returncode
        self._status_stdout = status_stdout
        self._pytest_timed_out = pytest_timed_out
        self._raise_on = raise_on
        self.calls: list[RecordedCall] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        stdin_text: str | None = None,
    ):
        words = tuple(str(word) for word in command)
        self.calls.append(
            RecordedCall(
                command=words,
                cwd=Path(cwd),
                env=dict(env) if env is not None else None,
                timeout=timeout,
                stdin_text=stdin_text,
            )
        )
        if self._raise_on is not None and self._raise_on(words):
            raise RuntimeError(f"injected failure on {' '.join(words)}")
        if words[0] == "git" and len(words) > 1 and words[1] == "status":
            return _outcome(0, self._status_stdout)
        if words[0] == "git" and len(words) > 1 and words[1] == "apply":
            return _outcome(self._apply_returncode, "", "error: patch failed: rag/reranker.py")
        if words[0] == "git":
            return _outcome(0, "")
        if "pytest" in words:
            if self._pytest_timed_out:
                return sandbox_module.CommandOutcome(returncode=-1, timed_out=True)
            return _outcome(self._pytest_returncode, self._pytest_stdout)
        if "benchmark_jev_retrieval.py" in " ".join(words):
            return self._replay(words)
        return _outcome(0, "")

    def _replay(self, words: tuple[str, ...]):
        path = Path(words[words.index("--json-out") + 1])
        payload = (
            self._baseline_benchmark
            if path.name == sandbox_module.BASELINE_REPORT_NAME
            else self._benchmark
        )
        if self._benchmark_returncode == 0 and payload is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        return _outcome(self._benchmark_returncode, "", "benchmark runner failed")

    def of(self, needle: str) -> list[RecordedCall]:
        return [call for call in self.calls if needle in " ".join(call.command)]


@pytest.fixture
def make_runner(monkeypatch) -> Callable[..., FakeRunner]:
    """Install a :class:`FakeRunner` as the sandbox's child-process seam."""

    def install(**kwargs) -> FakeRunner:
        fake = FakeRunner(**kwargs)
        monkeypatch.setattr(sandbox_module, "_run_command", fake)
        return fake

    return install


class DelegatingRunner:
    """Runs the real sandbox seam except on the one step it is told to break.

    The real seam is captured on construction, *before* the monkeypatch installs
    this object, so delegation never recurses into itself.
    """

    def __init__(self, break_on: Callable[[Sequence[str]], bool]) -> None:
        self._break_on = break_on
        self._real = sandbox_module._run_command
        self.calls: list[RecordedCall] = []

    def __call__(self, command, *, cwd, env=None, timeout=60.0, stdin_text=None):
        words = tuple(str(word) for word in command)
        self.calls.append(
            RecordedCall(
                command=words, cwd=Path(cwd), env=env, timeout=timeout, stdin_text=stdin_text
            )
        )
        if self._break_on(words):
            raise RuntimeError(f"injected failure on {' '.join(words)}")
        return self._real(command, cwd=cwd, env=env, timeout=timeout, stdin_text=stdin_text)


# ------------------------------------------------------------------
# The green path against the real repository.
# ------------------------------------------------------------------


def test_a_comment_only_patch_applies_and_its_target_tests_pass(cloned_repo: Path):
    candidate = CodePatchCandidate(
        artifact_type="code_patch",
        layer="reranking",
        paths=(REAL_TARGET,),
        diff=comment_diff(cloned_repo, REAL_TARGET),
        test_paths=REAL_TESTS,
        rationale="词面撞名候选淹没相关段落，沙箱跑通全流程",
        expected_effect="目标测试通过，主树不变",
        rollback_ref={"base_commit": BASE_COMMIT},
        confidence="medium",
    )
    before = worktree_list(cloned_repo)

    report = run_patch_sandbox(candidate, repo_root=cloned_repo, run_benchmark=False)

    assert isinstance(report, SandboxReport)
    assert report.applied is True
    assert report.tests_passed is True
    assert report.failed_tests == ()
    assert report.benchmark is None
    assert report.error == ""
    assert report.duration_s > 0.0
    # The clone's own tree is untouched and nothing was left behind.
    assert is_clean(cloned_repo)
    assert PROBE_COMMENT not in (cloned_repo / REAL_TARGET).read_text(encoding="utf-8")
    assert worktree_list(cloned_repo) == before


def test_the_target_tests_run_the_patched_copy_not_the_main_tree(cloned_repo: Path):
    """A behavioural regression in the diff must fail the target tests here."""
    candidate = CodePatchCandidate(
        artifact_type="code_patch",
        layer="reranking",
        paths=(REAL_TARGET,),
        diff=breaking_diff(
            cloned_repo,
            REAL_TARGET,
            needle="-document.rerank_score)[:top_k]",
            replacement="-document.rerank_score)[: top_k + 1]",
        ),
        test_paths=REAL_TESTS,
        rationale="重排截断偏移一行，证明跑的是 worktree 里的补丁副本",
        expected_effect="目标测试失败，失败用例按名上报",
        rollback_ref={"base_commit": BASE_COMMIT},
        confidence="low",
    )
    before = worktree_list(cloned_repo)

    report = run_patch_sandbox(candidate, repo_root=cloned_repo, run_benchmark=False)

    assert report.applied is True
    assert report.tests_passed is False
    assert report.failed_tests == ("tests/test_reranker.py::test_rerank_returns_top_k",)
    assert report.benchmark is None
    assert worktree_list(cloned_repo) == before
    assert is_clean(cloned_repo)
    assert "-document.rerank_score)[:top_k]" in (cloned_repo / REAL_TARGET).read_text(
        encoding="utf-8"
    )


# ------------------------------------------------------------------
# Refusals: a diff that does not apply, a dirty tree.
# ------------------------------------------------------------------


def test_a_diff_that_does_not_apply_is_refused_without_touching_the_tree(
    sandbox_repo: Path,
):
    candidate = make_candidate(sandbox_repo, diff=non_applying_diff(PROBE_TARGET))
    before = worktree_list(sandbox_repo)

    report = run_patch_sandbox(candidate, repo_root=sandbox_repo, run_benchmark=False)

    assert report.applied is False
    assert report.tests_passed is None
    assert report.failed_tests == ()
    assert report.benchmark is None
    assert "does_not_apply" in report.error
    assert report.duration_s > 0.0
    assert is_clean(sandbox_repo)
    assert PROBE_COMMENT not in (sandbox_repo / PROBE_TARGET).read_text(encoding="utf-8")
    assert worktree_list(sandbox_repo) == before


def test_a_dirty_main_tree_refuses_to_run(sandbox_repo: Path):
    (sandbox_repo / "uncommitted_probe.txt").write_text("dirty\n", encoding="utf-8")
    before = worktree_list(sandbox_repo)

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )

    assert report.applied is False
    assert report.tests_passed is None
    assert report.failed_tests == ()
    assert report.error == "main tree dirty"
    assert report.duration_s > 0.0
    assert worktree_list(sandbox_repo) == before


def test_a_dirty_main_tree_refuses_before_any_worktree_is_created(
    sandbox_repo: Path, make_runner
):
    """A refusal is a refusal: the seam never saw a ``worktree add``."""
    runner = make_runner(status_stdout="?? uncommitted_probe.txt\n")

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )

    assert report.error == "main tree dirty"
    # No worktree was ever added; the release step is a no-op either way.
    assert [call.command[2] for call in runner.of("worktree")] == ["remove", "prune"]
    assert [call.command for call in runner.of("status")] == [("git", "status", "--porcelain")]


def test_an_out_of_boundary_candidate_never_reaches_git(sandbox_repo: Path, make_runner):
    """Layer 1 is re-run inside the sandbox: self-modification is refused here too."""
    runner = make_runner()
    candidate = make_candidate(
        sandbox_repo,
        paths=("rag/evolution/sandbox.py",),
        diff=non_applying_diff("rag/evolution/sandbox.py"),
    )

    report = run_patch_sandbox(candidate, repo_root=sandbox_repo, run_benchmark=False)

    assert report.applied is False
    assert report.error.startswith("boundary:")
    assert "rag/evolution/**" in report.error
    # Layer 1 refuses before the sandbox ever asks git for anything.
    assert [call.command[1] for call in runner.of("git") if call.command[1] != "worktree"] == []


# ------------------------------------------------------------------
# The target tests.
# ------------------------------------------------------------------


def test_a_timed_out_target_test_run_is_inconclusive_not_a_rejection(
    sandbox_repo: Path, make_runner
):
    """A timeout is ``needs_more_evidence``; it never reads as a pass or a fail."""
    runner = make_runner(pytest_timed_out=True)

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, tests_timeout=30.0
    )

    assert report.applied is True
    assert report.tests_passed is None
    assert report.failed_tests == ()
    assert report.benchmark is None
    assert "timed out after 30s" in report.error
    assert runner.of("pytest")[0].timeout == 30.0
    assert sandbox_gate_outcome(report)[0] == "needs_more_evidence"


def test_failing_target_tests_are_reported_by_name(sandbox_repo: Path):
    diff = breaking_diff(
        sandbox_repo, PROBE_TARGET, needle="return list(items)", replacement="return []"
    )
    before = worktree_list(sandbox_repo)

    report = run_patch_sandbox(
        make_candidate(sandbox_repo, diff=diff), repo_root=sandbox_repo, run_benchmark=False
    )

    assert report.applied is True
    assert report.tests_passed is False
    assert report.failed_tests == ("tests/test_probe.py::test_probe_rank_keeps_order",)
    assert "tests/test_probe.py" in report.error
    assert report.benchmark is None
    assert is_clean(sandbox_repo)
    assert worktree_list(sandbox_repo) == before


def test_target_tests_run_with_the_worktree_as_their_root(sandbox_repo: Path, make_runner):
    runner = make_runner()
    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )
    assert report.applied is True

    pytest_call = runner.of("pytest")[0]
    worktree = pytest_call.cwd
    assert worktree.name == "worktree"
    assert worktree.parent != sandbox_repo
    assert pytest_call.env["PYTHONPATH"] == str(worktree)
    assert pytest_call.env["PYTHONHASHSEED"] == "0"
    assert Path(pytest_call.command[0]).name.startswith("python")
    assert "tests/test_probe.py" in pytest_call.command


def test_no_host_configuration_reaches_the_child_processes(
    sandbox_repo: Path, make_runner, monkeypatch
):
    monkeypatch.setenv("RAG_JEV_MODE", "active")
    monkeypatch.setenv("RAG_DEEPSEEK_API_KEY", "host-secret")
    runner = make_runner()

    run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False)

    recorded = [call for call in runner.calls if call.env is not None]
    assert recorded, "every child process must run under the sandbox environment"
    for call in recorded:
        assert not [key for key in call.env if key.startswith("RAG_")]
        assert call.env["PYTHONHASHSEED"] == "0"
        assert call.env["PYTHONPATH"].endswith("worktree")
    # A worktree is a tracked-files-only copy of HEAD: git never wrote .env there.
    assert not (runner.of("pytest")[0].cwd / ".env").exists()


def test_every_child_stays_inside_the_sandbox_temp_root(sandbox_repo: Path, make_runner):
    runner = make_runner()

    run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False)

    # apply / pytest live in the worktree; only git plumbing runs in the repo.
    for needle in ("apply", "pytest"):
        calls = runner.of(needle)
        assert calls, needle
        for call in calls:
            sandbox_root = call.cwd.parent
            assert call.cwd.name == "worktree"
            assert sandbox_root.name.startswith("rag-patch-sandbox-")
            assert sandbox_root.parent == Path(tempfile.gettempdir()).resolve()
            assert call.cwd.resolve() != sandbox_repo.resolve()
            assert sandbox_repo.resolve() not in call.cwd.resolve().parents
    # The sandbox root itself is gone once the run is over.
    assert not runner.of("pytest")[0].cwd.parent.exists()


def test_the_patch_travels_on_stdin_as_utf8(sandbox_repo: Path, make_runner):
    """The diff is piped to git, never written to a file the sandbox has to clean."""
    runner = make_runner(apply_returncode=0)
    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )
    assert report.applied is True

    apply_call = runner.of("apply")[0]
    assert apply_call.stdin_text == make_candidate(sandbox_repo).diff
    assert apply_call.command[:3] == ("git", "apply", "--whitespace=nowarn")
    assert apply_call.timeout <= 60.0


# ------------------------------------------------------------------
# The benchmark replay.
# ------------------------------------------------------------------


def test_the_benchmark_replay_compares_both_sides_against_the_layer_two_thresholds(
    sandbox_repo: Path, make_runner
):
    runner = make_runner(
        benchmark=_benchmark_payload(cases=4, route_accuracy=0.75, candidate_top5_hit_rate=0.5),
        baseline_benchmark=_benchmark_payload(cases=4, route_accuracy=0.75),
    )

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.applied is True
    assert report.tests_passed is True
    assert report.error == ""
    benchmark = report.benchmark
    assert benchmark is not None
    assert benchmark["cases"] == 4
    # 逐侧比较：干净工作树 vs 打过补丁的工作树（不是脚本自己的 legacy/Jev 两面）
    jev = benchmark["sides"]["jev"]
    assert jev["baseline"]["top5_hit_rate"] == 0.75
    assert jev["patched"]["top5_hit_rate"] == 0.5
    assert jev["top5_delta_pp"] == pytest.approx(-25.0)
    assert benchmark["route_delta_pp"] == 0.0
    assert benchmark["top5_delta_pp"] == pytest.approx(-25.0)
    assert benchmark["thresholds"] == {
        "route_drop_max_pp": ROUTE_DELTA_MAX_PP,
        "top5_drop_max_pp": TOP5_DELTA_MAX_PP,
    }
    assert benchmark["passed"] is False
    assert "top-5" in benchmark["verdict"]

    replays = runner.of("benchmark_jev_retrieval.py")
    assert [call.cwd.name for call in replays] == ["baseline", "worktree"]
    for replay in replays:
        assert replay.command[replay.command.index("--kb-id") + 1] == "rag_docs"
        # 每一次回放都要 import 自己那棵树，否则两次都在量同一份代码
        assert replay.env["PYTHONPATH"] == str(replay.cwd)
        json_out = Path(replay.command[replay.command.index("--json-out") + 1])
        assert json_out.parent == replay.cwd.parent


def test_a_benchmark_regression_is_reported_without_rejecting_the_run(
    sandbox_repo: Path, make_runner
):
    runner = make_runner(
        benchmark=_benchmark_payload(cases=4),  # 打过补丁：Jev 路由 0.75
        baseline_benchmark=_benchmark_payload(cases=4, candidate_route_accuracy=1.0),  # 干净：1.0
    )

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.applied is True
    assert report.tests_passed is True  # 运行本身没有被拒绝……
    assert report.benchmark["route_delta_pp"] == pytest.approx(-25.0)
    assert report.benchmark["passed"] is False  # ……但门禁结论是失败
    assert "route" in report.benchmark["verdict"]
    assert report.error == ""
    assert runner.of("benchmark_jev_retrieval.py")


def test_candidate_side_failures_fail_the_replay_verdict(sandbox_repo: Path, make_runner):
    runner = make_runner(
        benchmark=_benchmark_payload(cases=4, candidate_failed_calls=2),
        baseline_benchmark=_benchmark_payload(cases=4),
    )

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.benchmark["passed"] is False
    assert "failed" in report.benchmark["verdict"]
    assert runner.of("benchmark_jev_retrieval.py")


def test_a_pass_replay_is_reported_as_a_pass(sandbox_repo: Path, make_runner):
    runner = make_runner(benchmark=_benchmark_payload(cases=4, top5_hit_rate=0.75))
    # 两侧同载荷 ⇒ 补丁没有影响

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.benchmark["passed"] is True
    assert report.benchmark["route_delta_pp"] == 0.0
    assert report.benchmark["top5_delta_pp"] == 0.0
    assert runner.of("benchmark_jev_retrieval.py")


def test_a_benchmark_that_cannot_run_is_an_error_not_a_rejection(
    sandbox_repo: Path, make_runner
):
    runner = make_runner(benchmark_returncode=1)

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.applied is True
    assert report.tests_passed is True
    assert report.benchmark is None
    assert "benchmark" in report.error
    assert runner.of("benchmark_jev_retrieval.py")


def test_the_replay_is_skipped_once_the_target_tests_already_failed(
    sandbox_repo: Path, make_runner
):
    runner = make_runner(
        benchmark=_benchmark_payload(),
        pytest_returncode=1,
        pytest_stdout="FAILED tests/test_probe.py::test_probe_rank_keeps_order - assert []\n",
    )

    report = run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo)

    assert report.applied is True
    assert report.tests_passed is False
    assert report.benchmark is None
    assert runner.of("benchmark_jev_retrieval.py") == []


def test_the_replay_can_be_switched_off(sandbox_repo: Path, make_runner):
    runner = make_runner(benchmark=_benchmark_payload())

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )

    assert report.benchmark is None
    assert runner.of("benchmark_jev_retrieval.py") == []


# ------------------------------------------------------------------
# Cleanup guarantees.
# ------------------------------------------------------------------


def test_cleanup_runs_even_when_a_step_raises(sandbox_repo: Path, monkeypatch):
    fake = FakeRunner(raise_on=lambda words: words[0] == "git" and words[1] == "apply")
    monkeypatch.setattr(sandbox_module, "_run_command", fake)
    before = worktree_list(sandbox_repo)

    with pytest.raises(RuntimeError, match="injected failure"):
        run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo)

    assert worktree_list(sandbox_repo) == before
    worktree_calls = [call.command for call in fake.of("worktree")]
    assert worktree_calls[0][2] == "add"
    assert worktree_calls[1][2] == "remove"
    assert worktree_calls[-1][2] == "prune"
    assert not runner_root_exists(fake)


def test_cleanup_runs_when_the_worktree_add_itself_raises(sandbox_repo: Path, monkeypatch):
    fake = FakeRunner(raise_on=lambda words: words[:3] == ("git", "worktree", "add"))
    monkeypatch.setattr(sandbox_module, "_run_command", fake)
    before = worktree_list(sandbox_repo)

    with pytest.raises(RuntimeError, match="injected failure"):
        run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo)

    assert worktree_list(sandbox_repo) == before
    assert runner_root_exists(fake) is False


def runner_root_exists(runner: FakeRunner) -> bool:
    """Whether any recorded child-process root still exists on disk."""
    return any(
        call.cwd.parent.exists()
        for call in runner.calls
        if call.cwd.name in ("worktree", "baseline")
    )


def test_cleanup_removes_a_real_worktree_when_pytest_explodes(sandbox_repo: Path, monkeypatch):
    """The real thing: a real worktree exists, a real step dies, real cleanup runs."""
    fake = DelegatingRunner(break_on=lambda words: "pytest" in words)
    monkeypatch.setattr(sandbox_module, "_run_command", fake)
    before = worktree_list(sandbox_repo)

    with pytest.raises(RuntimeError, match="injected failure"):
        run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo)

    assert worktree_list(sandbox_repo) == before
    worktree_paths = [call.cwd for call in fake.calls if call.cwd.name == "worktree"]
    assert worktree_paths, "a real worktree must have been created"
    assert not any(path.exists() for path in worktree_paths)


def test_a_report_carries_a_json_safe_summary(sandbox_repo: Path, make_runner):
    """The report is what an audit endpoint stores, so it must serialize as it is."""
    make_runner(benchmark=_benchmark_payload())
    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    payload = report.to_dict()
    assert payload["applied"] is True
    assert payload["tests_passed"] is True
    assert payload["failed_tests"] == []
    assert payload["benchmark"]["route_delta_pp"] == 0.0
    assert json.loads(json.dumps(payload)) == payload


# ------------------------------------------------------------------
# The write-back seam (Task 4 reads ``gate="sandbox"``).
# ------------------------------------------------------------------


def test_the_gate_name_is_the_one_the_review_endpoints_read():
    assert GATE_NAME == "sandbox"


def test_a_green_run_claims_only_what_the_sandbox_can_prove(sandbox_repo: Path, make_runner):
    """默认**不跑**回放：沙箱只证"能应用 + 目标测试与固定回归网通过"。

    回归 A/B 需要模型凭据（嵌入与重排都是外部调用），而沙箱刻意不带凭据——在里面跑回放
    量到的是"没有密钥"，不是补丁。所以默认关闭，且门禁结论必须**明说缺了哪一步、归谁做**。
    """
    runner = make_runner(benchmark=_benchmark_payload())

    report = run_patch_sandbox(make_candidate(sandbox_repo), repo_root=sandbox_repo)

    assert report.applied is True
    assert report.tests_passed is True
    assert report.benchmark is None  # 默认不跑回放
    assert runner.of("benchmark_jev_retrieval.py") == []

    status, detail = sandbox_gate_outcome(report)

    assert status == "awaiting_verification"
    assert detail.startswith("sandbox passed:")
    assert "regression A/B is not measured here" in detail
    assert "administrator after applying" in detail


def test_a_replay_that_could_not_run_still_writes_back_awaiting_verification(
    sandbox_repo: Path, make_runner
):
    make_runner(benchmark_returncode=1)
    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    status, detail = sandbox_gate_outcome(report)

    assert status == "awaiting_verification"
    assert "replay unavailable" in detail


def test_a_refused_patch_writes_back_rejected(sandbox_repo: Path):
    report = run_patch_sandbox(
        make_candidate(sandbox_repo, diff=non_applying_diff(PROBE_TARGET)),
        repo_root=sandbox_repo,
        run_benchmark=False,
    )

    status, detail = sandbox_gate_outcome(report)

    assert status == "rejected"
    assert "does_not_apply" in detail


def test_a_failing_test_run_writes_back_rejected_with_the_test_names(sandbox_repo: Path):
    diff = breaking_diff(
        sandbox_repo, PROBE_TARGET, needle="return list(items)", replacement="return []"
    )
    report = run_patch_sandbox(
        make_candidate(sandbox_repo, diff=diff), repo_root=sandbox_repo, run_benchmark=False
    )

    status, detail = sandbox_gate_outcome(report)

    assert status == "rejected"
    assert "tests/test_probe.py::test_probe_rank_keeps_order" in detail


def test_a_replay_regression_writes_back_rejected(sandbox_repo: Path, make_runner):
    make_runner(
        # 干净一侧 Jev 路由 1.0，打过补丁一侧 0.75 ⇒ 补丁造成 25pp 回归
        benchmark=_benchmark_payload(cases=4, candidate_route_accuracy=0.75),
        baseline_benchmark=_benchmark_payload(cases=4, candidate_route_accuracy=1.0),
    )
    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    status, detail = sandbox_gate_outcome(report)

    assert status == "rejected"
    assert "route accuracy dropped 25.0pp" in detail


def test_a_report_is_the_only_thing_the_seam_needs():
    """The write-back works on a hand-built report, so Task 4 can unit-test it."""
    refused = SandboxReport(applied=False, tests_passed=None, error="main tree dirty")
    assert sandbox_gate_outcome(refused) == ("rejected", "sandbox refused the patch: main tree dirty")


# ------------------------------------------------------------------
# The fixed regression net: verification that does not depend on the candidate.
# ------------------------------------------------------------------


def test_the_fixed_regression_suite_always_runs_beside_the_candidates_targets(
    sandbox_repo: Path, make_runner, monkeypatch
):
    """Every run verifies the repository baseline, not only the candidate's own tests.

    The end-to-end replay cannot run in an environment that never receives
    credentials, so the sandbox's only offline verification is the test run: it
    has to cover the shared surfaces a patch can reach, and it must not shrink
    to whatever the candidate chose to name.
    """
    monkeypatch.setattr(sandbox_module, "REGRESSION_TEST_PATHS", PROBE_TESTS)
    runner = make_runner()
    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )

    assert report.tests_passed is True
    command = runner.of("pytest")[0].command
    for path in PROBE_TESTS:
        assert path in command


def test_a_failing_regression_test_rejects_the_candidate(sandbox_repo: Path, make_runner):
    """A regression outside the candidate's own targets is still a rejection."""
    failing = "tests/test_jev_passage_policy.py::test_selection_carries_the_question_set_version_of_its_judgments"
    make_runner(
        pytest_returncode=1,
        pytest_stdout=f"FAILED {failing} - AssertionError: assert '' == 'passages-v1'\n1 failed, 5 passed in 0.11s\n",
    )

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=False
    )

    assert report.tests_passed is False
    assert report.failed_tests == (failing,)
    status, detail = sandbox_gate_outcome(report)
    assert status == "rejected"
    assert "test_jev_passage_policy" in detail


def test_every_fixed_regression_path_exists_in_this_repository():
    """The net names real files: a typo would silently verify nothing."""
    missing = [path for path in REGRESSION_TEST_PATHS if not (REPO_ROOT / path).is_file()]
    assert missing == []


def test_the_net_is_empty_only_where_the_repository_does_not_carry_it(sandbox_repo: Path):
    """A synthetic repository runs its targets alone; the real one runs the net."""
    assert sandbox_module._existing_regression_paths(sandbox_repo) == []
    assert sandbox_module._existing_regression_paths(REPO_ROOT) == list(REGRESSION_TEST_PATHS)


# ------------------------------------------------------------------
# A replay that measured nothing is not evidence.
# ------------------------------------------------------------------


def test_a_replay_that_never_prepared_anything_is_not_evidence(sandbox_repo: Path, make_runner):
    """Two empty runs must never read as "no regression".

    A knowledge-base request without a principal is answered by the enterprise
    guard before retrieval, in microseconds and without a recorded failure: both
    sides then report identical metrics, which the layer-2 comparison would
    otherwise call a pass.
    """
    make_runner(benchmark=_benchmark_payload(prep_p50_ms=0.0))

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.tests_passed is True
    assert report.benchmark is None
    assert "measured nothing" in report.error
    status, detail = sandbox_gate_outcome(report)
    assert status == "awaiting_verification"
    assert "replay unavailable" in detail


def test_a_replay_whose_baseline_failed_is_not_evidence(sandbox_repo: Path, make_runner):
    """A baseline that failed some calls cannot serve as the comparison base."""
    make_runner(benchmark=_benchmark_payload(baseline_failed_calls=3))

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.benchmark is None
    assert "baseline" in report.error


def test_a_replay_carrying_no_preparation_latency_is_not_evidence(sandbox_repo: Path, make_runner):
    """A report that omits the latency cannot show the path ever ran."""
    payload = _benchmark_payload()
    for side in ("old", "new"):
        payload[side].pop("prep_p50_ms")
    make_runner(benchmark=payload)

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.benchmark is None
    assert "no preparation latency" in report.error


def test_a_replay_that_measured_no_samples_is_not_evidence(sandbox_repo: Path, make_runner):
    payload = _benchmark_payload(cases=0)
    payload["sample_count"] = 0
    make_runner(benchmark=payload)

    report = run_patch_sandbox(
        make_candidate(sandbox_repo), repo_root=sandbox_repo, run_benchmark=True
    )

    assert report.benchmark is None
    assert "no samples" in report.error


# ------------------------------------------------------------------
# 基准回放的只读语料（层 2/3 能证明"无回归"的前提）。
# ------------------------------------------------------------------


def test_the_replay_env_carries_the_read_only_corpus_snapshot(tmp_path):
    """配了快照且目录存在时，**只有回放**拿到 RAG_QDRANT_PATH。

    沙箱子进程会剥掉所有 RAG_* 设置以保持隔离，于是回放此前拿不到语料、只能报
    "replay unavailable"——层 2/3 因此只能证明"目标测试通过"，不能证明"无回归"。
    """
    from rag.evolution.sandbox import _replay_env, _sandbox_env

    worktree = tmp_path / "wt"
    worktree.mkdir()
    snapshot = tmp_path / "snap"
    snapshot.mkdir()

    replay_env = _replay_env(worktree, str(snapshot))

    assert replay_env["RAG_QDRANT_PATH"] == str(snapshot)
    assert replay_env["PYTHONPATH"] == str(worktree)  # 其余隔离语义不变
    # 目标测试仍跑在剥离后的真空环境里，只有回放这一条命令放宽读取。
    assert "RAG_QDRANT_PATH" not in _sandbox_env(worktree)


def test_the_replay_env_omits_a_missing_or_unconfigured_snapshot(tmp_path):
    """没配置、或配置了但目录不存在时不得注入：宁可报"回放不可用"，也不要假通过。"""
    from rag.evolution.sandbox import _replay_env

    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert "RAG_QDRANT_PATH" not in _replay_env(worktree, None)
    assert "RAG_QDRANT_PATH" not in _replay_env(worktree, "")
    assert "RAG_QDRANT_PATH" not in _replay_env(worktree, str(tmp_path / "nope"))


def test_the_replay_env_hands_over_the_acl_database_too(tmp_path):
    """快照目录里的 users.db 也要交给回放：没有主体时回放的指标全是空转值。"""
    from rag.evolution.sandbox import _replay_env

    worktree = tmp_path / "wt"
    worktree.mkdir()
    snapshot = tmp_path / "data"
    (snapshot / "qdrant_data").mkdir(parents=True)
    (snapshot / "users.db").write_bytes(b"")

    env = _replay_env(worktree, str(snapshot))

    assert env["RAG_QDRANT_PATH"] == str(snapshot / "qdrant_data")
    assert env["RAG_USERS_DB_PATH"] == str(snapshot / "users.db")
    assert "RAG_MEMORY_DB_PATH" not in env  # 目录里没有就不注入


def test_the_replay_command_replays_as_a_principal():
    """配置了主体才传 --user-id：没主体时实测 prep P50=0.0ms、route 0.15（空转）。"""
    from rag.evolution.sandbox import _benchmark_command

    plain = _benchmark_command(kb_id="kb1", json_out=Path("x.json"))
    assert "--user-id" not in plain

    with_principal = _benchmark_command(kb_id="kb1", json_out=Path("x.json"), user_id=1)
    assert with_principal[with_principal.index("--user-id") + 1] == "1"


def test_a_failed_threshold_exit_still_yields_a_verdict(tmp_path, monkeypatch):
    """runner 因**自身阈值**不达标而 exit 1 时，要以报告文件为准给出门禁结论。

    否则层 2/3 永远读不到结论（这正是"回放不可用"长期存在的另一半原因）。
    """
    from rag.evolution import sandbox as sandbox_module

    def fake_run_command(command, *, cwd=None, env=None, timeout=None, stdin_text=None):
        path = Path(command[command.index("--json-out") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_benchmark_payload()), encoding="utf-8")
        return sandbox_module.CommandOutcome(returncode=1, stderr="thresholds failed")

    monkeypatch.setattr(sandbox_module, "_run_command", fake_run_command)

    payload, error = sandbox_module._run_benchmark(
        worktree=tmp_path, env={}, sandbox_root=tmp_path, kb_id="kb1", timeout=10.0, user_id=1
    )

    assert error == ""
    assert payload is not None
    verdict = sandbox_module.patch_verdict(payload, payload)
    assert verdict is not None and "passed" in verdict


def test_an_exit_without_a_report_is_still_an_error(tmp_path, monkeypatch):
    """没有报告文件时，非零退出仍是"没跑完"——不能让缺证据变成结论。"""
    from rag.evolution import sandbox as sandbox_module

    def fake_run_command(command, *, cwd=None, env=None, timeout=None, stdin_text=None):
        return sandbox_module.CommandOutcome(returncode=1, stderr="boom")

    monkeypatch.setattr(sandbox_module, "_run_command", fake_run_command)

    payload, error = sandbox_module._run_benchmark(
        worktree=tmp_path, env={}, sandbox_root=tmp_path, kb_id="kb1", timeout=10.0
    )

    assert payload is None
    assert error == "benchmark replay did not complete: exit_1"


def test_the_replay_command_can_bound_its_repetitions():
    """回放次数可调：带凭据时默认 5 次重复每侧要十几分钟，回归检查用 2 次即可。"""
    from rag.evolution.sandbox import _benchmark_command

    plain = _benchmark_command(kb_id="kb1", json_out=Path("x.json"))
    assert "--runs" not in plain and "--warmups" not in plain  # 沙箱默认不变

    bounded = _benchmark_command(kb_id="kb1", json_out=Path("x.json"), runs=2, warmups=0)
    assert bounded[bounded.index("--runs") + 1] == "2"
    assert bounded[bounded.index("--warmups") + 1] == "0"


# ------------------------------------------------------------------
# 黄金回放 fixture：只给回放注入，且过期一律判为不可用（绝不给出通过的结论）。
# ------------------------------------------------------------------


def _fresh_fixture(tmp_path, worktree: Path) -> Path:
    from rag.evolution.replay_fixture import ReplayFixture, seam_hashes

    path = tmp_path / "fixture.json"
    ReplayFixture(
        embeddings={}, reranks={}, routes={}, rewrites={},
        corpus="corpus", base_commit="a" * 40, seams=seam_hashes(worktree), created_at=1.0,
    ).save(path)
    return path


def test_the_replay_env_carries_the_fixture_and_the_staleness_check_accepts_it(tmp_path):
    from rag.evolution.sandbox import _replay_env, fixture_staleness

    worktree = tmp_path / "wt"
    (worktree / "rag").mkdir(parents=True)
    for name in ("embedder", "reranker", "agent", "query_rewriter"):
        (worktree / "rag" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    fixture = _fresh_fixture(tmp_path, worktree)

    assert fixture_staleness(str(fixture), worktree) == ""
    env = _replay_env(worktree, None, str(fixture))
    assert env["RAG_EVOLUTION_REPLAY_FIXTURE"] == str(fixture)
    # 不配置时不注入：默认部署的回放行为（方案 C）不变。
    assert "RAG_EVOLUTION_REPLAY_FIXTURE" not in _replay_env(worktree, None)


def test_a_patched_seam_makes_the_fixture_stale(tmp_path):
    """改过 seam 模块的补丁 ⇒ 录下的答案描述的是**另一份代码**，必须判过期。"""
    from rag.evolution.sandbox import fixture_staleness

    worktree = tmp_path / "wt"
    (worktree / "rag").mkdir(parents=True)
    for name in ("embedder", "reranker", "agent", "query_rewriter"):
        (worktree / "rag" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    fixture = _fresh_fixture(tmp_path, worktree)

    (worktree / "rag" / "reranker.py").write_text("x = 2  # patched\n", encoding="utf-8")

    reason = fixture_staleness(str(fixture), worktree)
    assert "seam modules changed" in reason and "reranker" in reason
    assert fixture_staleness(str(tmp_path / "absent.json"), worktree) == "fixture file not found"


def test_a_stale_fixture_makes_the_replay_unavailable_rather_than_passing(
    sandbox_repo: Path, make_runner, tmp_path
):
    """过期 fixture 不得让门禁通过：报告 benchmark=None + 明确原因，状态仍待人工。"""
    from rag.evolution.sandbox import sandbox_gate_outcome

    run_worktree = sandbox_repo  # fixture 的 seam 哈希取自这里，随后故意改一个模块
    (run_worktree / "rag").mkdir(exist_ok=True)
    for name in ("embedder", "reranker", "agent", "query_rewriter"):
        (run_worktree / "rag" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    fixture = _fresh_fixture(tmp_path, run_worktree)
    (run_worktree / "rag" / "reranker.py").write_text("x = 2\n", encoding="utf-8")

    make_runner(benchmark=_benchmark_payload())
    report = run_patch_sandbox(
        make_candidate(sandbox_repo),
        repo_root=sandbox_repo,
        run_benchmark=True,
        replay_fixture=str(fixture),
    )

    assert report.applied is True
    assert report.tests_passed is True
    assert report.benchmark is None
    assert "fixture unusable" in report.error
    status, detail = sandbox_gate_outcome(report)
    assert status == "awaiting_verification"
    assert "replay unavailable" in detail


# ------------------------------------------------------------------
# 结构化 A/B 指标与受支持的写回（此前结论没有生产写回者）。
# ------------------------------------------------------------------


class _RecordingStore:
    """只记录 advance_candidate 的调用，用来断言写回把什么交给了状态机。"""

    def __init__(self):
        self.calls: list[dict] = []

    def advance_candidate(self, candidate_id, new_status, **kwargs):
        self.calls.append({"candidate_id": candidate_id, "status": new_status, **kwargs})
        return {"status": new_status, "gate_log": [{"gate": kwargs.get("gate")}]}


def test_sandbox_metrics_carry_the_side_by_side_numbers():
    from rag.evolution.sandbox import sandbox_gate_metrics

    report = SandboxReport(
        applied=True,
        tests_passed=True,
        benchmark=_benchmark_payload(),
        failed_tests=(),
        error="",
    )

    metrics = sandbox_gate_metrics(report)

    assert metrics["applied"] is True
    assert metrics["tests_passed"] is True
    verdict = metrics["verdict"]
    assert verdict["passed"] is True
    assert set(verdict["sides"])
    assert "route_delta_pp" in verdict and "top5_delta_pp" in verdict


def test_sandbox_metrics_say_when_there_is_no_comparison():
    """有报告但不可比 ⇒ 显式标出来，别让"没有数字"看起来像"没有变差"。"""
    from rag.evolution.sandbox import sandbox_gate_metrics

    metrics = sandbox_gate_metrics(
        SandboxReport(applied=True, tests_passed=True, benchmark={"note": "不可比"}, failed_tests=())
    )

    assert "verdict" not in metrics
    assert metrics["verdict_unavailable"] is True

    # 回放**根本没跑完**（benchmark=None）也要显式标注：否则审查台上"没有数字"看起来像"没有变差"。
    none_report = sandbox_gate_metrics(
        SandboxReport(applied=True, tests_passed=True, benchmark=None, failed_tests=())
    )
    assert "verdict" not in none_report
    assert none_report["verdict_unavailable"] is True


def test_record_sandbox_outcome_writes_status_detail_and_numbers():
    from rag.evolution.sandbox import GATE_NAME, record_sandbox_outcome

    store = _RecordingStore()
    report = SandboxReport(
        applied=True, tests_passed=True, benchmark=_benchmark_payload(), failed_tests=(), error=""
    )

    record_sandbox_outcome(store, 7, report, actor_id="admin:cli", note="跑过沙箱")

    call = store.calls[0]
    assert call["candidate_id"] == 7
    assert call["gate"] == GATE_NAME
    assert call["gate_detail"]
    assert call["gate_metrics"]["verdict"]["passed"] is True
