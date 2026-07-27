import pytest

from rag.kb_file_store import (
    final_document_path,
    promote_stage,
    resolve_stored_path,
    stage_upload,
)


def test_kb_document_path_contains_kb_and_database_document_id(tmp_path):
    stage = stage_upload(tmp_path, "report.pdf", b"content")
    destination = final_document_path(tmp_path, "kb_finance", 42, "report.pdf")

    result = promote_stage(stage, destination)

    assert result == tmp_path / "kb_finance" / "42" / "report.pdf"
    assert result.read_bytes() == b"content"
    assert not stage.exists()


def test_final_path_rejects_traversal_components(tmp_path):
    with pytest.raises(ValueError):
        final_document_path(tmp_path, "../other", 2, "report.pdf")


def test_stored_path_outside_kb_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(ValueError):
        resolve_stored_path(tmp_path, str(outside))
