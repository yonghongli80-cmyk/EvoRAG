"""Task 5: phase-2 observation metrics computed from stored signals only."""

import json

import pytest

from config import settings
from rag.evolution.clusterer import FailureClusterer
from rag.evolution.metrics import METRIC_DEFINITIONS, METRIC_PHASES, compute_metrics
from rag.evolution.models import FailureObservation
from rag.evolution.store import FailureTraceStore

NOW = 1_000_000.0

# The stored answerability signal; "answered" produced an answer, "refused" declined.
_KIND_TO_ANSWERABILITY = {"answered": "answerable", "refused": "insufficient_evidence"}


@pytest.fixture
def store(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    yield store
    store.close()


def observation(store, question, *, kind="answered", feedback_kind=None, **kw):
    base = dict(
        kb_id="kb1",
        failure_stage="retrieval",
        question=question,
        route="rag",
        answerability_kind=_KIND_TO_ANSWERABILITY[kind],
        chunk_hashes=("a" * 64,),
        best_score=0.8,
    )
    if feedback_kind is not None:
        base["feedback_kind"] = feedback_kind
    base.update(kw)
    return store.add_observation(FailureObservation(**base))


# --------------------------------------------------------------------------
# Plan Task 5 Step 1 focused tests.
# --------------------------------------------------------------------------


def test_empty_window_is_zero_division_safe(store):
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    assert metrics["rates"]["problem_solve_rate"] is None
    assert metrics["counts"]["observed"] == 0
    assert set(metrics["definitions"]) == set(METRIC_DEFINITIONS)


def test_wrong_answer_rate_uses_stored_signals_only(store):
    observation(store, "q1")
    observation(store, "q2", feedback_kind="not_useful")
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    assert metrics["rates"]["wrong_answer_rate"] == 0.5


def test_refusal_accuracy_counts_confirmed_refusals(store):
    observation(store, "q1", kind="refused", feedback_kind="useful")
    observation(store, "q2", kind="refused", feedback_kind="not_useful")
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    assert metrics["rates"]["refusal_accuracy_rate"] == 0.5


def test_window_boundaries_are_inclusive_and_ordered(store):
    observation(store, "old", created_at=NOW - 31 * 86400)
    observation(store, "fresh", created_at=NOW)
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    assert metrics["counts"]["observed"] == 1


# --------------------------------------------------------------------------
# Window edges, empty denominators and latency.
# --------------------------------------------------------------------------


def test_window_edge_equal_to_cutoff_is_inclusive(store):
    observation(store, "edge", created_at=NOW - 30 * 86400)
    metrics = compute_metrics(store, window_days=30, now=NOW)
    assert metrics["counts"]["observations"] == 1


def test_one_second_outside_the_window_is_excluded(store):
    observation(store, "outside", created_at=NOW - 30 * 86400 - 1)
    metrics = compute_metrics(store, window_days=30, now=NOW)
    assert metrics["counts"]["observations"] == 0


def test_all_phase2_rates_are_none_on_an_empty_store(store):
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    for name in (
        "problem_solve_rate",
        "generalization_rate",
        "wrong_answer_rate",
        "refusal_accuracy_rate",
        "avg_latency_ms",
    ):
        assert metrics["rates"][name] is None
    assert metrics["counts"]["skips_by_reason"] == {}


def test_single_observation_produces_zero_division_safe_metrics(store):
    observation(store, "only", feedback_kind="useful", total_ms=42.0)
    metrics = compute_metrics(store, window_days=30, now=NOW)
    assert metrics["counts"]["observations"] == 1
    assert metrics["rates"]["avg_latency_ms"] == 42.0
    assert metrics["rates"]["wrong_answer_rate"] == 0.0
    assert metrics["rates"]["refusal_accuracy_rate"] is None
    assert metrics["rates"]["problem_solve_rate"] is None
    assert metrics["rates"]["generalization_rate"] is None


def test_avg_latency_averages_in_window_observations_only(store):
    observation(store, "a", total_ms=10.0)
    observation(store, "b", total_ms=20.0)
    observation(store, "stale", total_ms=999.0, created_at=NOW - 40 * 86400)
    metrics = compute_metrics(store, window_days=30, now=NOW)
    assert metrics["rates"]["avg_latency_ms"] == 15.0


def test_window_days_defaults_to_the_cluster_window_setting(store):
    observation(store, "recent")
    metrics = compute_metrics(store)
    assert metrics["window_days"] == settings.evolution_cluster_window_days
    assert metrics["counts"]["observations"] == 1


# --------------------------------------------------------------------------
# Funnel, promotion and generalization over mixed statuses.
# --------------------------------------------------------------------------


def test_mixed_statuses_and_pools_feed_funnel_and_solve_rate(store):
    observation(store, "dup-1", kind="refused")
    observation(store, "dup-2", kind="refused")
    result = FailureClusterer(store, threshold=2, window_days=30).cluster_once()
    assert result["promoted"] == 1
    observation(store, "single", kind="refused")  # stays in the observation queue

    metrics = compute_metrics(store, window_days=30, now=NOW)
    counts = metrics["counts"]
    assert counts["observations"] == 3
    assert counts["observed"] == 1
    assert counts["clustered"] == 2
    assert counts["clusters"] == 1
    assert counts["candidate_pool"] == 1
    assert counts["observation_queue"] == 0
    assert counts["funnel"] == {
        "observed": 1,
        "clustered": 2,
        "awaiting_diagnosis_review": 1,
        "skips": {},
    }
    assert metrics["rates"]["problem_solve_rate"] == 1.0
    assert metrics["rates"]["generalization_rate"] == 1.0


def test_generalization_requires_a_recurring_root_cause(store):
    observation(store, "dup-1", kind="refused")
    observation(store, "dup-2", kind="refused")
    observation(store, "lonely", kind="refused", kb_id="kb2")
    result = FailureClusterer(store, threshold=2, window_days=30).cluster_once()
    assert result["promoted"] == 1

    metrics = compute_metrics(store, window_days=30, now=NOW)
    assert metrics["counts"]["clusters"] == 2
    assert metrics["rates"]["problem_solve_rate"] == 0.5
    assert metrics["rates"]["generalization_rate"] == pytest.approx(2 / 3)


def test_skip_log_feeds_the_funnel(store):
    # Skip counts are the all-time store signal (no windowed skip read exists).
    store.log_skip("permission_denied", question="blocked-q", route="blocked", kb_id="kb1")
    store.log_skip("transient_external")
    metrics = compute_metrics(store, window_days=30, now=NOW)
    assert metrics["counts"]["skipped"] == 2
    assert metrics["counts"]["skips_by_reason"] == {"permission_denied": 1, "transient_external": 1}
    assert metrics["counts"]["funnel"]["skips"] == {"permission_denied": 1, "transient_external": 1}


# --------------------------------------------------------------------------
# Definition text, explicit absence, and the no-question-text boundary.
# --------------------------------------------------------------------------


def test_definitions_cover_design_section_eleven_with_phases():
    expected = {
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
    assert {name: METRIC_PHASES[name] for name in METRIC_DEFINITIONS} == expected


def test_later_phase_metrics_are_absent_with_phase_annotation(store):
    metrics = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    for name, phase in (
        ("citation_accuracy_rate", 3),
        ("structured_accuracy_rate", 3),
        ("regression_count", 4),
        ("request_cost", 4),
        ("candidate_cost", 4),
        ("manual_verification_pass_rate", 4),
        ("canary_rollback_rate", 5),
    ):
        assert metrics["definitions"][name]["available_from_phase"] == phase
        assert name not in metrics["rates"]
    for name in (
        "problem_solve_rate",
        "generalization_rate",
        "wrong_answer_rate",
        "refusal_accuracy_rate",
        "avg_latency_ms",
    ):
        assert name in metrics["rates"]


def test_metrics_payload_is_json_serializable(store):
    observation(store, "dup-1", kind="refused")
    observation(store, "dup-2", kind="refused")
    FailureClusterer(store, threshold=2, window_days=30).cluster_once()
    payload = compute_metrics(store, feedback_rows=[], window_days=30, now=NOW)
    assert set(payload) == {"window_days", "counts", "rates", "definitions"}
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_metrics_never_leak_question_text_or_config_names(store):
    questions = ["年假额度有多少", "secret-question-text-xyz"]
    for question in questions:
        observation(store, question, kind="refused", feedback_kind="useful", total_ms=12.5)
    metrics = compute_metrics(store, window_days=30, now=NOW)

    blob = json.dumps(metrics, ensure_ascii=False)
    for question in questions:
        assert question not in blob
    assert "RAG_" not in blob
    assert "question" not in blob
    for name, definition in METRIC_DEFINITIONS.items():
        assert definition
        assert metrics["definitions"][name]["definition"] == definition
