"""Data models and link extraction for Hypertext Wiki."""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict


class WikiPage(BaseModel):
    """An atomic Hypertext Wiki article entry."""

    model_config = ConfigDict(frozen=True)

    id: int
    kb_id: str
    title: str
    slug: str
    content: str
    tags: tuple[str, ...] = ()
    current_revision_id: int
    created_at: float
    updated_at: float


class WikiRevision(BaseModel):
    """An immutable versioned snapshot of a Wiki page."""

    model_config = ConfigDict(frozen=True)

    id: int
    page_id: int
    revision_num: int
    title: str
    content: str
    delta: str | None = None
    author_id: str | None = None
    comment: str
    sha256_hash: str
    created_at: float


_WIKI_LINK_PATTERN = re.compile(r"\[\[([^\[\]\n\r|]+)(?:\|[^\[\]\n\r]+)?\]\]")


def extract_wiki_links(content: str) -> list[str]:
    """Extract all unique target page titles referenced as [[Target]] or [[Target|Alias]].

    Order of first occurrence is preserved.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _WIKI_LINK_PATTERN.finditer(content):
        title = match.group(1).strip()
        if title and title not in seen:
            seen.add(title)
            result.append(title)
    return result


def compute_content_sha256(title: str, content: str) -> str:
    """Compute deterministic SHA-256 hash for a page's title and content."""
    hasher = hashlib.sha256()
    hasher.update(title.strip().encode("utf-8"))
    hasher.update(b"\n---\n")
    hasher.update(content.encode("utf-8"))
    return hasher.hexdigest()
