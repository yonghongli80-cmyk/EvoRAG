"""Task 2: deterministic clustering and the three-strikes admission rule."""

import time

from rag.evolution.clusterer import FailureClusterer
from rag.evolution.models import FailureObservation
from rag.evolution.store import FailureTraceStore


def observation(store, question, *, stage="retrieval", score=0.11, **kw):
    base = dict(
        kb_id="kb1",
        failure_stage=stage,
        question=question,
        route="rag",
        chunk_hashes=(f"{abs(hash(question)):064x}",),
        best_score=score,
        answerability_kind="insufficient_evidence",
    )
    base.update(kw)
    return store.add_observation(FailureObservation(**base))


def test_same_root_cause_promotes_only_on_third_occurrence(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    clusterer = FailureClusterer(store, threshold=3, window_days=30)
    for i in range(2):
        observation(store, f"年假额度{i}")
        clusterer.cluster_once()
        clusters = store.list_clusters()
        assert clusters[0]["pool"] == "observation_queue"
        assert clusters[0]["member_count"] == i + 1
    observation(store, "年假额度2")
    result = clusterer.cluster_once()
    assert result["promoted"] == 1
    cluster = store.list_clusters()[0]
    assert cluster["pool"] == "candidate_pool"
    assert cluster["status"] == "awaiting_diagnosis_review"
    members = store.list_observations(cluster_id=cluster["id"])
    assert all(m["status"] == "clustered" for m in members)


def test_reclustering_is_idempotent(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    observation(store, "q1")
    clusterer = FailureClusterer(store, threshold=3)
    clusterer.cluster_once()
    result = clusterer.cluster_once()
    assert result == {"scanned": 0, "clustered": 0, "promoted": 0, "skipped": 0}
    assert store.list_clusters()[0]["member_count"] == 1


def test_priority_signals_elevate_the_cluster(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="generation", question="q", route="rag",
        feedback_kind="not_useful", chunk_hashes=("a" * 64,),
    ))
    FailureClusterer(store, threshold=3).cluster_once()
    assert store.list_clusters()[0]["priority"] == "high"


def test_stage_and_root_cause_are_deterministic(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    o = store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="retrieval", question="q", route="rag",
        best_score=0.05, chunk_hashes=(), answerability_kind="insufficient_evidence",
    ))
    stage, feature = FailureClusterer(store).classify(o)
    assert (stage, feature) == ("retrieval", "recall_miss")


def test_window_parks_stale_observations_in_the_queue(tmp_path):
    """Window = now - window_days*86400; stale pending rows stay in the queue."""
    store = FailureTraceStore(tmp_path / "e.db")
    observation(store, "fresh-1")
    observation(store, "fresh-2")
    store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="retrieval", question="stale", route="rag",
        chunk_hashes=("a" * 64,), best_score=0.11,
        answerability_kind="insufficient_evidence",
        created_at=time.time() - 40 * 86400,
    ))
    result = FailureClusterer(store, threshold=3, window_days=30).cluster_once()
    assert result == {"scanned": 2, "clustered": 2, "promoted": 0, "skipped": 1}
    assert store.list_clusters()[0]["member_count"] == 2


def test_undetermined_root_cause_never_enters_the_candidate_pool(tmp_path):
    """Design section 6: an undetermined root cause can only wait in the queue."""
    store = FailureTraceStore(tmp_path / "e.db")
    clusterer = FailureClusterer(store, threshold=3)
    for i in range(3):
        observation(store, f"q{i}", score=0.9, answerability_kind=None)
        clusterer.cluster_once()
    cluster = store.list_clusters()[0]
    assert cluster["root_cause_feature"] == "undetermined"
    assert cluster["member_count"] == 3
    assert cluster["pool"] == "observation_queue"
    assert cluster["status"] == "clustered"
    members = store.list_observations(cluster_id=cluster["id"])
    assert all(m["status"] == "observed" for m in members)


def test_cluster_and_member_seams_serve_api_and_metrics(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    clusterer = FailureClusterer(store, threshold=2)
    for i in range(2):
        observation(store, f"q{i}")
        clusterer.cluster_once()
    clusters = store.list_clusters()
    assert len(clusters) == 1
    assert clusters[0]["pool"] == "candidate_pool"
    assert clusters[0]["member_count"] == 2
    members = store.list_observations(cluster_id=clusters[0]["id"])
    assert [m["status"] for m in members] == ["clustered", "clustered"]
    assert all(m["root_cause_feature"] == clusters[0]["root_cause_feature"] for m in members)
    assert [o["id"] for o in store.list_observations()] == [1, 2]


def test_purge_observations_before_deletes_only_old_rows(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    fresh = observation(store, "fresh")
    stale = store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="retrieval", question="stale", route="rag",
        created_at=time.time() - 100 * 86400,
    ))
    assert store.purge_observations_before(time.time() - 90 * 86400) == 1
    assert store.get_observation(fresh["id"]) is not None
    assert store.get_observation(stale["id"]) is None

def test_an_exception_failure_is_clustered_as_a_code_defect(tmp_path):
    """未捕获异常必须聚成 code_defect：召回启发式不能抢占异常的根因判定。

    2026-09-30：`ValueError: versioned evidence required` 的 6 条观测被聚成
    `root_cause_feature=recall_miss`——因为 `_classify_root_cause` 先看"没有块哈希 ⇒ 召回未命中"，
    而异常失败通常死在采纳任何块之前，于是 `exception_class ⇒ CODE_DEFECT` 那条**永远到不了**。
    """
    from rag.evolution.models import FailureStage

    store = FailureTraceStore(tmp_path / "e.db")
    clusterer = FailureClusterer(store, threshold=3, window_days=30)
    for index in range(3):
        observation(
            store,
            f"异常问题{index}",
            route="error",
            chunk_hashes=(),
            best_score=None,
            answerability_kind=None,
            exception_class="ValueError",
        )
        clusterer.cluster_once()

    cluster = store.list_clusters()[0]
    assert cluster["root_cause_feature"] == "code_defect"
    assert cluster["failure_stage"] == FailureStage.UNKNOWN.value


def test_an_exception_never_becomes_a_retrieval_stage_failure(tmp_path):
    """异常失败即使带着低分也不得被归到检索阶段——异常本身才是第一处偏离。"""
    from rag.evolution.models import FailureStage

    assert (
        FailureClusterer._classify_stage("error", (), (), 0.01, None, "ValueError")
        == FailureStage.UNKNOWN
    )
    # 非异常的同形观测仍按原启发式落到检索阶段。
    assert (
        FailureClusterer._classify_stage("rag", (), (), 0.01, None, None)
        == FailureStage.RETRIEVAL
    )
