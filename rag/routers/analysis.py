"""Analysis cards and question management router."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from rag.api_security import require_user
from rag.context import get_user_db
from rag.generator import generate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])


def _user_db():
    return get_user_db()


class CreateCardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="卡片名称")


class RenameCardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="新名称")


class AddQuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=5000, description="问题内容")
    answer: str = Field(default="", description="回答内容")
    source_mode: str = Field(default="", description="来源模式")
    source_message_id: int | None = Field(default=None, description="来源消息 ID")


class UpdateSummaryRequest(BaseModel):
    summary: str = Field(..., description="摘要内容")


class SuggestCardRequest(BaseModel):
    question: str = Field(..., min_length=1, description="问题内容")
    answer: str = Field(default="", description="回答内容")


require_analysis_user = require_user


def _generate_summary_llm(card_id: int, questions: list[dict]) -> str:
    """Generate a summary for an analysis card using the LLM.

    This is a module-level function so tests can monkeypatch it.
    """
    q_text = "\n".join(
        f"- {q['question']}" + (f"\n  回答: {q['answer']}" if q.get("answer") else "")
        for q in questions
    )
    prompt = (
        "请根据以下问题和回答，为这组分析卡片生成一段简洁的中文摘要（100-200字），"
        "概括这组问题的主题和核心内容。\n\n"
        f"问题列表：\n{q_text}"
    )
    return generate([{"role": "user", "content": prompt}])


def _build_card_markdown(card_name: str, summary: str, questions: list[dict]) -> str:
    """Build a Markdown representation of an analysis card.

    This is a module-level function so tests can call it directly.
    """
    lines = [f"# {card_name}"]
    if summary:
        lines.append("")
        lines.append(f"> {summary}")
    lines.append("")
    lines.append("## 问题列表")
    lines.append("")
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q['question']}")
        if q.get("answer"):
            lines.append(f"   - 回答：{q['answer']}")
        if q.get("source_mode"):
            lines.append(f"   - 来源：{q['source_mode']}")
        if q.get("created_at"):
            lines.append(f"   - 添加时间：{q['created_at']}")
    return "\n".join(lines) + "\n"


def _suggest_card_llm(question: str, answer: str, cards: list[dict]) -> dict:
    """Use LLM to suggest which card a question belongs to.

    Returns {"suggested_card_id": int|None, "confidence": float}.
    This is a module-level function so tests can monkeypatch it.
    """
    card_names = "\n".join(f"- id={c['id']}: {c['name']}" for c in cards)
    prompt = (
        "你是一个分析助手。根据以下问题和回答，判断它最应该归入哪个已有的分析卡片。\n\n"
        f"已有卡片：\n{card_names}\n\n"
        f"问题：{question}\n回答：{answer}\n\n"
        "请返回 JSON 格式：{\"card_id\": <id 或 null>, \"confidence\": <0-1 之间的浮点数>}\n"
        "如果没有合适的卡片，card_id 返回 null，confidence 返回 0。"
    )
    result = generate([{"role": "user", "content": prompt}])

    match = re.search(r"\{.*\}", result, re.DOTALL)
    if match:
        try:
            parsed = _json.loads(match.group())
            return {
                "suggested_card_id": parsed.get("card_id"),
                "confidence": float(parsed.get("confidence", 0)),
            }
        except (ValueError, KeyError):
            pass
    return {"suggested_card_id": None, "confidence": 0}


_DEFAULT_GENERATE_SUMMARY_LLM = _generate_summary_llm
_DEFAULT_SUGGEST_CARD_LLM = _suggest_card_llm


@router.get("/cards", summary="获取分析卡片列表")
async def list_analysis_cards(user: dict = Depends(require_user)):
    db = _user_db()
    return await asyncio.to_thread(db.list_cards, user["id"])


@router.post("/cards", summary="创建分析卡片")
async def create_analysis_card(
    req: CreateCardRequest,
    user: dict = Depends(require_user),
):
    db = _user_db()
    card_id = await asyncio.to_thread(db.create_card, user["id"], req.name)
    return {"id": card_id, "name": req.name}


@router.get("/cards/{card_id}/questions", summary="获取卡片问题列表")
async def list_analysis_questions(
    card_id: int,
    user: dict = Depends(require_user),
):
    db = _user_db()
    cards = await asyncio.to_thread(db.list_cards, user["id"])
    if not any(c["id"] == card_id for c in cards):
        raise HTTPException(status_code=404, detail="卡片不存在或无权操作")
    return await asyncio.to_thread(db.get_questions, card_id)


@router.delete("/cards/{card_id}", summary="删除分析卡片")
async def delete_analysis_card(
    card_id: int,
    user: dict = Depends(require_user),
):
    db = _user_db()
    deleted = await asyncio.to_thread(db.delete_card, card_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return {"status": "deleted"}


@router.put("/cards/{card_id}/name", summary="重命名分析卡片")
async def rename_analysis_card(
    card_id: int,
    req: RenameCardRequest,
    user: dict = Depends(require_user),
):
    db = _user_db()
    updated = await asyncio.to_thread(db.rename_card, card_id, req.name, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return {"id": card_id, "name": req.name}


@router.post("/cards/{card_id}/questions", summary="添加分析问题")
async def add_analysis_question(
    card_id: int,
    req: AddQuestionRequest,
    user: dict = Depends(require_user),
):
    db = _user_db()
    qid = await asyncio.to_thread(
        db.add_question,
        card_id,
        req.question,
        req.answer,
        req.source_mode,
        req.source_message_id,
        user["id"],
    )
    if qid is None:
        raise HTTPException(status_code=404, detail="卡片不存在或无权操作")
    return {"id": qid, "question": req.question, "answer": req.answer}


@router.delete("/cards/{card_id}/questions/{qid}", summary="删除分析问题")
async def delete_analysis_question(
    card_id: int,
    qid: int,
    user: dict = Depends(require_user),
):
    db = _user_db()
    deleted = await asyncio.to_thread(db.delete_question, qid, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="问题不存在")
    return {"status": "deleted"}


@router.get("/cards/{card_id}/summary", summary="获取卡片摘要")
async def get_card_summary(
    card_id: int,
    user: dict = Depends(require_user),
):
    db = _user_db()
    cards = await asyncio.to_thread(db.list_cards, user["id"])
    if not any(c["id"] == card_id for c in cards):
        raise HTTPException(status_code=404, detail="卡片不存在")
    summary = await asyncio.to_thread(db.get_card_summary, card_id)
    return {"summary": summary}


@router.put("/cards/{card_id}/summary", summary="更新卡片摘要")
async def update_card_summary(
    card_id: int,
    req: UpdateSummaryRequest,
    user: dict = Depends(require_user),
):
    db = _user_db()
    updated = await asyncio.to_thread(db.update_card_summary, card_id, req.summary, user["id"])
    if not updated:
        raise HTTPException(status_code=404, detail="卡片不存在")
    return {"summary": req.summary}


@router.post("/cards/{card_id}/summary/generate", summary="LLM 生成卡片摘要")
async def generate_card_summary(
    card_id: int,
    user: dict = Depends(require_user),
):
    db = _user_db()
    cards = await asyncio.to_thread(db.list_cards, user["id"])
    if not any(c["id"] == card_id for c in cards):
        raise HTTPException(status_code=404, detail="卡片不存在")
    questions = await asyncio.to_thread(db.get_questions, card_id)
    if not questions:
        raise HTTPException(status_code=400, detail="卡片中没有问题，无法生成摘要")
    summary = await asyncio.to_thread(_generate_summary_llm, card_id, questions)
    await asyncio.to_thread(db.update_card_summary, card_id, summary, user["id"])
    return {"summary": summary}


@router.get("/cards/{card_id}/export", summary="导出分析卡片")
async def export_analysis_card(
    card_id: int,
    format: str = "markdown",
    user: dict = Depends(require_user),
):
    db = _user_db()
    cards = await asyncio.to_thread(db.list_cards, user["id"])
    card = next((c for c in cards if c["id"] == card_id), None)
    if not card:
        raise HTTPException(status_code=404, detail="卡片不存在")
    if format != "markdown":
        raise HTTPException(status_code=400, detail=f"不支持的导出格式：{format}")
    questions = await asyncio.to_thread(db.get_questions, card_id)
    summary = await asyncio.to_thread(db.get_card_summary, card_id)

    md = _build_card_markdown(card["name"], summary, questions)
    return PlainTextResponse(content=md, media_type="text/markdown; charset=utf-8")


@router.post("/suggest-card", summary="建议问题归入哪个卡片")
async def suggest_card(
    req: SuggestCardRequest,
    user: dict = Depends(require_user),
):
    db = _user_db()
    cards = await asyncio.to_thread(db.list_cards, user["id"])
    if not cards:
        return {
            "suggested_card_id": None,
            "suggested_card_name": None,
            "confidence": 0,
            "all_cards": [],
        }
    cards_with_questions = []
    for c in cards:
        qs = await asyncio.to_thread(db.get_questions, c["id"])
        cards_with_questions.append({**c, "questions": qs})

    suggestion = await asyncio.to_thread(_suggest_card_llm, req.question, req.answer, cards_with_questions)
    suggested_id = suggestion["suggested_card_id"]
    suggested_name = None
    if suggested_id is not None:
        for c in cards:
            if c["id"] == suggested_id:
                suggested_name = c["name"]
                break
    return {
        "suggested_card_id": suggested_id,
        "suggested_card_name": suggested_name,
        "confidence": suggestion["confidence"],
        "all_cards": [{"id": c["id"], "name": c["name"]} for c in cards],
    }
