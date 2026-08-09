"""批量导入处理器 — Excel/CSV 结构化数据导入。"""
import csv
import logging
from pathlib import Path

from config import settings
from rag.models import Chunk

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".csv", ".xlsx"}


class BatchImporter:
    def parse(self, file_path: str, mode: str, config: dict = None) -> list[Chunk]:
        """解析文件，返回 Chunk 列表。

        Args:
            file_path: 文件路径
            mode: 导入模式 - qa_pair / document / table
            config: 配置参数
                qa_pair: question_col, answer_col
                document: content_col
                table: (无额外参数)
        """
        config = config or {}
        suffix = Path(file_path).suffix.lower()

        if suffix == ".csv":
            rows = self._read_csv(file_path)
        elif suffix == ".xlsx":
            rows = self._read_excel(file_path)
        else:
            raise ValueError(f"不支持的文件格式: {suffix}，支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")

        if mode == "qa_pair":
            return self._parse_qa_pair(rows, config, file_path)
        elif mode == "document":
            return self._parse_document(rows, config, file_path)
        elif mode == "table":
            return self._parse_table(rows, file_path)
        else:
            raise ValueError(f"不支持的导入模式: {mode}，支持: qa_pair / document / table")

    def _read_csv(self, file_path: str) -> list[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
        return rows

    def _read_excel(self, file_path: str) -> list[dict]:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h) if h else f"col_{i}" for i, h in enumerate(rows[0])]
        result = []
        for row in rows[1:]:
            result.append({headers[i]: str(row[i]) if row[i] else "" for i in range(min(len(headers), len(row)))})
        wb.close()
        return result

    def _parse_qa_pair(self, rows: list[dict], config: dict, file_path: str) -> list[Chunk]:
        q_col = config.get("question_col", "问题")
        a_col = config.get("answer_col", "答案")
        doc_name = Path(file_path).stem
        chunks = []
        for i, row in enumerate(rows):
            q = row.get(q_col, "").strip()
            a = row.get(a_col, "").strip()
            if q and a:
                text = f"问：{q}\n答：{a}"
                chunks.append(Chunk(text=text, doc_name=doc_name, chunk_index=i))
        return chunks

    def _parse_document(self, rows: list[dict], config: dict, file_path: str) -> list[Chunk]:
        col = config.get("content_col", "content")
        doc_name = Path(file_path).stem
        chunks = []
        for i, row in enumerate(rows):
            text = row.get(col, "").strip()
            if text:
                chunks.append(Chunk(text=text, doc_name=doc_name, chunk_index=i))
        return chunks

    def _parse_table(self, rows: list[dict], file_path: str) -> list[Chunk]:
        if not rows:
            return []
        doc_name = Path(file_path).stem
        headers = list(rows[0].keys())
        lines = ["\t".join(headers)]
        for row in rows:
            lines.append("\t".join(str(row.get(h, "")) for h in headers))
        text = "\n".join(lines)
        if len(text) > settings.max_chunk_chars:
            logger.warning(
                "表格导入内容超过 %s 字符，已截断: %s",
                settings.max_chunk_chars,
                file_path,
            )
            text = text[: settings.max_chunk_chars]
        return [Chunk(text=text, doc_name=doc_name, chunk_index=0)]
