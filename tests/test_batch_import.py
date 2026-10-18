"""批量导入测试。"""
import os
import tempfile


def test_batch_import_endpoint_exists():
    """POST /batch-import 端点应该存在。"""
    from fastapi.testclient import TestClient

    from rag.api import app
    client = TestClient(app)
    response = client.post("/batch-import")
    assert response.status_code != 404


def test_batch_importer_parses_csv_qa_pair():
    """CSV QA 对模式：每行是一个问答对。"""
    import csv

    from rag.batch_importer import BatchImporter

    # 创建临时 CSV 文件
    csv_path = tempfile.mktemp(suffix=".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["什么是 mTLS？", "mTLS 是双向 TLS 认证"])
        writer.writerow(["什么是 Raft？", "Raft 是一致性协议"])

    try:
        importer = BatchImporter()
        chunks = importer.parse(csv_path, mode="qa_pair", config={"question_col": "问题", "answer_col": "答案"})
        assert len(chunks) == 2
        assert "mTLS" in chunks[0].text
        assert "Raft" in chunks[1].text
    finally:
        os.unlink(csv_path)


def test_batch_importer_parses_csv_document():
    """CSV document 模式：每行是一个文档片段。"""
    import csv

    from rag.batch_importer import BatchImporter

    csv_path = tempfile.mktemp(suffix=".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["content"])
        writer.writerow(["第一章：系统架构"])
        writer.writerow(["第二章：部署指南"])

    try:
        importer = BatchImporter()
        chunks = importer.parse(csv_path, mode="document", config={"content_col": "content"})
        assert len(chunks) == 2
    finally:
        os.unlink(csv_path)


def test_batch_importer_parses_excel():
    """Excel 模式应该解析 xlsx 文件。"""
    from rag.batch_importer import BatchImporter

    # 创建临时 xlsx
    try:
        import openpyxl
        xlsx_path = tempfile.mktemp(suffix=".xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["问题", "答案"])
        ws.append(["什么是 K8s？", "Kubernetes 容器编排"])
        wb.save(xlsx_path)

        importer = BatchImporter()
        chunks = importer.parse(xlsx_path, mode="qa_pair", config={"question_col": "问题", "answer_col": "答案"})
        assert len(chunks) == 1
        assert "K8s" in chunks[0].text
    finally:
        if os.path.exists(xlsx_path):
            os.unlink(xlsx_path)


def test_batch_importer_unsupported_format():
    """不支持的文件格式应该抛 ValueError。"""
    from rag.batch_importer import BatchImporter
    importer = BatchImporter()
    try:
        importer.parse("test.xyz", mode="qa_pair")
        raise AssertionError("应该抛 ValueError")
    except ValueError as e:
        assert "不支持" in str(e)


def test_batch_importer_table_mode():
    """table 模式应该将整张表转为文本。"""
    import csv

    from rag.batch_importer import BatchImporter

    csv_path = tempfile.mktemp(suffix=".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["姓名", "部门", "薪资"])
        writer.writerow(["张三", "技术", "15000"])
        writer.writerow(["李四", "产品", "12000"])

    try:
        importer = BatchImporter()
        chunks = importer.parse(csv_path, mode="table")
        assert len(chunks) == 1  # 整张表作为一个 chunk
        assert "张三" in chunks[0].text
        assert "李四" in chunks[0].text
    finally:
        os.unlink(csv_path)


def test_batch_importer_parses_gbk_encoded_csv():
    """GBK 编码的 CSV 文件应能自动检测并正确解析。"""
    import csv

    from rag.batch_importer import BatchImporter

    csv_path = tempfile.mktemp(suffix=".csv")
    with open(csv_path, "w", newline="", encoding="gbk") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["中文GBK测试问题", "中文GBK测试答案内容"])

    try:
        importer = BatchImporter()
        chunks = importer.parse(csv_path, mode="qa_pair", config={"question_col": "问题", "answer_col": "答案"})
        assert len(chunks) == 1
        assert "中文GBK测试问题" in chunks[0].text
        assert "中文GBK测试答案内容" in chunks[0].text
    finally:
        os.unlink(csv_path)

