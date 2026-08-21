"""增量索引测试。"""
import os
import tempfile


def test_compute_file_hash():
    """compute_file_hash 应该对同一文件返回相同 hash。"""
    from rag.folder_indexer import compute_file_hash
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as path:
        path.write("test content")
        path_name = path.name
    try:
        h1 = compute_file_hash(path_name)
        h2 = compute_file_hash(path_name)
        assert h1 == h2
        assert len(h1) == 32  # MD5 hex
    finally:
        os.unlink(path_name)


def test_compute_file_hash_different_for_different_content():
    """不同内容的文件应该有不同的 hash。"""
    from rag.folder_indexer import compute_file_hash
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as p1:
        p1.write("content A")
        p1_name = p1.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as p2:
        p2.write("content B")
        p2_name = p2.name
    try:
        assert compute_file_hash(p1_name) != compute_file_hash(p2_name)
    finally:
        os.unlink(p1_name)
        os.unlink(p2_name)


def test_load_index_state_empty():
    """不存在的 state 文件应该返回空 dict。"""
    from rag.folder_indexer import load_index_state
    result = load_index_state("/nonexistent/path/state.json")
    assert result == {}


def test_save_and_load_index_state():
    """保存后加载应该返回相同数据。"""
    from rag.folder_indexer import load_index_state, save_index_state
    path = tempfile.mktemp(suffix=".json")
    try:
        state = {"files": {"test.txt": {"hash": "abc123"}}}
        save_index_state(path, state)
        loaded = load_index_state(path)
        assert loaded == state
    finally:
        os.unlink(path)


def test_diff_index_finds_new_files():
    """diff_index 应该检测新增文件。"""
    from rag.folder_indexer import diff_index
    current = {"a.txt": "hash1", "b.txt": "hash2"}
    stored = {"a.txt": "hash1"}
    added, modified, deleted = diff_index(current, stored)
    assert "b.txt" in added
    assert len(modified) == 0
    assert len(deleted) == 0


def test_diff_index_finds_modified_files():
    """diff_index 应该检测修改的文件。"""
    from rag.folder_indexer import diff_index
    current = {"a.txt": "hash_new"}
    stored = {"a.txt": "hash_old"}
    added, modified, deleted = diff_index(current, stored)
    assert len(added) == 0
    assert "a.txt" in modified
    assert len(deleted) == 0


def test_diff_index_finds_deleted_files():
    """diff_index 应该检测删除的文件。"""
    from rag.folder_indexer import diff_index
    current = {}
    stored = {"a.txt": "hash1"}
    added, modified, deleted = diff_index(current, stored)
    assert len(added) == 0
    assert len(modified) == 0
    assert "a.txt" in deleted
