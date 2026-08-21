"""Tests for data cleaning pipeline."""
from rag.cleaner import (
    clean_document,
    clean_text,
    deduplicate_chunks,
    detect_and_decode,
    document_hash,
    extract_metadata,
)


def test_detect_and_decode_gbk():
    """GBK 编码自动检测并正确解码。"""
    raw = "你好世界".encode("gbk")
    result = detect_and_decode(raw)
    assert result == "你好世界"


def test_detect_and_decode_utf8():
    """UTF-8 编码正确解码。"""
    raw = b"Hello World"
    result = detect_and_decode(raw)
    assert result == "Hello World"


def test_clean_text_removes_bom():
    """BOM 字符被移除。"""
    result = clean_text("﻿Hello")
    assert "﻿" not in result
    assert result == "Hello"


def test_clean_text_removes_zero_width():
    """零宽空格被移除。"""
    result = clean_text("Hello​World")
    assert "​" not in result
    assert result == "HelloWorld"


def test_clean_text_normalizes_nbsp():
    """不间断空格转为普通空格。"""
    result = clean_text("Hello World")
    assert result == "Hello World"


def test_clean_text_collapses_blank_lines():
    """连续空行合并为两个换行。"""
    result = clean_text("line1\n\n\n\nline2")
    assert result == "line1\n\nline2"


def test_clean_text_preserves_content():
    """正常内容不受影响。"""
    text = "这是一个正常的文档。\n包含多行内容。"
    result = clean_text(text)
    assert result == text


def test_document_hash_deterministic():
    """相同内容生成相同 hash。"""
    h1 = document_hash("test content")
    h2 = document_hash("test content")
    assert h1 == h2
    assert len(h1) == 32  # MD5 hex length


def test_document_hash_different_content():
    """不同内容生成不同 hash。"""
    h1 = document_hash("content A")
    h2 = document_hash("content B")
    assert h1 != h2


def test_deduplicate_chunks_removes_duplicates():
    """高度相似的段落被去重。"""
    chunks = [
        "RAG 是一种检索增强生成技术。",
        "RAG 是一种检索增强生成技术",  # Very similar (>95%)
        "向量数据库用于存储嵌入向量。",
    ]
    result = deduplicate_chunks(chunks)
    assert len(result) == 2


def test_deduplicate_chunks_keeps_unique():
    """不相似的段落全部保留。"""
    chunks = [
        "第一段内容。",
        "完全不同的第二段。",
        "第三段也是独特内容。",
    ]
    result = deduplicate_chunks(chunks)
    assert len(result) == 3


def test_extract_metadata_title():
    """提取 Markdown 标题。"""
    text = "# 系统设计文档\n\n正文内容。"
    metadata = extract_metadata(text)
    assert metadata.get("title") == "系统设计文档"


def test_extract_metadata_author():
    """提取作者信息。"""
    text = "作者：张三\n正文内容。"
    metadata = extract_metadata(text)
    assert metadata.get("author") == "张三"


def test_extract_metadata_date():
    """提取日期信息。"""
    text = "日期：2026-05-29\n正文内容。"
    metadata = extract_metadata(text)
    assert metadata.get("date") == "2026-05-29"


def test_extract_metadata_empty():
    """无元数据时返回空 dict。"""
    text = "普通正文，没有元数据。"
    metadata = extract_metadata(text)
    assert metadata == {}


def test_clean_document_full_pipeline():
    """完整清洗流程：清理 + 元数据提取。"""
    text = "﻿# 测试文档\n作者：李四\n日期：2026-05-29\n\n正文内容。"
    cleaned, metadata = clean_document(text)
    assert "﻿" not in cleaned
    assert metadata.get("title") == "测试文档"
    assert metadata.get("author") == "李四"
    assert metadata.get("date") == "2026-05-29"


def test_load_text_uses_cleaner():
    """loader 加载 txt 应经过 cleaner 处理。"""
    import os
    import tempfile

    from rag.loader import load

    # Create a temp file with BOM
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write("﻿测试内容".encode())
        tmp_path = f.name

    try:
        result = load(tmp_path)
        assert "﻿" not in result
        assert "测试内容" in result
    finally:
        os.unlink(tmp_path)


def test_load_text_handles_gbk():
    """loader 加载 GBK 编码文件应自动检测。"""
    import os
    import tempfile

    from rag.loader import load

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write("你好世界".encode("gbk"))
        tmp_path = f.name

    try:
        result = load(tmp_path)
        assert "你好世界" in result
    finally:
        os.unlink(tmp_path)
