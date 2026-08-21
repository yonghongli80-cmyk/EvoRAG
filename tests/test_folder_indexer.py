"""Tests for folder_indexer module."""
import os
from unittest.mock import patch

import pytest


def test_scan_folder_returns_supported_files(tmp_path):
    from rag.folder_indexer import scan_folder

    (tmp_path / "doc.txt").write_text("hello")
    (tmp_path / "doc.md").write_text("# title")
    (tmp_path / "doc.pdf").touch()
    (tmp_path / "image.png").touch()
    (tmp_path / "data.csv").touch()

    result = scan_folder(str(tmp_path))

    basenames = [os.path.basename(f) for f in result]
    assert "doc.txt" in basenames
    assert "doc.md" in basenames
    assert "doc.pdf" in basenames
    assert "image.png" not in basenames
    # CSV is a first-class indexed material in the unified format registry.
    assert "data.csv" in basenames


def test_scan_folder_returns_empty_for_empty_dir(tmp_path):
    from rag.folder_indexer import scan_folder

    result = scan_folder(str(tmp_path))
    assert result == []


def test_scan_folder_raises_on_missing_dir():
    from rag.folder_indexer import scan_folder

    with pytest.raises(FileNotFoundError):
        scan_folder("/nonexistent/path")


def test_scan_folder_recursive(tmp_path):
    from rag.folder_indexer import scan_folder

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.txt").write_text("content")
    (tmp_path / "root.md").write_text("# root")

    result = scan_folder(str(tmp_path))
    basenames = [os.path.basename(f) for f in result]
    assert "deep.txt" in basenames
    assert "root.md" in basenames


@patch("rag.embedder.embed")
@patch("rag.vector_store.replace")
@patch("rag.chunker.chunk")
@patch("rag.loader.load")
def test_index_folder_returns_stats(mock_load, mock_chunk, mock_replace, mock_embed, tmp_path):
    from rag.folder_indexer import index_folder
    from rag.models import Chunk

    (tmp_path / "a.txt").write_text("content a")
    (tmp_path / "b.txt").write_text("content b")

    mock_load.return_value = "some text"
    mock_chunk.return_value = [Chunk(text="chunk", doc_name="a.txt", chunk_index=0)]
    mock_embed.return_value = [[0.1] * 1024]

    result = index_folder(str(tmp_path))

    assert result["files"] == 2
    assert result["chunks"] == 2
    assert result["seconds"] >= 0
    mock_replace.assert_called_once()


@patch("rag.embedder.embed")
@patch("rag.vector_store.replace")
@patch("rag.chunker.chunk")
@patch("rag.loader.load")
def test_index_folder_skips_unsupported(mock_load, mock_chunk, mock_replace, mock_embed, tmp_path):
    from rag.folder_indexer import index_folder
    from rag.models import Chunk

    (tmp_path / "ok.txt").write_text("content")
    (tmp_path / "skip.png").touch()

    mock_load.return_value = "content"
    mock_chunk.return_value = [Chunk(text="chunk", doc_name="ok.txt", chunk_index=0)]
    mock_embed.return_value = [[0.1] * 1024]

    result = index_folder(str(tmp_path))

    assert result["files"] == 1
    mock_load.assert_called_once()


@patch("rag.embedder.embed")
@patch("rag.vector_store.add")
@patch("rag.vector_store.clear")
@patch("rag.chunker.chunk")
@patch("rag.loader.load")
def test_index_folder_empty_dir(mock_load, mock_chunk, mock_clear, mock_add, mock_embed, tmp_path):
    from rag.folder_indexer import index_folder

    result = index_folder(str(tmp_path))

    assert result["files"] == 0
    assert result["chunks"] == 0
    mock_clear.assert_called_once()
    mock_load.assert_not_called()
