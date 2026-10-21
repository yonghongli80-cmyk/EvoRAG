"""Repeatable old-versus-new preparation benchmark for the Jev retrieval path.

Compares the legacy preparation path against the Jev preparation path on the same labeled
cases, alternating run order to reduce time-of-day bias. Answer generation is not part of the
preparation acceptance numbers; optional end-to-end timings are recorded separately.

Usage:
    python scripts/benchmark_jev_retrieval.py \
        --cases benchmarks/jev_retrieval_cases.json \
        --runs 5 --warmups 1 \
        --json-out artifacts/jev-benchmark.json \
        --markdown-out artifacts/jev-benchmark.md

Secrets: the benchmark never prints credentials, question text is redacted unless
``--include-questions`` is supplied, and reports contain no chunk text.
"""

import argparse
import json
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Expected route labels map to the routing domain actually produced by each path.
ROUTE_EXPECTATIONS = {
    "rag": "rag",
    "structured": "structured",
    "tool": "tool",
    "clarification": "clarification",
}

#: 答案位置：接住这个请求之后**该不该作答**。与 ``expected_route`` 正交——路由说"哪条链路"，
#: 位置说"有没有给出答案"。产品口径（2026-10-19）：证据不足就拒答是正确行为；因此
#: 该作答却拒答（假拒答）与该拒答却作答（幻觉风险）都必须能被记成错，而在此之前
#: 基准里没有任何一档表达它（``ROUTE_EXPECTATIONS`` 只有四个链路标签）。
ANSWER_POSITIONS = ("answer", "refuse")

#: 只有这几种判定算"给出了答案"。``clarification_required``/``permission_limited`` 都是
#: 没有答案的固定文案；``answerable_with_warning`` 是有历史证据时的限定回答，仍算作答。
_ANSWERED_KINDS = frozenset({"answerable", "answerable_with_warning"})

_REQUIRED_CASE_FIELDS = ("id", "question", "expected_route", "kb_id")


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    question: str
    expected_route: str
    kb_id: str
    expected_source_names: tuple[str, ...] = ()
    notes: str = ""
    category: str = "ordinary"
    #: 接住请求之后该不该作答：``"answer"`` / ``"refuse"`` / ``""``（不记分）。
    #: 缺省空串让既有用例逐字节仍然合法；一旦给出，就必须是本模块认识的取值。
    expected_position: str = ""
    #: 多轮用例的**先行轮次**，按时间顺序：`(("user", "…"), ("assistant", "…"), …)`。
    #: 空元组 = 单轮（既有 20 例全是这种，所以旧用例文件逐字节仍然合法）。
    #: 它测的是"指代补全"：问题本身没有先行词（"它呢？"），能不能答对取决于前面那几轮。
    history: tuple[tuple[str, str], ...] = ()


#: 用例类别。``multi_turn`` 是 2026-10-12 加的：既有 20 例全是单轮，而"评测分辨率"这个结构性
#: 瓶颈的一部分正是**一轮一个样本**——多轮是真实流量里最主要的形态，也是唯一能测出
#: `query_rewrite_history_turns` 有没有用的形态。
CASE_CATEGORIES = ("ordinary", "special", "multi_turn")

#: 验收档案：**哪几条判据适用于这一批用例**（阈值本身一个字不动）。
#: 单轮是既有那套；多轮把时延与改写率降为 advisory —— 在"必须靠历史补全指代"的用例上，
#: 两侧都会改写（实测 old 1.000 / new 1.000），那两条阈值描述的比较已经不存在了。
ACCEPTANCE_SINGLE_TURN = "single_turn"
ACCEPTANCE_MULTI_TURN = "multi_turn"
ACCEPTANCE_PROFILES = (ACCEPTANCE_SINGLE_TURN, ACCEPTANCE_MULTI_TURN)

#: 历史轮次里允许的角色。``system``/``tool`` 刻意不收：基准要复现的是**用户与助手**的对话轨迹，
#: 别的角色会引入与"指代补全"无关的变量。
HISTORY_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class PreparationOutcome:
    """What one preparation call produced; produced by the caller-supplied callables."""

    preparation_ms: float
    route: str
    source_names: tuple[str, ...] = ()
    rewrite_called: bool = False
    external_calls: int = 0
    end_to_end_ms: float | None = None
    failure: str | None = None
    #: ``prepared["decision"].kind``：这条请求最后**有没有给出答案**。``None`` = 没有判定
    #: （旧路径不建 evidence bundle，崩溃的请求也没有），此时"该不该作答"不记分。
    answerability: str | None = None


@dataclass(frozen=True)
class Sample:
    case_id: str
    path: str
    category: str
    preparation_ms: float
    end_to_end_ms: float | None
    route: str
    route_correct: bool
    top1_hit: bool | None
    top5_hit: bool | None
    rewrite_called: bool
    external_calls: int
    failure: str | None
    #: 该次采样的判定：``answerable`` 等；``None`` = 没有判定可读。
    answerability: str | None = None
    #: 用例期望的位置，与 ``BenchmarkCase.expected_position`` 同义，便于聚合时逐条比较。
    expected_position: str = ""
    #: 这次请求是否真的给出了答案（由 :attr:`answerability` 折算）。
    answered: bool = False
    #: 已知"没有给出答案"、且这是设计上的正确形态（结构化澄清的追问）。
    #: 它和"判定说 insufficient_evidence"一样不计作答，但必须出现在分母里——否则
    #: 一次追问会被错误地留在分母之外，把假拒答统计成"没测"。
    known_unanswered: bool = False
    #: 用例带 ``expected_position`` 时才有：位置是否与期望一致（否则 ``None``，不进分母）。
    position_correct: bool | None = None


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float


@dataclass(frozen=True)
class PathMetrics:
    sample_count: int
    prep_p50_ms: float
    prep_p95_ms: float
    rewrite_rate: float
    route_accuracy: float
    top1_hit_rate: float
    top5_hit_rate: float
    external_calls_per_query: float
    failed_calls: int
    route_accuracy_by_category: dict[str, float] = field(default_factory=dict)
    ordinary_rewrite_rate: float = 0.0
    ordinary_sample_count: int = 0
    end_to_end_p50_ms: float | None = None
    end_to_end_p95_ms: float | None = None
    #: 带 ``expected_position`` 的采样数。0 = 这批用例没有答案位置标签，位置判据不适用。
    refusal_case_count: int = 0
    #: 期望拒答却作答：幻觉风险，一律算错。
    refusal_escapes: int = 0
    #: 期望作答却拒答：假拒答，一律算错。
    refusal_misses: int = 0
    position_accuracy: float = 0.0


@dataclass(frozen=True)
class CasePathBreakdown:
    """Per-case, per-path aggregates used to inspect failures case by case."""

    case_id: str
    category: str
    expected_route: str
    run_count: int
    route_correct_count: int
    rewrite_count: int
    external_calls_total: int
    prep_p50_ms: float
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseBreakdown:
    case_id: str
    category: str
    expected_route: str
    old: CasePathBreakdown
    new: CasePathBreakdown


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str
    #: True for criteria the profile reports but does not enforce (multi-turn p50/p95/rewrite).
    #: Rendering must not present them as PASS: a check whose detail says "8875.3ms <= 5415.8ms"
    #: while being marked PASS reads as "the slower path passed" — measured 2026-10-19.
    advisory: bool = False


@dataclass(frozen=True)
class AcceptanceVerdict:
    passed: bool
    failures: tuple[str, ...]
    checks: tuple[AcceptanceCheck, ...]


@dataclass(frozen=True)
class BenchmarkReport:
    cases: int
    warmups: int
    runs_per_path: int
    sample_count: int
    old: PathMetrics
    new: PathMetrics
    verdict: AcceptanceVerdict
    failures: tuple[str, ...] = ()
    case_breakdowns: tuple[CaseBreakdown, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "warmups": self.warmups,
            "runs_per_path": self.runs_per_path,
            "sample_count": self.sample_count,
            "old": _metrics_to_dict(self.old),
            "new": _metrics_to_dict(self.new),
            "acceptance": {
                "passed": self.verdict.passed,
                "failures": list(self.verdict.failures),
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "advisory": check.advisory,
                        "detail": check.detail,
                    }
                    for check in self.verdict.checks
                ],
            },
            "failures": list(self.failures),
            "case_breakdowns": [
                {
                    "case_id": item.case_id,
                    "category": item.category,
                    "expected_route": item.expected_route,
                    "old": _case_path_to_dict(item.old),
                    "new": _case_path_to_dict(item.new),
                }
                for item in self.case_breakdowns
            ],
        }


def _case_path_to_dict(item: CasePathBreakdown) -> dict[str, Any]:
    return {
        "run_count": item.run_count,
        "route_correct_count": item.route_correct_count,
        "rewrite_count": item.rewrite_count,
        "external_calls_total": item.external_calls_total,
        "prep_p50_ms": item.prep_p50_ms,
        "failures": list(item.failures),
    }


def _metrics_to_dict(metrics: PathMetrics) -> dict[str, Any]:
    return {
        "sample_count": metrics.sample_count,
        "prep_p50_ms": metrics.prep_p50_ms,
        "prep_p95_ms": metrics.prep_p95_ms,
        "rewrite_rate": metrics.rewrite_rate,
        "ordinary_rewrite_rate": metrics.ordinary_rewrite_rate,
        "ordinary_sample_count": metrics.ordinary_sample_count,
        "route_accuracy": metrics.route_accuracy,
        "route_accuracy_by_category": dict(metrics.route_accuracy_by_category),
        "top1_hit_rate": metrics.top1_hit_rate,
        "top5_hit_rate": metrics.top5_hit_rate,
        "external_calls_per_query": metrics.external_calls_per_query,
        "failed_calls": metrics.failed_calls,
        "end_to_end_p50_ms": metrics.end_to_end_p50_ms,
        "end_to_end_p95_ms": metrics.end_to_end_p95_ms,
        "refusal_case_count": metrics.refusal_case_count,
        "refusal_escapes": metrics.refusal_escapes,
        "refusal_misses": metrics.refusal_misses,
        "position_accuracy": metrics.position_accuracy,
    }


def load_cases(path: str | Path) -> tuple[BenchmarkCase, ...]:
    """Load and validate the deterministic case set; secrets never belong in it."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = raw["cases"] if isinstance(raw, dict) else raw
    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    for entry in entries:
        for required in _REQUIRED_CASE_FIELDS:
            if not entry.get(required):
                raise ValueError(f"benchmark case missing required field: {required}")
        case_id = entry["id"]
        if case_id in seen:
            raise ValueError(f"duplicate benchmark case id: {case_id}")
        seen.add(case_id)
        if entry["expected_route"] not in ROUTE_EXPECTATIONS:
            raise ValueError(f"unknown expected_route for {case_id}: {entry['expected_route']}")
        category = entry.get("category", "ordinary")
        if category not in CASE_CATEGORIES:
            raise ValueError(f"unknown category for {case_id}: {category}")
        position = entry.get("expected_position") or ""
        if position and position not in ANSWER_POSITIONS:
            raise ValueError(f"unknown expected_position for {case_id}: {position}")
        cases.append(
            BenchmarkCase(
                id=case_id,
                question=entry["question"],
                expected_route=entry["expected_route"],
                kb_id=entry["kb_id"],
                expected_source_names=tuple(entry.get("expected_source_names") or ()),
                notes=entry.get("notes", ""),
                category=category,
                expected_position=position,
                history=_load_history(case_id, entry.get("history")),
            )
        )
    if not cases:
        raise ValueError("benchmark case set is empty")
    return tuple(cases)


def _load_history(case_id: str, raw: object) -> tuple[tuple[str, str], ...]:
    """Validate one case's prior turns.

    A malformed history is rejected at load time rather than becoming a mysterious preparation
    result later: the whole point of these cases is that the follow-up question cannot be answered
    from its own words, so a history that silently did not load would make the case measure nothing
    while still reporting a number.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"history for {case_id} must be a list of {{\"role\", \"content\"}} entries")
    history: list[tuple[str, str]] = []
    for index, turn in enumerate(raw):
        if not isinstance(turn, dict):
            raise ValueError(f"history[{index}] for {case_id} must be an object")
        role = str(turn.get("role") or "").strip()
        content = str(turn.get("content") or "").strip()
        if role not in HISTORY_ROLES:
            raise ValueError(
                f"history[{index}] for {case_id} has role {role!r} (expected one of {HISTORY_ROLES})"
            )
        if not content:
            raise ValueError(f"history[{index}] for {case_id} has empty content")
        history.append((role, content))
    if history and history[-1][0] != "assistant":
        # 一个多轮用例的最后一轮必须是助手：跟随其后的就是被测的那个问题。
        raise ValueError(f"history for {case_id} must end with an assistant turn")
    return tuple(history)


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile over unsorted samples; empty input is rejected."""
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0.0 <= p <= 1.0:
        raise ValueError("percentile p must be within [0, 1]")
    ordered = sorted(values)
    rank = min(max(math.ceil(p * len(ordered)), 1), len(ordered))
    return float(ordered[rank - 1])


def summarize(values: Sequence[float]) -> LatencySummary:
    """Summarize a latency sample list; empty input is rejected."""
    if not values:
        raise ValueError("summarize requires at least one sample")
    ordered = sorted(float(value) for value in values)
    return LatencySummary(
        count=len(ordered),
        p50_ms=percentile(ordered, 0.50),
        p95_ms=percentile(ordered, 0.95),
        min_ms=ordered[0],
        max_ms=ordered[-1],
        mean_ms=sum(ordered) / len(ordered),
    )


def _rate(hits: int, total: int) -> float:
    return (hits / total) if total else 0.0


def compute_metrics(samples: Sequence[Sample]) -> PathMetrics:
    """Aggregate one path's samples; failed calls are counted, never timed."""
    successful = [sample for sample in samples if sample.failure is None]
    preparation = [sample.preparation_ms for sample in successful]
    end_to_end = [
        sample.end_to_end_ms for sample in successful if sample.end_to_end_ms is not None
    ]
    with_sources = [sample for sample in successful if sample.top5_hit is not None]
    top1_values = [sample for sample in with_sources if sample.top1_hit is not None]

    by_category: dict[str, list[Sample]] = {}
    for sample in successful:
        by_category.setdefault(sample.category, []).append(sample)

    ordinary = [sample for sample in successful if sample.category == "ordinary"]
    # 答案位置只统计**有判定可读的成功采样**：崩溃的请求已经由 route 判据算错，不能
    # 再被重复记成"假拒答"；没有判定（旧路径）的采样既不进分子也不进分母。
    scored_position = [sample for sample in successful if sample.position_correct is not None]
    # 期望拒答却作答 = 幻觉风险；期望作答却拒答 = 假拒答。两者都必须能被分开看见，
    # 所以它们由 ``Sample.expected_position`` 与 ``Sample.answered`` 直接比较得出，
    # 而不是从 "answerability 是否为 None" 反推。
    escapes = sum(1 for sample in scored_position if sample.expected_position == "refuse" and sample.answered)
    misses = sum(1 for sample in scored_position if sample.expected_position == "answer" and not sample.answered)
    if preparation:
        latency = summarize(preparation)
        p50_ms, p95_ms = latency.p50_ms, latency.p95_ms
    else:
        p50_ms = p95_ms = 0.0

    return PathMetrics(
        sample_count=len(samples),
        prep_p50_ms=p50_ms,
        prep_p95_ms=p95_ms,
        rewrite_rate=_rate(sum(1 for s in successful if s.rewrite_called), len(successful)),
        ordinary_rewrite_rate=_rate(sum(1 for s in ordinary if s.rewrite_called), len(ordinary)),
        ordinary_sample_count=len(ordinary),
        route_accuracy=_rate(sum(1 for s in successful if s.route_correct), len(successful)),
        top1_hit_rate=_rate(sum(1 for s in top1_values if s.top1_hit), len(top1_values)),
        top5_hit_rate=_rate(sum(1 for s in with_sources if s.top5_hit), len(with_sources)),
        external_calls_per_query=(
            sum(s.external_calls for s in successful) / len(successful) if successful else 0.0
        ),
        failed_calls=len(samples) - len(successful),
        route_accuracy_by_category={
            category: _rate(sum(1 for s in group if s.route_correct), len(group))
            for category, group in sorted(by_category.items())
        },
        end_to_end_p50_ms=summarize(end_to_end).p50_ms if end_to_end else None,
        end_to_end_p95_ms=summarize(end_to_end).p95_ms if end_to_end else None,
        refusal_case_count=len(scored_position),
        refusal_escapes=escapes,
        refusal_misses=misses,
        position_accuracy=_rate(sum(1 for s in scored_position if s.position_correct), len(scored_position)),
    )


def evaluate_acceptance(
    old: PathMetrics, new: PathMetrics, *, profile: str = ACCEPTANCE_SINGLE_TURN
) -> AcceptanceVerdict:
    """Apply the fixed acceptance thresholds; thresholds are never weakened to pass.

    The rewrite-reduction criterion targets ordinary questions, so it is evaluated on the
    ordinary-category samples while overall rewrite rates stay in the detail line.

    ``profile`` selects **which criteria apply to this case set**, never how tight they are
    (2026-10-13): the single-turn profile is unchanged, and the multi-turn one keeps ``executed``,
    ``route`` and ``top5`` as hard criteria while reporting p50/p95/rewrite as **advisory**——
    they stay visible in the report but cannot fail it. The reason is structural, not a relaxation:
    resolving an anaphor requires reading history and rewriting, so in a multi-turn set both paths
    rewrite every case by construction and the -30%/-20%/-70% single-turn targets describe a
    comparison that no longer exists (measured: old 1.000 / new 1.000 rewrite rate). Treating them
    as gates would fail the set for doing exactly what it is there to do.
    """
    if profile not in ACCEPTANCE_PROFILES:
        raise ValueError(f"unknown acceptance profile: {profile!r} (expected one of {ACCEPTANCE_PROFILES})")
    multi_turn = profile == ACCEPTANCE_MULTI_TURN
    p50_target = old.prep_p50_ms * 0.70
    p95_target = old.prep_p95_ms * 0.80
    old_ordinary = old.ordinary_rewrite_rate if old.ordinary_sample_count else old.rewrite_rate
    new_ordinary = new.ordinary_rewrite_rate if new.ordinary_sample_count else new.rewrite_rate
    if old_ordinary > 0:
        rewrite_target = old_ordinary * 0.30
        rewrite_ok = new_ordinary <= rewrite_target
    else:
        rewrite_target = 0.0
        rewrite_ok = new_ordinary == 0.0
    top5_floor = old.top5_hit_rate - 0.02
    executed = old.prep_p50_ms > 0.0 and new.prep_p50_ms > 0.0

    def advisory(check: AcceptanceCheck) -> AcceptanceCheck:
        """Report the number without letting it decide (multi-turn profile only).

        ``passed`` keeps the measured comparison so the JSON stays informative, but ``advisory``
        marks it as non-deciding and the renderer prints ``[ADVISORY]`` instead of ``[PASS]``. The
        detail is phrased as "observed vs target" rather than "observed <= target", because the
        latter states a falsehood whenever the target is missed.
        """
        if not multi_turn:
            return check
        return AcceptanceCheck(check.name, check.passed, check.detail, advisory=True)

    checks = (
        AcceptanceCheck(
            "executed",
            executed,
            (
                f"preparation path ran on both sides (prep P50 {old.prep_p50_ms:.1f}ms / "
                f"{new.prep_p50_ms:.1f}ms)"
                if executed
                else (
                    f"preparation path never ran (old {old.prep_p50_ms:.1f}ms, "
                    f"new {new.prep_p50_ms:.1f}ms): a knowledge-base run is the enterprise "
                    "path and needs --user-id, otherwise every preparation is answered "
                    "before retrieval"
                )
            ),
        ),
        advisory(
            AcceptanceCheck(
                "p50",
                new.prep_p50_ms <= p50_target,
                f"prep P50 {new.prep_p50_ms:.1f}ms vs target {p50_target:.1f}ms (old {old.prep_p50_ms:.1f}ms, need -30%)",
            )
        ),
        advisory(
            AcceptanceCheck(
                "p95",
                new.prep_p95_ms <= p95_target,
                f"prep P95 {new.prep_p95_ms:.1f}ms vs target {p95_target:.1f}ms (old {old.prep_p95_ms:.1f}ms, need -20%)",
            )
        ),
        advisory(
            AcceptanceCheck(
                "rewrite",
                rewrite_ok,
                f"ordinary rewrite rate {new_ordinary:.3f} vs target {rewrite_target:.3f} (old {old_ordinary:.3f}, need -70%; overall {new.rewrite_rate:.3f} vs {old.rewrite_rate:.3f})",
            )
        ),
        AcceptanceCheck(
            "route",
            new.route_accuracy >= 0.95,
            f"route accuracy {new.route_accuracy:.3f} vs required 0.950",
        ),
        AcceptanceCheck(
            "top5",
            new.top5_hit_rate >= top5_floor,
            f"Top-5 hit rate {new.top5_hit_rate:.3f} vs floor {top5_floor:.3f} (old {old.top5_hit_rate:.3f}, max loss 2pp)",
        ),
        _refusal_check(new),
    )
    failures = tuple(check.name for check in checks if not check.passed and not check.advisory)
    return AcceptanceVerdict(passed=not failures, failures=failures, checks=checks)


def _refusal_check(new: PathMetrics) -> AcceptanceCheck:
    """答案位置判据：该拒答却作答、该作答却拒答，都不允许出现。

    产品口径（2026-10-19）是"证据不足就拒答"。这两个计数把这条口径变成可验收的东西，
    但它们只在用例真的带了 ``expected_position`` 时才有意义：一条都没有时判据是
    **advisory**（报告里能看到，但不能因为它失败），因为"没测"不等于"测过且通过"。
    """
    if new.refusal_case_count == 0:
        return AcceptanceCheck(
            "refusal",
            True,
            "no case carries expected_position; answer position not scored in this set",
            advisory=True,
        )
    detail = (
        f"answer position {new.position_accuracy:.3f} over {new.refusal_case_count} sample(s); "
        f"answered where a refusal was required: {new.refusal_escapes}, "
        f"refused where an answer was required: {new.refusal_misses}"
    )
    return AcceptanceCheck(
        "refusal",
        new.refusal_escapes == 0 and new.refusal_misses == 0,
        detail,
    )


def _answered(answerability: str | None) -> bool:
    """Did this request actually produce an answer?

    ``clarification_required`` and ``permission_limited`` are fixed copy that give **no** answer,
    so they are not answering. ``answerable_with_warning`` does answer (with historical evidence).
    """
    return answerability in _ANSWERED_KINDS


def _sample_from(case: BenchmarkCase, path: str, outcome: PreparationOutcome) -> Sample:
    expected_domain = ROUTE_EXPECTATIONS[case.expected_route]
    position = case.expected_position
    if outcome.failure is not None:
        return Sample(
            case_id=case.id,
            path=path,
            category=case.category,
            preparation_ms=0.0,
            end_to_end_ms=None,
            route="error",
            route_correct=False,
            top1_hit=None,
            top5_hit=None,
            rewrite_called=False,
            external_calls=0,
            failure=outcome.failure,
        )
    names = tuple(outcome.source_names)
    top1_hit = None
    top5_hit = None
    if case.expected_source_names:
        expected = set(case.expected_source_names)
        top1_hit = bool(names) and names[0] in expected
        top5_hit = any(name in expected for name in names[:5])
    answered = _answered(outcome.answerability)
    # 结构化澄清是"追问"：设计上就没有给出答案，但没有 ``decision`` 可读。
    # 2026-10-19 实测 ``找一下副厂配件明细`` 会落到这里（5 个来源、answerability=None）。
    # 它是已知的未作答，必须进分母；而"既无判定、又不是已知未作答"的形态读不出来，不进分母。
    known_unanswered = outcome.answerability is None and outcome.route == "structured"
    scorable = outcome.answerability is not None or known_unanswered
    # 没有答案位置标签的用例不记位置；有的用例必须有可读的位置判定，否则"读不到"会被
    # 悄悄记成"拒答"或"作答"——那正是这类记分最容易出现偏差的地方。
    if position and not scorable:
        raise ValueError(
            f"case {case.id} carries expected_position={position!r} but the path reported "
            f"no answerability decision and no known-unanswered route (route={outcome.route!r})"
        )
    position_correct = (("answer" if answered else "refuse") == position) if (position and scorable) else None
    return Sample(
        case_id=case.id,
        path=path,
        category=case.category,
        preparation_ms=outcome.preparation_ms,
        end_to_end_ms=outcome.end_to_end_ms,
        route=outcome.route,
        route_correct=outcome.route == expected_domain,
        top1_hit=top1_hit,
        top5_hit=top5_hit,
        rewrite_called=outcome.rewrite_called,
        external_calls=outcome.external_calls,
        failure=None,
        answerability=outcome.answerability,
        expected_position=position,
        answered=answered,
        known_unanswered=known_unanswered,
        position_correct=position_correct,
    )


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    old_prepare: Callable[[BenchmarkCase], PreparationOutcome],
    new_prepare: Callable[[BenchmarkCase], PreparationOutcome],
    *,
    runs: int = 5,
    warmups: int = 1,
    profile: str = ACCEPTANCE_SINGLE_TURN,
) -> BenchmarkReport:
    """Run both preparation paths per case in alternating order and aggregate the report."""
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if warmups < 0:
        raise ValueError("warmups must not be negative")

    samples: list[Sample] = []
    failures: list[str] = []

    def invoke(prepare, case, session_id):
        try:
            return prepare(case, session_id)
        except TypeError:
            # 只接受一个参数的准备函数（既有替身、以及不想管会话的调用方）照旧可用。
            try:
                return prepare(case)
            except Exception as error:  # a failed call is recorded, never silently dropped
                kind = type(error).__name__
                message = str(error).strip().replace("\n", " ")[:200]
                failures.append(f"{case.id}: {kind}: {message}")
                return PreparationOutcome(preparation_ms=0.0, route="error", failure=kind)
        except Exception as error:  # a failed call is recorded, never silently dropped
            kind = type(error).__name__
            message = str(error).strip().replace("\n", " ")[:200]
            failures.append(f"{case.id}: {kind}: {message}")
            return PreparationOutcome(preparation_ms=0.0, route="error", failure=kind)

    for case in cases:
        for warmup_index in range(warmups):
            # 每次调用一个**自己的会话 id**：多轮用例会把历史写进它，共用一个会话就会把上一轮的
            # 问答累积进去，于是第二次调用读到的"历史"里混着上一次的答案。
            for path in ("old", "new"):
                prepare = old_prepare if path == "old" else new_prepare
                invoke(prepare, case, f"benchmark-{case.id}-warmup{warmup_index}-{path}")
        for run_index in range(runs):
            order = ("old", "new") if run_index % 2 == 0 else ("new", "old")
            for path in order:
                prepare = old_prepare if path == "old" else new_prepare
                session_id = f"benchmark-{case.id}-run{run_index}-{path}"
                samples.append(_sample_from(case, path, invoke(prepare, case, session_id)))

    old_metrics = compute_metrics([sample for sample in samples if sample.path == "old"])
    new_metrics = compute_metrics([sample for sample in samples if sample.path == "new"])
    verdict = evaluate_acceptance(old_metrics, new_metrics, profile=profile)
    return BenchmarkReport(
        cases=len(cases),
        warmups=warmups,
        runs_per_path=runs,
        sample_count=len(samples),
        old=old_metrics,
        new=new_metrics,
        verdict=verdict,
        failures=tuple(failures),
        case_breakdowns=tuple(_case_breakdowns(cases, samples)),
    )


def _case_breakdowns(
    cases: Sequence[BenchmarkCase], samples: Sequence[Sample]
) -> list[CaseBreakdown]:
    breakdowns = []
    for case in cases:
        by_path = {
            path: [sample for sample in samples if sample.case_id == case.id and sample.path == path]
            for path in ("old", "new")
        }
        breakdowns.append(
            CaseBreakdown(
                case_id=case.id,
                category=case.category,
                expected_route=case.expected_route,
                old=_case_path_breakdown(case, by_path["old"]),
                new=_case_path_breakdown(case, by_path["new"]),
            )
        )
    return breakdowns


def _case_path_breakdown(case: BenchmarkCase, samples: Sequence[Sample]) -> CasePathBreakdown:
    preparation = [sample.preparation_ms for sample in samples if sample.failure is None]
    return CasePathBreakdown(
        case_id=case.id,
        category=case.category,
        expected_route=case.expected_route,
        run_count=len(samples),
        route_correct_count=sum(1 for sample in samples if sample.route_correct),
        rewrite_count=sum(1 for sample in samples if sample.rewrite_called),
        external_calls_total=sum(sample.external_calls for sample in samples),
        prep_p50_ms=percentile(preparation, 0.50) if preparation else 0.0,
        failures=tuple(sample.failure for sample in samples if sample.failure is not None),
    )


def _escape_markdown(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("`", "\\`")


def render_markdown(
    report: BenchmarkReport,
    *,
    cases: Sequence[BenchmarkCase] = (),
    include_questions: bool = False,
) -> str:
    """Render the Markdown report; question text is redacted unless explicitly included."""
    questions = {case.id: case.question for case in cases}
    lines = [
        "# Jev Retrieval Benchmark",
        "",
        f"- cases: {report.cases}",
        f"- warmups per path: {report.warmups}",
        f"- runs per path: {report.runs_per_path}",
        f"- samples: {report.sample_count}",
        "",
        "## Preparation latency",
        "",
        "| path | P50 (ms) | P95 (ms) | end-to-end P50 (ms) |",
        "| --- | --- | --- | --- |",
        f"| old | {report.old.prep_p50_ms:.1f} | {report.old.prep_p95_ms:.1f} | {_optional(report.old.end_to_end_p50_ms)} |",
        f"| new | {report.new.prep_p50_ms:.1f} | {report.new.prep_p95_ms:.1f} | {_optional(report.new.end_to_end_p50_ms)} |",
        "",
        "## Quality and cost",
        "",
        "| path | route accuracy | Top-1 hit | Top-5 hit | rewrite rate | ordinary rewrite rate | external calls/query | failed |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        f"| old | {report.old.route_accuracy:.3f} | {report.old.top1_hit_rate:.3f} | {report.old.top5_hit_rate:.3f} | {report.old.rewrite_rate:.3f} | {report.old.ordinary_rewrite_rate:.3f} ({report.old.ordinary_sample_count}) | {report.old.external_calls_per_query:.2f} | {report.old.failed_calls} |",
        f"| new | {report.new.route_accuracy:.3f} | {report.new.top1_hit_rate:.3f} | {report.new.top5_hit_rate:.3f} | {report.new.rewrite_rate:.3f} | {report.new.ordinary_rewrite_rate:.3f} ({report.new.ordinary_sample_count}) | {report.new.external_calls_per_query:.2f} | {report.new.failed_calls} |",
        "",
        "## Route accuracy by category (new path)",
        "",
    ]
    if report.new.route_accuracy_by_category:
        for category, accuracy in report.new.route_accuracy_by_category.items():
            lines.append(f"- {category}: {accuracy:.3f}")
    else:
        lines.append("- (no successful samples)")
    if report.case_breakdowns:
        lines.extend(
            [
                "",
                "## Per-case breakdown",
                "",
                "| case | category | expected | old route ok | old rewrites | old P50 (ms) | new route ok | new rewrites | new P50 (ms) |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in report.case_breakdowns:
            lines.append(
                f"| {_escape_markdown(item.case_id)} | {item.category} | {item.expected_route} "
                f"| {item.old.route_correct_count}/{item.old.run_count} | {item.old.rewrite_count} | {item.old.prep_p50_ms:.0f} "
                f"| {item.new.route_correct_count}/{item.new.run_count} | {item.new.rewrite_count} | {item.new.prep_p50_ms:.0f} |"
            )
    lines.extend(["", "## Acceptance", "", f"verdict: {'PASS' if report.verdict.passed else 'FAIL'}", ""])
    for check in report.verdict.checks:
        marker = "ADVISORY" if check.advisory else ("PASS" if check.passed else "FAIL")
        lines.append(f"- [{marker}] {check.name}: {check.detail}")
    if report.failures:
        lines.extend(["", "## Failed calls", ""])
        for failure in report.failures:
            lines.append(f"- {_escape_markdown(failure)}")
    if include_questions and questions:
        lines.extend(["", "## Cases", "", "| id | question |", "| --- | --- |"])
        for case_id, question in questions.items():
            lines.append(f"| {_escape_markdown(case_id)} | {_escape_markdown(question)} |")
    lines.append("")
    return "\n".join(lines)


def _optional(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _ensure_project_root_on_path() -> None:
    """Direct script execution must resolve the project's own packages.

    With ``python scripts/benchmark_jev_retrieval.py`` the script directory comes first on
    ``sys.path`` and ``config``/``rag`` would otherwise resolve to the editable-install copies in
    site-packages, whose relative data paths point outside the repository.
    """
    root = str(Path(__file__).resolve().parents[1])
    # The editable install already appends the project root via a .pth file, so a plain
    # membership check would skip reordering; site-packages holds a stale config.py copy that
    # must not win. Move the project root to the front instead.
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)


def build_live_preparations(
    *,
    kb_id: str,
    memory_db_path: str = ":memory:",
    user_id: int | None = None,
    user_db_path: str | None = None,
) -> tuple[Callable[..., PreparationOutcome], Callable[..., PreparationOutcome]]:
    """Build in-process old/new preparation callables against one cached pipeline per KB.

    The same pipeline serves both paths: ``settings.jev_mode`` selects the legacy or the Jev
    preparation path per call, so the comparison isolates the path rather than the process.
    External-call counting wraps the real seams and still calls through to them.

    ``user_id`` runs the enterprise path (permission filtering enabled) with that principal.
    The returned callables take ``(case, session_id="benchmark")``; a **multi-turn** case seeds its
    own prior turns into that session and reads exactly as many rounds as its history holds, while
    single-turn cases behave exactly as before.
    """
    _ensure_project_root_on_path()
    from unittest.mock import patch

    import rag.agent
    import rag.pipeline
    from config import settings
    from rag.pipeline import RAGPipeline
    from rag.user_db import UserDB

    user_db = None
    user = None
    if user_id is not None:
        user_db = UserDB(user_db_path or settings.users_db_path)
        user = user_db.get_user_by_id(user_id)
        if user is None:
            raise ValueError(f"benchmark user not found: {user_id}")

    pipelines: dict[str, RAGPipeline] = {}

    def pipeline_for(case: BenchmarkCase) -> RAGPipeline:
        target_kb = case.kb_id or kb_id
        if target_kb not in pipelines:
            previous_mode = settings.jev_mode
            settings.jev_mode = "active"  # always construct a service so both paths can run
            try:
                pipelines[target_kb] = RAGPipeline(
                    kb_id=target_kb,
                    memory_db_path=memory_db_path,
                    user_db=user_db,
                )
            finally:
                settings.jev_mode = previous_mode
        return pipelines[target_kb]

    def prepare(
        case: BenchmarkCase,
        *,
        jev_path: bool,
        session_id: str = "benchmark",
        history_turns: int | None = None,
    ) -> PreparationOutcome:
        pipeline = pipeline_for(case)
        previous_mode = settings.jev_mode
        previous_allowlist = settings.jev_kb_allowlist
        previous_history_turns = settings.query_rewrite_history_turns
        settings.jev_mode = "active" if jev_path else "off"
        settings.jev_kb_allowlist = case.kb_id or kb_id
        counters = {"rewrite": 0, "rerank": 0, "route": 0, "jev": 0}

        # 每个用例从冷缓存开始：改写缓存是产品行为，但基准要测"这次请求真实调了多少次模型"。
        # 跨用例复用会让 old/new 两侧白拿命中，rewrite 计数与 external_calls 都会失真。
        cache = getattr(pipeline, "_rewrite_cache", None)
        cache_lock = getattr(pipeline, "_rewrite_cache_lock", None)
        if cache is not None and cache_lock is not None:
            with cache_lock:
                cache.clear()

        # 多轮：把这个用例的先行轮次写进**它自己的会话**，再让改写按这个用例的历史长度去读。
        # 会话 id 由调用方给（每次调用一个），因为同一个会话被反复调用会把上一轮的问答累积进去，
        # 于是"历史"变成"历史 + 上次的答案"，两次调用测的就不是同一个东西了。
        #
        # 轮数按**用例自己的历史长度**定，而不是读全局的 `query_rewrite_history_turns`：
        # 用例声明了几轮，就喂几轮；全局开关保持调用方设的值（默认 0 ⇒ 单轮用例逐字节不变）。
        if case.history:
            for role, content in case.history:
                pipeline.memory.add_message(session_id, role, content)
            turns = len(case.history) if history_turns is None else int(history_turns)
            settings.query_rewrite_history_turns = max(0, turns)

        original_rewrite = rag.pipeline.rewrite_query
        original_route = rag.agent.route_question
        original_rerank = pipeline.reranker.rerank
        original_jev_route = pipeline.jev.route if pipeline.jev is not None else None
        original_jev_select = pipeline.jev.select if pipeline.jev is not None else None

        def counting(original, key):
            def wrapper(*args, **kwargs):
                counters[key] += 1
                return original(*args, **kwargs)

            return wrapper

        patches = [
            patch("rag.pipeline.rewrite_query", counting(original_rewrite, "rewrite")),
            patch("rag.agent.route_question", counting(original_route, "route")),
            patch.object(pipeline.reranker, "rerank", counting(original_rerank, "rerank")),
        ]
        if original_jev_route is not None:
            patches.append(patch.object(pipeline.jev, "route", counting(original_jev_route, "jev")))
            patches.append(patch.object(pipeline.jev, "select", counting(original_jev_select, "jev")))
        try:
            for active in patches:
                active.start()
            started = time.perf_counter_ns()
            prepared, error = pipeline._prepare_context(
                case.question, session_id, None, user=user, kb_id=case.kb_id
            )
            preparation_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        finally:
            for active in reversed(patches):
                active.stop()
            settings.jev_mode = previous_mode
            settings.jev_kb_allowlist = previous_allowlist
            settings.query_rewrite_history_turns = previous_history_turns

        if error is not None:
            return PreparationOutcome(preparation_ms=preparation_ms, route="blocked", failure=error)
        route = _routing_domain(prepared)
        source_names = tuple(
            source["doc_name"] for source in prepared.get("sources", []) if source.get("doc_name")
        )
        return PreparationOutcome(
            preparation_ms=preparation_ms,
            route=route,
            source_names=source_names,
            rewrite_called=counters["rewrite"] > 0,
            external_calls=sum(counters.values()),
            # 判定层的结论本来就在 prepared 上（``_finalize_rag_context`` 写入，SSE 的
            # ``done.reliability`` 也是它）。基准此前只取走它的路由与来源，把这个"到底有没有
            # 给出答案"的结论丢掉了，于是"该作答却拒答"在数字里根本不存在。
            answerability=_answerability_kind(prepared),
        )

    def old(
        case: BenchmarkCase,
        session_id: str = "benchmark",
        *,
        history_turns: int | None = None,
    ) -> PreparationOutcome:
        return prepare(case, jev_path=False, session_id=session_id, history_turns=history_turns)

    def new(
        case: BenchmarkCase,
        session_id: str = "benchmark",
        *,
        history_turns: int | None = None,
    ) -> PreparationOutcome:
        return prepare(case, jev_path=True, session_id=session_id, history_turns=history_turns)

    return old, new


def _answerability_kind(prepared: dict) -> str | None:
    """The answerability decision of one prepared request, or ``None`` when none was made.

    Reads the same object the SSE ``done.reliability`` payload is built from, so the benchmark
    and the endpoint cannot disagree about whether a request was answered. Only the ``rag``
    preparation path builds an evidence bundle; structured/tool/fixed answers carry no such
    decision and stay ``None`` rather than being guessed.
    """
    decision = (prepared or {}).get("decision")
    kind = getattr(decision, "kind", None)
    return kind if isinstance(kind, str) and kind else None


def _routing_domain(prepared: dict) -> str:
    """Return the effective routing decision.

    A recorded Jev route wins when it is a real domain; otherwise the recorded legacy routing
    decision is used, falling back to the prepared route kind.
    """
    metadata = prepared.get("decision_metadata") or {}
    jev_route = metadata.get("jev_route")
    if jev_route in ("rag", "structured", "tool", "clarification"):
        return jev_route
    legacy_route = metadata.get("legacy_route")
    if legacy_route in ("rag", "structured", "agent"):
        return {"rag": "rag", "structured": "structured", "agent": "tool"}[legacy_route]
    route = prepared.get("route", "")
    return {"rag": "rag", "structured": "structured", "agent": "tool", "fixed": "clarification"}.get(
        route, route
    )


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_project_root_on_path()
    # 沙箱回放：环境里给了黄金 fixture 就从它重放四个出网 seam（缺答案会响亮失败，不连网络）。
    from rag.evolution.replay_fixture import install_from_env

    install_from_env()
    parser = argparse.ArgumentParser(description="Benchmark legacy versus Jev preparation paths.")
    parser.add_argument("--cases", default="benchmarks/jev_retrieval_cases.json")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--markdown-out", default=None)
    parser.add_argument("--include-questions", action="store_true")
    parser.add_argument(
        "--acceptance",
        default=ACCEPTANCE_SINGLE_TURN,
        choices=ACCEPTANCE_PROFILES,
        help=(
            "which criteria apply to this case set (thresholds are identical either way; the "
            "multi_turn profile reports p50/p95/rewrite as advisory because both paths rewrite "
            "every multi-turn case by construction)"
        ),
    )
    parser.add_argument("--kb-id", default="rag_docs")
    parser.add_argument("--memory-db-path", default=":memory:")
    parser.add_argument("--user-id", type=int, default=None, help="Run the enterprise path with this principal")
    parser.add_argument("--user-db", default=None, help="User database path for --user-id")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    old_prepare, new_prepare = build_live_preparations(
        kb_id=args.kb_id,
        memory_db_path=args.memory_db_path,
        user_id=args.user_id,
        user_db_path=args.user_db,
    )
    report = run_benchmark(
        cases,
        old_prepare,
        new_prepare,
        runs=args.runs,
        warmups=args.warmups,
        profile=args.acceptance,
    )

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_markdown(report, cases=cases, include_questions=args.include_questions),
            encoding="utf-8",
        )
    print(render_markdown(report, cases=cases, include_questions=args.include_questions))
    return 0 if report.verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
