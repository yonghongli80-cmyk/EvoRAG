"""Streamed, size-limited, hashed quarantine writes.

Content is never loaded wholesale into HTTP-process memory; each iteration
reads one bounded chunk, updates the running SHA-256 and size, and writes it
before the next chunk is requested.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from rag.materials.formats import FormatCapabilityRegistry
from rag.materials.models import MaterialLimits

_SIGNATURE_BYTES = 16


class MaterialTooLarge(Exception):
    def __init__(self, filename: str, limit_bytes: int):
        self.filename = filename
        self.limit_bytes = limit_bytes
        super().__init__(f"material exceeds {limit_bytes} bytes: {filename}")


@dataclass(frozen=True)
class StagedMaterial:
    filename: str
    byte_size: int
    sha256: str
    detected_mime: str
    path: Path


async def stream_upload_to_stage(
    upload: AsyncIterator[bytes],
    filename: str,
    declared_mime: str | None,
    root: Path,
    registry: FormatCapabilityRegistry,
    limits: MaterialLimits,
) -> StagedMaterial:
    """Quarantine an upload stream under ``root/staging`` and hash it on the fly.

    On any failure the temporary stage file (and empty stage directory) are
    removed; nothing else under ``root`` is touched.
    """
    safe_name = Path(filename).name
    stage_dir = root / "staging"
    stage_dir.mkdir(parents=True, exist_ok=True)
    stage = stage_dir / f"{uuid.uuid4().hex}-{safe_name}"

    digest = hashlib.sha256()
    size = 0
    signature = b""
    try:
        with open(stage, "wb") as fh:
            async for chunk in upload:
                if not chunk:
                    continue
                if len(signature) < _SIGNATURE_BYTES:
                    signature = (signature + chunk)[:_SIGNATURE_BYTES]
                digest.update(chunk)
                size += len(chunk)
                if size > limits.file_bytes:
                    raise MaterialTooLarge(filename, limits.file_bytes)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        resolution = registry.resolve(safe_name, declared_mime, signature)
        return StagedMaterial(
            filename=safe_name,
            byte_size=size,
            sha256=digest.hexdigest(),
            detected_mime=resolution.detected_mime,
            path=stage,
        )
    except BaseException:
        stage.unlink(missing_ok=True)
        try:
            stage_dir.rmdir()
        except OSError:
            pass
        raise
