"""Stage, finalize, promote, and resolve paths for immutable document versions.

Layout under the configured root::

    root/staging/<source-key>.bin        temporary staged content
    root/<document_id>/<version_no>/<name>   immutable final version file

Final paths and stored-path resolution are both confined to the root; any
traversal outside it raises ``ValueError``.
"""

from pathlib import Path

from rag.document_sources.base import SourceObject


def _ensure_within(root: Path, candidate: Path) -> Path:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    if not candidate_resolved.is_relative_to(root_resolved):
        raise ValueError("path escapes configured root")
    return candidate_resolved


def stage_version(root: Path, source: SourceObject) -> Path:
    """Write a source object's content to a staging file and return its path.

    The staged filename keeps the original extension so format loaders can
    recognise the content before promotion.
    """
    staging_dir = root / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    token = source.source_key.replace(":", "-")
    stage = staging_dir / f"{token}-{source.display_name}"
    stage.write_bytes(source.content)
    return stage


def final_version_path(root: Path, document_id: int, version_no: int, filename: str) -> Path:
    """Return the immutable destination path for a document version."""
    safe_name = Path(filename).name
    if safe_name in {"", ".", ".."} or safe_name != filename:
        raise ValueError("invalid filename")
    dest = root / str(document_id) / str(version_no) / safe_name
    return _ensure_within(root, dest)


def promote_version(stage: Path, destination: Path) -> None:
    """Atomically move a staged file to its final version path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage.replace(destination)


def resolve_version_path(root: Path, stored_path: str) -> Path:
    """Resolve a stored version path and verify it stays under root."""
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return _ensure_within(root, candidate)
