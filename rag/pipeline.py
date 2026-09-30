import asyncio
import json as _json
import logging
import threading
import time
from typing import NamedTuple

from config import settings
from rag.chunker import chunk
from rag.cleaner import clean_document, deduplicate_chunks
from rag.embedder import embed
from rag.evolution.models import DiagnosticDetail, NonEvolvableReason
from rag.evolution.recorder import FailureRecorder
from rag.generator import generate, generate_stream
from rag.guard import check_injection, check_output, sanitize_input
from rag.jev.policy import KbCapabilities
from rag.jev.service import JevDecisionService
from rag.jev.shadow import ShadowCase, ShadowComparison, ShadowEvaluator
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
from rag.reranker import Reranker
from rag.retriever import Retriever
from rag.tracker import ExecutionTrace, ExecutionTracker, ToolCall
from rag.vector_store import add, clear

logger = logging.getLogger(__name__)

_HISTORICAL_EVIDENCE_WARNING = "注意：以下回答仅依据历史版本资料，内容可能已过期。\n\n"
_RETRIEVAL_OVERSAMPLE = 3

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


def _bundle_top_score(prepared: dict) -> float | None:
    """Best ACL-filtered retrieval score from the reliability bundle, when one was built."""
    metrics = getattr(prepared.get("evidence_bundle"), "retrieval_metrics", None)
    return getattr(metrics, "top_score", None)


def _agent_run_kwargs(prepared: dict) -> dict:
    """从 prepared 中提取需要透传给 agent.run 的 user/kb_id。

    只在调用方显式提供时才传递，避免改变旧的无权限调用签名
    ``run(question)``（向后兼容）。
    """
    kwargs = {}
    if prepared.get("user") is not None:
        kwargs["user"] = prepared["user"]
    if prepared.get("kb_id") is not None:
        kwargs["kb_id"] = prepared["kb_id"]
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
        failure_recorder: FailureRecorder | None = None,
        question_set_rollout=None,
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
                unique_set = set(unique_texts)
                self.chunks = [c for c in self.chunks if c.text in unique_set]
            embeddings = embed([c.text for c in self.chunks])
            clear()
            add(self.chunks, embeddings)
            self.retriever = Retriever(self.chunks)
        self.session_id = session_id or file_path
        from rag.memory import DialogueMemory

        self.memory = DialogueMemory(db_path=memory_db_path, generate_fn=generate)
        self.reranker = Reranker()
        # Jev 决策服务：仅当模式非 off 时创建默认实例；注入的服务由调用方负责生命周期。
        # 层 4 灰度：控制器既提供每请求的灰度键（下面发布），也提供问题集本身——把它的
        # provide() 交给客户端，候选问题集才可能在服务路径上生效。未注入控制器时客户端
        # 走静态问题集，在线行为零变化。
        self._owns_jev_service = False
        if jev_service is not None:
            self.jev = jev_service
        elif settings.jev_mode != "off":
            from rag.jev.client import JevClient

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
        from rag.agent import RAGAgent

        self.agent = RAGAgent(retriever=self.retriever, user_db=user_db, kb_id=kb_id or "rag_docs")
        self.tracker = ExecutionTracker(db_path=memory_db_path)
        self._owns_feedback_processor = feedback_processor is None
        self._owns_gap_analyzer = gap_analyzer is None
        if feedback_processor is None:
            from rag.feedback_processor import FeedbackProcessor

            feedback_processor = FeedbackProcessor(memory_db_path)
        if gap_analyzer is None:
            from rag.gap_analyzer import GapAnalyzer

            gap_analyzer = GapAnalyzer(memory_db_path)
        self._feedback_processor = feedback_processor
        self._gap_analyzer = gap_analyzer
        # 观测记录器：注入实例由调用方负责生命周期；自建实例仅在 flag 开启时创建。
        self._failure_recorder = None
        self._owns_failure_recorder = False
        if failure_recorder is not None:
            self._failure_recorder = failure_recorder
        elif settings.evolution_enabled:
            from rag.evolution.store import FailureTraceStore

            self._failure_recorder = FailureRecorder(
                FailureTraceStore(settings.evolution_db_path),
                max_queue=settings.evolution_queue_limit,
            )
            self._owns_failure_recorder = True
        # 层 4 灰度：问题集提供者由 rollout 控制器持有，这里只发布每请求的灰度键
        # （kb + 会话 + 问题），让同一请求的 preflight 与 passages 落入同一个桶。
        # 默认 None：不注入时在线行为零变化，Jev 客户端走静态问题集。
        self._question_set_rollout = question_set_rollout
        self._closed = False
        self._agent_lock = threading.Lock()
        self._async_agent_lock = asyncio.Lock()

        # 权限管理：注入 user_db 引用（由 api.py 传入，避免每次查询新建连接）
        self._user_db = user_db
        self._kb_id = kb_id or "rag_docs"
        self._enterprise_mode = kb_id is not None
        self._spreadsheet_coordinator = None
        if user_db is not None and kb_id is not None:
            from pathlib import Path

            from rag.spreadsheets.coordinator import SpreadsheetQueryCoordinator
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
            return DiagnosticDetail(
                kb_id=self._kb_id,
                failure_stage="generation" if prepared.get("chunk_hashes") else "retrieval",
                answerability_kind=decision.kind,
                confidence_level=confidence.level if confidence is not None else None,
                chunk_hashes=tuple(prepared.get("chunk_hashes") or ()),
                best_score=_bundle_top_score(prepared),
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
        """一次死掉的流式请求：先留痕，再分类。

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
            DiagnosticDetail(kb_id=self._kb_id, exception_class=type(error).__name__),
        )

    def _prepare_context(self, question: str, session_id: str, doc_name: str, top_k: int = 8, tags: list[str] = None, user: dict = None, kb_id: str = None):
        """公共逻辑：guard -> route -> retrieve -> judge -> build_messages。"""
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
        prepared, error = self._prepare_context_legacy(
            question, session_id, doc_name, top_k, tags, user, kb_id,
            enterprise_request, effective_kb_id, coordinator,
        )
        if error is None and settings.jev_mode != "off":
            prepared.setdefault("stage_timings", {})
            prepared.setdefault("decision_metadata", {}).setdefault("jev_mode", settings.jev_mode)
        return prepared, error

    def _prepare_context_legacy(
        self, question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, coordinator,
    ):
        """旧链路：确定性结构化尝试 -> DeepSeek 路由 -> 无条件改写 -> 检索 -> 旧 reranker。"""
        structured = self._attempt_structured_query(question, coordinator, user, effective_kb_id)
        if structured is not None:
            return structured, None

        from rag.agent import route_question
        route = route_question(question)

        if route == "agent":
            if enterprise_request:
                return self._fixed_decision(
                    question,
                    effective_kb_id,
                    "clarification_required",
                    ("legacy agent route has no typed evidence",),
                ), None
            prepared = {"route": "agent", "question": question}
            # 透传当前用户/知识库，供 agent 的 retrieve 工具做权限过滤
            if user is not None:
                prepared["user"] = user
            if kb_id is not None:
                prepared["kb_id"] = kb_id
            return prepared, None

        rewritten = rewrite_query(question)
        candidates = self._retrieve_visible_candidates(
            rewritten,
            top_k=top_k * _RETRIEVAL_OVERSAMPLE,
            doc_name=doc_name,
            tags=tags,
            user=user,
            kb_id=kb_id,
        )
        context = self._post_rank_acl(self.reranker.rerank(rewritten, candidates), user, effective_kb_id)
        context = self._drop_injected_chunks(context)[:top_k]
        self._submit_shadow_case(question, rewritten, "rag", context, candidates, coordinator)
        prepared = self._finalize_rag_context(
            question, session_id, context, top_k, effective_kb_id,
            build_bundle=enterprise_request,
        )
        prepared.setdefault("decision_metadata", {})["legacy_route"] = route
        return prepared, None

    def _submit_shadow_case(self, question, query, legacy_route, context, candidates, coordinator):
        """Queue a non-blocking shadow comparison; never alters the current response."""
        evaluator = getattr(self, "_shadow_evaluator", None)
        if evaluator is None:
            return
        evaluator.submit(ShadowCase(
            question=question,
            query=query,
            legacy_route=legacy_route,
            legacy_ranks=tuple(
                (f"{chunk.doc_name}#{chunk.chunk_index}", chunk.rerank_score) for chunk in context
            ),
            candidates=tuple(candidates),
            capabilities=KbCapabilities(has_structured_data=coordinator is not None),
        ))

    def _prepare_context_with_jev(
        self, question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, coordinator,
    ):
        """Jev 主链路：输入 guard 之后先预判路由，再检索与候选判断。"""
        capabilities = KbCapabilities(has_structured_data=coordinator is not None)
        started = time.perf_counter()
        route_decision = self.jev.route(question, capabilities)
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

        if route_decision.route == "structured":
            structured = self._attempt_structured_query(question, coordinator, user, effective_kb_id)
            if structured is not None:
                return self._attach_jev_stage(structured, stage), None
            # 结构化协调器无法处理时退回文档检索

        elif route_decision.route == "tool":
            if enterprise_request:
                # 企业模式继续遵守既有 typed-evidence 限制
                prepared = self._fixed_decision(
                    question,
                    effective_kb_id,
                    "clarification_required",
                    ("jev tool route has no typed evidence",),
                )
                return self._attach_jev_stage(prepared, stage), None
            prepared = {"route": "agent", "question": question}
            if user is not None:
                prepared["user"] = user
            if kb_id is not None:
                prepared["kb_id"] = kb_id
            return self._attach_jev_stage(prepared, stage), None

        elif route_decision.route == "legacy":
            # 预判低置信度或 Jev 不可用：完整降级到旧路由与旧 reranker。
            # **降级也留痕**：否则这些请求既没有预判版本也没有判定统计，事后只看到
            # "旧链路作答"，无法解释是谁降级的、为什么，也无法与候选对照
            # （2026-09-30 端到端灰度验证时踩到）。判定阶段确实没跑，故统计量为 0，
            # 由 jev_route="legacy" 与 route_reason 表达降级事实。
            prepared, error = self._prepare_context_legacy(
                question, session_id, doc_name, top_k, tags, user, kb_id,
                enterprise_request, effective_kb_id, coordinator,
            )
            if prepared is not None:
                prepared = self._attach_jev_stage(prepared, stage)
            return prepared, error

        prepared, stage = self._prepare_rag_with_jev(
            question, session_id, doc_name, top_k, tags, user, kb_id,
            enterprise_request, effective_kb_id, route_decision, stage,
        )
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
            (rewrite_query(question) if initial_rewrite else question, initial_rewrite)
        ]
        stage["attempts"] = len(attempts)
        stage["rewrites"] = 1 if initial_rewrite else 0
        context: list[Chunk] = []
        conflicts: list[dict] = []
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
                    self.reranker.rerank(query, candidates), user, effective_kb_id
                )
                context = self._drop_injected_chunks(context)[:top_k]
                conflicts = []
                break
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
                context = self._post_rank_acl(list(selection.accepted), user, effective_kb_id)
                context = self._drop_injected_chunks(context)[:top_k]
                break
            if not rewrite_used and retrieval_route:
                # 首轮无答案证据：允许一次改写重试；已改写则停止，不再循环。
                attempts.append((rewrite_query(question), True))
                stage["attempts"] = len(attempts)
                stage["rewrites"] = 1
            index += 1
        stage["passages_ms"] = (time.perf_counter() - passages_started) * 1000
        prepared = self._finalize_rag_context(
            question, session_id, context, top_k, effective_kb_id,
            build_bundle=enterprise_request or not context,
            conflicts=tuple(conflicts),
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
            from rag.versioned_retrieval import retrieve_current_then_history

            raw_context = retrieve_current_then_history(
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
            from rag.permissions import filter_chunks_by_permission
            raw_context = filter_chunks_by_permission(
                self._user_db, effective_kb_id, raw_context, user
            )
        return raw_context

    def _post_rank_acl(self, context: list[Chunk], user: dict | None, effective_kb_id: str) -> list[Chunk]:
        """Preserve the post-rank ACL filter as defense in depth."""
        if user is not None and self._user_db is not None:
            from rag.permissions import filter_chunks_by_permission
            return filter_chunks_by_permission(
                self._user_db, effective_kb_id, context, user
            )
        return context

    def _drop_injected_chunks(self, context: list[Chunk]) -> list[Chunk]:
        """Remove chunks blocked by the deterministic document injection guard."""
        safe_context = []
        for candidate in context:
            document_guard = check_injection(candidate.text)
            if document_guard.blocked:
                logger.warning(
                    "检索文档注入命中并排除: doc=%s chunk=%s reason=%s",
                    candidate.doc_name,
                    candidate.chunk_index,
                    document_guard.reason,
                )
                continue
            safe_context.append(candidate)
        return safe_context

    def _finalize_rag_context(
        self, question, session_id, context, top_k, effective_kb_id, *, build_bundle, conflicts=(),
    ):
        """Build messages, sources, feedback weights, and the optional reliability gate."""
        import hashlib
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
        chunk_hashes = [hashlib.md5(c.text.encode()).hexdigest() for c in context]
        weights = self._feedback_processor.get_weights(chunk_hashes)

        sid = session_id or self.session_id or "default"
        messages = self.memory.build_messages(sid, question, context)
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
            for i, c in enumerate(context)
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

    def _fixed_decision(self, question, effective_kb_id, kind, reasons, *, permission_limited=False):
        """Build a fixed refusal without any external call."""
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
            "answer": fixed_decision_copy(decision.kind),
            "context": [],
            "sources": [],
            "decision": decision,
            "confidence": confidence,
            "evidence_bundle": bundle,
        }

    def _attempt_structured_query(self, question, coordinator, user, effective_kb_id):
        """Run the deterministic spreadsheet coordinator; return prepared route or None."""
        if coordinator is None or user is None or effective_kb_id == "rag_docs":
            return None
        outcome = coordinator.query(question, kb_id=effective_kb_id, user=user)
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
        from types import SimpleNamespace

        from rag.permissions import filter_chunks_by_permission

        proxies = [SimpleNamespace(doc_name=s.get("doc_name")) for s in sources]
        filtered = filter_chunks_by_permission(self._user_db, kb_id, proxies, user)
        allowed_names = {p.doc_name for p in filtered}
        return [s for s in sources if s.get("doc_name") in allowed_names]

    def query(self, question: str, top_k: int = 8, session_id: str = None, doc_name: str = None, tags: list[str] = None, user: dict = None, kb_id: str = None) -> QueryResult:
        sid = session_id or self.session_id or "default"
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
                **_agent_run_kwargs(prepared),
            )
            context = []
            sources = []
            tool_calls = [ToolCall(**c) for c in captured_calls]
        else:
            answer = generate(prepared["messages"])
            decision = prepared.get("decision")
            if decision is not None and decision.kind == "answerable_with_warning":
                answer = _HISTORICAL_EVIDENCE_WARNING + answer
            context = prepared["context"]
            sources = prepared["sources"]
            tool_calls = []

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

    async def query_stream(self, question: str, top_k: int = 8, session_id: str = None, doc_name: str = None, tags: list[str] = None, user: dict = None, kb_id: str = None):
        """流式查询，yield SSE 格式的事件字符串。"""
        sid = session_id or self.session_id or "default"
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
            async for event in self._stream_prepared(question, prepared, sid, start_time):
                yield event
        except Exception as exc:
            # 流式中途失败：同样先留痕再分类。已准备好的判定统计量与问题集版本照旧写进
            # 轨迹，所以这次失败仍然可归因。异常继续上抛，端点照旧给客户端一个 error 事件。
            await asyncio.to_thread(
                self._record_stream_failure, question, start_time, prepared, exc
            )
            raise

    async def _stream_prepared(self, question, prepared, sid, start_time):
        """一次已准备完毕的请求的流式输出；异常由 :meth:`query_stream` 兜底留痕。"""
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
                **_agent_run_kwargs(prepared),
            )
            yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
            sources = []
            tool_calls = [ToolCall(**c) for c in captured_calls]
        else:
            answer = ""
            decision = prepared.get("decision")
            if decision is not None and decision.kind == "answerable_with_warning":
                answer = _HISTORICAL_EVIDENCE_WARNING
                yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
            async for token in generate_stream(prepared["messages"]):
                answer += token
                yield f'data: {_json.dumps({"type": "token", "content": token}, ensure_ascii=False)}\n\n'
            sources = prepared["sources"]
            tool_calls = []

        # 后处理
        # 注意：流式输出时 token 已经逐个发送给客户端，check_output 无法拦截已发送的内容。
        # 这是流式输出的固有权衡：延迟 vs 安全。过滤后的 answer 仅用于 memory/cache 持久化。
        # 生产环境可通过内容审核 API 在客户端侧做二次过滤。
        output_check = check_output(answer)
        if output_check.filtered:
            answer = output_check.text

        yield f'data: {_json.dumps({"type": "sources", "sources": sources}, ensure_ascii=False)}\n\n'

        total_ms = (time.time() - start_time) * 1000
        chunk_hashes = prepared.get("chunk_hashes", [])
        trace = ExecutionTrace(
            question=question, route=prepared["route"], answer=answer,
            total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
            stage_timings=prepared.get("stage_timings") or {},
            decision_metadata=prepared.get("decision_metadata") or {},
        )
        await asyncio.to_thread(self.tracker.save, trace)
        self._submit_failure_observation(trace, prepared)

        # Gap analysis for stream
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
    import argparse

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
