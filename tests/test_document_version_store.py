"""Task 2: source-neutral upload adapter and version file store tests."""

import hashlib
from pathlib import Path

import pytest

from rag.document_sources.upload import UploadDocumentSource
from rag.document_version_store import (
    final_version_path,
    promote_version,
    resolve_version_path,
    stage_version,
)


def test_upload_source_has_stable_hash_and_upload_identity():
    source = UploadDocumentSource.from_bytes("../policy.pdf", b"policy-v1")

    assert source.source_type == "upload"
    assert source.display_name == "policy.pdf"
    assert source.content_hash == hashlib.sha256(b"policy-v1").hexdigest()
    assert source.source_key.startswith("upload:")


def test_upload_source_rejects_empty_or_dot_filename():
    with pytest.raises(ValueError):
        UploadDocumentSource.from_bytes("..", b"x")
    with pytest.raises(ValueError):
        UploadDocumentSource.from_bytes("", b"x")
    with pytest.raises(ValueError):
        UploadDocumentSource.from_bytes(".", b"x")


def test_version_path_cannot_escape_root(tmp_path):
    root = tmp_path / "versions"

    with pytest.raises(ValueError):
        final_version_path(root, 4, 1, "../../secret.txt")

    with pytest.raises(ValueError):
        resolve_version_path(root, str(tmp_path / "outside.txt"))


def test_stage_and_promote_cycle(tmp_path):
    source = UploadDocumentSource.from_bytes("policy.pdf", b"policy-v1")
    root = tmp_path / "versions"

    stage = stage_version(root, source)
    assert stage.is_file()
    assert stage.read_bytes() == b"policy-v1"

    dest = final_version_path(root, 4, 1, "policy.pdf")
    promote_version(stage, dest)

    assert dest.is_file()
    assert dest.read_bytes() == b"policy-v1"
    assert not stage.exists()


def test_final_version_path_is_namespaced_by_document(tmp_path):
    root = tmp_path / "versions"
    dest = final_version_path(root, 4, 1, "policy.pdf")
    assert str(dest).endswith(str(Path("4") / "1" / "policy.pdf"))


def test_resolve_version_path_returns_promoted_file(tmp_path):
    source = UploadDocumentSource.from_bytes("policy.pdf", b"v1")
    root = tmp_path / "versions"
    stage = stage_version(root, source)
    dest = final_version_path(root, 4, 1, "policy.pdf")
    promote_version(stage, dest)

    resolved = resolve_version_path(root, str(dest))
    assert resolved == dest.resolve()
