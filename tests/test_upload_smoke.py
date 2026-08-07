"""上传冒烟测试：回归保障 python-multipart 依赖。

依赖清单修复前（`requirements.txt` 含 `python-multipart`、`redis`，
但 `pyproject.toml [project.dependencies]` 缺失），`pip install -e .`
装出的运行时缺少 python-multipart，FastAPI 解析 multipart 表单会直接
抛错，`POST /upload` 返回 500；补依赖后上传请求可被正常解析。

本测试只验证「上传接口可达且不再 500」这一修复目标：
- 下游索引（KnowledgeBaseManager）与管线刷新（RAGPipeline）均 mock，
  不依赖真实 qdrant / embedder / redis / 外部 API key；
- 落盘目录 monkeypatch 到 tmp，不污染仓库 data/upload。
"""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import rag.api as api_module


def test_upload_smoke_not_500(tmp_path, monkeypatch):
    """POST /upload 上传小文本文件，断言返回 200（而非 500）。"""
    # 上传落盘目录隔离到 tmp，避免写进仓库 data/upload
    monkeypatch.setattr(api_module, "DATA_DIR", tmp_path)

    with (
        patch("rag.api.KnowledgeBaseManager") as mock_kb_cls,
        patch("rag.pipeline.RAGPipeline"),
    ):
        mock_manager = MagicMock()
        mock_manager.add_document.return_value = 1
        mock_kb_cls.return_value = mock_manager

        client = TestClient(api_module.app)

        tmp_file = tmp_path / "hello.txt"
        tmp_file.write_bytes(b"hello rag upload")

        with open(tmp_file, "rb") as f:
            response = client.post(
                "/upload",
                files={"file": ("hello.txt", f, "text/plain")},
            )

    assert response.status_code == 200, (
        f"上传接口应返回 200（依赖修复后 multipart 可正常解析），"
        f"实际 {response.status_code}: {response.text}"
    )
    data = response.json()
    assert data["status"] == "uploaded"
