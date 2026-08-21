"""Tests for rag.bm25_store — BM25 SQLite persistence."""
from rag.bm25_store import BM25Store
from rag.models import Chunk


def _make_chunks(n: int = 3) -> list[Chunk]:
    return [
        Chunk(text=f"chunk text {i}", doc_name=f"doc{i}.md", chunk_index=i)
        for i in range(n)
    ]


def test_save_and_load_roundtrip(tmp_path):
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)
    chunks = _make_chunks()

    store.save_chunks("col1", chunks)
    loaded = store.load_chunks("col1")

    assert len(loaded) == len(chunks)
    for orig, got in zip(chunks, loaded, strict=False):
        assert got.text == orig.text
        assert got.doc_name == orig.doc_name
        assert got.chunk_index == orig.chunk_index
    store.close()


def test_has_chunks_true_when_data_exists(tmp_path):
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)

    assert store.has_chunks("col1") is False

    store.save_chunks("col1", _make_chunks())

    assert store.has_chunks("col1") is True
    store.close()


def test_has_chunks_false_for_empty_collection(tmp_path):
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)

    store.save_chunks("col1", _make_chunks())

    assert store.has_chunks("col_nonexistent") is False
    store.close()


def test_save_overwrites_old_data(tmp_path):
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)

    store.save_chunks("col1", _make_chunks(2))
    assert len(store.load_chunks("col1")) == 2

    store.save_chunks("col1", _make_chunks(5))
    loaded = store.load_chunks("col1")
    assert len(loaded) == 5
    store.close()


def test_load_empty_collection_returns_empty_list(tmp_path):
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)

    loaded = store.load_chunks("nonexistent")
    assert loaded == []
    store.close()


def test_different_collections_are_independent(tmp_path):
    db = str(tmp_path / "test.db")
    store = BM25Store(db_path=db)

    store.save_chunks("colA", _make_chunks(2))
    store.save_chunks("colB", _make_chunks(4))

    assert len(store.load_chunks("colA")) == 2
    assert len(store.load_chunks("colB")) == 4
    store.close()
