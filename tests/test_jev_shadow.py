"""Task 7: stage tracing and non-blocking shadow evaluation tests."""

import threading
import time

from rag.jev.models import PreflightDecision
from rag.jev.policy import KbCapabilities, PassageSelection, RouteDecision
from rag.jev.shadow import ShadowCase, ShadowEvaluator
from rag.models import Chunk
from rag.tracker import ExecutionTrace, ExecutionTracker


def shadow_case() -> ShadowCase:
    candidate = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    return ShadowCase(
        question="年假有几天",
        query="年假有几天",
        legacy_route="rag",
        legacy_ranks=(("policy.md#0", 0.8),),
        candidates=(candidate,),
        capabilities=KbCapabilities(),
    )


class BlockingJev:
    def __init__(self):
        self.release = threading.Event()
        self.route_calls = 0
        self.select_calls = 0

    def route(self, question, capabilities):
        self.route_calls += 1
        self.release.wait(timeout=5)
        return RouteDecision(route="rag", should_rewrite=False, reason="retrieval_confident", preflight=None)

    def select(self, question, candidates, *, query=None, top_k=None):
        self.select_calls += 1
        self.release.wait(timeout=5)
        return PassageSelection(accepted=tuple(candidates), conflicts=(), excluded_ids=(), has_answer_evidence=True)


def test_shadow_submit_does_not_wait_for_jev():
    blocking = BlockingJev()
    evaluator = ShadowEvaluator(blocking, max_workers=1, queue_limit=8)
    started = time.perf_counter()
    assert evaluator.submit(shadow_case()) is True
    assert time.perf_counter() - started < 0.05
    blocking.release.set()
    evaluator.close(wait=True)


def test_full_queue_drops_work_instead_of_blocking():
    blocking = BlockingJev()
    evaluator = ShadowEvaluator(blocking, max_workers=1, queue_limit=1)
    assert evaluator.submit(shadow_case()) is True
    assert evaluator.submit(shadow_case()) is False
    assert evaluator.stats()["dropped"] == 1
    blocking.release.set()
    evaluator.close(wait=True)
    assert evaluator.stats()["dropped"] == 1


def test_close_stops_accepting_work_and_shuts_down_executor():
    blocking = BlockingJev()
    evaluator = ShadowEvaluator(blocking, max_workers=1, queue_limit=4)
    assert evaluator.submit(shadow_case()) is True
    blocking.release.set()
    evaluator.close(wait=True)
    assert evaluator.submit(shadow_case()) is False
    assert evaluator.stats()["closed"] is True


class RecordingJev:
    def __init__(self):
        self.route_calls = 0
        self.select_calls = 0
        self.comparisons = []

    def route(self, question, capabilities):
        self.route_calls += 1
        return RouteDecision(
            route="rag",
            should_rewrite=False,
            reason="retrieval_confident",
            preflight=PreflightDecision(
                intent="retrieval_qa",
                intent_confidence=0.9,
                complexity="simple",
                complexity_confidence=0.9,
                rewrite_probability=0.1,
                model="jev-latest",
                question_set_version="preflight-v1",
                duration_ms=1.0,
            ),
        )

    def select(self, question, candidates, *, query=None, top_k=None):
        self.select_calls += 1
        candidate = candidates[0]
        return PassageSelection(
            accepted=(candidate,),
            conflicts=(),
            excluded_ids=(),
            has_answer_evidence=True,
        )


def _wait_for(predicate, timeout=2.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_shadow_reuses_one_client_and_records_comparisons():
    jev = RecordingJev()
    comparisons = []
    evaluator = ShadowEvaluator(
        jev, max_workers=2, queue_limit=8, on_comparison=comparisons.append
    )
    assert evaluator.submit(shadow_case()) is True
    assert evaluator.submit(shadow_case()) is True
    assert _wait_for(lambda: evaluator.stats()["completed"] == 2)

    assert jev.route_calls == 2
    assert jev.select_calls == 2
    assert len(comparisons) == 2
    assert comparisons[0].legacy_route == "rag"
    assert comparisons[0].jev_route == "rag"
    assert comparisons[0].route_agrees is True
    assert comparisons[0].jev_available is True
    assert comparisons[0].top_k_overlap == 1.0
    evaluator.close(wait=True)


def test_shadow_records_failure_kind_without_raising():
    class FailingJev(RecordingJev):
        def route(self, question, capabilities):
            self.route_calls += 1
            return RouteDecision(
                route="legacy", should_rewrite=False, reason="preflight_unavailable:timeout", preflight=None
            )

        def select(self, question, candidates, *, query=None, top_k=None):
            self.select_calls += 1
            return None

    comparisons = []
    evaluator = ShadowEvaluator(FailingJev(), max_workers=1, queue_limit=4, on_comparison=comparisons.append)
    assert evaluator.submit(shadow_case()) is True
    assert _wait_for(lambda: evaluator.stats()["completed"] == 1)

    assert comparisons[0].jev_available is False
    assert "timeout" in comparisons[0].failure_kind
    evaluator.close(wait=True)


def test_shadow_survives_unexpected_client_errors():
    class ExplodingJev(RecordingJev):
        def route(self, question, capabilities):
            raise RuntimeError("unexpected shadow failure")

    evaluator = ShadowEvaluator(ExplodingJev(), max_workers=1, queue_limit=4)
    assert evaluator.submit(shadow_case()) is True
    assert _wait_for(lambda: evaluator.stats()["completed"] == 1)
    evaluator.close(wait=True)


def test_trace_serializes_stage_timings_without_secret(tmp_path):
    tracker = ExecutionTracker(tmp_path / "trace.db")
    tracker.save(
        ExecutionTrace(
            question="q",
            route="rag",
            stage_timings={"jev_preflight_ms": 12.5, "jev_passages_ms": 30.0},
            decision_metadata={
                "jev_model": "jev-latest",
                "question_set_version": "preflight-v1",
                "jev_route": "rag",
                "intent_confidence": 0.9,
                "attempt_count": 1,
                "api_key": "secret-test-key",
                "passages": [{"text": "unauthorized"}],
            },
        )
    )
    details = tracker.get_recent(1)[0]["details"]
    assert details
    assert "secret-test-key" not in details
    assert "unauthorized" not in details
    assert "jev_preflight_ms" in details
    assert "jev-latest" in details


def test_trace_tool_calls_and_stage_timings_round_trip(tmp_path):
    from rag.tracker import ToolCall

    tracker = ExecutionTracker(tmp_path / "trace.db")
    tracker.save(
        ExecutionTrace(
            question="计算增长率",
            route="agent",
            tool_calls=[ToolCall(tool_name="calculate", input="1+1", output="2", duration_ms=5.0)],
            stage_timings={"preparation_ms": 12.0},
        )
    )
    details = tracker.get_recent(1)[0]["details"]
    assert "calculate" in details
    assert "preparation_ms" in details
