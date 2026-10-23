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
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol, runtime_checkable

from config import settings
from rag import agent as _agent_mod
from rag import feedback_processor as _feedback_processor_mod
from rag import gap_analyzer as _gap_analyzer_mod
from rag import memory as _memory_mod
from rag import permissions as _permissions_mod
from rag import versioned_retrieval as _versioned_retrieval_mod
from rag.chunker import chunk
from rag.cleaner import clean_document, deduplicate_chunks
from rag.embedder import embed
from rag.evolution.models import DiagnosticDetail, NonEvolvableReason
from rag.generator import generate, generate_stream
from rag.guard import (
    apply_semantic_injection_check,
    check_document_injection,
    check_injection,
    check_output,
    sanitize_input,
)
from rag.jev.client import JevClient
from rag.jev.policy import KbCapabilities, is_self_contained_lookup
from rag.jev.service import JevDecisionService
from rag.jev.shadow import ShadowCase, ShadowComparison, ShadowEvaluator
from rag.llm_pool import get_llm_pool_router
from rag.loader import load
from rag.models import Chunk
from rag.query_rewriter import rewrite_query
from rag.redis_cache import RedisCache  # noqa: F401 - public compatibility import
from rag.reliability import (
    assess_confidence,
    build_evidence_bundle,
    decide_answerability,
    fixed_decision_copy,
)
from rag.reliability.models import AnswerabilityDecision, ConfidenceAssessment
from rag.reranker import Reranker, merge_ranked_chunks
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
        """Active Jev routing requires mode=active and an allowlisted knowledge base."""
        jev = getattr(self, "jev", None)
        if jev is None or settings.jev_mode != "active":
            return False
        allowlist = {item.strip() for item in settings.jev_kb_allowlist.split(",") if item.strip()}
        return effective_kb_id in allowlist

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
        """路由决策，多轮时按历史修正一次：**只在"它说要澄清、但历史能补全"这一种情况下重新判**。

        2026-10-13 的多轮基准实测：追问本身不含先行词时（"那一步具体是怎么做的？"），路由层只看
        当前这一句，判 `clarification` 就直接返回，7 例只对 1 例；而旧路径无条件改写，把指代补全之后
        照常走 RAG，7/7。修法是让上下文只在这一处生效：

        * 没判澄清（含 legacy/tool/structured/rag）⇒ 原样返回，**零额外调用**；
        * 没有历史、或改写结果与原问法相同 ⇒ 原样返回澄清，**零额外调用**；
        * 有历史且改写确实改变了问法 ⇒ 用补全后的问法**重新判一次**，按新决策继续。

        只多花一次预判（不是两次模型调用），且只花在"本来要拒答"的那些追问上。改写不可用时退回
        原决策 —— 补全失败不该变成路由失败。
        """
        route_decision = self.jev.route(question, capabilities)
        if getattr(route_decision, "route", "") != "clarification":
            return route_decision
        if not self._history_is_available(session_id):
            return route_decision
        try:
            completed = self._rewrite_for_retrieval(question, session_id)
        except Exception:  # noqa: BLE001 - 补全失败就按原决策走，绝不因此改变答复形态
            return route_decision
        completed = str(completed or "").strip()
        if not completed or completed == question.strip():
            return route_decision
        corrected = self.jev.route(completed, capabilities)
        # 重新判之后仍然要澄清的话，保留**原**决策（原文才是用户问的那句）。
        if getattr(corrected, "route", "") == "clarification":
            return route_decision
        return corrected

    def _memory_session_id(self, session_id: str | None, user: dict | None) -> str:
        """Principal-scoped key for every DialogueMemory read and write (B-01).

        The API always passes the authenticated user, so a bare ``session_id`` (and
        the default ``"default"``) becomes ``u{user_id}:{session_id}``. Without a
        principal (CLI/offline) the legacy bare key is kept: there is nobody to
        isolate, and changing it would silently forget existing local history.
        """
        user_id = user.get("id") if user else None
        return _memory_mod.session_key(user_id, session_id or getattr(self, "session_id", None))

    def _history_is_available(self, session_id: str) -> bool:
        """这个会话里有没有可用的历史轮次；读不到就当作没有。"""
        try:
            return bool(self.memory.get_recent_messages(session_id, n=2))
        except Exception:  # noqa: BLE001 - 记忆读不到时按"没有历史"处理
            return False

    def _rewrite_for_retrieval(self, question: str, session_id: str) -> str:
        """检索前改写：按配置带上最近几轮对话。

        **默认 0 轮 ⇒ 与既有行为逐字节一致**（`rewrite_query(question)` 原样调用）。
        指代补全只在多轮里才有意义（"它呢？"当轮没有先行词），而回答生成那条路**本来**就带上下文
        （`self.memory.build_messages`）—— 缺口只在检索前的改写这一处。

        改写由 LLM 生成，同一问题多次调用会得到不同句子；实测（2026-10-21）其中一种会把
        "帮我找资料"改写成"如何查询资料"，使同一候选池的重排分从 0.7 掉到 0.03。因此这里
        按 (问题, 历史签名) 缓存改写结果，减少重复抖动与模型调用（见 ``_rewrite_cached``）。
        """
        turns = int(getattr(settings, "query_rewrite_history_turns", 0) or 0)
        if turns <= 0:
            return self._rewrite_cached(question, "", lambda: rewrite_query(question))
        try:
            messages = self.memory.get_recent_messages(session_id, n=turns * 2)
            summary = self.memory.get_summary(session_id)
        except Exception:  # noqa: BLE001 - 记忆读不到就退回既有行为，绝不让改写失败
            # 不缓存：这是"没有历史"的退化结果，缓存它会让 memory 恢复后仍返回无上下文改写。
            return rewrite_query(question)
        history = [(m.role, m.content) for m in messages]
        return self._rewrite_cached(
            question,
            _history_signature(history, summary),
            lambda: rewrite_query(question, history=history, summary=summary),
        )

    def _rewrite_cached(self, question: str, signature: str, produce) -> str:
        """Return a cached rewrite or produce and cache a new one.

        Only a non-empty rewrite that actually **changed** the question is cached. ``rewrite_query``
        returns the original question when the model call fails, and freezing that would turn a
        transient provider error into a permanent loss of recall for that question.
        """
        if not getattr(settings, "query_rewrite_cache_enabled", True):
            return produce()
        cache = getattr(self, "_rewrite_cache", None)
        lock = getattr(self, "_rewrite_cache_lock", None)
        if cache is None or lock is None:
            # Tests build pipelines with object.__new__ and skip __init__; create lazily.
            cache = OrderedDict()
            lock = threading.Lock()
            self._rewrite_cache = cache
            self._rewrite_cache_lock = lock
        key = (question, signature)
        with lock:
            cached = cache.get(key)
            if cached is not None:
                cache.move_to_end(key)
                # 旧版本可能已经把降级文案写进缓存（P1-01）：命中它也不能当改写结果。
                return question if is_degraded_answer(cached) else cached
        rewritten = produce()
        if is_degraded_answer(rewritten):
            # A degradation copy is never a valid rewrite: using it as the retrieval
            # query would permanently pin this question to garbage recall (P1-01).
            return question
        if not rewritten or rewritten == question:
            return rewritten
        with lock:
            cache[key] = rewritten
            cache.move_to_end(key)
            limit = max(1, int(getattr(settings, "query_rewrite_cache_size", 512) or 512))
            while len(cache) > limit:
                cache.popitem(last=False)
        return rewritten

    def _rerank_for_evidence(self, original_question, retrieval_query, candidates, record=None):
        """Rerank with the retrieval query, and also with the user's original wording.

        Measured 2026-10-21: an LLM rewrite can turn "find this for me" into "how do I query for
        this"; the reranker then scores the same relevant document near zero and the request flips
        to a refusal. Judging both phrasings and keeping the higher score removes the flip without
        touching the evidence threshold.

        The second pass is deliberately conditional: with ``below_threshold`` (default) it only runs
        when the rewrite pass is already weak enough to risk a refusal, so the common request keeps
        its single-rerank latency. A degraded reranker (all scores ``None``) and an unchanged query
        never trigger it.
        """
        if not candidates:
            return []
        primary = self.reranker.rerank(retrieval_query, candidates)
        if record is not None:
            record["rewrite_dual_rerank"] = False
        if retrieval_query.strip() == original_question.strip():
            return primary
        if not any(chunk.rerank_score is not None for chunk in primary):
            # 重排器降级（断路器/接口失败）：再调一次只会同样失败。
            return primary
        mode = settings.rewrite_dual_rerank_mode
        if mode == "off":
            return primary
        if mode == "below_threshold":
            best = max(chunk.rerank_score for chunk in primary if chunk.rerank_score is not None)
            if best >= settings.rewrite_dual_rerank_threshold:
                return primary
        secondary = self.reranker.rerank(original_question, candidates)
        if record is not None:
            record["rewrite_dual_rerank"] = True
        return merge_ranked_chunks(primary, secondary) or primary

    def _prepare_context(self, question: str, session_id: str, doc_name: str, top_k: int = 8, tags: list[str] = None, user: dict = None, kb_id: str = None):
        """公共逻辑：guard -> route -> retrieve -> judge -> build_messages。"""
        # 任何进入 pipeline 的记忆读写都先落到主体命名空间（幂等，见 session_key）。
        session_id = self._memory_session_id(session_id, user)
        question = sanitize_input(question)
        guard = check_injection(question)
        if guard.blocked:
            return None, f"请求被安全策略拦截：{guard.reason}"

        enterprise_request = getattr(self, "_enterprise_mode", False) or kb_id is not None
        effective_kb_id = kb_id or getattr(self, "_kb_id", "rag_docs")
        if enterprise_request and (user is None or self._user_db is None):
            bundle = build_evidence_bundle(
                question,
                [],
                kb_id=effective_kb_id,
                acl_checked=True,
                permission_limited=True,
            )
            decision = decide_answerability(bundle)
            confidence = assess_confidence(bundle, decision)
            return {
                "route": "fixed",
                "answer": fixed_decision_copy(decision.kind),
                "context": [],
                "sources": [],
                "decision": decision,
                "confidence": confidence,
                "evidence_bundle": bundle,
            }, None

        coordinator = getattr(self, "_spreadsheet_coordinator", None)

        cache_key_kb = effective_kb_id
        cached = None  # Final-answer caching is disabled until ACL versioning is end-to-end.
        if cached:
            # 缓存命中时也要做权限过滤：sources 可能引用用户无权查看的文档
            sources = cached.get("sources") or [{"doc_name": "缓存", "chunk_index": 0}]
            if user is not None and self._user_db is not None:
                sources = self._filter_sources_by_permission(sources, cache_key_kb, user)
            return {"route": "cached", "answer": cached.get("answer", ""), "context": [], "sources": sources}, None

        if self._jev_active(effective_kb_id):
            rollout = getattr(self, "_question_set_rollout", None)
            if rollout is not None:
                # 层 4 灰度键：同一请求内所有 Jev 判定共用，保证 preflight 与 passages
                # 的候选/当前问题集选择一致；键只存在于进程内，不落库、不记日志。
                with rollout.request_key(f"{effective_kb_id}|{session_id}|{question}"):
                    return self._prepare_context_with_jev(
                        question, session_id, doc_name, top_k, tags, user, kb_id,
                        enterprise_request, effective_kb_id, coordinator,
                    )
            return self._prepare_context_with_jev(
                question, session_id, doc_name, top_k, tags, user, kb_id,
                enterprise_request, effective_kb_id, coordinator,
            )
        prepare_fn = (
            self._prepare_context_legacy
            if self._prepare_context_legacy != self._prepare_context_standard
            else self._prepare_context_standard
        )
        prepared, error = prepare_fn(
            question, session_id, doc_name, top_k, tags, user, kb_id,
            enterprise_request, effective_kb_id, coordinator,
        )
        if error is None and settings.jev_mode != "off":
            prepared.setdefault("stage_timings", {})
            prepared.setdefault("decision_metadata", {}).setdefault("jev_mode", settings.jev_mode)
        return prepared, error

    def _prepare_context_standard(
        self, question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, coordinator, stage=None,
    ):
        """基准检索流水线 (Classic RAG)：结构化优先 -> 意图路由 -> 改写 -> 召回 -> 重排 -> 安全/ACL过滤 -> 上下文定稿。"""
        record = stage if stage is not None else {}
        structured = self._attempt_structured_query(
            question, coordinator, user, effective_kb_id, record=record
        )
        structured_clarification = None
        if structured is not None:
            if not self._is_structured_clarification(structured):
                return structured, None
            # 澄清不抢检索（2026-10-19）：问句里出现字段名并不等于表格里就有可直接算出的东西。
            # "r50相关价格" 会被判成结构化，但规划器只会回一句"请指明字段"，而文档里其实有价格。
            # 所以先让它走完检索，拿不到来源时再回澄清——顺序反了就是把能答的问题变成反问。
            structured_clarification = structured

        # 企业模式里 agent 路由只有"固定拒答"一个结局（typed evidence 限制），先问一次路由
        # 模型再拒答等于每条企业问答白付一次模型调用，还放大尾延迟（A-07）。直接走 RAG。
        route = "rag" if enterprise_request else _agent_mod.route_question(question)

        if route == "agent":
            prepared = {"route": "agent", "question": question}
            # 透传当前用户/知识库，供 agent 的 retrieve 工具做权限过滤
            if user is not None:
                prepared["user"] = user
            if kb_id is not None:
                prepared["kb_id"] = kb_id
            return prepared, None

        rewritten = (
            question
            if stage is not None and is_self_contained_lookup(question)
            else self._rewrite_for_retrieval(question, session_id)
        )
        candidates = self._retrieve_visible_candidates(
            rewritten,
            top_k=top_k * _RETRIEVAL_OVERSAMPLE,
            doc_name=doc_name,
            tags=tags,
            user=user,
            kb_id=kb_id,
        )
        context = self._post_rank_acl(
            self._rerank_for_evidence(question, rewritten, candidates, record=record),
            user,
            effective_kb_id,
        )
        context = self._drop_injected_chunks(context)[:top_k]
        self._submit_shadow_case(question, rewritten, "rag", context, candidates, coordinator, effective_kb_id, user)
        prepared = self._finalize_rag_context(
            question, session_id, context, top_k, effective_kb_id,
            build_bundle=enterprise_request or not context,
        )
        prepared.setdefault("decision_metadata", {})["legacy_route"] = route
        # 结构化尝试的种类跟着这次请求走：它解释"为什么结构化没有帮上忙"，
        # 也是 details_structured_query_kind 唯一的来源（见 _attempt_structured_query）。
        if "structured_query_kind" in record:
            prepared["decision_metadata"]["structured_query_kind"] = record["structured_query_kind"]
        if "rewrite_dual_rerank" in record:
            prepared["decision_metadata"]["rewrite_dual_rerank"] = record["rewrite_dual_rerank"]
        if structured_clarification is not None and not _produced_answer(prepared):
            # 检索没有给出**答案**，才把结构化澄清交给用户（此时它比"资料不足"更有用）。
            # 判据不是"有没有来源"：Jev 路径的拒答本来就带来源（引用兜底），
            # 于是"有来源"曾被当成"检索赢了"，把澄清丢掉、只回一句"现有可见资料不足"。
            return structured_clarification, None
        return prepared, None

    # 向后兼容别名：供历史代码与测试打桩
    _prepare_context_legacy = _prepare_context_standard

    #: 判定器可见的字段名上限：够它认出"问题指向表格列"即可，再多只会拉长提示词与尾延迟。
    _STRUCTURED_FIELD_HINT_LIMIT = 40

    def _structured_field_names(self, coordinator, kb_id, user) -> tuple[str, ...]:
        """Field display names the judge may be shown, or ``()`` when there is nothing to say.

        Reuses ``build_authorized_catalog`` — the very call ``has_datasets`` makes — instead of adding
        another accessor: everything it returns is already filtered by document permission, so the
        hint cannot widen what a principal may learn. Placeholder names (``Column 3``) are dropped:
        they carry no signal and would eat the budget.
        """
        db = getattr(coordinator, "db", None)
        root = getattr(coordinator, "datasets_root", None)
        if db is None or root is None or not kb_id:
            return ()
        try:
            from rag.spreadsheets.catalog import build_authorized_catalog

            catalog = build_authorized_catalog(db, root, kb_id, user)
        except Exception:  # noqa: BLE001 - 这只是给判定器的附加证据，取不到就退回只有布尔
            return ()
        names: list[str] = []
        seen: set[str] = set()
        for dataset in catalog.datasets:
            for sheet in dataset.sheets:
                for column in sheet.columns:
                    name = str(column.display_name or "").strip()
                    if not name or name in seen or name.lower().startswith("column "):
                        continue
                    seen.add(name)
                    names.append(name)
                    if len(names) >= self._STRUCTURED_FIELD_HINT_LIMIT:
                        return tuple(names)
        return tuple(names)

    def _submit_shadow_case(self, question, query, legacy_route, context, candidates, coordinator, effective_kb_id=None, user=None):
        """Queue a non-blocking shadow comparison; never alters the current response."""
        evaluator = getattr(self, "_shadow_evaluator", None)
        if evaluator is None:
            return
        has_datasets = getattr(coordinator, "has_datasets", None)
        has_structured = bool(coordinator is not None and (has_datasets(effective_kb_id, user) if has_datasets and effective_kb_id else True))
        evaluator.submit(ShadowCase(
            question=question,
            query=query,
            legacy_route=legacy_route,
            legacy_ranks=tuple(
                (f"{chunk.doc_name}#{chunk.chunk_index}", chunk.rerank_score) for chunk in context
            ),
            candidates=tuple(candidates),
            capabilities=KbCapabilities(
                has_structured_data=has_structured,
                structured_fields=(
                    self._structured_field_names(coordinator, effective_kb_id, user)
                    if has_structured
                    else ()
                ),
            ),
        ))

    def _prepare_context_with_jev(
        self, question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, coordinator,
    ):
        """Jev 主链路：输入 guard 之后先预判路由，再检索与候选判断。"""
        has_datasets = getattr(coordinator, "has_datasets", None)
        has_structured = bool(coordinator is not None and (has_datasets(effective_kb_id, user) if has_datasets else True))
        capabilities = KbCapabilities(
            has_structured_data=has_structured,
            structured_fields=(
                self._structured_field_names(coordinator, effective_kb_id, user)
                if has_structured
                else ()
            ),
        )
        started = time.perf_counter()
        route_decision = self._route_with_history_correction(
            question, session_id, capabilities
        )
        preflight = route_decision.preflight
        stage = {
            "_started": started,
            "preflight_ms": (time.perf_counter() - started) * 1000,
            "passages_ms": 0.0,
            "route": route_decision.route,
            "reason": route_decision.reason,
            "question_set_version": preflight.question_set_version if preflight is not None else "",
            "passages_question_set_version": "",
            "intent_confidence": preflight.intent_confidence if preflight is not None else None,
            "rewrite_probability": preflight.rewrite_probability if preflight is not None else None,
            "failure_kind": (
                route_decision.reason.split(":", 1)[1]
                if route_decision.reason.startswith("preflight_unavailable:")
                else None
            ),
            "attempts": 0,
            "rewrites": 0,
            # 判定层原始统计量：多轮尝试各取最大值（"判定层最多看到过多少"），
            # 失败的归因据此区分"没召回"与"召回了却没被判为答案证据"。
            "passage_judged": 0,
            "passage_relevant": 0,
            "passage_evidence": 0,
            "passage_contradicted": 0,
            "passage_injected": 0,
        }

        if route_decision.route == "clarification":
            prepared = self._fixed_decision(
                question,
                effective_kb_id,
                "clarification_required",
                (route_decision.reason,),
            )
            return self._attach_jev_stage(prepared, stage), None

        structured_clarification = None
        if route_decision.route == "structured":
            structured = self._attempt_structured_query(
                question, coordinator, user, effective_kb_id, record=stage
            )
            if structured is not None and not self._is_structured_clarification(structured):
                return self._attach_jev_stage(structured, stage), None
            if structured is not None:
                # 同上：澄清让位于文档检索，检索拿不到来源时才回澄清。
                structured_clarification = structured
            # 结构化协调器无法处理时退回文档检索

        elif route_decision.route == "tool":
            if enterprise_request:
                # 企业模式继续遵守既有 typed-evidence 限制；文案要说清"需要工具执行"，
                # 不能说成"资料冲突/范围不明确"（A-07）。
                prepared = self._fixed_decision(
                    question,
                    effective_kb_id,
                    "clarification_required",
                    ("jev tool route has no typed evidence",),
                    answer=_TOOL_ROUTE_UNAVAILABLE_COPY,
                )
                return self._attach_jev_stage(prepared, stage), None
            prepared = {"route": "agent", "question": question}
            if user is not None:
                prepared["user"] = user
            if kb_id is not None:
                prepared["kb_id"] = kb_id
            return self._attach_jev_stage(prepared, stage), None

        elif route_decision.route == "legacy":
            # 预判低置信度或 Jev 不可用：完整降级到基准路由与重排流水线。
            # **降级也留痕**：否则这些请求既没有预判版本也没有判定统计，事后只看到
            # "旧链路作答"，无法解释是谁降级的、为什么，也无法与候选对照
            # （2026-09-30 端到端灰度验证时踩到）。判定阶段确实没跑，故统计量为 0，
            # 由 jev_route="legacy" 与 route_reason 表达降级事实。
            prepare_fn = (
                self._prepare_context_legacy
                if self._prepare_context_legacy != self._prepare_context_standard
                else self._prepare_context_standard
            )
            prepared, error = prepare_fn(
                question, session_id, doc_name, top_k, tags, user, kb_id,
                enterprise_request, effective_kb_id, coordinator,
                # stage 必须传进去：结构化尝试的结果种类写在它上面，
                # 再由 _attach_jev_stage 搬进 decision_metadata（否则这一列永远是 NULL）。
                stage=stage,
            )
            if prepared is not None:
                prepared = self._attach_jev_stage(prepared, stage)
            return prepared, error

        prepared, stage = self._prepare_rag_with_jev(
            question, session_id, doc_name, top_k, tags, user, kb_id,
            enterprise_request, effective_kb_id, route_decision, stage,
        )
        if structured_clarification is not None and not _produced_answer(prepared or {}):
            # 与旧链路同一条判据（见 ``_produced_answer``）：澄清只在检索**没给出答案**时胜出。
            # 拒答带来源是常态，所以不能用"有没有来源"来判定。
            return self._attach_jev_stage(structured_clarification, stage), None
        return prepared, None

    def _prepare_rag_with_jev(
        self, question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, route_decision, stage,
    ):
        """RAG 主链路：原问题检索（除非策略请求改写），Jev 候选判断，最多一次改写重试。"""
        retrieve_top_k = top_k * _RETRIEVAL_OVERSAMPLE
        # should_rewrite 与改写重试都是检索路径建议；结构化路由回退到文档检索时两者都不适用。
        retrieval_route = route_decision.route == "rag"
        initial_rewrite = retrieval_route and route_decision.should_rewrite
        # 状态机：attempts=[(检索查询, 是否已改写)]；最多追加一次改写重试，之后必须停止。
        attempts = [
            (self._rewrite_for_retrieval(question, session_id) if initial_rewrite else question, initial_rewrite)
        ]
        stage["attempts"] = len(attempts)
        stage["rewrites"] = 1 if initial_rewrite else 0
        context: list[Chunk] = []
        conflicts: list[dict] = []
        # 引用兜底：判定层判为"相关"的块即使没有直接答案证据，对用户仍有引用价值。
        # 此前只有"找到答案证据"的那一次尝试才把采纳结果带出去，于是每次尝试都没有答案证据时
        # sources 为空——比旧路径少给了本该给出的来源（2026-10-19 实测 real-025：期望文档被判
        # rel=0.79/证据 0.40，旧路径排第 1，新路径 0 来源、Top-5 丢召回）。**不因此放宽任何
        # 阈值**：这些块只进 sources（在证据包里是 ``role="background"``，可答性/置信度都不
        # 统计它们），不进 context、不当答案证据，答案照旧是"证据不足"。
        # 取采纳最多的一次尝试，与上面判定统计量"跨尝试取最大"的口径一致。
        citation_fallback: list[Chunk] = []
        # 答案证据 / 引用（含背景块）在命中答案证据的那次尝试里定稿（A-03）。
        citations: list[Chunk] | None = None
        background: list[Chunk] = []
        passages_started = time.perf_counter()
        index = 0
        while index < len(attempts):
            query, rewrite_used = attempts[index]
            candidates = self._retrieve_visible_candidates(
                query,
                top_k=retrieve_top_k,
                doc_name=doc_name,
                tags=tags,
                user=user,
                kb_id=kb_id,
            )
            # ACL 过滤后的候选才允许发送给 Jev；不可见文本不得越过边界。
            selection = self.jev.select(question, candidates, query=query, top_k=top_k)
            if selection is None:
                # Jev 候选判断不可用：旧 reranker 兜底，来源仍标注为 reranker
                context = self._post_rank_acl(
                    self._rerank_for_evidence(question, query, candidates, record=stage),
                    user,
                    effective_kb_id,
                )
                context = self._drop_injected_chunks(context)[:top_k]
                conflicts = []
                break
            # 价格类问题里，脱敏占位符不算答案证据：保留引用，但不让模型编金额。
            selection = _drop_redacted_price_evidence(question, selection)
            conflicts = [self._conflict_payload(item) for item in selection.conflicts]
            stage["passages_question_set_version"] = selection.question_set_version
            # 判定层原始统计量（跨尝试取最大）：末次尝试可能因为改写而候选更少，取最大
            # 才是"判定层本可以依据的最强证据"，也才不会把改写后的退化读成召回缺口。
            stage["passage_judged"] = max(stage["passage_judged"], selection.judged)
            stage["passage_relevant"] = max(stage["passage_relevant"], selection.relevant)
            stage["passage_evidence"] = max(stage["passage_evidence"], selection.with_evidence)
            stage["passage_contradicted"] = max(stage["passage_contradicted"], selection.contradicted)
            stage["passage_injected"] = max(stage["passage_injected"], selection.injected)
            if selection.has_answer_evidence:
                # A-03：答案只依据答案证据；判定为"相关但无答案证据"的背景块只进引用。
                visible = self._drop_injected_chunks(
                    self._post_rank_acl(list(selection.accepted), user, effective_kb_id),
                    # 判定层已经给过语义注入结论（并把这些块排除在 accepted 之外），
                    # 不再为同一批块付第二次模型调用（A-05）。
                    semantic=False,
                )[:top_k]
                evidence_keys = {
                    (chunk.doc_name, chunk.chunk_index, chunk.text)
                    for chunk in selection.evidence_chunks()
                }
                answer_evidence = [
                    chunk
                    for chunk in visible
                    if (chunk.doc_name, chunk.chunk_index, chunk.text) in evidence_keys
                ]
                if answer_evidence:
                    context = answer_evidence
                    citations = visible
                    background = [
                        chunk
                        for chunk in visible
                        if (chunk.doc_name, chunk.chunk_index, chunk.text) not in evidence_keys
                    ]
                    break
                # 证据块全被 ACL/文档 guard 摘掉了：按"没有答案证据"处理，继续兜底。
            # 这一次尝试没有答案证据：记下它采纳的相关块做引用兜底（见上面的说明）。
            if len(selection.accepted) > len(citation_fallback):
                citation_fallback = list(selection.accepted)
            if not rewrite_used and retrieval_route and not is_self_contained_lookup(question):
                # 首轮无答案证据：允许一次改写重试；已改写则停止，不再循环。
                attempts.append((self._rewrite_for_retrieval(question, session_id), True))
                stage["attempts"] = len(attempts)
                stage["rewrites"] = 1
            index += 1
        stage["passages_ms"] = (time.perf_counter() - passages_started) * 1000
        if not context and citation_fallback:
            # ACL 复查与文档注入 guard 在这一层照旧生效：被引用不等于被信任。
            citations = self._drop_injected_chunks(
                self._post_rank_acl(citation_fallback, user, effective_kb_id),
                semantic=False,
            )[:top_k]
            # 引用兜底的块是背景：证据包保留 role=background，拒答原因因此能区分
            # "没检索到"和"检索到了、只是没有答案证据"（A-10）。
            background = list(citations)
        prepared = self._finalize_rag_context(
            question, session_id, context, top_k, effective_kb_id,
            build_bundle=enterprise_request or not context,
            conflicts=tuple(conflicts),
            citations=citations,
            background=background,
        )
        return self._attach_jev_stage(prepared, stage), stage

    @staticmethod
    def _attach_jev_stage(prepared: dict, stage: dict) -> dict:
        """Attach sanitized stage timings and decision metadata for the execution trace."""
        prepared["stage_timings"] = {
            "jev_preflight_ms": stage["preflight_ms"],
            "jev_passages_ms": stage["passages_ms"],
            "preparation_ms": (time.perf_counter() - stage["_started"]) * 1000,
        }
        prepared["decision_metadata"] = {
            "jev_mode": settings.jev_mode,
            "question_set_version": stage["question_set_version"],
            "passages_question_set_version": stage["passages_question_set_version"],
            "jev_route": stage["route"],
            "route_reason": stage["reason"],
            "intent_confidence": stage["intent_confidence"],
            "rewrite_probability": stage["rewrite_probability"],
            "failure_kind": stage["failure_kind"],
            "attempt_count": stage["attempts"],
            "rewrite_count": stage["rewrites"],
            "passage_judged": stage["passage_judged"],
            "passage_relevant": stage["passage_relevant"],
            "passage_evidence": stage["passage_evidence"],
            "passage_contradicted": stage["passage_contradicted"],
            "passage_injected": stage["passage_injected"],
        }
        # 结构化尝试的种类（见 _attempt_structured_query）：只有真的试过才有这个键，
        # 没试过就不写——缺省是"没试"，不是"试了但没结果"。
        if stage.get("structured_query_kind"):
            prepared["decision_metadata"]["structured_query_kind"] = stage["structured_query_kind"]
        if "rewrite_dual_rerank" in stage:
            prepared["decision_metadata"]["rewrite_dual_rerank"] = stage["rewrite_dual_rerank"]
        return prepared

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
        """Retrieve, version-scope, and ACL-filter candidates before any external judge."""
        effective_kb_id = kb_id or self._kb_id
        feedback_weight_cache: dict[str, float] = {}

        def feedback_weight(chunk_hash: str) -> float:
            if chunk_hash not in feedback_weight_cache:
                weight = self._feedback_processor.get_weight(chunk_hash)
                feedback_weight_cache[chunk_hash] = 1.0 if weight is None else weight
            return feedback_weight_cache[chunk_hash]

        if user is not None and self._user_db is not None:
            raw_context = _versioned_retrieval_mod.retrieve_current_then_history(
                self.retriever,
                query,
                top_k=top_k,
                user=user,
                user_db=self._user_db,
                kb_id=effective_kb_id,
                doc_name=doc_name,
                tags=tags,
                weights=feedback_weight,
            )
        else:
            raw_context = self.retriever.retrieve(
                query,
                top_k=top_k,
                doc_name=doc_name,
                tags=tags,
                weights=feedback_weight,
            )
        # ACL must run before any external judge so unauthorized text never crosses that boundary.
        if user is not None and self._user_db is not None:
            raw_context = _permissions_mod.filter_chunks_by_permission(
                self._user_db, effective_kb_id, raw_context, user
            )
        return raw_context

    def _post_rank_acl(self, context: list[Chunk], user: dict | None, effective_kb_id: str) -> list[Chunk]:
        """Preserve the post-rank ACL filter as defense in depth."""
        if user is not None and self._user_db is not None:
            return _permissions_mod.filter_chunks_by_permission(
                self._user_db, effective_kb_id, context, user
            )
        return context

    def _drop_injected_chunks(self, context: list[Chunk], *, semantic: bool = True) -> list[Chunk]:
        """Remove chunks blocked by the injection guards.

        ``semantic`` runs the optional model-side check on top of the deterministic one. The Jev
        route passes ``semantic=False`` for judged passages: its judge already produced the
        ``contains_prompt_injection`` verdict for those chunks, and judging them twice would pay
        for the same call twice. Every other path (legacy reranker, Jev fallback) opts in, so the
        semantic layer no longer depends on which route served the request (A-05).
        """
        safe_context = []
        for candidate in context:
            document_guard = check_document_injection(candidate.text)
            if document_guard.blocked:
                logger.warning(
                    "检索文档注入命中并排除: doc=%s chunk=%s reason=%s",
                    candidate.doc_name,
                    candidate.chunk_index,
                    document_guard.reason,
                )
                continue
            safe_context.append(candidate)
        if not semantic or not safe_context:
            return safe_context
        return self._drop_semantically_injected(safe_context)

    def _drop_semantically_injected(self, context: list[Chunk]) -> list[Chunk]:
        """Apply the optional semantic injection detector; fail-open when it is unavailable."""
        detector = getattr(self, "_semantic_injection_detector", None)
        if detector is None:
            return context
        result = apply_semantic_injection_check([chunk.text for chunk in context], detector)
        if not result.checked:
            return context
        flagged = set(result.flagged)
        return [chunk for index, chunk in enumerate(context) if index not in flagged]

    def _finalize_rag_context(
        self, question, session_id, context, top_k, effective_kb_id, *, build_bundle, conflicts=(),
        citations=None, background=(),
    ):
        """Build messages, sources, feedback weights, and the optional reliability gate.

        ``citations`` names the passages to list as sources when they differ from the answering
        context: the Jev path cites the passages its judge found relevant even when none of them
        carried answer evidence and the request is therefore refused. A refusal may not hide the
        documents that were looked at — the legacy path lists them as sources too, so dropping them
        made the Jev path lose Top-5 recall (2026-10-19, real-025). Citations are **sources only**:
        messages, feedback hashes and the evidence bundle keep using ``context``, so the answer
        itself is still built from (and refused for the lack of) answer evidence. ``None`` keeps the
        1:1 context→sources mapping every other caller relies on.

        ``background`` names the citation-only passages that must still reach the evidence bundle
        as ``role="background"`` refs (A-03): they are visible candidates for metrics and reasons,
        but they never count as answer evidence.
        """
        if build_bundle and context:
            # 版本边界：版本化 bundle 只能引用带 document_id/document_version_id 的块。
            # BM25 降级快照的块没有这些字段（密集检索抖动时会走到那条路），此前它们会让
            # build_evidence_bundle 抛 ValueError("versioned evidence required")，整条请求
            # 以 HTTP 200 + error 事件收场（2026-09-30 实测 3/4 概率）。这里把不可引用的块
            # 摘掉：答案、引用与哈希都只基于可引用的块；全不可引用时自然落到"证据不足"。
            # 检索层保持原样——BM25 快照没有版本元数据是它的正当形态，不该在检索层被禁掉。
            citable = [
                chunk
                for chunk in context
                if chunk.document_id is not None and chunk.document_version_id is not None
            ]
            if len(citable) != len(context):
                logger.warning(
                    "dropped %d chunk(s) without version metadata before the evidence bundle",
                    len(context) - len(citable),
                )
                context = citable
        background_chunks = [
            chunk
            for chunk in background
            if chunk.document_id is not None and chunk.document_version_id is not None
        ] if build_bundle else []
        chunk_hashes = [hashlib.md5(c.text.encode()).hexdigest() for c in context]
        weights = self._feedback_processor.get_weights(chunk_hashes)

        sid = session_id or self.session_id or "default"
        messages = self.memory.build_messages(sid, question, context)
        cited = list(context) if citations is None else list(citations)
        sources = [
            {
                "doc_name": c.doc_name,
                "chunk_index": c.chunk_index,
                "text_preview": c.text[:100],
                "document_id": c.document_id,
                "document_version_id": c.document_version_id,
                "section_title": c.section_title,
                "is_historical": c.historical_weight < 1.0,
                "rank": i,
            }
            for i, c in enumerate(cited)
        ]
        prepared = {
            "route": "rag", "question": question, "context": context,
            "messages": messages, "sources": sources, "sid": sid,
            "chunk_hashes": chunk_hashes, "weights": weights,
        }
        if build_bundle:
            bundle = build_evidence_bundle(
                question,
                context,
                kb_id=effective_kb_id,
                acl_checked=True,
                conflicts=[dict(item) for item in conflicts],
                background=background_chunks,
            )
            decision = decide_answerability(bundle)
            confidence = assess_confidence(bundle, decision)
            prepared["decision"] = decision
            prepared["confidence"] = confidence
            prepared["evidence_bundle"] = bundle
            if decision.kind in {
                "clarification_required",
                "insufficient_evidence",
                "permission_limited",
            }:
                prepared["route"] = "fixed"
                prepared["answer"] = fixed_decision_copy(decision.kind)
        return prepared

    def _fixed_decision(self, question, effective_kb_id, kind, reasons, *, permission_limited=False, answer=None):
        """Build a fixed refusal without any external call.

        ``answer`` overrides the kind's generic copy when the decision has a specific, more
        accurate explanation (e.g. "this question needs tool execution", A-07).
        """
        bundle = build_evidence_bundle(
            question,
            [],
            kb_id=effective_kb_id,
            acl_checked=True,
            permission_limited=permission_limited,
        )
        decision = AnswerabilityDecision(kind=kind, reasons=tuple(reasons))
        confidence = assess_confidence(bundle, decision)
        return {
            "route": "fixed",
            "answer": answer if answer is not None else fixed_decision_copy(decision.kind),
            "context": [],
            "sources": [],
            "decision": decision,
            "confidence": confidence,
            "evidence_bundle": bundle,
        }

    @staticmethod
    def _is_structured_clarification(prepared: dict | None) -> bool:
        """True when the structured attempt produced only a clarifying question.

        Such an outcome must not preempt retrieval: it asks the user for a field while the documents
        may already hold the answer (measured 2026-10-19 on ``r50相关价格``-style questions).
        """
        if (prepared or {}).get("route") != "structured":
            return False
        result = (prepared or {}).get("structured_result") or {}
        return result.get("kind") == "clarification_required"

    def _attempt_structured_query(self, question, coordinator, user, effective_kb_id, record=None):
        """Run the deterministic spreadsheet coordinator; return prepared route or None.

        ``record`` (optional) receives ``{"structured_query_kind": <outcome kind>}`` for **every**
        attempt, including the ones that return ``None``. Without it the kind is unrecoverable:
        ``not_applicable`` (coordinator declined), ``unsupported`` (planner could not build a plan)
        and ``failed`` (internal execution error) all collapsed into the same ``None``, so
        "structured execution broke" was indistinguishable from "there is no table data" — and
        ``details_structured_query_kind`` never got written at all.
        """
        if coordinator is None or user is None or effective_kb_id == "rag_docs":
            return None
        has_datasets = getattr(coordinator, "has_datasets", None)
        if has_datasets is not None and not has_datasets(effective_kb_id, user):
            return None
        outcome = coordinator.query(question, kb_id=effective_kb_id, user=user)
        if record is not None:
            record["structured_query_kind"] = outcome.kind
        if outcome.kind in {"structured_result", "hybrid_result"}:
            answer = "\n".join(f"{fact.label}：{fact.value}" for fact in outcome.computed_facts)
            payload = {
                "kind": outcome.kind,
                "facts": [{"label": fact.label, "value": fact.value} for fact in outcome.computed_facts],
                "sources": [
                    {"dataset_id": source.dataset_id, "sheet_key": source.sheet_key, "cell_range": source.cell_range}
                    for source in outcome.citations
                ],
                "rows_scanned": outcome.rows_scanned,
            }
            return {"route": "structured", "answer": answer, "context": [], "sources": [], "structured_result": payload}
        if outcome.kind == "clarification_required":
            question_text = outcome.clarification.question if outcome.clarification else "请补充表格查询条件"
            return {"route": "structured", "answer": question_text, "context": [], "sources": [], "structured_result": {"kind": "clarification_required", "facts": [], "sources": [], "rows_scanned": 0, "clarification": outcome.clarification.model_dump() if outcome.clarification else None}}
        return None

    @staticmethod
    def _conflict_payload(chunk: Chunk) -> dict:
        return {
            "doc_name": chunk.doc_name,
            "chunk_index": chunk.chunk_index,
            "text_preview": chunk.text[:100],
            "score_source": chunk.score_source,
        }

    def _filter_sources_by_permission(self, sources: list, kb_id: str, user: dict) -> list:
        """过滤缓存命中的 sources（dict 而非 Chunk 对象）中用户无权查看的文档来源。

        将每个 source 包装成仅含 doc_name 的轻量对象，复用
        filter_chunks_by_permission 的同一套权限逻辑：admin 不过滤；普通用户
        仅保留显式授权的文档；没有权限记录的文档默认拒绝。
        """
        if not sources or self._user_db is None:
            return sources

        proxies = [SimpleNamespace(doc_name=s.get("doc_name")) for s in sources]
        filtered = _permissions_mod.filter_chunks_by_permission(self._user_db, kb_id, proxies, user)
        allowed_names = {p.doc_name for p in filtered}
        return [s for s in sources if s.get("doc_name") in allowed_names]

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
