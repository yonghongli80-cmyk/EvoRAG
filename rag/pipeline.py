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
from rag.generator import generate, generate_stream
from rag.guard import check_injection, check_output, sanitize_input
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

    def _prepare_context(self, question: str, session_id: str, doc_name: str, top_k: int = 8, tags: list[str] = None, user: dict = None, kb_id: str = None):
        """公共逻辑：guard -> route -> retrieve -> rerank -> build_messages。"""
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
        if coordinator is not None and user is not None and effective_kb_id != "rag_docs":
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
                return {"route": "structured", "answer": answer, "context": [], "sources": [], "structured_result": payload}, None
            if outcome.kind == "clarification_required":
                question_text = outcome.clarification.question if outcome.clarification else "请补充表格查询条件"
                return {"route": "structured", "answer": question_text, "context": [], "sources": [], "structured_result": {"kind": "clarification_required", "facts": [], "sources": [], "rows_scanned": 0, "clarification": outcome.clarification.model_dump() if outcome.clarification else None}}, None

        cache_key_kb = effective_kb_id
        cached = None  # Final-answer caching is disabled until ACL versioning is end-to-end.
        if cached:
            # 缓存命中时也要做权限过滤：sources 可能引用用户无权查看的文档
            sources = cached.get("sources") or [{"doc_name": "缓存", "chunk_index": 0}]
            if user is not None and self._user_db is not None:
                sources = self._filter_sources_by_permission(sources, cache_key_kb, user)
            return {"route": "cached", "answer": cached.get("answer", ""), "context": [], "sources": sources}, None
        from rag.agent import route_question
        route = route_question(question)

        if route == "agent":
            if enterprise_request:
                bundle = build_evidence_bundle(
                    question,
                    [],
                    kb_id=effective_kb_id,
                    acl_checked=True,
                )
                decision = AnswerabilityDecision(
                    kind="clarification_required",
                    reasons=("legacy agent route has no typed evidence",),
                )
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
            prepared = {"route": "agent", "question": question}
            # 透传当前用户/知识库，供 agent 的 retrieve 工具做权限过滤
            if user is not None:
                prepared["user"] = user
            if kb_id is not None:
                prepared["kb_id"] = kb_id
            return prepared, None

        rewritten = rewrite_query(question)
        # Oversampling: 检索更多结果，过滤后再截取 top_k
        oversample_factor = 3
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
                rewritten,
                top_k=top_k * oversample_factor,
                user=user,
                user_db=self._user_db,
                kb_id=kb_id or self._kb_id,
                doc_name=doc_name,
                tags=tags,
                weights=feedback_weight,
            )
        else:
            raw_context = self.retriever.retrieve(
                rewritten,
                top_k=top_k * oversample_factor,
                doc_name=doc_name,
                tags=tags,
                weights=feedback_weight,
            )
        # ACL must run before reranking so unauthorized text never crosses that boundary.
        if user is not None and self._user_db is not None:
            from rag.permissions import filter_chunks_by_permission
            raw_context = filter_chunks_by_permission(
                self._user_db, effective_kb_id, raw_context, user
            )

        context = self.reranker.rerank(rewritten, raw_context)

        # Preserve the post-rerank ACL filter as defense in depth.
        if user is not None and self._user_db is not None:
            from rag.permissions import filter_chunks_by_permission
            context = filter_chunks_by_permission(
                self._user_db, effective_kb_id, context, user
            )

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
        context = safe_context
        context = context[:top_k]

        # Compute feedback weights for reranked chunks
        import hashlib
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
        if enterprise_request:
            bundle = build_evidence_bundle(
                question,
                context,
                kb_id=effective_kb_id,
                acl_checked=True,
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
        return prepared, None

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
            self.tracker.save(ExecutionTrace(question=question, route="blocked", answer=error, total_ms=0))
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
        self.tracker.save(ExecutionTrace(
            question=question, route=prepared["route"], answer=answer,
            total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
        ))

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

        prepared, error = await asyncio.to_thread(
            self._prepare_context, question, sid, doc_name, top_k, tags=tags, user=user, kb_id=kb_id
        )
        if error:
            yield f'data: {_json.dumps({"type": "error", "reason": error}, ensure_ascii=False)}\n\n'
            await asyncio.to_thread(
                self.tracker.save, ExecutionTrace(question=question, route="blocked", answer=error, total_ms=0)
            )
            return

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
        await asyncio.to_thread(
            self.tracker.save,
            ExecutionTrace(
                question=question, route=prepared["route"], answer=answer,
                total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
            ),
        )

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
