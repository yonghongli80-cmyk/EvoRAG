from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.models import Chunk


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
    texts = splitter.split_text(text)
    return [
        Chunk(text=t, doc_name=doc_name, chunk_index=i, metadata=dict(metadata or {}), section_title=section_title)
        for i, t in enumerate(texts)
    ]
