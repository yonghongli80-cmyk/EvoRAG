"""JWT Secret 持久化测试。"""

import os
from unittest.mock import patch


def test_jwt_secret_creates_file_when_missing(tmp_path, monkeypatch):
    """文件不存在时，生成 secret 并写入文件。"""
    from rag.auth import _load_or_create_secret

    # tests/conftest.py 固定了 RAG_JWT_SECRET（避免读写仓库里的 data/jwt_secret.txt），
    # 这里测的是"没有环境变量时的文件回退"，所以必须先摘掉它。
    monkeypatch.delenv("RAG_JWT_SECRET", raising=False)

    secret_file = tmp_path / "jwt_secret.txt"
    secret = _load_or_create_secret(secret_file)

    assert len(secret) >= 32, f"secret 太短: {len(secret)} 字符"
    assert secret_file.exists(), "secret 文件未创建"
    assert secret_file.read_text().strip() == secret


def test_jwt_secret_reads_from_existing_file(tmp_path, monkeypatch):
    """文件存在时，读取文件内容而非重新生成。"""
    from rag.auth import _load_or_create_secret

    monkeypatch.delenv("RAG_JWT_SECRET", raising=False)

    secret_file = tmp_path / "jwt_secret.txt"
    secret_file.write_text("my-existing-secret-abcdef1234567890ab")

    secret = _load_or_create_secret(secret_file)
    assert secret == "my-existing-secret-abcdef1234567890ab"


def test_jwt_secret_same_after_restart(tmp_path, monkeypatch):
    """两次调用返回同一 secret（模拟重启）。"""
    from rag.auth import _load_or_create_secret

    monkeypatch.delenv("RAG_JWT_SECRET", raising=False)

    secret_file = tmp_path / "jwt_secret.txt"
    secret1 = _load_or_create_secret(secret_file)
    secret2 = _load_or_create_secret(secret_file)

    assert secret1 == secret2, f"两次调用 secret 不同: {secret1!r} != {secret2!r}"


def test_jwt_secret_env_takes_priority(tmp_path):
    """环境变量 RAG_JWT_SECRET 优先于文件。"""
    from rag.auth import _load_or_create_secret

    secret_file = tmp_path / "jwt_secret.txt"
    secret_file.write_text("file-secret")

    with patch.dict(os.environ, {"RAG_JWT_SECRET": "env-secret-override"}):
        secret = _load_or_create_secret(secret_file)

    assert secret == "env-secret-override"
