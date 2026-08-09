import csv

import pytest

from config import settings
from rag.batch_importer import BatchImporter
from rag.loader import load


def test_loader_rejects_file_larger_than_configured_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * 11)

    with pytest.raises(ValueError, match="文件大小"):
        load(str(oversized))


def test_table_importer_truncates_oversized_combined_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_chunk_chars", 20)
    csv_path = tmp_path / "report.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["detail"])
        writer.writeheader()
        writer.writerow({"detail": "x" * 100})

    chunks = BatchImporter().parse(str(csv_path), mode="table")

    assert len(chunks) == 1
    assert len(chunks[0].text) == 20
