"""按行结构切块：重叠可以重复整行，但不能吐出一个半行碎片。

**为什么需要这一步（实测，2026-10-11）**：`RecursiveCharacterTextSplitter(chunk_size=500,
chunk_overlap=80)` 的**切点本来就在行边界上**（该库的表格行长度中位数 60、90 分位 75、最大 348，
没有一行超过 500 —— 我最初以为"行比块还长所以被迫切进行里"，量了之后被推翻），
但 **overlap 是纯字符级的**：新块会把上一块尾部 80 个字符原样再吐一遍，
于是**块首落在半行中间**。实测 `kb_accessories` 的 104 块里 **91 块**首行以数字或制表符开头
（"…878-01 RICOH…"这种从中间开始的碎片），而**块尾**行边界本来就正常 ——
我早期用"块尾没有换行 = 被截断"当指标，那是**错的代理**（切分器本就会去掉末尾换行）。

修法很小：把每块的起点**对齐到行边界**。被丢掉的那一小段是上一块**已经完整包含**的重复内容，
所以不丢信息；重叠因此变成"上一块末尾若干个**整行**"，正是它本来该有的含义。
"""

from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.models import Chunk


def _line_start_offsets(text: str) -> set[int]:
    """The offsets at which a line begins (0 counts)."""
    offsets = {0}
    for index, character in enumerate(text):
        if character == "\n":
            offsets.add(index + 1)
    return offsets


def _align_starts_to_lines(text: str, pieces: list[str]) -> list[str]:
    """Trim each piece's leading partial line, if the splitter's overlap created one.

    The splitter returns substrings in order, so a moving cursor locates each one exactly; a piece
    that does not begin at a line start is trimmed to its first newline. A piece that is itself a
    single partial line (no newline) is kept as is — dropping it entirely would lose text.
    """
    starts = _line_start_offsets(text)
    aligned: list[str] = []
    cursor = 0
    for piece in pieces:
        index = text.find(piece, cursor)
        if index < 0:  # 不该发生；保守起见原样保留，绝不因为对齐而丢内容
            aligned.append(piece)
            continue
        cursor = index
        if index in starts:
            aligned.append(piece)
            continue
        head, separator, tail = piece.partition("\n")
        aligned.append(tail if separator and tail else piece)
    return aligned


def chunk(
    text: str,
    chunk_size: int = None,
    overlap: int = None,
    doc_name: str = "",
    metadata: dict[str, str] = None,
    section_title: str = None,
) -> list[Chunk]:
    if not text:
        return []
    if chunk_size is None or overlap is None:
        from config import settings

        chunk_size = chunk_size or settings.chunk_size
        overlap = overlap or settings.chunk_overlap
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        length_function=len,
        separators=["\n\n", "\n", "。", ".", " ", ""],
    )
    texts = _align_starts_to_lines(text, splitter.split_text(text))
    return [
        Chunk(text=t, doc_name=doc_name, chunk_index=i, metadata=dict(metadata or {}), section_title=section_title)
        for i, t in enumerate(texts)
    ]
