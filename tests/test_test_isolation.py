from pathlib import Path


def test_api_tests_never_use_repository_data_paths():
    import rag.api as api
    from config import settings

    repository = Path(__file__).resolve().parents[1]
    assert not Path(api.user_db.db_path).resolve().is_relative_to(repository)
    assert not Path(settings.qdrant_path).resolve().is_relative_to(repository)
    assert not Path(api.DATA_DIR).resolve().is_relative_to(repository)
