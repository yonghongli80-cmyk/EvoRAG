"""Safe, durable storage primitives for knowledge-base source files."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def _single_component(value: str, label: str) -> str:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return value


def stage_upload(root: Path, safe_filename: str, content: bytes) -> Path:
    filename = _single_component(safe_filename, "filename")
    staging = root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / f"{uuid.uuid4().hex}-{filename}"
    path.write_bytes(content)
    return path


def final_document_path(
    root: Path, kb_id: str, document_id: int, safe_filename: str
) -> Path:
    safe_kb = _single_component(kb_id, "kb_id")
    filename = _single_component(safe_filename, "filename")
    if document_id <= 0:
        raise ValueError("invalid document_id")
    return root / safe_kb / str(document_id) / filename


def promote_stage(stage: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=False)
    os.replace(stage, destination)
    return destination


def resolve_stored_path(root: Path, stored_path: str) -> Path:
    resolved_root = root.resolve()
    resolved = Path(stored_path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("stored path escapes kb root")
    return resolved
