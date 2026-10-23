from __future__ import annotations

import traceback
from pathlib import Path

from rag.stage_timing import current_stage_timings


def _exception_frame(error: BaseException) -> str:
    """异常栈的最后一帧，形如 ``rag/pipeline_query.py:700:stream_prepared``。

    只取帧引用（文件:行:函数）：它足够让诊断与补丁生成指向正确的位置，又不会把异常消息、
    提示词或用户文本带进"存储不含模型/用户文本"的边界。库外的帧（例如 site-packages 里的
    调用）退化成文件名，让下游按关键词回退排序。
    """
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return ""
    frame = frames[-1]
    try:
        root = Path(__file__).resolve().parent.parent
        label = Path(frame.filename).resolve().relative_to(root).as_posix()
    except ValueError:
        label = Path(frame.filename).name
    return f"{label}:{frame.lineno}:{frame.name}"


def evolution_submission(bindings, owner, question, prepared, error=None):
    """Build a diagnosable detail from ACL-filtered prepared data; None means nothing to record.

    The question text travels on the execution trace; this helper only decides
    which stored signals describe one diagnosable failure.

    ``failure_stage`` is attributed **from the refusal reasons** (A-10): a request that
    retrieved nothing, one that retrieved candidates without answer evidence, and one whose
    scores are unavailable (rerank/scoring dependency degraded) are three different failures
    and must not all be recorded as retrieval gaps.
    """
    DiagnosticDetail = bindings['DiagnosticDetail']
    NonEvolvableReason = bindings['NonEvolvableReason']
    _REFUSAL_FAILURE_STAGES = bindings['_REFUSAL_FAILURE_STAGES']
    _bundle_top_score = bindings['_bundle_top_score']
    if error is not None or prepared is None:
        # guard 拦截：不产生观测，只计入跳过日志。
        return DiagnosticDetail(
            kb_id=owner._kb_id, non_evolvable_reason=NonEvolvableReason.PERMISSION_DENIED
        )
    decision = prepared.get("decision")
    if decision is None:
        return None
    if decision.kind == "insufficient_evidence":
        confidence = prepared.get("confidence")
        metadata = prepared.get("decision_metadata") or {}
        reasons = tuple(getattr(decision, "reasons", ()) or ())
        stage = next(
            (
                _REFUSAL_FAILURE_STAGES[reason]
                for reason in reasons
                if reason in _REFUSAL_FAILURE_STAGES
            ),
            "generation" if prepared.get("chunk_hashes") else "retrieval",
        )
        return DiagnosticDetail(
            kb_id=owner._kb_id,
            failure_stage=stage,
            answerability_kind=decision.kind,
            confidence_level=confidence.level if confidence is not None else None,
            chunk_hashes=tuple(prepared.get("chunk_hashes") or ()),
            best_score=_bundle_top_score(prepared),
            # 评分不可用是重排/评分依赖的瞬时故障（与熔断同类），不是可诊断的产品缺陷：
            # 记成跳过，绝不把一次依赖降级变成一条"检索缺口"观测。
            non_evolvable_reason=(
                NonEvolvableReason.TRANSIENT_EXTERNAL
                if "scores_unavailable" in reasons
                else None
            ),
            # 这次请求试过结构化没有、结果是什么。`failed` 尤其重要：它是内部执行错误，
            # 此前与"没有表格数据"一样都表现为 None，诊断时无法区分。
            structured_query_kind=metadata.get("structured_query_kind") or None,
        )
    if decision.kind == "permission_limited":
        # 权限不足：同样只计入跳过日志，绝不产生观测。
        return DiagnosticDetail(
            kb_id=owner._kb_id, non_evolvable_reason=NonEvolvableReason.PERMISSION_DENIED
        )
    return None


def submit_failure_observation(bindings, owner, trace, prepared, error=None):
    """Queue one non-blocking failure observation; the online path never waits."""
    recorder = getattr(owner, "_failure_recorder", None)
    if recorder is None:
        return
    detail = owner._evolution_submission(trace.question, prepared, error)
    if detail is None:
        return
    recorder.submit(trace, detail)


def record_stream_failure(
    bindings, owner,
    question: str,
    start_time: float,
    prepared: dict | None,
    error: BaseException,
) -> None:
    """一次失败：流式请求死掉，或 Agent 子路径失败后降级——先留痕，再分类。

    传输/供应商侧故障（超时、连接、限流）是设计第 5 节明确排除的类别，记为跳过；
    **其余异常是内部缺陷**（例如证据缺少版本元数据），记为观测并带上 exception_class
    ——这类失败恰恰是进化回路该看到的东西，不该被当成网络抖动埋掉。异常本身由调用方
    继续上抛，客户端照旧收到一个 error 事件。
    """
    DiagnosticDetail = bindings['DiagnosticDetail']
    ExecutionTrace = bindings['ExecutionTrace']
    NonEvolvableReason = bindings['NonEvolvableReason']
    _TRANSIENT_ERROR_NAMES = bindings['_TRANSIENT_ERROR_NAMES']
    _exception_frame = bindings['_exception_frame']
    time = bindings['time']
    CircuitOpenError = bindings['CircuitOpenError']
    circuit_open_cause = bindings['circuit_open_cause']
    collector = current_stage_timings()
    stage_timings = dict((prepared or {}).get("stage_timings") or {})
    if collector is not None:
        snapshot = collector.snapshot() if (prepared or {}).get("route") == "agent" else collector.seal()
        stage_timings.update(snapshot)
    outcome = (
        "degraded"
        if isinstance(error, CircuitOpenError) or circuit_open_cause(error) is not None
        else "failed"
    )
    decision_metadata = dict((prepared or {}).get("decision_metadata") or {})
    decision_metadata["execution_outcome"] = outcome
    trace = ExecutionTrace(
        question=question,
        route="error",
        answer="",
        total_ms=(time.time() - start_time) * 1000,
        chunk_hashes=(prepared or {}).get("chunk_hashes") or [],
        stage_timings=stage_timings,
        decision_metadata=decision_metadata,
    )
    owner.tracker.save(trace)
    recorder = getattr(owner, "_failure_recorder", None)
    if recorder is None:
        return
    if type(error).__name__ in _TRANSIENT_ERROR_NAMES:
        recorder.submit(
            trace,
            DiagnosticDetail(
                kb_id=owner._kb_id,
                non_evolvable_reason=NonEvolvableReason.TRANSIENT_EXTERNAL,
            ),
        )
        return
    recorder.submit(
        trace,
        DiagnosticDetail(
            kb_id=owner._kb_id,
            exception_class=type(error).__name__,
            exception_frame=_exception_frame(error),
        ),
    )

