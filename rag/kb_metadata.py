"""知识库元数据生成 — 目录提取 + 概述生成。"""
import json
import logging
import re

from rag.generator import generate

logger = logging.getLogger(__name__)


def generate_toc(content: str) -> dict:
    """LLM 提取文档标题层级结构。失败时返回兜底结构。"""
    # 先用正则提取所有标题（快速、完整）
    sections = []
    current_section = None
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('# '):
            # 一级标题 = 章节
            title = line.lstrip('# ').strip()
            if title:
                current_section = {"title": title, "subsections": []}
                sections.append(current_section)
        elif line.startswith('## ') and current_section:
            # 二级标题 = 子章节
            title = line.lstrip('# ').strip()
            if title:
                current_section["subsections"].append({"title": title})

    if sections:
        # 正则提取成功，直接返回
        doc_title = ""
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('# ') and not line.startswith('## '):
                doc_title = line.lstrip('# ').strip()
                break
        return {"title": doc_title or "未知", "sections": sections}

    # 正则提取失败，用 LLM（截取更多内容）
    prompt = f"""请从以下文档中提取标题层级结构，输出 JSON 格式：
{{"title": "文档标题", "sections": [{{"title": "章节标题", "subsections": [...]}}, ...]}}

文档内容：
{content[:8000]}"""
    try:
        result = generate([{"role": "user", "content": prompt}])
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return {"title": "未知", "sections": []}
    except Exception as e:
        logger.warning("目录生成失败: %s", e)
        return {"title": "未知", "sections": []}


def generate_summary(content: str) -> str:
    """LLM 生成文档概述。失败时返回空字符串。"""
    prompt = f"""请用 100-200 字概括以下文档的核心内容，包括：主题、覆盖范围、关键知识点。

文档内容：
{content[:2000]}"""
    try:
        return generate([{"role": "user", "content": prompt}])
    except Exception as e:
        logger.warning("概述生成失败: %s", e)
        return ""
