"""数据清洗管道 — 编码检测、乱码清理、去重、元数据提取。"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

import chardet


def detect_and_decode(raw_bytes: bytes) -> str:
    """自动检测编码并解码。尝试 chardet 检测结果，失败则依次尝试常见编码。"""
    detected = chardet.detect(raw_bytes)
    encoding = detected.get("encoding")
    confidence = float(detected.get("confidence") or 0)
    # Short GBK text is often misidentified as Thai by chardet at very low
    # confidence.  Do not let an unreliable guess override the Chinese
    # encoding fallbacks used by this application.
    if encoding and confidence >= 0.8:
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    # chardet 无法检测或解码失败时，依次尝试常见编码
    for enc in ("utf-8", "gbk", "gb2312", "gb18030", "big5", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def clean_text(text: str) -> str:
    """清理特殊字符：BOM、零宽空格、不间断空格、控制字符。"""
    text = text.replace("﻿", "")  # BOM
    text = text.replace("​", "")  # 零宽空格
    text = text.replace(" ", " ")  # 不间断空格 → 普通空格
    # 去除控制字符（保留换行、制表符）
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    # 合并连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def document_hash(text: str) -> str:
    """文档级 MD5 hash，用于去重。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def deduplicate_chunks(chunks: list[str], threshold: float = 0.95) -> list[str]:
    """段落级去重，相似度超过阈值的段落被去除。

    策略：先用 normalized-text hash 去除完全重复，再对最近 50 个
    unique chunk 做 SequenceMatcher 近似去重，避免 O(n²) 全量比较。
    """
    if not chunks:
        return []

    unique: list[str] = []
    seen_normalized: set[str] = set()

    for chunk in chunks:
        # 快速路径：规范化后完全相同 → 直接跳过
        normalized = chunk.strip().lower()
        if normalized in seen_normalized:
            continue

        # 慢速路径：只和最近 50 个 unique chunk 做近似比较
        is_dup = False
        for existing in unique[-50:]:
            if _similarity(chunk, existing) >= threshold:
                is_dup = True
                break

        if not is_dup:
            unique.append(chunk)
            seen_normalized.add(normalized)

    return unique


def _similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度。"""
    return SequenceMatcher(None, a, b).ratio()


def extract_metadata(text: str) -> dict:
    """提取文档元数据：标题、作者、日期。"""
    metadata: dict[str, str] = {}
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if title_match:
        metadata["title"] = title_match.group(1).strip()
    author_match = re.search(r"(?:作者|Author)[：:]\s*(.+)", text)
    if author_match:
        metadata["author"] = author_match.group(1).strip()
    date_match = re.search(r"(?:日期|Date)[：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
    if date_match:
        metadata["date"] = date_match.group(1).strip()
    return metadata


def clean_document(text: str) -> tuple[str, dict]:
    """完整清洗流程：清理 + 元数据提取。返回 (清洗后文本, 元数据)。"""
    cleaned = clean_text(text)
    metadata = extract_metadata(cleaned)
    return cleaned, metadata
