import argparse
import asyncio
import hashlib
import json as _json
import logging
import re
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace  # noqa: F401 - public facade patch surface
from typing import Any, NamedTuple, Protocol, runtime_checkable

from config import settings
from rag import agent as _agent_mod
from rag import feedback_processor as _feedback_processor_mod
from rag import gap_analyzer as _gap_analyzer_mod
from rag import memory as _memory_mod
from rag import permissions as _permissions_mod  # noqa: F401 - public facade patch surface
from rag import pipeline_jev as _pipeline_jev
from rag import pipeline_prepare as _pipeline_prepare
from rag import versioned_retrieval as _versioned_retrieval_mod  # noqa: F401 - public facade patch surface
from rag.chunker import chunk
from rag.cleaner import clean_document, deduplicate_chunks
from rag.embedder import embed
from rag.evolution.models import DiagnosticDetail, NonEvolvableReason
from rag.generator import generate, generate_stream
from rag.guard import (
    apply_semantic_injection_check,  # noqa: F401 - public facade patch surface
    check_document_injection,  # noqa: F401 - public facade patch surface
    check_injection,  # noqa: F401 - public facade patch surface
    check_output,
    sanitize_input,  # noqa: F401 - public facade patch surface
)
from rag.jev.client import JevClient
from rag.jev.policy import KbCapabilities, is_self_contained_lookup  # noqa: F401 - public facade patch surface
from rag.jev.service import JevDecisionService
from rag.jev.shadow import ShadowCase, ShadowComparison, ShadowEvaluator  # noqa: F401 - public facade patch surface
from rag.llm_pool import get_llm_pool_router
from rag.loader import load
from rag.models import Chunk
from rag.pipeline_bindings import PipelineBindings
from rag.query_rewriter import rewrite_query  # noqa: F401 - public facade patch surface
from rag.redis_cache import RedisCache  # noqa: F401 - public compatibility import
from rag.reliability import (
    assess_confidence,  # noqa: F401 - public facade patch surface
    build_evidence_bundle,  # noqa: F401 - public facade patch surface
    decide_answerability,  # noqa: F401 - public facade patch surface
    fixed_decision_copy,  # noqa: F401 - public facade patch surface
)
from rag.reliability.models import AnswerabilityDecision, ConfidenceAssessment
from rag.reranker import Reranker, merge_ranked_chunks  # noqa: F401 - public facade patch surface
from rag.resilience import (
    DEGRADED_ANSWER_TEXT,
    CircuitOpenError,
    circuit_open_cause,
    is_degraded_answer,
)
from rag.retriever import Retriever
from rag.spreadsheets.coordinator import SpreadsheetQueryCoordinator
from rag.tracker import ExecutionTrace, ExecutionTracker, ToolCall
from rag.vector_store import add, delete_doc
from rag.vector_store import clear as clear  # noqa: F401

logger = logging.getLogger(__name__)
_PIPELINE_BINDINGS = PipelineBindings(MappingProxyType(globals()))

def _binding_for(owner):
    return getattr(owner, "_pipeline_bindings", _PIPELINE_BINDINGS)


@runtime_checkable
class FailureObserver(Protocol):
    """可插拔的失败观测器/记录器协议，解除主干链路对演化子系统的直接编译期依赖。"""

    def submit(self, trace: ExecutionTrace, detail: Any = None) -> bool:
        ...

    def close(self) -> None:
        ...


def _create_default_failure_recorder() -> FailureObserver | None:
    """按需构建默认的演化失败记录器；解耦主干对演化子系统的直接编译期依赖。"""
    try:
        from rag.evolution.recorder import FailureRecorder
        from rag.evolution.store import FailureTraceStore

        return FailureRecorder(
            FailureTraceStore(settings.evolution_db_path),
            max_queue=settings.evolution_queue_limit,
        )
    except ImportError:
        logger.warning("Evolution subsystem not available; failure recording disabled.")
        return None

_HISTORICAL_EVIDENCE_WARNING = "注意：以下回答仅依据历史版本资料，内容可能已过期。\n\n"
_RETRIEVAL_OVERSAMPLE = 3

#: 拒答原因 → 失败阶段（A-10）。原因由 ``decide_answerability`` 给出，明确区分三种情况：
#: 什么都没检索到（检索缺口）；检索到并打过分、只是没有答案证据（证据门/生成侧）；
#: 以及分数不可用（评分依赖降级，不是产品缺陷，因此阶段记 unknown 且按瞬时故障跳过诊断）。
_REFUSAL_FAILURE_STAGES = {
    "nothing_retrieved": "retrieval",
    "no_answer_evidence": "generation",
    "scores_unavailable": "unknown",
}

#: 传输/供应商侧故障的异常类名：这类失败重试即可，记为跳过而不是观测（设计第 5 节把
#: 瞬态外部故障排除在诊断之外）。按类名判断，避免 pipeline 直接依赖 provider SDK——
#: 诊断器在自己的模块里引入 openai 是因为它必须构造客户端，这里只是在分类异常。
_TRANSIENT_ERROR_NAMES = frozenset(
    {
        "APITimeoutError",
        "APIConnectionError",
        "APIConnectionTimeoutError",
        "RateLimitError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "RemoteProtocolError",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        # 熔断本身就是供应商侧暂时不可用（重试即可），按瞬态外部故障记为跳过，
        # 而不是当成内部缺陷进诊断队列。
        "CircuitOpenError",
    }
)


class QueryResult(NamedTuple):
    answer: str
    context: list[Chunk]
    sources: list[dict]
    decision: AnswerabilityDecision | None = None
    confidence: ConfidenceAssessment | None = None


def _reliability_payload(prepared: dict) -> dict | None:
    decision = prepared.get("decision")
    confidence = prepared.get("confidence")
    if decision is None or confidence is None:
        return None
    return {
        "confidence_level": confidence.level,
        "confidence_reasons": list(confidence.reasons),
        "answerability_kind": decision.kind,
    }


def _degraded_sse_event() -> str:
    """SSE error event for a generation that was never produced (A-02).

    It is deliberately **not** a ``done`` event: a degraded request must not carry
    reliability or look like a formal answer. The client sees an explicit failure.
    """
    payload = {"type": "error", "reason": DEGRADED_ANSWER_TEXT, "degraded": True}
    return f'data: {_json.dumps(payload, ensure_ascii=False)}\n\n'


#: 判定层认为"这次请求给出了答案"的取值。与基准的位置记分（``ANSWER_POSITIONS``）同源：
#: ``clarification_required`` / ``permission_limited`` 都是没有答案的固定文案。
_ANSWERED_DECISION_KINDS = frozenset({"answerable", "answerable_with_warning"})


#: 企业模式拒绝 agent/tool 分支时的固定文案。旧文案说"资料存在冲突或范围不明确"，
#: 但真实原因是"这个问题需要工具执行，而企业问答只基于已授权资料"（A-07）。文案必须说
#: 清楚真实原因，否则用户会去改时间范围/部门，而问题不会因此变得可答。
_TOOL_ROUTE_UNAVAILABLE_COPY = (
    "这个问题需要工具执行或计算，当前知识库问答只基于已授权资料作答。"
    "请改写为资料查询类问题，或联系管理员开启工具能力。"
)


def _produced_answer(prepared: dict) -> bool:
    """Did this prepared request actually give the user an answer?

    问的不是"检索有没有返回来源"——Jev 路径的拒答**本来就带来源**（判定为相关但无答案证据的
    块进 citations 兜底），所以"有来源"曾经被当成"检索赢了"。实测（2026-10-19，
    kb_accessories「镜头价格合计」）：规划器要求澄清"请选择要计算的字段"，检索找到 7 个来源，
    证据门判 insufficient_evidence ⇒ 澄清被丢掉，用户看到"现有可见资料不足"——既没有答案，
    也没有那句本来能让他把问题问清楚的话。

    非企业请求不建 evidence bundle，没有判定可读，退回"有没有来源"这一旧口径。
    """
    decision = prepared.get("decision")
    if decision is None:
        return bool(prepared.get("sources"))
    return getattr(decision, "kind", None) in _ANSWERED_DECISION_KINDS


def _bundle_top_score(prepared: dict) -> float | None:
    """Best ACL-filtered retrieval score from the reliability bundle, when one was built."""
    metrics = getattr(prepared.get("evidence_bundle"), "retrieval_metrics", None)
    return getattr(metrics, "top_score", None)


def _agent_run_kwargs(prepared: dict, *, pipeline=None, start_time: float = None) -> dict:
    """从 prepared 中提取需要透传给 agent.run 的 user/kb_id，并接上失败上报。

    权限参数只在调用方显式提供时才传递，保持旧的无权限调用签名 ``run(question)``（向后兼容）。
    ``failure_reporter`` 则总是接上（只要给了 pipeline）：agent 子路径失败并降级时，由它走
    **与流式失败相同的那条路径**提交观测——否则 Agent 路径的失败对进化回路完全不可见。
    """
    kwargs = {}
    if prepared.get("user") is not None:
        kwargs["user"] = prepared["user"]
    if prepared.get("kb_id") is not None:
        kwargs["kb_id"] = prepared["kb_id"]
    if pipeline is not None and start_time is not None:
        kwargs["failure_reporter"] = lambda error: pipeline._record_stream_failure(
            str(prepared.get("question") or ""), start_time, prepared, error
        )
    return kwargs


def _log_shadow_comparison(comparison: ShadowComparison) -> None:
    """Record one shadow comparison without question text, chunk text, or credentials."""
    logger.info(
        "Jev shadow comparison: legacy_route=%s jev_route=%s agrees=%s available=%s failure=%s overlap=%.3f",
        comparison.legacy_route,
        comparison.jev_route,
        comparison.route_agrees,
        comparison.jev_available,
        comparison.failure_kind,
        comparison.top_k_overlap,
    )


def _exception_frame(error: BaseException) -> str:
    """异常栈的最后一帧，形如 ``rag/pipeline.py:700:_finalize_rag_context``。

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


_PRICE_INTENT_RE = re.compile(r"价格|售价|报价|多少钱|费用|金额|成本|单价|总价")
_REDACTED_VALUE_RE = re.compile(r"[<＜]金额[>＞]|\[REDACTED\]|\*{3,}")


def _drop_redacted_price_evidence(question: str, selection):
    """A redacted amount is not an answer to a price question.

    Jev can correctly judge a model/parameter row as relevant and still see the price
    cell as ``<金额>``. Treating that as answer evidence produces a confident answer
    that cannot contain the asked amount. Keep the chunk for citations, but report no
    usable evidence so the pipeline refuses instead of inventing a price.
    """
    if not selection.has_answer_evidence or not _PRICE_INTENT_RE.search(question):
        return selection
    evidence_chunks = [
        chunk
        for chunk in selection.accepted
        if chunk.metadata.get("jev_answer_evidence") == "true"
    ]
    if evidence_chunks and all(
        _REDACTED_VALUE_RE.search(chunk.text) for chunk in evidence_chunks
    ):
        return replace(selection, has_answer_evidence=False)
    return selection


def _history_signature(history: list[tuple[str, str]], summary: str | None) -> str:
    """Stable key for the conversation context a rewrite was produced from.

    The rewrite is LLM-generated, so two calls with the same question but different history are
    legitimately different inputs. The signature keeps the cache from serving one turn's rewrite
    to another turn that only shares the question text.
    """
    digest = hashlib.sha256()
    for role, content in history:
        digest.update(str(role).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(content).encode("utf-8"))
        digest.update(b"\0")
    digest.update(str(summary or "").encode("utf-8"))
    return digest.hexdigest()


class RAGPipeline:
    def __init__(
        self,
        file_path: str = None,
        session_id: str = None,
        memory_db_path: str = settings.memory_db_path,
        kb_id: str = None,
        user_db=None,
        feedback_processor=None,
        gap_analyzer=None,
        jev_service: JevDecisionService | None = None,
        failure_recorder: FailureObserver | None = None,
        question_set_rollout=None,
        semantic_injection_detector=None,
    ):
        self._pipeline_bindings = _PIPELINE_BINDINGS
        if kb_id:
            # 查询已有知识库，不重新索引
            self.chunks = []
            self.retriever = Retriever(self.chunks, collection_name=kb_id)
        else:
            # 索引新文档到默认集合
            text = load(file_path)
            text, metadata = clean_document(text)
            doc_name = file_path.split("/")[-1].split("\\")[-1]
            self.chunks = chunk(text, doc_name=doc_name, metadata=metadata)
            # 去重（SequenceMatcher 相似度 > 0.95 的段落被去除）
            chunk_texts = [c.text for c in self.chunks]
            unique_texts = deduplicate_chunks(chunk_texts)
            if len(unique_texts) < len(chunk_texts):
                chunks_by_text = {c.text: c for c in reversed(self.chunks)}
                self.chunks = [chunks_by_text[text] for text in unique_texts]
            embeddings = embed([c.text for c in self.chunks])
            delete_doc("rag_docs", doc_name)
            add(self.chunks, embeddings)
            self.retriever = Retriever(self.chunks)
        self.session_id = session_id or file_path
        self.memory = _memory_mod.DialogueMemory(db_path=memory_db_path, generate_fn=generate)
        self.reranker = Reranker()
        # Jev 决策服务：仅当模式非 off 时创建默认实例；注入的服务由调用方负责生命周期。
        # 层 4 灰度：控制器既提供每请求的灰度键（下面发布），也提供问题集本身——把它的
        # provide() 交给客户端，候选问题集才可能在服务路径上生效。未注入控制器时客户端
        # 走静态问题集，在线行为零变化。
        self._owns_jev_service = False
        if jev_service is not None:
            self.jev = jev_service
        elif settings.jev_mode != "off":
            provider = (
                question_set_rollout.provide if question_set_rollout is not None else None
            )
            self.jev = JevDecisionService(JevClient(question_set_provider=provider))
            self._owns_jev_service = True
        else:
            self.jev = None
        # 影子模式：旧链路正常响应，后台比对 Jev 判断。
        self._shadow_evaluator = None
        self._owns_shadow_evaluator = False
        if settings.jev_mode == "shadow" and self.jev is not None:
            self._shadow_evaluator = ShadowEvaluator(self.jev, on_comparison=_log_shadow_comparison)
            self._owns_shadow_evaluator = True
        # 语义注入检测（A-05）：独立的一层，不再跟着 Jev 路由模式走。显式注入的检测器优先；
        # 否则任何可用的 Jev 服务都提供它（含 shadow/注入式 jev_service），legacy 路径同样生效。
        # 检测器不可用/报错时 fail-open，见 rag.guard.apply_semantic_injection_check。
        self._semantic_injection_detector = semantic_injection_detector
        if self._semantic_injection_detector is None and self.jev is not None:
            self._semantic_injection_detector = getattr(self.jev, "detect_injection", None)
        self.agent = _agent_mod.RAGAgent(retriever=self.retriever, user_db=user_db, kb_id=kb_id or "rag_docs")
        self.tracker = ExecutionTracker(db_path=memory_db_path)
        self._owns_feedback_processor = feedback_processor is None
        self._owns_gap_analyzer = gap_analyzer is None
        if feedback_processor is None:
            feedback_processor = _feedback_processor_mod.FeedbackProcessor(memory_db_path)
        if gap_analyzer is None:
            gap_analyzer = _gap_analyzer_mod.GapAnalyzer(memory_db_path)
        self._feedback_processor = feedback_processor
        self._gap_analyzer = gap_analyzer
        # 观测记录器：注入实例由调用方负责生命周期；自建实例仅在 flag 开启时创建。
        self._failure_recorder = None
        self._owns_failure_recorder = False
        if failure_recorder is not None:
            self._failure_recorder = failure_recorder
        elif settings.evolution_enabled:
            self._failure_recorder = _create_default_failure_recorder()
            self._owns_failure_recorder = self._failure_recorder is not None
        # 层 4 灰度：问题集提供者由 rollout 控制器持有，这里只发布每请求的灰度键
        # （kb + 会话 + 问题），让同一请求的 preflight 与 passages 落入同一个桶。
        # 默认 None：不注入时在线行为零变化，Jev 客户端走静态问题集。
        self._question_set_rollout = question_set_rollout
        self._closed = False
        self._agent_lock = threading.Lock()
        self._async_agent_lock = asyncio.Lock()
        # 检索前改写的进程内缓存：同一 (问题, 历史签名) 复用一次改写，消掉重复抖动。
        # 只缓存改变了问题的非空结果（见 _rewrite_cached）。
        self._rewrite_cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._rewrite_cache_lock = threading.Lock()

        # 权限管理：注入 user_db 引用（由 api.py 传入，避免每次查询新建连接）
        self._user_db = user_db
        self._kb_id = kb_id or "rag_docs"
        self._enterprise_mode = kb_id is not None
        self._spreadsheet_coordinator = None
        if user_db is not None and kb_id is not None:
            self._spreadsheet_coordinator = SpreadsheetQueryCoordinator(
                user_db, Path(settings.structured_datasets_root)
            )

    def close(self) -> None:
        """Close connections owned by this pipeline; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self.memory.close()
        self.tracker.close()
        reranker = getattr(self, "reranker", None)
        if reranker is not None and hasattr(reranker, "close"):
            reranker.close()
        if self._owns_feedback_processor:
            self._feedback_processor.close()
        if self._owns_gap_analyzer:
            self._gap_analyzer.close()
        if self._owns_shadow_evaluator and self._shadow_evaluator is not None:
            self._shadow_evaluator.close()
        if self._owns_jev_service and self.jev is not None:
            self.jev.close()
        if self._owns_failure_recorder and self._failure_recorder is not None:
            self._failure_recorder.close()

    def _jev_active(self, effective_kb_id: str) -> bool:
        return _pipeline_jev._jev_active(_binding_for(self), self, effective_kb_id)

    def _evolution_submission(self, question, prepared, error=None):
        """Build a diagnosable detail from ACL-filtered prepared data; None means nothing to record.

        The question text travels on the execution trace; this helper only decides
        which stored signals describe one diagnosable failure.

        ``failure_stage`` is attributed **from the refusal reasons** (A-10): a request that
        retrieved nothing, one that retrieved candidates without answer evidence, and one whose
        scores are unavailable (rerank/scoring dependency degraded) are three different failures
        and must not all be recorded as retrieval gaps.
        """
        if error is not None or prepared is None:
            # guard 拦截：不产生观测，只计入跳过日志。
            return DiagnosticDetail(
                kb_id=self._kb_id, non_evolvable_reason=NonEvolvableReason.PERMISSION_DENIED
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
                kb_id=self._kb_id,
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
                kb_id=self._kb_id, non_evolvable_reason=NonEvolvableReason.PERMISSION_DENIED
            )
        return None

    def _submit_failure_observation(self, trace: ExecutionTrace, prepared, error=None):
        """Queue one non-blocking failure observation; the online path never waits."""
        recorder = getattr(self, "_failure_recorder", None)
        if recorder is None:
            return
        detail = self._evolution_submission(trace.question, prepared, error)
        if detail is None:
            return
        recorder.submit(trace, detail)

    def _record_stream_failure(
        self,
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
        trace = ExecutionTrace(
            question=question,
            route="error",
            answer="",
            total_ms=(time.time() - start_time) * 1000,
            chunk_hashes=(prepared or {}).get("chunk_hashes") or [],
            stage_timings=(prepared or {}).get("stage_timings") or {},
            decision_metadata=(prepared or {}).get("decision_metadata") or {},
        )
        self.tracker.save(trace)
        recorder = getattr(self, "_failure_recorder", None)
        if recorder is None:
            return
        if type(error).__name__ in _TRANSIENT_ERROR_NAMES:
            recorder.submit(
                trace,
                DiagnosticDetail(
                    kb_id=self._kb_id,
                    non_evolvable_reason=NonEvolvableReason.TRANSIENT_EXTERNAL,
                ),
            )
            return
        recorder.submit(
            trace,
            DiagnosticDetail(
                kb_id=self._kb_id,
                exception_class=type(error).__name__,
                exception_frame=_exception_frame(error),
            ),
        )


    def _route_with_history_correction(self, question: str, session_id: str, capabilities):
        return _pipeline_prepare._route_with_history_correction(_binding_for(self), self, question, session_id, capabilities)

    def _memory_session_id(self, session_id: str | None, user: dict | None) -> str:
        return _pipeline_prepare._memory_session_id(_binding_for(self), self, session_id, user)

    def _history_is_available(self, session_id: str) -> bool:
        return _pipeline_prepare._history_is_available(_binding_for(self), self, session_id)

    def _rewrite_for_retrieval(self, question: str, session_id: str) -> str:
        return _pipeline_prepare._rewrite_for_retrieval(_binding_for(self), self, question, session_id)

    def _rewrite_cached(self, question: str, signature: str, produce) -> str:
        return _pipeline_prepare._rewrite_cached(_binding_for(self), self, question, signature, produce)

    def _rerank_for_evidence(self, original_question, retrieval_query, candidates, record=None):
        return _pipeline_prepare._rerank_for_evidence(_binding_for(self), self, original_question, retrieval_query, candidates, record)

    def _prepare_context(self, question: str, session_id: str, doc_name: str, top_k: int = 8, tags: list[str] = None, user: dict = None, kb_id: str = None):
        return _pipeline_prepare._prepare_context(_binding_for(self), self, question, session_id, doc_name, top_k, tags, user, kb_id)

    def _prepare_context_standard(
    self, question, session_id, doc_name, top_k, tags, user, kb_id,
    enterprise_request, effective_kb_id, coordinator, stage=None,
):
        return _pipeline_prepare._prepare_context_standard(_binding_for(self), self, question, session_id, doc_name, top_k, tags, user, kb_id, enterprise_request, effective_kb_id, coordinator, stage)

    # 向后兼容别名：供历史代码与测试打桩
    _prepare_context_legacy = _prepare_context_standard

    #: 判定器可见的字段名上限：够它认出"问题指向表格列"即可，再多只会拉长提示词与尾延迟。
    _STRUCTURED_FIELD_HINT_LIMIT = 40

    def _structured_field_names(self, coordinator, kb_id, user) -> tuple[str, ...]:
        return _pipeline_jev._structured_field_names(_binding_for(self), self, coordinator, kb_id, user)

    def _submit_shadow_case(self, question, query, legacy_route, context, candidates, coordinator, effective_kb_id=None, user=None):
        return _pipeline_jev._submit_shadow_case(_binding_for(self), self, question, query, legacy_route, context, candidates, coordinator, effective_kb_id, user)

    def _prepare_context_with_jev(
    self, question, session_id, doc_name, top_k, tags, user, kb_id,
    enterprise_request, effective_kb_id, coordinator,
):
        return _pipeline_jev._prepare_context_with_jev(_binding_for(self), self, question, session_id, doc_name, top_k, tags, user, kb_id, enterprise_request, effective_kb_id, coordinator)

    def _prepare_rag_with_jev(
    self, question, session_id, doc_name, top_k, tags, user, kb_id,
    enterprise_request, effective_kb_id, route_decision, stage,
):
        return _pipeline_jev._prepare_rag_with_jev(_binding_for(self), self, question, session_id, doc_name, top_k, tags, user, kb_id, enterprise_request, effective_kb_id, route_decision, stage)

    @staticmethod
    def _attach_jev_stage(prepared: dict, stage: dict) -> dict:
        return _pipeline_jev._attach_jev_stage(_PIPELINE_BINDINGS, prepared, stage)

    def _retrieve_visible_candidates(
    self,
    query: str,
    *,
    top_k: int,
    doc_name: str | None,
    tags: list[str] | None,
    user: dict | None,
    kb_id: str,
) -> list[Chunk]:
        return _pipeline_prepare._retrieve_visible_candidates(_binding_for(self), self, query, top_k=top_k, doc_name=doc_name, tags=tags, user=user, kb_id=kb_id)

    def _post_rank_acl(self, context: list[Chunk], user: dict | None, effective_kb_id: str) -> list[Chunk]:
        return _pipeline_prepare._post_rank_acl(_binding_for(self), self, context, user, effective_kb_id)

    def _drop_injected_chunks(self, context: list[Chunk], *, semantic: bool = True) -> list[Chunk]:
        return _pipeline_prepare._drop_injected_chunks(_binding_for(self), self, context, semantic=semantic)

    def _drop_semantically_injected(self, context: list[Chunk]) -> list[Chunk]:
        return _pipeline_prepare._drop_semantically_injected(_binding_for(self), self, context)

    def _finalize_rag_context(
    self, question, session_id, context, top_k, effective_kb_id, *, build_bundle, conflicts=(),
    citations=None, background=(),
):
        return _pipeline_prepare._finalize_rag_context(_binding_for(self), self, question, session_id, context, top_k, effective_kb_id, build_bundle=build_bundle, conflicts=conflicts, citations=citations, background=background)

    def _fixed_decision(self, question, effective_kb_id, kind, reasons, *, permission_limited=False, answer=None):
        return _pipeline_prepare._fixed_decision(_binding_for(self), self, question, effective_kb_id, kind, reasons, permission_limited=permission_limited, answer=answer)

    @staticmethod
    def _is_structured_clarification(prepared: dict | None) -> bool:
        return _pipeline_prepare._is_structured_clarification(_PIPELINE_BINDINGS, prepared)

    def _attempt_structured_query(self, question, coordinator, user, effective_kb_id, record=None):
        return _pipeline_prepare._attempt_structured_query(_binding_for(self), self, question, coordinator, user, effective_kb_id, record)

    @staticmethod
    def _conflict_payload(chunk: Chunk) -> dict:
        return _pipeline_prepare._conflict_payload(_PIPELINE_BINDINGS, chunk)

    def _filter_sources_by_permission(self, sources: list, kb_id: str, user: dict) -> list:
        return _pipeline_prepare._filter_sources_by_permission(_binding_for(self), self, sources, kb_id, user)

    def query(self, question: str, top_k: int = 8, session_id: str = None, doc_name: str = None, tags: list[str] = None, user: dict = None, kb_id: str = None) -> QueryResult:
        sid = self._memory_session_id(session_id, user)
        start_time = time.time()

        prepared, error = self._prepare_context(question, sid, doc_name, top_k, tags=tags, user=user, kb_id=kb_id)
        if error:
            trace = ExecutionTrace(question=question, route="blocked", answer=error, total_ms=0)
            self.tracker.save(trace)
            self._submit_failure_observation(trace, None, error)
            return QueryResult(answer=error, context=[], sources=[])

        if prepared["route"] == "cached":
            self.memory.add_message(sid, "user", question)
            self.memory.add_message(sid, "assistant", prepared["answer"])
            if self.memory.should_summarize(sid):
                self.memory.summarize_old_rounds(sid)
            return QueryResult(answer=prepared["answer"], context=[], sources=prepared["sources"])

        if prepared["route"] in {"fixed", "structured"}:
            answer = prepared["answer"]
            context = prepared["context"]
            sources = prepared["sources"]
            tool_calls = []
        elif prepared["route"] == "agent":
            captured_calls = []
            answer = self.agent.run(
                prepared["question"],
                tool_calls=captured_calls,
                **_agent_run_kwargs(prepared, pipeline=self, start_time=start_time),
            )
            context = []
            sources = []
            tool_calls = [ToolCall(**c) for c in captured_calls]
        else:
            try:
                answer = generate(prepared["messages"])
            except CircuitOpenError as error:
                # 熔断不是答案：留失败轨迹/观测后把可识别失败交给端点，绝不入 memory、
                # 也绝不附带 reliability（P1-02）。
                logger.warning("sync generation degraded because the circuit breaker is open")
                self._record_stream_failure(question, start_time, prepared, error)
                raise
            decision = prepared.get("decision")
            if decision is not None and decision.kind == "answerable_with_warning":
                answer = _HISTORICAL_EVIDENCE_WARNING + answer
            context = prepared["context"]
            sources = prepared["sources"]
            tool_calls = []

        if is_degraded_answer(answer):
            # 防御旧调用方/旧 provider 仍把降级文案当字符串返回：它同样不是答案。
            error = CircuitOpenError("generator")
            logger.warning("sync generation degraded: the degradation copy was returned as an answer")
            self._record_stream_failure(question, start_time, prepared, error)
            raise error

        total_ms = (time.time() - start_time) * 1000
        chunk_hashes = prepared.get("chunk_hashes", [])
        trace = ExecutionTrace(
            question=question, route=prepared["route"], answer=answer,
            total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
            stage_timings=prepared.get("stage_timings") or {},
            decision_metadata=prepared.get("decision_metadata") or {},
        )
        self.tracker.save(trace)
        self._submit_failure_observation(trace, prepared)

        # Gap analysis: check if this is a retrieval gap
        if prepared["route"] == "rag" and chunk_hashes:
            weights = prepared.get("weights", {})
            best_weight = max(weights.values()) if weights else 1.0
            if self._gap_analyzer.is_gap(best_weight, answer):
                self._gap_analyzer.record_gap(question, best_weight)

        output_check = check_output(answer)
        if output_check.filtered:
            answer = output_check.text
        self.memory.add_message(sid, "user", question)
        self.memory.add_message(sid, "assistant", answer)
        if self.memory.should_summarize(sid):
            self.memory.summarize_old_rounds(sid)
        return QueryResult(
            answer=answer,
            context=context,
            sources=sources,
            decision=prepared.get("decision"),
            confidence=prepared.get("confidence"),
        )

    async def query_stream(
        self,
        question: str,
        top_k: int = 8,
        session_id: str = None,
        doc_name: str = None,
        tags: list[str] = None,
        user: dict = None,
        kb_id: str = None,
        model: str = None,
        models: list[str] = None,
    ):
        """流式查询，yield SSE 格式的事件字符串。"""
        sid = self._memory_session_id(session_id, user)
        start_time = time.time()

        try:
            prepared, error = await asyncio.to_thread(
                self._prepare_context, question, sid, doc_name, top_k, tags=tags, user=user, kb_id=kb_id
            )
        except Exception as exc:
            # 准备阶段就可能失败（实测：检索到的证据缺版本元数据时 build_evidence_bundle
            # 抛 ValueError）。此前这类请求以 HTTP 200 + error 事件收场，却没有轨迹、没有
            # 观测、没有跳过记录——运营侧连"有请求失败了"都看不到（2026-09-30 踩到）。
            await asyncio.to_thread(self._record_stream_failure, question, start_time, None, exc)
            raise
        if error:
            yield f'data: {_json.dumps({"type": "error", "reason": error}, ensure_ascii=False)}\n\n'
            trace = ExecutionTrace(question=question, route="blocked", answer=error, total_ms=0)
            await asyncio.to_thread(self.tracker.save, trace)
            self._submit_failure_observation(trace, None, error)
            return

        try:
            async for event in self._stream_prepared(
                question, prepared, sid, start_time, model=model, models=models
            ):
                yield event
        except Exception as exc:
            # 流式中途失败：同样先留痕再分类。已准备好的判定统计量与问题集版本照旧写进
            # 轨迹，所以这次失败仍然可归因。异常继续上抛，端点照旧给客户端一个 error 事件。
            await asyncio.to_thread(
                self._record_stream_failure, question, start_time, prepared, exc
            )
            raise

    async def _stream_prepared(
        self,
        question,
        prepared,
        sid,
        start_time,
        model: str = None,
        models: list[str] = None,
    ):
        """一次已准备完毕的请求的流式输出；异常由 :meth:`query_stream` 兜底留痕。"""
        # 轨迹 route 的默认值就是路由决策本身。竞技场（多模型并行）只是 RAG 路由之上的
        # 一种执行模式——``prepared["route"]`` 仍是 "rag"，轨迹另记 "arena"，让审计能区分
        # "单模型回答"与"竞技场合并回答"。下游按 route 分支的地方（recorder 的 blocked、
        # clusterer 的 structured 特判，evidence 的 blocked 排除，metrics 完全不读 route）
        # 都不会因为多一个 "arena" 取值算错；evolution 的 route 列是自由文本，聚类键里也
        # 不含 route，所以竞技场观测仍与同名问题的普通 RAG 观测聚在一起。
        trace_route = prepared["route"]
        if prepared["route"] == "cached":
            yield f'data: {_json.dumps({"type": "token", "content": prepared["answer"]}, ensure_ascii=False)}\n\n'
            yield f'data: {_json.dumps({"type": "sources", "sources": prepared["sources"]}, ensure_ascii=False)}\n\n'
            await asyncio.to_thread(self.memory.add_message, sid, "user", question)
            await asyncio.to_thread(self.memory.add_message, sid, "assistant", prepared["answer"])
            if await asyncio.to_thread(self.memory.should_summarize, sid):
                await asyncio.to_thread(self.memory.summarize_old_rounds, sid)
            yield 'data: {"type": "done"}\n\n'
            return

        if prepared["route"] in {"fixed", "structured"}:
            answer = prepared["answer"]
            yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
            if prepared["route"] == "structured":
                yield f'data: {_json.dumps({"type": "structured_result", **prepared["structured_result"]}, ensure_ascii=False)}\n\n'
            sources = prepared["sources"]
            tool_calls = []
        elif prepared["route"] == "agent":
            captured_calls = []
            answer = await asyncio.to_thread(
                self.agent.run,
                prepared["question"],
                tool_calls=captured_calls,
                **_agent_run_kwargs(prepared, pipeline=self, start_time=start_time),
            )
            yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
            sources = []
            tool_calls = [ToolCall(**c) for c in captured_calls]
        elif models and len(models) > 1:
            router = get_llm_pool_router()
            arena_targets = [
                router.providers[provider_id]
                for provider_id in models
                if provider_id in router.providers
            ]
            if arena_targets and not any(provider.is_available() for provider in arena_targets):
                # 所有被选中的模型都处于熔断/停用状态：没有模型会产出 token，这不是答案。
                error = CircuitOpenError("llm_pool_arena")
                logger.warning("arena generation degraded: every selected provider is unavailable")
                await asyncio.to_thread(
                    self._record_stream_failure, question, start_time, prepared, error
                )
                yield _degraded_sse_event()
                return
            model_answers: dict[str, str] = {}
            async for event in router.generate_stream_parallel(prepared["messages"], provider_ids=models):
                if event.get("type") == "token":
                    m_id = event.get("model", "")
                    model_answers[m_id] = model_answers.get(m_id, "") + event.get("content", "")
                if event.get("type") != "done":
                    yield f'data: {_json.dumps(event, ensure_ascii=False)}\n\n'

            # 汇总各模型回答写入对话记忆与执行跟踪，保证多轮对话与审计观测完整。
            # 合并回答不再在这里自行落库后提前 return，而是与单模型分支共用下面的统一后处理：
            # check_output 过滤后的文本才入记忆/落轨迹，失败也照常交给自演化观测。流式已发出的
            # token 收不回来（见下方后处理处的说明），这一点与单模型分支是同一个固有权衡。
            answer = "\n\n".join(f"### [{m}]\n{ans}" for m, ans in model_answers.items())
            sources = prepared["sources"]
            tool_calls = []
            trace_route = "arena"
        else:
            answer = ""
            decision = prepared.get("decision")
            if decision is not None and decision.kind == "answerable_with_warning":
                answer = _HISTORICAL_EVIDENCE_WARNING
                yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'

            router = get_llm_pool_router()
            if len(router.providers) > 1 or model:
                try:
                    async for event in router.generate_stream_balanced(prepared["messages"], preferred_provider_id=model):
                        token = event.get("content", "")
                        answer += token
                        yield f'data: {_json.dumps(event, ensure_ascii=False)}\n\n'
                except Exception as error:
                    cause = circuit_open_cause(error)
                    if cause is None:
                        raise
                    logger.warning("stream generation degraded: %s is open", cause.service)
                    await asyncio.to_thread(
                        self._record_stream_failure, question, start_time, prepared, cause
                    )
                    yield _degraded_sse_event()
                    return
            else:
                try:
                    async for token in generate_stream(prepared["messages"]):
                        answer += token
                        yield f'data: {_json.dumps({"type": "token", "content": token}, ensure_ascii=False)}\n\n'
                except CircuitOpenError as error:
                    logger.warning("stream generation degraded because the generator breaker is open")
                    await asyncio.to_thread(
                        self._record_stream_failure, question, start_time, prepared, error
                    )
                    yield _degraded_sse_event()
                    return
            sources = prepared["sources"]
            tool_calls = []

        if is_degraded_answer(answer):
            # 防御旧 provider 仍把降级文案当 token 流回来：不写 memory、不发 done。
            error = CircuitOpenError("generator")
            logger.warning("stream generation degraded: the degradation copy was streamed as tokens")
            await asyncio.to_thread(
                self._record_stream_failure, question, start_time, prepared, error
            )
            yield _degraded_sse_event()
            return

        # 后处理
        # 注意：流式输出时 token 已经逐个发送给客户端，check_output 无法拦截已发送的内容。
        # 这是流式输出的固有权衡：延迟 vs 安全。过滤后的 answer 仅用于 memory/trace 持久化。
        # 生产环境可通过内容审核 API 在客户端侧做二次过滤。
        output_check = check_output(answer)
        if output_check.filtered:
            answer = output_check.text

        yield f'data: {_json.dumps({"type": "sources", "sources": sources}, ensure_ascii=False)}\n\n'

        total_ms = (time.time() - start_time) * 1000
        chunk_hashes = prepared.get("chunk_hashes", [])
        trace = ExecutionTrace(
            question=question, route=trace_route, answer=answer,
            total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
            stage_timings=prepared.get("stage_timings") or {},
            decision_metadata=prepared.get("decision_metadata") or {},
        )
        await asyncio.to_thread(self.tracker.save, trace)
        self._submit_failure_observation(trace, prepared)

        # Gap analysis for stream
        # 竞技场分支同样落在这里：它的 prepared["route"] 是 "rag"（合并回答仍是对 RAG 证据的
        # 回答），所以低分无证据的竞技场回答也会进入缺口记录，与单模型分支一致。
        if prepared["route"] == "rag" and chunk_hashes:
            def record_gap():
                weights = prepared.get("weights", {})
                best_weight = max(weights.values()) if weights else 1.0
                if self._gap_analyzer.is_gap(best_weight, answer):
                    self._gap_analyzer.record_gap(question, best_weight)

            await asyncio.to_thread(record_gap)

        await asyncio.to_thread(self.memory.add_message, sid, "user", question)
        await asyncio.to_thread(self.memory.add_message, sid, "assistant", answer)
        if await asyncio.to_thread(self.memory.should_summarize, sid):
            await asyncio.to_thread(self.memory.summarize_old_rounds, sid)
        done = {"type": "done"}
        reliability = _reliability_payload(prepared)
        if reliability is not None:
            done["reliability"] = reliability
        yield f'data: {_json.dumps(done, ensure_ascii=False)}\n\n'

def main():
    parser = argparse.ArgumentParser(description="RAG Pipeline CLI")
    parser.add_argument("file", help="Path to the document file (.txt, .md)")
    args = parser.parse_args()

    logger.info("Indexing %s...", args.file)
    pipeline = RAGPipeline(args.file)
    logger.info("Ready. Enter your questions (Ctrl+C to exit).")

    try:
        while True:
            question = input("Q: ")
            if not question.strip():
                continue
            answer = pipeline.query(question)
            logger.info("A: %s", answer.answer)
    except (EOFError, KeyboardInterrupt):
        logger.info("Bye.")


if __name__ == "__main__":
    main()
