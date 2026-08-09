"""Startup must rebuild an index when its Qdrant collection was lost."""

import json
from unittest.mock import MagicMock


def test_startup_reindexes_when_index_state_exists_but_collection_is_missing(tmp_path, monkeypatch):
    import rag.api as api
    import rag.folder_indexer as folder_indexer
    import rag.vector_store as vector_store

    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    document = upload_dir / "document.txt"
    document.write_text("rebuild me", encoding="utf-8")
    document_hash = folder_indexer.compute_file_hash(str(document))
    (tmp_path / "index_state.json").write_text(
        json.dumps({"files": {"document.txt": {"hash": document_hash}}}),
        encoding="utf-8",
    )

    missing_collection_client = MagicMock()
    missing_collection_client.collection_exists.return_value = False
    index_folder = MagicMock(return_value={"files": 1, "chunks": 1})

    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(vector_store, "_get_client", lambda: missing_collection_client)
    monkeypatch.setattr(folder_indexer, "index_folder", index_folder)
    monkeypatch.setattr(api, "_get_or_create_pipeline", MagicMock())

    api.auto_index_on_startup()

    index_folder.assert_called_once_with(str(upload_dir))
