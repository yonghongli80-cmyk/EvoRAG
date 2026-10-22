"""Query, streaming, model arena, conversations, feedback, and suggest routes."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json as _json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import rag.evolution.store as store_mod
from config import settings
from rag.api_security import get_user_db, require_kb_view, require_user
from rag.citations import CitationRecord
from rag.context import (
    _get_or_create_pipeline,
    get_failure_recorder,
    get_pipeline,
    get_pipeline_lock,
)
from rag.evolution.models import DiagnosticDetail, FailureStage
from rag.evolution.service_control import log_line
from rag.generator import generate
from rag.guard import check_output
from rag.knowledge_tasks import TaskService
from rag.llm_pool import get_llm_pool_router
from rag.org_auth.audit import OrgAuditService
from rag.org_auth.repository import OrgRepository, is_org_policy
from rag.permissions import check_doc_permission
from rag.pipeline import _HISTORICAL_EVIDENCE_WARNING, _agent_run_kwargs
from rag.resilience import (
    DEGRADED_ANSWER_TEXT,
    CircuitOpenError,
    circuit_open_cause,
    is_degraded_answer,
)
from rag.suggest import suggest_questions
from rag.tracker import ExecutionTrace
from rag.user_db import UserDB

logger = logging.getLogger(__name__)

router = APIRouter()

_resolve_conversation_user = require_user


class QueryRequest(BaseModel):
    question: str = Field(..., description="用户提问")
    top_k: int = Field(default=5, ge=1, description="返回相关文档数量")
    session_id: str | None = Field(default=None, description="会话 ID（用于多轮对话记忆）")
    conversation_id: int | None = Field(default=None, description="对话 ID")
    doc_name: str | None = Field(default=None, description="限定检索的文档名称")
    tags: list[str] | None = Field(default=None, description="按标签过滤文档")
    kb_id: str | None = Field(default=None, description="目标知识库；默认 rag_docs")


class QueryResponse(BaseModel):
    answer: str = Field(..., description="回答内容")
    sources: list[dict] = Field(default=[], description="引用来源")


class StreamQueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1)
    session_id: str | None = None
    conversation_id: int | None = None
    doc_name: str | None = None
    tags: list[str] | None = None
    kb_id: str | None = None
    model: str | None = Field(default=None, description="指定单模型，默认走负载均衡")
    models: list[str] | None = Field(default=None, description="指定多模型并发对比（Arena 模式）")


class SuggestRequest(BaseModel):
    question: str = Field(..., max_length=2000)
    answer: str = Field(..., max_length=5000)


class FeedbackRequest(BaseModel):
    message_id: int
    value: str = Field(pattern="^(positive|negative)$")
    comment: str | None = None


class RegenerateRequest(BaseModel):
    conversation_id: int
    message_id: int


class CreateConversationRequest(BaseModel):
    mode: str = Field(default="file", pattern="^(file|kb|analysis)$", description="对话模式")


class GenerateTitleRequest(BaseModel):
    question: str = Field(..., description="用户的第一条问题")


def _audit_org_event(db: UserDB, user: dict, action: str, kb_id: str, reason: str = "authorized") -> None:
    if is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        OrgAuditService(db).record(
            int(user["id"]),
            action,
            "knowledge_base",
            kb_id,
            "success",
            reason,
        )


def _record_refusal_task(
    db: UserDB,
    user: dict,
    question: str,
    kb_id: str,
    doc_name: str | None,
) -> None:
    document_id = None
    if doc_name:
        document = db.get_document_permissions_by_names([doc_name], kb_id).get(doc_name)
        if document is not None:
            try:
                check_doc_permission(db, document["doc_name"], kb_id, user, "view")
            except HTTPException:
                pass
            else:
                document_id = document["id"]
    try:
        TaskService(db).record_refusal(int(user["id"]), question, document_id)
    except Exception:
        logger.exception("failed to record insufficient-evidence task")


def _submit_feedback_observation(db: UserDB, req: FeedbackRequest, user_id: int) -> bool:
    try:
        recorder = get_failure_recorder()
        if recorder is None:
            return False
        question = db.get_message_question(req.message_id)
        if not question:
            return False

        store = store_mod.FailureTraceStore(settings.evolution_db_path)
        try:
            for row in store.list_observations():
                meta = row.get("details_decision_metadata") or row.get("decision_metadata") or {}
                if isinstance(meta, dict) and int(meta.get("feedback_message_id") or 0) == int(req.message_id):
                    return False
        finally:
            store.close()
        reliability = db.get_message_reliability(req.message_id, user_id) or {}
        kb_id = str(reliability.get("kb_id") or "unknown")
        trace = ExecutionTrace(
            question=question,
            route="feedback",
            decision_metadata={
                "feedback_message_id": int(req.message_id),
                "source": "feedback",
            },
        )
        detail = DiagnosticDetail(
            kb_id=kb_id,
            failure_stage=FailureStage.UNKNOWN,
            answerability_kind=reliability.get("answerability_kind"),
            confidence_level=reliability.get("confidence_level"),
            feedback_kind="negative_with_comment" if req.comment else "negative",
        )
        return bool(recorder.submit(trace, detail))
    except Exception as error:  # noqa: BLE001
        logger.exception("feedback observation failed")
        with contextlib.suppress(Exception):
            log_line(
                Path(__file__).resolve().parent.parent.parent / "artifacts" / "feedback-observation.log",
                f"message {getattr(req, 'message_id', '?')} NOT recorded: {type(error).__name__}: {error}",
            )
        return False


def _get_llm_pool_router_instance():
    return get_llm_pool_router()


@router.post("/query", response_model=QueryResponse, summary="查询知识库", description="对已索引的文档进行语义检索问答")
async def query(
    req: QueryRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    target_kb = req.kb_id or "rag_docs"

    # B-01：归属校验必须在生成之前，且早于 pipeline 解析/构造。此前只在生成后落库时
    # 校验，别的用户可以把顺序可猜的 conversation_id 塞进 prompt，读走别人的历史。
    session_id = req.session_id
    conversation = None
    if user and req.conversation_id is not None:
        conversation = await asyncio.to_thread(
            db.get_conversation, req.conversation_id, user["id"]
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="对话不存在或无权访问")
        session_id = f"conv_{req.conversation_id}"

    if target_kb != "rag_docs":
        require_kb_view(db, target_kb, user)
        await asyncio.to_thread(_audit_org_event, db, user, "answer_requested", target_kb)

        def _resolve_sync_pipeline():
            with get_pipeline_lock().write():
                return _get_or_create_pipeline(target_kb)

        current_pipeline = await asyncio.to_thread(_resolve_sync_pipeline)
    else:
        with get_pipeline_lock().read():
            current_pipeline = get_pipeline()
            if current_pipeline is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": "尚未索引文档，请先调用 POST /index 上传文档"},
                )

    try:
        result = await asyncio.to_thread(
            contextvars.copy_context().run,
            current_pipeline.query,
            req.question,
            top_k=req.top_k,
            session_id=session_id,
            doc_name=req.doc_name,
            tags=req.tags,
            user=user,
            kb_id=target_kb,
        )
    except CircuitOpenError:
        # A-02：熔断是失败，不是答案。不落会话消息、不附 reliability，显式告知客户端降级。
        logger.warning("查询因模型熔断而降级: conversation_id=%s", req.conversation_id)
        return JSONResponse(
            status_code=503,
            content={"error": DEGRADED_ANSWER_TEXT, "degraded": True},
        )
    if getattr(getattr(result, "decision", None), "kind", None) == "insufficient_evidence":
        await asyncio.to_thread(
            _record_refusal_task, db, user, req.question, target_kb, req.doc_name
        )

    if conversation is not None:
        await asyncio.to_thread(db.add_message, req.conversation_id, "user", req.question)
        reliability_kwargs = {}
        confidence = getattr(result, "confidence", None)
        decision = getattr(result, "decision", None)
        if confidence is not None and decision is not None:
            reliability_kwargs = {
                "confidence_level": confidence.level,
                "confidence_reasons": confidence.reasons,
                "answerability_kind": decision.kind,
            }
        await asyncio.to_thread(
            db.add_message,
            req.conversation_id,
            "assistant",
            result.answer,
            **reliability_kwargs,
        )

    return QueryResponse(answer=result.answer, sources=result.sources)


@router.get("/models", summary="获取可用大模型列表", description="返回当前模型池中所有可用的模型及默认模型")
async def get_available_models():
    router_instance = _get_llm_pool_router_instance()
    return {
        "default_model": router_instance.default_provider_id,
        "models": router_instance.list_models(),
    }


@router.post("/query/stream", summary="流式查询知识库", description="SSE 流式返回查询结果")
async def query_stream(
    req: StreamQueryRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    target_kb = req.kb_id or "rag_docs"

    # B-01：SSE 端点同样在进入生成、甚至解析 pipeline 之前完成归属校验，否则流已经
    # 开出去了才发现是别人的对话。
    session_id = req.session_id
    conversation = None
    if user and req.conversation_id is not None:
        conversation = await asyncio.to_thread(
            db.get_conversation, req.conversation_id, user["id"]
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="对话不存在或无权访问")
        session_id = f"conv_{req.conversation_id}"

    if target_kb != "rag_docs":
        require_kb_view(db, target_kb, user)
        await asyncio.to_thread(_audit_org_event, db, user, "answer_requested", target_kb)

        def _resolve_stream_pipeline():
            with get_pipeline_lock().write():
                return _get_or_create_pipeline(target_kb)

        current_pipeline = await asyncio.to_thread(_resolve_stream_pipeline)
    else:
        with get_pipeline_lock().read():
            current_pipeline = get_pipeline()
            if current_pipeline is None:
                return JSONResponse(status_code=400, content={"error": "尚未索引文档"})

    async def event_generator():
        answer_buffer = ""
        citation_records: list[CitationRecord] = []
        reliability = None
        try:
            async for event in current_pipeline.query_stream(
                req.question,
                top_k=req.top_k,
                session_id=session_id,
                doc_name=req.doc_name,
                tags=req.tags,
                user=user,
                kb_id=target_kb,
                model=req.model,
                models=req.models,
            ):
                # 解析一次再按 type 分派：原先用子串 `'"type": "done"'` 匹配，依赖
                # `json.dumps` 的默认空格格式——换个紧凑序列化就静默漏掉 reliability
                # （2026-10-21 实测：桩用紧凑 JSON 时判定结论整条丢失）。
                parsed = None
                if event.startswith("data: "):
                    try:
                        parsed = _json.loads(event.removeprefix("data: ").strip())
                    except Exception:
                        parsed = None
                kind = parsed.get("type") if isinstance(parsed, dict) else None
                if kind == "token":
                    answer_buffer += parsed.get("content", "")
                elif kind == "sources":
                    for source in parsed.get("sources", []):
                        if source.get("document_id") and source.get("document_version_id"):
                            citation_records.append(
                                CitationRecord(
                                    document_id=source["document_id"],
                                    document_version_id=source["document_version_id"],
                                    chunk_index=source.get("chunk_index", 0),
                                    section_title=source.get("section_title") or "",
                                    text_preview=source.get("text_preview", "")[:200],
                                    rank=source.get("rank", 0),
                                    is_historical=bool(source.get("is_historical", False)),
                                )
                            )
                elif kind == "done":
                    reliability = parsed.get("reliability")
                    continue
                elif kind == "error":
                    # 熔断/准备失败是终止事件：不补 done（done 会让前端把这次失败当成
                    # 一次正常收尾）。内存与轨迹由 pipeline 负责，这里已无回答可落库。
                    yield event
                    return
                yield event
        except Exception as error:
            logger.exception("流式查询中途失败")
            cause = circuit_open_cause(error)
            if cause is not None:
                payload = {
                    "type": "error",
                    "reason": DEGRADED_ANSWER_TEXT,
                    "degraded": True,
                }
            else:
                payload = {"type": "error", "reason": "流式生成失败，请稍后重试"}
            yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            return

        assistant_message_id = None
        if reliability and reliability.get("answerability_kind") == "insufficient_evidence":
            await asyncio.to_thread(
                _record_refusal_task,
                db,
                user,
                req.question,
                target_kb,
                req.doc_name,
            )
        if conversation is not None and answer_buffer:
            await asyncio.to_thread(db.add_message, req.conversation_id, "user", req.question)
            persistence_key = f"stream:{req.conversation_id}:{hashlib.sha256(req.question.encode('utf-8')).hexdigest()[:16]}"
            assistant_message_id = await asyncio.to_thread(
                db.persist_assistant_answer_with_citations,
                req.conversation_id,
                answer_buffer,
                citation_records,
                persistence_key,
                **(reliability or {}),
            )
        done_payload: dict = {"type": "done", "assistant_message_id": assistant_message_id}
        if reliability is not None:
            # 判定层的结论必须转给客户端：前端读的正是 `done.reliability.answerability_kind`
            # （拒答原因）。此前这里只把 reliability 用于记录拒答工单与落库，随后自己发一个
            # 不含它的 done，于是浏览器永远说不出"为什么没作答"（2026-10-21 实测）。
            done_payload["reliability"] = reliability
        yield f"data: {_json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/suggest", summary="生成追问建议", description="基于问答生成 3 个推荐追问")
async def suggest(req: SuggestRequest, user: dict = Depends(require_user)):
    questions = await asyncio.to_thread(suggest_questions, req.question, req.answer)
    return {"questions": questions}


@router.post("/feedback", summary="提交反馈")
async def submit_feedback(
    req: FeedbackRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    value_int = 1 if req.value == "positive" else -1
    owned = await asyncio.to_thread(db.message_belongs_to_user, req.message_id, user["id"])
    if not owned:
        raise HTTPException(status_code=404, detail="消息不存在或无权操作")
    await asyncio.to_thread(db.add_feedback, req.message_id, user["id"], value_int, req.comment or "")
    if value_int < 0:
        await asyncio.to_thread(_submit_feedback_observation, db, req, int(user["id"]))
    return {"status": "ok"}


_REGENERATE_DEFAULT_KB = "rag_docs"

_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low", "unavailable"})
_ANSWERABILITY_KINDS = frozenset(
    {
        "answerable",
        "answerable_with_warning",
        "clarification_required",
        "insufficient_evidence",
        "permission_limited",
    }
)


def _regenerate_target_kb(db: UserDB, message_id: int, reliability: dict) -> str:
    """Resolve the knowledge base an already-persisted answer came from (A-06).

    ``/regenerate`` used to always rebuild the answer from the default
    ``rag_docs`` collection, so regenerating an answer that came from a
    knowledge base either failed or answered from the wrong corpus.

    The reliability record is authoritative when it carries ``kb_id``. Existing
    rows do not (the chat message schema has no such column), so the persisted
    citations are used instead: every citation of one answer points into the KB
    that produced it. Only when neither source identifies exactly one KB do we
    fall back to the default library.
    """
    explicit = str((reliability or {}).get("kb_id") or "").strip()
    if explicit:
        return explicit
    try:
        citations = db.get_answer_citations(message_id)
    except Exception:  # noqa: BLE001 - inference must never break regenerate
        logger.debug("could not read citations for regenerate", exc_info=True)
        return _REGENERATE_DEFAULT_KB
    kb_ids: set[str] = set()
    for citation in citations:
        document_id = citation.get("document_id")
        if not document_id:
            continue
        document = db.get_document_permission_by_id(int(document_id))
        if document is not None and document.get("kb_id"):
            kb_ids.add(str(document["kb_id"]))
    if len(kb_ids) == 1:
        return kb_ids.pop()
    return _REGENERATE_DEFAULT_KB


def _persist_regenerated_reliability(
    db: UserDB, message_id: int, prepared: dict
) -> dict:
    """Persist the new assessment for a regenerated answer, or invalidate the old one.

    Leaving the previous confidence/answerability attached would present the old
    judgment as if it described the new text (A-06). A route without an
    assessment invalidates the stale values instead of silently keeping them.
    """
    decision = prepared.get("decision")
    confidence = prepared.get("confidence")
    if decision is not None and confidence is not None:
        reliability = {
            "confidence_level": confidence.level,
            "confidence_reasons": list(confidence.reasons),
            "answerability_kind": decision.kind,
        }
        if (
            reliability["confidence_level"] not in _CONFIDENCE_LEVELS
            or reliability["answerability_kind"] not in _ANSWERABILITY_KINDS
        ):
            raise HTTPException(status_code=500, detail="可靠性评估结果无效")
    else:
        reliability = {
            "confidence_level": "unavailable",
            "confidence_reasons": [],
            "answerability_kind": "answerable",
        }
    reasons_json = _json.dumps(reliability["confidence_reasons"], ensure_ascii=False)
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE chat_messages SET confidence_level = ?, "
            "confidence_reasons_json = ?, answerability_kind = ? WHERE id = ?",
            (
                reliability["confidence_level"],
                reasons_json,
                reliability["answerability_kind"],
                message_id,
            ),
        )
    return reliability


@router.post("/regenerate", summary="重新生成回答")
async def regenerate(
    req: RegenerateRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    messages = await asyncio.to_thread(db.get_messages, req.conversation_id, user["id"])
    target = None
    user_question = None
    for i, msg in enumerate(messages):
        if msg["id"] == req.message_id and msg["role"] == "assistant":
            target = msg
            if i > 0 and messages[i - 1]["role"] == "user":
                user_question = messages[i - 1]["content"]
            break

    if not target:
        raise HTTPException(status_code=404, detail="消息不存在")
    if not user_question:
        raise HTTPException(status_code=400, detail="找不到对应的用户问题")

    # A-06：按该答案的来源知识库重生成，而不是固定 rag_docs。
    reliability_record = await asyncio.to_thread(
        db.get_message_reliability, req.message_id, user["id"]
    )
    target_kb = await asyncio.to_thread(
        _regenerate_target_kb, db, req.message_id, reliability_record or {}
    )

    if target_kb != _REGENERATE_DEFAULT_KB:
        require_kb_view(db, target_kb, user)

        def _resolve_kb_pipeline():
            with get_pipeline_lock().write():
                return _get_or_create_pipeline(target_kb)

        current_pipeline = await asyncio.to_thread(_resolve_kb_pipeline)
    else:
        with get_pipeline_lock().read():
            current_pipeline = get_pipeline()
            if current_pipeline is None:
                raise HTTPException(status_code=400, detail="尚未索引文档")

    session_id = current_pipeline._memory_session_id(f"conv_{req.conversation_id}", user)
    prepared, error = await asyncio.to_thread(
        current_pipeline._prepare_context,
        user_question,
        session_id,
        None,
        user=user,
        kb_id=target_kb,
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    try:
        if prepared["route"] in {"fixed", "structured", "cached"}:
            new_answer = prepared["answer"]
        elif prepared["route"] == "agent":
            new_answer = await asyncio.to_thread(
                current_pipeline.agent.run,
                user_question,
                **_agent_run_kwargs(prepared),
            )
        else:
            new_answer = await asyncio.to_thread(generate, prepared["messages"], 0.7)
            decision = prepared.get("decision")
            if decision is not None and decision.kind == "answerable_with_warning":
                # 与 pipeline.query 对齐：历史版本证据的提示属于回答契约（A-06）。
                new_answer = _HISTORICAL_EVIDENCE_WARNING + new_answer
    except CircuitOpenError:
        logger.warning("regenerate degraded: circuit breaker is open")
        return JSONResponse(
            status_code=503,
            content={"error": DEGRADED_ANSWER_TEXT, "degraded": True},
        )

    if is_degraded_answer(new_answer):
        logger.warning("regenerate degraded: the degradation copy was returned as an answer")
        return JSONResponse(
            status_code=503,
            content={"error": DEGRADED_ANSWER_TEXT, "degraded": True},
        )

    # 与 pipeline.query 的后处理一致：输出过滤后的文本才落库（A-06）。
    output_check = check_output(new_answer)
    if output_check.filtered:
        new_answer = output_check.text

    await asyncio.to_thread(db.update_message, req.message_id, new_answer, user["id"])
    reliability = await asyncio.to_thread(
        _persist_regenerated_reliability, db, req.message_id, prepared
    )
    return {
        "message_id": req.message_id,
        "answer": new_answer,
        "reliability": reliability,
    }


@router.get("/conversations/search", summary="搜索对话")
async def search_conversations(
    q: str = Query(..., min_length=1, description="搜索词"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_user),
):
    db = get_user_db()

    def _search():
        results = db.search_conversations(user["id"], q, page=page, size=size)
        return {"results": results, "page": page, "size": size}

    return await asyncio.to_thread(_search)


@router.post("/conversations", summary="新建对话")
async def create_conversation(
    req: CreateConversationRequest | None = None,
    user: dict = Depends(require_user),
):
    req = req or CreateConversationRequest()
    db = get_user_db()
    cid = await asyncio.to_thread(db.create_conversation, user["id"], "", req.mode)
    return {"id": cid, "title": "新对话", "mode": req.mode}


@router.get("/conversations", summary="列出对话")
async def list_conversations(
    mode: str | None = None,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    result = await asyncio.to_thread(db.list_conversations, user["id"], mode)
    logger.debug(
        "list_conversations mode=%s user=%s count=%d modes=%s",
        mode,
        user["id"],
        len(result),
        [c.get("mode") for c in result],
    )
    return result


@router.delete("/conversations/{cid}", summary="删除对话")
async def delete_conversation(
    cid: int,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    if not await asyncio.to_thread(db.delete_conversation, cid, user["id"]):
        raise HTTPException(status_code=404, detail="对话不存在")
    return {"status": "deleted"}


@router.post("/conversations/{cid}/generate-title", summary="生成对话标题")
async def generate_conversation_title(
    cid: int,
    req: GenerateTitleRequest,
    user: dict = Depends(require_user),
):
    db = get_user_db()

    def _generate():
        prompt = f"请用一句话概括以下问题，作为对话标题，不超过20个字，不要加引号：\n{req.question}"
        try:
            title = generate([{"role": "user", "content": prompt}], temperature=0.3)
            title = title.strip().replace('"', "").replace("'", "").replace("。", "")
            if len(title) > 30:
                title = title[:30]
        except Exception:
            title = req.question[:20]

        db.update_conversation_title(cid, user["id"], title)
        return title

    title = await asyncio.to_thread(_generate)
    return {"title": title}


@router.get("/conversations/{cid}/messages", summary="获取对话消息")
async def get_conversation_messages(
    cid: int,
    user: dict = Depends(require_user),
):
    db = get_user_db()
    return await asyncio.to_thread(db.get_messages, cid, user["id"])
