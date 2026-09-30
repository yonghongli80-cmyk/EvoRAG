"""Deterministic failure clustering and the three-strikes admission rule.

The clusterer only reads stored signals (route, tool names, scores, hashes,
feedback kinds, exception classes, answerability kinds): no model calls and no
hardcoded question/answer mappings. A cluster leaves the observation queue only
when the same root cause (knowledge base x failure stage x root-cause feature)
reaches ``threshold`` occurrences inside the window; a single or undetermined
failure keeps waiting in the queue, and reclustering can never double-count a
member because attachment and the member count are single atomic statements.
"""

from __future__ import annotations

import hashlib
import logging
import time

from config import settings
from rag.evolution.models import EvolutionStatus, FailureStage, ObservationPool, RootCauseFeature
from rag.evolution.store import FailureTraceStore

logger = logging.getLogger(__name__)

# Negative feedback kinds, mirroring rag/knowledge_feedback.py's accepted kinds.
NEGATIVE_FEEDBACK_KINDS = frozenset({"not_useful", "outdated"})
# rag/reliability/answerability.py refuses with this kind when visible evidence
# conflicts, so it is the stored evidence-conflict signal for phase 2.
CONFLICT_ANSWERABILITY_KINDS = frozenset({"clarification_required"})


def cluster_key(kb_id: str, failure_stage: FailureStage, root_cause_feature: RootCauseFeature) -> str:
    """Deterministic cluster signature: sha256(kb_id | failure_stage | root_cause_feature)."""
    raw = f"{kb_id}|{failure_stage}|{root_cause_feature}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_low_score(best_score: float | None) -> bool:
    """A score below the reliability layer's qualified-evidence bar is a low score."""
    return best_score is not None and best_score < settings.reliability_min_score


class FailureClusterer:
    """Clusters observed failures and applies the phase-2 admission rule."""

    def __init__(self, store: FailureTraceStore, *, threshold: int = 3, window_days: int = 30):
        self._store = store
        self._threshold = threshold
        self._window_days = window_days

    def classify(self, observation: dict) -> tuple[FailureStage, RootCauseFeature]:
        """Deterministic (failure stage, root cause) for one stored observation row."""
        route = observation.get("route") or ""
        tool_names = observation.get("details_tool_names") or ()
        chunk_hashes = observation.get("details_chunk_hashes") or ()
        best_score = observation.get("details_best_score")
        exception_class = observation.get("details_exception_class")
        answerability_kind = observation.get("details_answerability_kind")
        feedback_kind = observation.get("details_feedback_kind")
        return self._classify_stage(
            route, tool_names, chunk_hashes, best_score, answerability_kind, exception_class
        ), self._classify_root_cause(
            chunk_hashes, best_score, answerability_kind, feedback_kind, exception_class
        )

    def cluster_once(self, *, limit: int = 200) -> dict:
        """Cluster at most ``limit`` pending observations; returns counts for reconciliation."""
        now = time.time()
        cutoff = now - self._window_days * 86400
        pending = self._store.list_pending_observations(cutoff=cutoff, limit=limit)
        groups: dict[str, tuple[tuple[FailureStage, RootCauseFeature], list[dict]]] = {}
        for row in pending:
            stage, feature = self.classify(row)
            key = cluster_key(row["kb_id"], stage, feature)
            groups.setdefault(key, ((stage, feature), []))[1].append(row)
        clustered = 0
        promoted = 0
        for key, ((stage, feature), rows) in groups.items():
            if self._admit_group(key, stage, feature, rows, cutoff=cutoff, now=now):
                promoted += 1
            clustered += len(rows)
        return {
            "scanned": len(pending),
            "clustered": clustered,
            "promoted": promoted,
            "skipped": self._store.count_stale_pending_observations(cutoff),
        }

    def _admit_group(
        self,
        key: str,
        stage: FailureStage,
        feature: RootCauseFeature,
        rows: list[dict],
        *,
        cutoff: float,
        now: float,
    ) -> bool:
        """Attach one group's members and promote the cluster when the rule allows it."""
        cluster = self._store.upsert_cluster(
            cluster_key=key,
            kb_id=rows[0]["kb_id"],
            failure_stage=str(stage),
            root_cause_feature=str(feature),
            seen_at=now,
        )
        attached = [
            member
            for member in self._store.list_observations(cluster_id=cluster["id"])
            if member["created_at"] >= cutoff
        ]
        priority = self._priority(attached + rows)
        for row in rows:
            self._store.attach_cluster_member(
                row["id"],
                cluster["id"],
                failure_stage=str(stage),
                root_cause_feature=str(feature),
                now=now,
            )
        cluster = self._store.refresh_cluster(
            cluster["id"], cutoff=cutoff, priority=priority, now=now
        )
        if cluster["member_count"] < self._threshold:
            return False
        if feature == RootCauseFeature.UNDETERMINED:
            # Design section 6: an undetermined root cause never leaves the queue.
            return False
        if cluster["pool"] != ObservationPool.OBSERVATION_QUEUE.value:
            return False
        admitted = self._store.promote_cluster(
            cluster["id"],
            new_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
            pool=ObservationPool.CANDIDATE_POOL.value,
            member_status=EvolutionStatus.CLUSTERED.value,
            now=now,
        )
        return admitted is not None

    @staticmethod
    def _classify_stage(
        route: str,
        tool_names: tuple[str, ...],
        chunk_hashes: tuple[str, ...],
        best_score: float | None,
        answerability_kind: str | None,
        exception_class: str | None,
    ) -> FailureStage:
        if route == "structured" and exception_class:
            return FailureStage.STRUCTURED_QUERY
        # 未捕获异常：请求死在异常里，既不是检索也不是生成阶段（异常本身才是第一处偏离）。
        # 必须排在低分启发式之前，否则"异常 + 低分"会被误记成检索阶段失败。
        if exception_class:
            return FailureStage.UNKNOWN
        if tool_names:
            return FailureStage.TOOL
        if _is_low_score(best_score) and not chunk_hashes:
            return FailureStage.RETRIEVAL
        if answerability_kind == "insufficient_evidence":
            return FailureStage.GENERATION
        return FailureStage.UNKNOWN

    @staticmethod
    def _classify_root_cause(
        chunk_hashes: tuple[str, ...],
        best_score: float | None,
        answerability_kind: str | None,
        feedback_kind: str | None,
        exception_class: str | None,
    ) -> RootCauseFeature:
        # 异常优先于召回启发式：异常失败通常在采纳任何块之前就死了，"没有块哈希"描述的是
        # 后果而不是根因。此前这条检查排在最后，因此**永远不可达**（2026-09-30：6 条
        # ValueError 观测被聚成 recall_miss，而它们的真实根因是代码缺陷）。
        if exception_class:
            return RootCauseFeature.CODE_DEFECT
        if not chunk_hashes or _is_low_score(best_score):
            return RootCauseFeature.RECALL_MISS
        if answerability_kind == "insufficient_evidence":
            return RootCauseFeature.GENERATOR_IGNORED_EVIDENCE
        if feedback_kind in NEGATIVE_FEEDBACK_KINDS:
            return RootCauseFeature.PROMPT_OR_WORKFLOW_DEFECT
        if answerability_kind in CONFLICT_ANSWERABILITY_KINDS:
            return RootCauseFeature.EVIDENCE_CONFLICT
        return RootCauseFeature.UNDETERMINED

    @staticmethod
    def _priority(members: list[dict]) -> str:
        """Design section 5: negative feedback, conflicts, exceptions or a blocked route raise priority."""
        for member in members:
            if (
                member.get("details_feedback_kind") in NEGATIVE_FEEDBACK_KINDS
                or member.get("details_exception_class")
                or member.get("details_answerability_kind") in CONFLICT_ANSWERABILITY_KINDS
                or member.get("route") == "blocked"
            ):
                return "high"
        return "normal"


__all__ = ["FailureClusterer", "cluster_key"]
