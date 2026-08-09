import asyncio
import json as _json
import logging
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
from rag.reranker import Reranker
from rag.redis_cache import RedisCache
from rag.retriever import Retriever
from rag.tracker import ExecutionTrace, ExecutionTracker, ToolCall
from rag.vector_store import add, clear

logger = logging.getLogger(__name__)


class QueryResult(NamedTuple):
    answer: str
    context: list[Chunk]
    sources: list[dict]


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
        self, file_path: str = None, session_id: str = None, memory_db_path: str = settings.memory_db_path, kb_id: str = None, user_db=None
    ):
        if kb_id:
            # 查询已有知识库，不重新索引
            self.chunks = []
            self.retriever = Retriever(self.chunks, collection_name=kb_id)
        else:
            # 索引新文档到默认集合
            text = load(file_path)
            text, _metadata = clean_document(text)
            doc_name = file_path.split("/")[-1].split("\\")[-1]
            self.chunks = chunk(text, doc_name=doc_name)
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
        self._cache = RedisCache()

        # 权限管理：注入 user_db 引用（由 api.py 传入，避免每次查询新建连接）
        self._user_db = user_db
        self._kb_id = kb_id or "rag_docs"

    def _prepare_context(self, question: str, session_id: str, doc_name: str, top_k: int = 8, tags: list[str] = None, user: dict = None, kb_id: str = None):
        """公共逻辑：guard -> cache -> route -> retrieve -> rerank -> build_messages。"""
        question = sanitize_input(question)
        guard = check_injection(question)
        if guard.blocked:
            return None, f"请求被安全策略拦截：{guard.reason}"

        cache_key_kb = kb_id or self._kb_id
        cache_key_user = user["id"] if user else "__anon__"
        cached = self._cache.get(question, cache_key_kb, cache_key_user)
        if cached:
            # 缓存命中时也要做权限过滤：sources 可能引用用户无权查看的文档
            sources = cached.get("sources") or [{"doc_name": "缓存", "chunk_index": 0}]
            if user is not None and self._user_db is not None:
                sources = self._filter_sources_by_permission(sources, cache_key_kb, user)
            return {"route": "cached", "answer": cached.get("answer", ""), "context": [], "sources": sources}, None

        from rag.agent import route_question
        route = route_question(question)

        if route == "agent":
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
        raw_context = self.retriever.retrieve(rewritten, top_k=top_k * oversample_factor, doc_name=doc_name, tags=tags)
        context = self.reranker.rerank(rewritten, raw_context)

        # 权限过滤：移除无权文档的 chunks（user is not None 且有 user_db 时才过滤）
        if user is not None and self._user_db is not None:
            from rag.permissions import filter_chunks_by_permission
            context = filter_chunks_by_permission(self._user_db, kb_id or self._kb_id, context, user)

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
        from rag.feedback_processor import FeedbackProcessor

        chunk_hashes = [hashlib.md5(c.text.encode()).hexdigest() for c in context]
        fp = FeedbackProcessor(settings.memory_db_path)
        try:
            weights = fp.get_weights(chunk_hashes)
        finally:
            fp.close()

        sid = session_id or self.session_id or "default"
        messages = self.memory.build_messages(sid, question, context)
        sources = [
            {"doc_name": c.doc_name, "chunk_index": c.chunk_index, "text_preview": c.text[:100]} for c in context
        ]
        return {
            "route": "rag", "question": question, "context": context,
            "messages": messages, "sources": sources, "sid": sid,
            "chunk_hashes": chunk_hashes, "weights": weights,
        }, None

    def _filter_sources_by_permission(self, sources: list, kb_id: str, user: dict) -> list:
        """过滤缓存命中的 sources（dict 而非 Chunk 对象）中用户无权查看的文档来源。

        将每个 source 包装成仅含 doc_name 的轻量对象，复用 filter_chunks_by_permission
        的同一套权限逻辑（admin 不过滤 / 无权限记录视为公开保留 / 无权文档过滤）。
        不使用 get_accessible_doc_names 直接过滤，因为后者只返回有权限记录的文档，
        会误删“旧文档（无权限记录，视为公开）”的来源。
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
            return QueryResult(answer=prepared["answer"], context=[], sources=prepared["sources"])

        if prepared["route"] == "agent":
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
            from rag.gap_analyzer import GapAnalyzer
            weights = prepared.get("weights", {})
            best_weight = max(weights.values()) if weights else 1.0
            if GapAnalyzer.is_gap(best_weight, answer):
                ga = GapAnalyzer(settings.memory_db_path)
                ga.record_gap(question, best_weight)
                ga.close()

        output_check = check_output(answer)
        if output_check.filtered:
            answer = output_check.text
        self.memory.add_message(sid, "user", question)
        self.memory.add_message(sid, "assistant", answer)
        if self.memory.should_summarize(sid):
            self.memory.summarize_old_rounds(sid)
        self._cache.set(question, answer, kb_id or self._kb_id, user["id"] if user else "__anon__", sources=sources)
        return QueryResult(answer=answer, context=context, sources=sources)

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
            yield 'data: {"type": "done"}\n\n'
            return

        if prepared["route"] == "agent":
            captured_calls = []
            answer = await asyncio.to_thread(
                self.agent.run,
                prepared["question"],
                tool_calls=captured_calls,
                **_agent_run_kwargs(prepared),
            )
            yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
            context = []
            sources = []
            tool_calls = [ToolCall(**c) for c in captured_calls]
        else:
            answer = ""
            async for token in generate_stream(prepared["messages"]):
                answer += token
                yield f'data: {_json.dumps({"type": "token", "content": token}, ensure_ascii=False)}\n\n'
            context = prepared["context"]
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
                from rag.gap_analyzer import GapAnalyzer

                weights = prepared.get("weights", {})
                best_weight = max(weights.values()) if weights else 1.0
                if GapAnalyzer.is_gap(best_weight, answer):
                    ga = GapAnalyzer(settings.memory_db_path)
                    ga.record_gap(question, best_weight)
                    ga.close()

            await asyncio.to_thread(record_gap)

        await asyncio.to_thread(self.memory.add_message, sid, "user", question)
        await asyncio.to_thread(self.memory.add_message, sid, "assistant", answer)
        if await asyncio.to_thread(self.memory.should_summarize, sid):
            await asyncio.to_thread(self.memory.summarize_old_rounds, sid)
        await asyncio.to_thread(
            self._cache.set,
            question,
            answer,
            kb_id or self._kb_id,
            user["id"] if user else "__anon__",
            sources=sources,
        )

        yield 'data: {"type": "done"}\n\n'

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
