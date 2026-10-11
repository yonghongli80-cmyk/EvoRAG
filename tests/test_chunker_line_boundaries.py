"""按行结构切块：重叠可以重复整行，但不能吐出半行碎片。

实测动机（2026-10-11）：`chunk_overlap=80` 是**纯字符级**的，于是 `kb_accessories` 的 104 块里
**91 块**首行以数字或制表符开头（从表格行中间开始的碎片）。切点本身在行边界上 ——
我最初以为"行比块长所以被迫切进去"，量了之后被推翻。
"""

from __future__ import annotations

from rag.chunker import chunk

#: 一张表格：每行都完整、都不超过 chunk_size，但整体超过它 ⇒ 必然产生重叠。
ROWS = "".join(f"{i:03d}\tFX-{i:03d}\t镜头 {i} 支\t￥{i * 137}\t现货\n" for i in range(1, 61))


def test_every_chunk_starts_at_a_line_boundary():
    pieces = chunk(ROWS, chunk_size=200, overlap=80, doc_name="t.md")

    assert len(pieces) > 2, "夹具应当产生多块，否则测不到重叠"
    for piece in pieces:
        index = ROWS.find(piece.text)
        assert index >= 0
        assert index == 0 or ROWS[index - 1] == "\n", "块首必须落在行边界上（不能是半行碎片）"


def test_alignment_loses_no_content():
    """被丢掉的那一小段必须是上一块**已经完整包含**的重复内容。"""
    pieces = chunk(ROWS, chunk_size=200, overlap=80, doc_name="t.md")

    joined = "".join(piece.text for piece in pieces)
    for row in ROWS.strip().splitlines():
        assert row in joined, "每一行都必须至少完整出现在某一块里"


def test_a_single_partial_line_is_kept_rather_than_dropped():
    """极端情形（整块就是半行）保守保留 —— 对齐不能变成丢内容。"""
    text = "x" * 300 + "\n" + "y" * 300
    pieces = chunk(text, chunk_size=100, overlap=20, doc_name="t.md")

    assert any("x" * 50 in piece.text for piece in pieces)
    assert any("y" * 50 in piece.text for piece in pieces)


def test_plain_prose_still_splits():
    prose = "这是一句测试文本。" * 200
    pieces = chunk(prose, chunk_size=200, overlap=40, doc_name="p.md")

    assert len(pieces) > 1
    assert all(piece.text for piece in pieces)
