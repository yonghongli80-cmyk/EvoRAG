"""Task 4: layer-3 bounded shadow comparison of candidate Jev question sets.

The candidate question set never touches the online path: every comparison runs on
a bounded executor, drops work instead of blocking, and treats a candidate failure
as a recorded availability difference. Services are injected fakes, so nothing here
performs a real Jev/model call.
"""

import json
import threading
import time

import pytest

from rag.jev.candidate_shadow import CandidateShadowComparison, CandidateShadowEvaluator
from rag.jev.policy import PassageSelection, RouteDecision
from rag.models import Chunk


def chunk(name: str, index: int) -> Chunk:
    return Chunk(text=f"chunk {name}#{index}", doc_name=name, chunk_index=index)


def selection_of(*chunks: Chunk) -> PassageSelection:
    return PassageSelection(accepted=tuple(chunks), conflicts=(), excluded_ids=(), has_answer_evidence=True)


class FakeJevService:
    """Deterministic stand-in for ``JevDecisionService`` (no model, no network)."""

    def __init__(
        self,
        *,
        route: str = "rag",
        reason: str = "retrieval_confident",
        select_result: PassageSelection | None = None,
        select_unavailable: bool = False,
        route_error: Exception | None = None,
        select_error: Exception | None = None,
    ):
        self._route = route
        self._reason = reason
        self._select_result = select_result
        self._select_unavailable = select_unavailable
        self._route_error = route_error
        self._select_error = select_error
        self._lock = threading.Lock()
        self.route_calls = 0
        self.select_calls = 0

    def route(self, question, capabilities):
        with self._lock:
            self.route_calls += 1
        if self._route_error is not None:
            raise self._route_error
        return RouteDecision(route=self._route, should_rewrite=False, reason=self._reason, preflight=None)

    def select(self, question, candidates, *, query=None, top_k=None):
        with self._lock:
            self.select_calls += 1
        if self._select_error is not None:
            raise self._select_error
        if self._select_unavailable:
            return None
        if self._select_result is not None:
            return self._select_result
        return selection_of(*candidates)


class BlockingJevService:
    """A service that only answers once ``release`` is set, to expose queue behaviour."""

    def __init__(self):
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.route_calls = 0
        self.select_calls = 0

    def route(self, question, capabilities):
        with self._lock:
            self.route_calls += 1
        self.release.wait(timeout=5)
        return RouteDecision(route="rag", should_rewrite=False, reason="retrieval_confident", preflight=None)

    def select(self, question, candidates, *, query=None, top_k=None):
        with self._lock:
            self.select_calls += 1
        self.release.wait(timeout=5)
        return selection_of(*candidates)


class ScriptedJevService:
    """Per-question route/selection answers, so one evaluator can hold a mixed sample."""

    def __init__(self, routes=None, selections=None, errors=()):
        self._routes = dict(routes or {})
        self._selections = dict(selections or {})
        self._errors = set(errors)
        self._lock = threading.Lock()
        self.route_calls = 0
        self.select_calls = 0

    def route(self, question, capabilities):
        with self._lock:
            self.route_calls += 1
        if question in self._errors:
            raise RuntimeError("scripted route failure")
        route, reason = self._routes.get(question, ("rag", "retrieval_confident"))
        return RouteDecision(route=route, should_rewrite=False, reason=reason, preflight=None)

    def select(self, question, candidates, *, query=None, top_k=None):
        with self._lock:
            self.select_calls += 1
        if question in self._errors:
            raise RuntimeError("scripted select failure")
        if question in self._selections:
            return self._selections[question]
        return selection_of(*candidates)


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Sampling.
# ---------------------------------------------------------------------------


def test_sample_rate_one_samples_every_submission():
    current = FakeJevService(route="rag", reason="retrieval_confident")
    candidate = FakeJevService(route="tool", reason="tool_confident")
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=8)
    try:
        assert evaluator.submit("年假有几天", kb_id="kb1", query="年假有几天", candidates=(chunk("a.md", 0),)) is True
        assert evaluator.submit("报销流程是什么", kb_id="kb1", query="报销流程") is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 2)

        assert current.route_calls == 2
        assert candidate.route_calls == 2
        # Only the first submission carries candidates, so only it judges passages.
        assert current.select_calls == 1
        assert candidate.select_calls == 1
        assert evaluator.stats()["not_sampled"] == 0
    finally:
        evaluator.close(wait=True)


def test_sample_rate_zero_never_samples():
    current = FakeJevService()
    candidate = FakeJevService()
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=0.0, queue_limit=8)
    try:
        for index in range(5):
            assert evaluator.submit(f"问题{index}", kb_id="kb1", query=f"问题{index}") is False
        stats = evaluator.stats()
        assert stats["not_sampled"] == 5
        assert stats["sampled"] == 0
        assert stats["completed"] == 0
        assert current.route_calls == 0
        assert candidate.route_calls == 0
        assert evaluator.comparisons() == []
    finally:
        evaluator.close(wait=True)


def test_sample_rate_half_is_deterministic_and_roughly_half():
    questions = [f"确定性抽样问题-{index}" for index in range(400)]
    evaluator = CandidateShadowEvaluator(FakeJevService(), FakeJevService(), sample_rate=0.5, queue_limit=1000)
    repeat = CandidateShadowEvaluator(FakeJevService(), FakeJevService(), sample_rate=0.5, queue_limit=1000)
    try:
        queued = [evaluator.submit(question, kb_id="kb-sample", query=question) for question in questions]
        # Same (question, kb_id) pair, fresh evaluator: an identical sampling decision.
        repeated = [repeat.submit(question, kb_id="kb-sample", query=question) for question in questions]
        assert queued == repeated

        sampled = sum(queued)
        assert 0.35 * len(questions) <= sampled <= 0.65 * len(questions)
        assert evaluator.stats()["not_sampled"] == len(questions) - sampled
    finally:
        evaluator.close(wait=True)
        repeat.close(wait=True)


# ---------------------------------------------------------------------------
# Comparison recording.
# ---------------------------------------------------------------------------


def test_each_service_judged_once_and_comparison_recorded():
    current = FakeJevService(route="rag", reason="retrieval_confident")
    candidate = FakeJevService(route="rag", reason="retrieval_confident")
    sink: list[CandidateShadowComparison] = []
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=8, on_comparison=sink.append)
    try:
        question = "年假有几天"
        assert (
            evaluator.submit(
                question,
                kb_id="kb1",
                query="年假",
                candidates=(chunk("a.md", 0), chunk("b.md", 1)),
            )
            is True
        )
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

        assert current.route_calls == 1
        assert candidate.route_calls == 1
        assert current.select_calls == 1
        assert candidate.select_calls == 1

        records = evaluator.comparisons()
        assert len(records) == 1
        record = records[0]
        assert set(record) == {
            "question_digest",
            "route_agrees",
            "selection_agrees",
            "current_available",
            "candidate_available",
            "failure_kind",
        }
        assert record["route_agrees"] is True
        assert record["selection_agrees"] == 1.0
        assert record["current_available"] is True
        assert record["candidate_available"] is True
        assert record["failure_kind"] is None

        # The stored projection carries no question or chunk text.
        serialized = json.dumps(record, ensure_ascii=False)
        assert question not in serialized
        assert "chunk a.md#0" not in serialized

        # The sink sees the full comparison record, including the question.
        assert len(sink) == 1
        assert sink[0].question == question
        assert sink[0].route_agrees is True
    finally:
        evaluator.close(wait=True)


def test_route_disagreement_is_recorded_without_candidates():
    current = FakeJevService(route="rag", reason="retrieval_confident")
    candidate = FakeJevService(route="structured", reason="structured_confident")
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("这个季度的销售额", kb_id="kb1", query="这个季度的销售额") is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)
        record = evaluator.comparisons()[0]
        assert record["route_agrees"] is False
        assert record["current_available"] is True
        assert record["candidate_available"] is True
        assert record["failure_kind"] is None
        # No candidates were supplied, so no passage judgment happened.
        assert record["selection_agrees"] is None
        assert current.select_calls == 0
        assert candidate.select_calls == 0
    finally:
        evaluator.close(wait=True)


def test_selection_agreement_is_intersection_over_union():
    left, middle, right = chunk("a.md", 0), chunk("b.md", 1), chunk("c.md", 2)
    current = FakeJevService(select_result=selection_of(left, middle))
    candidate = FakeJevService(select_result=selection_of(middle, right))
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("报销流程", kb_id="kb1", query="报销流程", candidates=(left, middle, right)) is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)
        # {a.md#0, b.md#1} ∩ {b.md#1, c.md#2} = 1 over a union of 3.
        assert evaluator.comparisons()[0]["selection_agrees"] == 1 / 3
    finally:
        evaluator.close(wait=True)


def test_identical_and_disjoint_selections_agree_or_disagree_fully():
    left, right = chunk("a.md", 0), chunk("b.md", 1)
    same = CandidateShadowEvaluator(
        FakeJevService(select_result=selection_of(left)),
        FakeJevService(select_result=selection_of(left)),
        sample_rate=1.0,
        queue_limit=4,
    )
    disjoint = CandidateShadowEvaluator(
        FakeJevService(select_result=selection_of(left)),
        FakeJevService(select_result=selection_of(right)),
        sample_rate=1.0,
        queue_limit=4,
    )
    empty = CandidateShadowEvaluator(
        FakeJevService(select_result=selection_of()),
        FakeJevService(select_result=selection_of()),
        sample_rate=1.0,
        queue_limit=4,
    )
    try:
        assert same.submit("q", kb_id="kb", query="q", candidates=(left, right)) is True
        assert disjoint.submit("q", kb_id="kb", query="q", candidates=(left, right)) is True
        assert empty.submit("q", kb_id="kb", query="q", candidates=(left, right)) is True
        assert _wait_for(lambda: all(evaluator.stats()["completed"] == 1 for evaluator in (same, disjoint, empty)))
        # Both sides accept the same single chunk: perfect agreement.
        assert same.comparisons()[0]["selection_agrees"] == 1.0
        # Both sides accept exactly one chunk, but not the same one.
        assert disjoint.comparisons()[0]["selection_agrees"] == 0.0
        # Neither side accepts anything: agreement over an empty union.
        assert empty.comparisons()[0]["selection_agrees"] == 1.0
    finally:
        for evaluator in (same, disjoint, empty):
            evaluator.close(wait=True)


def test_comparisons_return_only_the_most_recent_entries():
    current = FakeJevService()
    candidate = FakeJevService()
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=16)
    try:
        for index in range(3):
            assert evaluator.submit(f"问题{index}", kb_id="kb", query=f"问题{index}") is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 3)

        assert len(evaluator.comparisons()) == 3
        trimmed = evaluator.comparisons(limit=2)
        assert len(trimmed) == 2
        assert evaluator.comparisons(limit=0) == []
        # Chronological order: the two most recent samples are the last two submitted.
        assert trimmed == evaluator.comparisons()[-2:]
    finally:
        evaluator.close(wait=True)


# ---------------------------------------------------------------------------
# Availability isolation: the candidate question set can never hurt the live path.
# ---------------------------------------------------------------------------


def test_candidate_route_failure_is_isolated():
    current = FakeJevService(route="rag", select_result=selection_of(chunk("a.md", 0)))
    candidate = FakeJevService(route_error=RuntimeError("candidate question set is broken"))
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("年假有几天", kb_id="kb1", query="年假", candidates=(chunk("a.md", 0),)) is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

        record = evaluator.comparisons()[0]
        assert record["current_available"] is True
        assert record["candidate_available"] is False
        assert record["failure_kind"] == "candidate:error:RuntimeError"
        # The current path was judged exactly once and its result is untouched.
        assert current.route_calls == 1
        assert current.select_calls == 1
        assert record["selection_agrees"] is None
        summary = evaluator.summary()
        assert summary["current_available"] == 1
        assert summary["candidate_available"] == 0
        assert summary["availability_difference"] == 1
    finally:
        # A candidate-side failure never reaches the caller, not even on shutdown.
        evaluator.close(wait=True)


def test_candidate_select_failure_is_isolated():
    current = FakeJevService(select_result=selection_of(chunk("a.md", 0)))
    candidate = FakeJevService(select_error=ValueError("off-contract judgments"))
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("年假有几天", kb_id="kb1", query="年假", candidates=(chunk("a.md", 0),)) is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

        record = evaluator.comparisons()[0]
        assert record["route_agrees"] is True
        assert record["current_available"] is True
        assert record["candidate_available"] is False
        assert record["failure_kind"] == "candidate:error:ValueError"
        assert record["selection_agrees"] is None
    finally:
        evaluator.close(wait=True)


def test_current_service_failure_stays_local():
    current = FakeJevService(route_error=TimeoutError("preflight too slow"))
    candidate = FakeJevService()
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("年假有几天", kb_id="kb1", query="年假") is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

        record = evaluator.comparisons()[0]
        assert record["current_available"] is False
        assert record["candidate_available"] is True
        assert record["failure_kind"] == "current:error:TimeoutError"
        assert record["route_agrees"] is False
        assert evaluator.summary()["availability_difference"] == -1
    finally:
        evaluator.close(wait=True)


def test_preflight_unavailability_is_recorded_as_unavailable():
    current = FakeJevService(route="rag", reason="retrieval_confident")
    candidate = FakeJevService(route="legacy", reason="preflight_unavailable:rate_limited")
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("年假有几天", kb_id="kb1", query="年假") is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

        record = evaluator.comparisons()[0]
        assert record["candidate_available"] is False
        assert record["current_available"] is True
        assert record["failure_kind"] == "candidate:rate_limited"
        # "rag" versus the legacy fallback the candidate side had to take.
        assert record["route_agrees"] is False
        # No route from the unavailable side means no passage judgment was attempted.
        assert candidate.select_calls == 0
    finally:
        evaluator.close(wait=True)


def test_passage_unavailability_is_recorded_as_unavailable():
    current = FakeJevService(select_result=selection_of(chunk("a.md", 0)))
    candidate = FakeJevService(select_unavailable=True)
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    try:
        assert evaluator.submit("年假有几天", kb_id="kb1", query="年假", candidates=(chunk("a.md", 0),)) is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

        record = evaluator.comparisons()[0]
        assert record["route_agrees"] is True
        assert record["current_available"] is True
        assert record["candidate_available"] is False
        assert record["failure_kind"] == "candidate:passages_unavailable"
        assert record["selection_agrees"] is None
    finally:
        evaluator.close(wait=True)


# ---------------------------------------------------------------------------
# Bounded executor: never block the online path.
# ---------------------------------------------------------------------------


def test_full_queue_drops_work_instead_of_blocking():
    blocking = BlockingJevService()
    evaluator = CandidateShadowEvaluator(blocking, blocking, sample_rate=1.0, queue_limit=1)
    try:
        started = time.perf_counter()
        assert evaluator.submit("q1", kb_id="kb", query="q1") is True
        assert evaluator.submit("q2", kb_id="kb", query="q2") is False
        assert evaluator.submit("q3", kb_id="kb", query="q3") is False
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"submit blocked for {elapsed:.3f}s"
        assert evaluator.stats()["dropped"] == 2
        assert evaluator.stats()["completed"] == 0

        blocking.release.set()
        assert _wait_for(lambda: evaluator.stats()["completed"] == 1)
        assert evaluator.stats()["dropped"] == 2
        assert len(evaluator.comparisons()) == 1
    finally:
        evaluator.close(wait=True)


def test_close_silently_drops_submissions():
    current = FakeJevService()
    candidate = FakeJevService()
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=4)
    evaluator.close(wait=True)
    assert evaluator.submit("年假有几天", kb_id="kb1", query="年假", candidates=(chunk("a.md", 0),)) is False
    assert evaluator.close() is None  # closing twice is a no-op
    assert evaluator.stats()["closed"] is True
    assert evaluator.stats()["dropped"] == 1
    assert current.route_calls == 0
    assert candidate.route_calls == 0
    assert evaluator.comparisons() == []


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        CandidateShadowEvaluator(FakeJevService(), FakeJevService(), sample_rate=1.5)
    with pytest.raises(ValueError):
        CandidateShadowEvaluator(FakeJevService(), FakeJevService(), sample_rate=-0.1)
    with pytest.raises(ValueError):
        CandidateShadowEvaluator(FakeJevService(), FakeJevService(), queue_limit=0)


# ---------------------------------------------------------------------------
# Summary shape.
# ---------------------------------------------------------------------------


def test_summary_reports_agreement_rates_and_availability_difference():
    left, middle = chunk("a.md", 0), chunk("b.md", 1)
    current = ScriptedJevService(
        routes={
            "agree": ("rag", "retrieval_confident"),
            "disagree": ("rag", "retrieval_confident"),
            "candidate-broken": ("rag", "retrieval_confident"),
            "both-tool": ("tool", "tool_confident"),
        },
        selections={"agree": selection_of(left, middle)},
    )
    candidate = ScriptedJevService(
        routes={
            "agree": ("rag", "retrieval_confident"),
            "disagree": ("structured", "structured_confident"),
            "both-tool": ("tool", "tool_confident"),
        },
        selections={"agree": selection_of(middle)},
        errors=("candidate-broken",),
    )
    evaluator = CandidateShadowEvaluator(current, candidate, sample_rate=1.0, queue_limit=16)
    try:
        for question in ("agree", "disagree", "candidate-broken", "both-tool"):
            payload = (left, middle) if question in ("agree", "disagree") else ()
            assert evaluator.submit(question, kb_id="kb", query=question, candidates=payload) is True
        assert _wait_for(lambda: evaluator.stats()["completed"] == 4)

        summary = evaluator.summary()
        assert summary["samples"] == 4
        # "agree" and "both-tool" agree on the route; "disagree" is a real route
        # mismatch and "candidate-broken" produced no candidate route at all.
        assert summary["route_agreement"] == pytest.approx(0.5)
        # Only "agree" and "disagree" carried candidates: "agree" accepts one of the
        # two current passages (0.5 overlap), "disagree" accepts the same two (1.0).
        assert summary["selection_agreement_mean"] == pytest.approx(0.75)
        assert summary["selection_agreement_samples"] == 2
        assert summary["current_available"] == 4
        assert summary["candidate_available"] == 3
        assert summary["availability_difference"] == 1
        assert summary["sample_rate"] == 1.0
        assert summary["queue_limit"] == 16
        assert summary["closed"] is False
    finally:
        evaluator.close(wait=True)


def test_summary_of_an_empty_evaluator_reports_no_samples():
    evaluator = CandidateShadowEvaluator(FakeJevService(), FakeJevService(), sample_rate=0.0)
    try:
        summary = evaluator.summary()
        assert summary["samples"] == 0
        assert summary["route_agreement"] is None
        assert summary["selection_agreement_mean"] is None
        assert summary["availability_difference"] == 0
    finally:
        evaluator.close(wait=True)
