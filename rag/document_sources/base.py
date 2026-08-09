"""Source-neutral contracts for document ingestion.

Every future source adapter (upload today, NAS/SMB/WebDAV later) produces a
frozen ``SourceObject`` so the versioned ingest service never depends on a
concrete transport.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SourceObject:
    source_type: str
    source_key: str
    display_name: str
    content: bytes
    content_hash: str
    observed_at: float


class DocumentSource(Protocol):
    def read(self, source_key: str) -> SourceObject:
        raise NotImplementedError
