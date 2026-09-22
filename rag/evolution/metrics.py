"""Phase-2 observation metrics: window aggregation over stored signals only.

Every number here derives from the evolution store's own rows (observations,
clusters, and the skip log): no question text, no chunk text, no hardcoded
question/answer mapping, and no cross-database join. Ratios are division-safe
(``None`` when the denominator is empty) and every metric ships with its
definition, so the API and the dashboard stay auditable.

Design section 11's full metric set is described in :data:`METRIC_DEFINITIONS`
with an ``available_from_phase`` annotation. Phase 2 computes only the subset
that stored signals support; the later metrics are explicitly absent (never
placeholder values) until their phase arrives.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from config import settings
from rag.evolution.clusterer import CONFLICT_ANSWERABILITY_KINDS, NEGATIVE_FEEDBACK_KINDS
from rag.evolution.models import EvolutionStatus, ObservationPool
from rag.evolution.store import FailureTraceStore

SECONDS_PER_DAY = 86400

# Stored answer signals, mirroring rag/reliability/models.py AnswerabilityKind.
ANSWERED_ANSWERABILITY_KINDS = frozenset({"answerable", "answerable_with_warning"})
REFUSED_ANSWERABILITY_KINDS = frozenset({"insufficient_evidence"})
CONFIRMING_FEEDBACK_KINDS = frozenset({"useful"})

# Design section 11 metric set: machine name -> first implementation phase.
METRIC_PHASES: dict[str, int] = {
    "problem_solve_rate": 2,
    "generalization_rate": 2,
    "wrong_answer_rate": 2,
    "refusal_accuracy_rate": 2,
    "avg_latency_ms": 2,
    "funnel": 2,
    "citation_accuracy_rate": 3,
    "structured_accuracy_rate": 3,
    "regression_count": 4,
    "request_cost": 4,
    "candidate_cost": 4,
    "manual_verification_pass_rate": 4,
    "canary_rollback_rate": 5,
}

# Metric name -> Chinese definition and calculation basis (auditable wording).
METRIC_DEFINITIONS: dict[str, str] = {
    "problem_solve_rate": (
        "问题解决率：窗口内已晋升候选池（离开观察队列、等待人工诊断）的聚类数 ÷ 窗口内聚类总数"
        "（聚类 first_seen_at ≥ 窗口起点；分母为 0 返回 None）。衡量同类根因失败被识别并准入诊断的进度。"
    ),
    "generalization_rate": (
        "同类问题泛化率：窗口内已聚类观测中，所属聚类在窗口内成员数 ≥ 2"
        "（同一知识库 × 失败阶段 × 根因特征跨多个问题复现）的观测占比（分母为 0 返回 None）。"
        "第 2 阶段无候选修复，此为泛化现象的存储信号代理口径。"
    ),
    "wrong_answer_rate": (
        "错误答案率：已回答观测（answerability_kind 为 answerable / answerable_with_warning）中"
        "带负向反馈（not_useful / outdated）或冲突记录的观测占比（分母为 0 返回 None）。"
    ),
    "refusal_accuracy_rate": (
        "拒答准确率：带反馈的拒答观测（answerability_kind = insufficient_evidence）中获 useful 反馈的占比"
        "（无反馈的拒答不计入分母；分母为 0 返回 None）。"
    ),
    "avg_latency_ms": (
        "平均延迟：窗口内观测 total_ms 的均值（毫秒；无观测返回 None）。"
    ),
    "funnel": (
        "观测漏斗：窗口内观测按状态计数（observed / clustered）＋ 窗口内 awaiting_diagnosis_review 聚类数 "
        "＋ 跳过请求按原因计数（evolution_skip_log 为全量口径，存储未提供窗口化跳过计数）。"
    ),
    "citation_accuracy_rate": (
        "引用正确率：引用反馈中 useful 占比。第 2 阶段按管理员划定的指标子集缺席（需引用级反馈跨库关联），"
        "第 3 阶段诊断辅助启用后随 API 下发；本阶段恒不返回，禁止占位假数据。"
    ),
    "structured_accuracy_rate": (
        "结构化计算准确率：结构化路由无错误信号（exception_class 为空）观测占比。"
        "第 2 阶段按管理员划定的指标子集缺席（结构化异常多已归入跳过日志），"
        "第 3 阶段诊断辅助启用后随 API 下发；本阶段恒不返回。"
    ),
    "regression_count": (
        "旧能力回归数：候选过门禁时旧能力回归计数。第 2 阶段无候选，恒不返回；第 4 阶段候选测试起由回归门产出。"
    ),
    "request_cost": (
        "请求成本：单请求 Token / 费用估算。evolution 库不存成本信号，第 2 阶段恒不返回；第 4 阶段起随候选评测下发。"
    ),
    "candidate_cost": (
        "候选成本：候选生成与测试的资源开销。第 2 阶段无候选，恒不返回；第 4 阶段候选生成起下发。"
    ),
    "manual_verification_pass_rate": (
        "人工核验通过率：awaiting_verification 聚类经管理员核验通过的比例。第 2 阶段无人工核验环节，恒不返回；"
        "第 4 阶段候选验证起下发。"
    ),
    "canary_rollback_rate": (
        "灰度回退率：灰度发布中回退的候选占灰度候选总数的比例。第 2 阶段无灰度，恒不返回；第 5 阶段灰度起下发。"
    ),
}


def compute_metrics(
    store: FailureTraceStore,
    *,
    feedback_rows: Sequence[Mapping[str, Any]] | None = None,
    window_days: int | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Aggregate the phase-2 observation metrics over one time window.

    ``feedback_rows`` is accepted for the phase-3 citation metrics (the API
    passes the user-database feedback rows it already reads); the phase-2 set
    is computed from the evolution store alone, so no cross-database join
    happens here. Returns a JSON-serializable dict with ``window_days``,
    ``counts``, ``rates`` and ``definitions``; every ratio is ``None`` when its
    denominator is empty, and later-phase metrics never appear in ``rates``.
    """
    window = int(window_days) if window_days is not None else int(settings.evolution_cluster_window_days)
    moment = float(now) if now is not None else time.time()
    cutoff = moment - window * SECONDS_PER_DAY

    observations = [row for row in store.list_observations() if float(row["created_at"]) >= cutoff]
    clusters = [row for row in store.list_clusters() if float(row["first_seen_at"]) >= cutoff]
    skips_by_reason = dict(store.count_skips_by_reason())

    members_per_cluster: dict[int, int] = {}
    for row in observations:
        cluster_id = row["cluster_id"]
        if cluster_id is not None:
            members_per_cluster[cluster_id] = members_per_cluster.get(cluster_id, 0) + 1
    clustered_members = sum(members_per_cluster.values())

    promoted = sum(1 for cluster in clusters if cluster["pool"] == ObservationPool.CANDIDATE_POOL.value)
    awaiting_review = sum(
        1 for cluster in clusters if cluster["status"] == EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value
    )
    by_status: dict[str, int] = {}
    for row in observations:
        status = str(row["status"])
        by_status[status] = by_status.get(status, 0) + 1

    answered = [
        row for row in observations if row["details_answerability_kind"] in ANSWERED_ANSWERABILITY_KINDS
    ]
    wrong = [
        row
        for row in answered
        if row["details_feedback_kind"] in NEGATIVE_FEEDBACK_KINDS
        or row["details_answerability_kind"] in CONFLICT_ANSWERABILITY_KINDS
    ]
    refused_with_feedback = [
        row
        for row in observations
        if row["details_answerability_kind"] in REFUSED_ANSWERABILITY_KINDS
        and row["details_feedback_kind"] is not None
    ]
    confirmed_refusals = sum(
        1 for row in refused_with_feedback if row["details_feedback_kind"] in CONFIRMING_FEEDBACK_KINDS
    )
    recurring_members = sum(count for count in members_per_cluster.values() if count >= 2)

    counts = {
        "observations": len(observations),
        "observed": by_status.get(EvolutionStatus.OBSERVED.value, 0),
        "clustered": by_status.get(EvolutionStatus.CLUSTERED.value, 0),
        "by_status": by_status,
        "clusters": len(clusters),
        "observation_queue": sum(
            1 for cluster in clusters if cluster["pool"] == ObservationPool.OBSERVATION_QUEUE.value
        ),
        "candidate_pool": promoted,
        "skipped": sum(skips_by_reason.values()),
        "skips_by_reason": skips_by_reason,
        "funnel": {
            "observed": by_status.get(EvolutionStatus.OBSERVED.value, 0),
            "clustered": by_status.get(EvolutionStatus.CLUSTERED.value, 0),
            "awaiting_diagnosis_review": awaiting_review,
            "skips": skips_by_reason,
        },
    }
    rates = {
        "problem_solve_rate": _ratio(promoted, len(clusters)),
        "generalization_rate": _ratio(recurring_members, clustered_members),
        "wrong_answer_rate": _ratio(len(wrong), len(answered)),
        "refusal_accuracy_rate": _ratio(confirmed_refusals, len(refused_with_feedback)),
        "avg_latency_ms": _mean([float(row["total_ms"]) for row in observations]),
    }
    definitions = {
        name: {"definition": METRIC_DEFINITIONS[name], "available_from_phase": METRIC_PHASES[name]}
        for name in METRIC_DEFINITIONS
    }
    return {"window_days": window, "counts": counts, "rates": rates, "definitions": definitions}


def _ratio(numerator: int, denominator: int) -> float | None:
    """Division-safe ratio: ``None`` when the denominator is empty."""
    if denominator == 0:
        return None
    return numerator / denominator


def _mean(values: list[float]) -> float | None:
    """Mean of the samples; ``None`` when there are none."""
    if not values:
        return None
    return sum(values) / len(values)


__all__ = ["METRIC_DEFINITIONS", "METRIC_PHASES", "compute_metrics"]
