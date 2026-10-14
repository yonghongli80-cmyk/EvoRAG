"""Unit tests for Markdown AST Structure-Aware Chunker."""

from __future__ import annotations

import pytest

from rag.wiki.chunker import MarkdownASTChunker, WikiChunk
from rag.wiki.models import WikiPage


@pytest.fixture
def chunker() -> MarkdownASTChunker:
    return MarkdownASTChunker(max_chunk_chars=600, min_chunk_chars=50)


def _make_page(
    content: str,
    title: str = "运维手册",
    kb_id: str = "kb_ops",
    page_id: int = 42,
) -> WikiPage:
    return WikiPage(
        id=page_id,
        kb_id=kb_id,
        title=title,
        slug="ops-manual",
        content=content,
        tags=("ops", "sop"),
        current_revision_id=1,
        created_at=1000.0,
        updated_at=1000.0,
    )


def test_empty_or_whitespace_page(chunker):
    page_empty = _make_page("")
    assert chunker.chunk_page(page_empty) == []

    page_ws = _make_page("   \n\n  \t  \n")
    assert chunker.chunk_page(page_ws) == []


def test_single_section_chunking(chunker):
    md = """# 服务部署指南

在正式发布生产前，请确保所有环境参数均已通过审核。
执行前请确认容器健康探测已就绪。
"""
    page = _make_page(md, title="部署文档")
    chunks = chunker.chunk_page(page)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, WikiChunk)
    assert chunk.page_id == 42
    assert chunk.kb_id == "kb_ops"
    assert chunk.section_path == "服务部署指南"
    assert chunk.heading_level == 1
    assert chunk.chunk_index == 0
    assert chunk.text.startswith("[Wiki: 部署文档 > 服务部署指南]")
    assert "容器健康探测已就绪" in chunk.text


def test_nested_heading_breadcrumbs(chunker):
    md = """# 系统架构
总体设计概述。

## 存储引擎
存储引擎负责全量持久化。

### SQLite 模块
SQLite 提供单机嵌入式事务保证。

### Qdrant 模块
Qdrant 负责高维稠密向量召回。
"""
    page = _make_page(md, title="架构白皮书")
    chunks = chunker.chunk_page(page)

    # 应该包含对应章节的面包屑路径
    paths = [c.section_path for c in chunks]
    assert "系统架构" in paths
    assert "系统架构 > 存储引擎" in paths
    assert "系统架构 > 存储引擎 > SQLite 模块" in paths
    assert "系统架构 > 存储引擎 > Qdrant 模块" in paths

    sqlite_chunk = next(c for c in chunks if "SQLite 模块" in c.section_path)
    assert sqlite_chunk.text.startswith("[Wiki: 架构白皮书 > 系统架构 > 存储引擎 > SQLite 模块]")
    assert sqlite_chunk.heading_level == 3


def test_atomic_table_protection(chunker):
    md = """# 规格参数表

以下为核心服务器规格清单：

| 节点编号 | CPU 核数 | 内存 (GB) | 存储 (TB) | 角色定位 |
| :--- | :--- | :--- | :--- | :--- |
| node-01 | 64 | 256 | 4.0 | 主搜索计算节点 |
| node-02 | 64 | 256 | 4.0 | 从搜索计算节点 |
| node-03 | 32 | 128 | 8.0 | 稠密向量索引库 |
| node-04 | 16 | 64  | 2.0 | 倒排与配置管理 |

表格结束说明。
"""
    page = _make_page(md, title="硬件配置")
    chunks = chunker.chunk_page(page)

    # 表格行必须保持在同一个切块内，不能跨块硬性截断表头或表身
    table_chunk = next(c for c in chunks if "| node-01 |" in c.text)
    assert "| node-02 |" in table_chunk.text
    assert "| node-03 |" in table_chunk.text
    assert "| node-04 |" in table_chunk.text
    assert "| 节点编号 |" in table_chunk.text


def test_atomic_code_fence(chunker):
    code_block = """```python
def calculate_rrf(rank_vector: int, rank_bm25: int, k: int = 60) -> float:
    score1 = 1.0 / (k + rank_vector + 1)
    score2 = 1.0 / (k + rank_bm25 + 1)
    return score1 + score2
```"""
    md = f"""# 融合算法

## 代码示例
以下为 RRF 计算源码：

{code_block}

后续逻辑说明。
"""
    page = _make_page(md, title="算法手册")
    chunks = chunker.chunk_page(page)

    code_chunk = next(c for c in chunks if "calculate_rrf" in c.text)
    assert "score1 = 1.0" in code_chunk.text
    assert "return score1 + score2" in code_chunk.text


def test_long_content_splits_gracefully():
    # max_chunk_chars 设为 200，触发段落切分
    tiny_chunker = MarkdownASTChunker(max_chunk_chars=200, min_chunk_chars=30)
    p1 = "这是第一段极其详尽的系统设计说明文字，详细描述了双模态知识底座的运行机理与演进原则。" * 3
    p2 = "这是第二段操作规程与故障恢复指导，要求运维人员在五分钟之内完成快照回滚与双路同步。" * 3
    md = f"""# 深度指南

{p1}

{p2}
"""
    page = _make_page(md, title="长文指南")
    chunks = tiny_chunker.chunk_page(page)

    assert len(chunks) >= 2
    for c in chunks:
        assert c.text.startswith("[Wiki: 长文指南 > 深度指南]")
        # 长度不能极度超标（除单行不可分长字符串外）
        assert len(c.raw_text) <= 300


def test_links_extracted_in_chunk_metadata(chunker):
    md = """# 关联指引
详情请参考 [[故障排查SOP]] 以及 [[备份方案|灾备手册]]。
如有疑问联系 [[值班团队]]。
"""
    page = _make_page(md, title="指引索引")
    chunks = chunker.chunk_page(page)

    assert len(chunks) == 1
    c = chunks[0]
    assert set(c.links) == {"故障排查SOP", "备份方案", "值班团队"}
    assert "故障排查SOP" in c.metadata["links"]


def test_to_chunk_conversion(chunker):
    md = """# 核心组件
这是核心组件的定义文本。
"""
    page = _make_page(md, title="系统组件", page_id=99)
    chunks = chunker.chunk_page(page)
    assert len(chunks) == 1

    wiki_chunk = chunks[0]
    std_chunk = wiki_chunk.to_chunk()

    assert std_chunk.text == wiki_chunk.text
    assert std_chunk.doc_name == "系统组件"
    assert std_chunk.document_id == 99
    assert std_chunk.chunk_index == 0
    assert std_chunk.chunk_kind == "wiki"
    assert std_chunk.metadata["section_path"] == "核心组件"
    assert std_chunk.metadata["kb_id"] == "kb_ops"
