"""Local browser-upload source adapter."""

import hashlib
import time
import uuid
from pathlib import Path

from rag.document_sources.base import SourceObject


class UploadDocumentSource:
    """Build a source-neutral ``SourceObject`` from raw uploaded bytes.

    The display name is always reduced to the basename so path traversal
    supplied by a client never reaches the version store. Path-escape
    protection for final destinations is enforced by
    ``rag.document_version_store``.
    """

    @staticmethod
    def from_bytes(filename: str, content: bytes) -> SourceObject:
        safe_name = Path(filename).name
        if safe_name in {"", ".", ".."}:
            raise ValueError("invalid upload filename")
        digest = hashlib.sha256(content).hexdigest()
        return SourceObject(
            source_type="upload",
            source_key=f"upload:{uuid.uuid4()}",
            display_name=safe_name,
            content=content,
            content_hash=digest,
            observed_at=time.time(),
        )
