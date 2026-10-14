"""Markdown AST Structure-Aware Chunker for Hypertext Wiki.

Preserves atomic structures (tables, code blocks) and attaches contextual
breadcrumbs ([Wiki: Title > Heading1 > Heading2]) to each chunk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt

from rag.models import Chunk
from rag.wiki.models import WikiPage, extract_wiki_links


@dataclass(frozen=True)
class WikiChunk:
    """A semantic chunk derived from a Wiki page via Markdown AST structure."""

    text: str
    raw_text: str
    section_path: str
    heading_level: int
    links: tuple[str, ...]
    kb_id: str
    page_id: int
    chunk_index: int
    metadata: dict[str, str] = field(default_factory=dict)

    def to_chunk(
        self,
        doc_permission_id: int | None = None,
        is_current: bool = True,
        validity_status: str = "valid",
    ) -> Chunk:
        """Convert to standard RAG Chunk model for indexing."""
        title = self.metadata.get("title", "")
        updated_at_val = self.metadata.get("updated_at")
        updated_at = float(updated_at_val) if updated_at_val is not None else None

        return Chunk(
            text=self.text,
            doc_name=title,
            chunk_index=self.chunk_index,
            doc_permission_id=doc_permission_id,
            metadata=dict(self.metadata),
            document_id=self.page_id,
            is_current=is_current,
            validity_status=validity_status,
            section_title=self.section_path or None,
            updated_at=updated_at,
            chunk_kind="wiki",
        )


class MarkdownASTChunker:
    """Structure-aware chunker using markdown-it AST parsing."""

    def __init__(
        self,
        max_chunk_chars: int = 800,
        min_chunk_chars: int = 100,
    ) -> None:
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self._md = MarkdownIt().enable("table")

    def chunk_page(self, page: WikiPage) -> list[WikiChunk]:
        """Chunk a WikiPage into structured WikiChunk instances."""
        content = page.content.strip()
        if not content:
            return []

        lines = page.content.splitlines(keepends=True)
        tokens = self._md.parse(page.content)

        # Structure blocks under headings
        # Each section: (heading_level, section_path, list of (is_atomic, block_text))
        sections: list[tuple[int, str, list[tuple[bool, str]]]] = []
        heading_stack: list[tuple[int, str]] = []

        current_heading_level = 0
        current_section_path = ""
        current_blocks: list[tuple[bool, str]] = []

        def flush_current_section() -> None:
            nonlocal current_blocks
            if current_blocks:
                sections.append((current_heading_level, current_section_path, current_blocks))
                current_blocks = []

        i = 0
        n = len(tokens)
        while i < n:
            token = tokens[i]
            if token.level == 0:
                if token.type == "heading_open":
                    flush_current_section()
                    lvl = int(token.tag[1:]) if len(token.tag) > 1 and token.tag[1:].isdigit() else 1
                    # Extract heading text from following inline token
                    inline_text = ""
                    j = i + 1
                    while j < n and tokens[j].type != "heading_close":
                        if tokens[j].type == "inline":
                            inline_text += tokens[j].content
                        j += 1
                    inline_text = inline_text.strip()

                    while heading_stack and heading_stack[-1][0] >= lvl:
                        heading_stack.pop()
                    heading_stack.append((lvl, inline_text))

                    current_heading_level = lvl
                    current_section_path = " > ".join(h[1] for h in heading_stack)
                    i = j + 1
                    continue

                if token.map is not None:
                    start_line, end_line = token.map
                    block_text = "".join(lines[start_line:end_line]).strip()
                    if block_text:
                        is_atomic = token.type in {"table_open", "fence", "code_block"}
                        current_blocks.append((is_atomic, block_text))

            i += 1

        flush_current_section()

        # Emit WikiChunks from sections
        chunks: list[WikiChunk] = []
        chunk_index = 0

        for heading_level, section_path, blocks in sections:
            accumulated_blocks: list[str] = []
            accumulated_len = 0

            for is_atomic, block_text in blocks:
                # If block itself is atomic and exceeds max_chunk_chars, or regular block
                block_len = len(block_text)

                if is_atomic:
                    if accumulated_blocks:
                        chunks.append(
                            self._create_chunk(
                                page=page,
                                raw_text="\n\n".join(accumulated_blocks),
                                section_path=section_path,
                                heading_level=heading_level,
                                chunk_index=chunk_index,
                            )
                        )
                        chunk_index += 1
                        accumulated_blocks = []
                        accumulated_len = 0

                    chunks.append(
                        self._create_chunk(
                            page=page,
                            raw_text=block_text,
                            section_path=section_path,
                            heading_level=heading_level,
                            chunk_index=chunk_index,
                        )
                    )
                    chunk_index += 1
                    continue

                # Non-atomic block: check if single block is larger than max_chunk_chars
                if block_len > self.max_chunk_chars:
                    if accumulated_blocks:
                        chunks.append(
                            self._create_chunk(
                                page=page,
                                raw_text="\n\n".join(accumulated_blocks),
                                section_path=section_path,
                                heading_level=heading_level,
                                chunk_index=chunk_index,
                            )
                        )
                        chunk_index += 1
                        accumulated_blocks = []
                        accumulated_len = 0

                    # Sub-split long block by sentences/newlines
                    sub_parts = self._split_text_into_parts(block_text, self.max_chunk_chars)
                    for part in sub_parts:
                        if part.strip():
                            chunks.append(
                                self._create_chunk(
                                    page=page,
                                    raw_text=part.strip(),
                                    section_path=section_path,
                                    heading_level=heading_level,
                                    chunk_index=chunk_index,
                                )
                            )
                            chunk_index += 1
                    continue

                # Check if adding this block exceeds max_chunk_chars
                new_len = accumulated_len + (2 if accumulated_blocks else 0) + block_len
                if accumulated_blocks and new_len > self.max_chunk_chars:
                    chunks.append(
                        self._create_chunk(
                            page=page,
                            raw_text="\n\n".join(accumulated_blocks),
                            section_path=section_path,
                            heading_level=heading_level,
                            chunk_index=chunk_index,
                        )
                    )
                    chunk_index += 1
                    accumulated_blocks = [block_text]
                    accumulated_len = block_len
                else:
                    accumulated_blocks.append(block_text)
                    accumulated_len = new_len

            if accumulated_blocks:
                chunks.append(
                    self._create_chunk(
                        page=page,
                        raw_text="\n\n".join(accumulated_blocks),
                        section_path=section_path,
                        heading_level=heading_level,
                        chunk_index=chunk_index,
                    )
                )
                chunk_index += 1

        return chunks

    def _create_chunk(
        self,
        page: WikiPage,
        raw_text: str,
        section_path: str,
        heading_level: int,
        chunk_index: int,
    ) -> WikiChunk:
        """Create a WikiChunk with contextual breadcrumb prefix and metadata."""
        header = (
            f"[Wiki: {page.title} > {section_path}]"
            if section_path
            else f"[Wiki: {page.title}]"
        )

        text = f"{header}\n\n{raw_text}"
        links = tuple(extract_wiki_links(raw_text))

        meta: dict[str, str] = {
            "title": page.title,
            "kb_id": page.kb_id,
            "page_id": str(page.id),
            "slug": page.slug,
            "section_path": section_path,
            "heading_level": str(heading_level),
            "chunk_index": str(chunk_index),
            "links": json.dumps(list(links), ensure_ascii=False),
            "chunk_kind": "wiki",
            "updated_at": str(page.updated_at),
        }

        return WikiChunk(
            text=text,
            raw_text=raw_text,
            section_path=section_path,
            heading_level=heading_level,
            links=links,
            kb_id=page.kb_id,
            page_id=page.id,
            chunk_index=chunk_index,
            metadata=meta,
        )

    def _split_text_into_parts(self, text: str, max_chars: int) -> list[str]:
        """Split a long paragraph along natural sentence or line boundaries."""
        if len(text) <= max_chars:
            return [text]

        # Split by sentence-ending punctuation or double newlines
        delimiters = re.compile(r"(\n\n|\n|[。！？\?!]\s*)")
        tokens = delimiters.split(text)

        parts: list[str] = []
        curr = ""
        for t in tokens:
            if not t:
                continue
            if len(curr) + len(t) <= max_chars:
                curr += t
            else:
                if curr.strip():
                    parts.append(curr.strip())
                # If a single token is longer than max_chars, split strictly
                if len(t) > max_chars:
                    for i in range(0, len(t), max_chars):
                        sub = t[i : i + max_chars]
                        if sub.strip():
                            parts.append(sub.strip())
                    curr = ""
                else:
                    curr = t

        if curr.strip():
            parts.append(curr.strip())
        return parts
