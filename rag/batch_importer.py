"""批量导入处理器 — Excel/CSV 结构化数据导入。"""
import csv
import io
import logging
from pathlib import Path

from config import settings
from rag.models import Chunk

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".csv", ".xlsx"}


class BatchImporter:
    def parse(
        self,
        file_path: str,
        mode: str,
        config: dict | None = None,
        doc_name: str | None = None,
    ) -> list[Chunk]:
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

        source_name = doc_name or Path(file_path).stem
        if mode == "qa_pair":
            return self._parse_qa_pair(rows, config, source_name)
        if mode == "document":
            return self._parse_document(rows, config, source_name)
        if mode == "table":
            return self._parse_table(rows, source_name)
        raise ValueError(f"不支持的导入模式: {mode}，支持: qa_pair / document / table")

    def _read_csv(self, file_path: str) -> list[dict]:
        from rag.cleaner import detect_and_decode

        with open(file_path, "rb") as f:
            raw = f.read()
        text = detect_and_decode(raw)
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

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

    def _parse_qa_pair(self, rows: list[dict], config: dict, doc_name: str) -> list[Chunk]:
        q_col = config.get("question_col", "问题")
        a_col = config.get("answer_col", "答案")
        chunks = []
        for i, row in enumerate(rows):
            q = row.get(q_col, "").strip()
            a = row.get(a_col, "").strip()
            if q and a:
                text = f"问：{q}\n答：{a}"
                chunks.append(Chunk(text=text, doc_name=doc_name, chunk_index=i))
        return chunks

    def _parse_document(self, rows: list[dict], config: dict, doc_name: str) -> list[Chunk]:
        col = config.get("content_col", "content")
        chunks = []
        for i, row in enumerate(rows):
            text = row.get(col, "").strip()
            if text:
                chunks.append(Chunk(text=text, doc_name=doc_name, chunk_index=i))
        return chunks

    def _parse_table(self, rows: list[dict], doc_name: str) -> list[Chunk]:
        if not rows:
            return []
        headers = list(rows[0].keys())
        lines = ["\t".join(headers)]
        for row in rows:
            lines.append("\t".join(str(row.get(h, "")) for h in headers))
        text = "\n".join(lines)
        if len(text) > settings.max_chunk_chars:
            logger.warning(
                "表格导入内容超过 %s 字符，已截断: %s",
                settings.max_chunk_chars,
                doc_name,
            )
            text = text[: settings.max_chunk_chars]
        return [Chunk(text=text, doc_name=doc_name, chunk_index=0)]

    def import_file(
        self,
        file_path: str,
        mode: str = "qa_pair",
        kb_id: str = "rag_docs",
        *,
        user: dict,
        user_db,
        config: dict | None = None,
        doc_name: str | None = None,
        duplicate_action: str = "reject",
    ) -> dict:
        """Run every document, vector and file write under its KB admission."""
        if not user or user.get("id") is None:
            raise ValueError("user is required for batch import")
        if user_db is None:
            raise ValueError("user_db is required for batch import")
        from rag.kb_lifecycle import kb_mutation

        with kb_mutation(user_db, kb_id):
            return self._import_file(
                file_path,
                mode,
                kb_id,
                user=user,
                user_db=user_db,
                config=config,
                doc_name=doc_name,
                duplicate_action=duplicate_action,
            )

    def _import_file(
        self,
        file_path: str,
        mode: str = "qa_pair",
        kb_id: str = "rag_docs",
        *,
        user: dict,
        user_db,
        config: dict | None = None,
        doc_name: str | None = None,
        duplicate_action: str = "reject",
    ) -> dict:
        """导入单个文件并对齐文档版本树与向量库。

        ``user``/``user_db`` 必填（B-08）：旧实现缺省会伪造一个管理员身份并
        直连 ``settings.users_db_path``（生产库），CLI 之外的调用可能因此在错误
        的主体或错误的库上写入。覆盖既有文档时还必须通过 ``require_doc_edit``，
        凭证只来自真实登录用户。
        """
        if not user or user.get("id") is None:
            raise ValueError("user is required for batch import")
        if user_db is None:
            raise ValueError("user_db is required for batch import")
        if duplicate_action not in {"reject", "overwrite", "keep"}:
            raise ValueError(f"不支持的 duplicate_action: {duplicate_action}")

        import time
        from dataclasses import replace

        from rag.api_security import require_doc_edit
        from rag.document_sources.upload import UploadDocumentSource
        from rag.document_version_store import (
            final_version_path,
            promote_version,
            stage_version,
        )
        from rag.document_versions import choose_document_name
        from rag.embedder import embed
        from rag.vector_store import (
            _ensure_collection_name,
            add_to_collection,
            delete_doc,
            mark_document_version_current,
        )

        user_id = int(user["id"])

        filename = doc_name or Path(file_path).name
        chunks = self.parse(file_path, mode=mode, config=config, doc_name=filename)
        if not chunks:
            raise ValueError("解析结果为空")

        final_name = choose_document_name(user_db, kb_id, filename, duplicate_action)
        existing = user_db.get_document_permission(final_name, kb_id)
        was_existing = existing is not None
        if was_existing:
            # 覆盖既有文档（或向其追加版本）需要 edit 权限，而不是伪造的 admin。
            require_doc_edit(user_db, final_name, kb_id, user)

        if not was_existing:
            document_id = user_db.create_document_permission(final_name, kb_id, user_id, 1, False)
        else:
            document_id = existing["id"]

        raw_bytes = Path(file_path).read_bytes()
        source = UploadDocumentSource.from_bytes(final_name, raw_bytes)
        versions_root = Path(settings.document_versions_root)
        stage = stage_version(versions_root, source, document_id=document_id)

        version_id = None
        indexed = False
        try:
            version_id = user_db.create_document_version(
                document_id, final_name, str(stage), source.content_hash, user_id
            )
            version_record = user_db.get_document_version_by_id(version_id)
            version_no = version_record["version_no"]

            versioned_chunks = [
                replace(
                    c,
                    document_id=document_id,
                    document_version_id=version_id,
                    version_no=version_no,
                    doc_permission_id=document_id,
                    is_current=True,
                )
                for c in chunks
            ]

            embeddings = embed([c.text for c in versioned_chunks])
            _ensure_collection_name(kb_id)
            add_to_collection(
                kb_id,
                versioned_chunks,
                embeddings,
                doc_permission_id=document_id,
                document_id=document_id,
                document_version_id=version_id,
                version_no=version_no,
                is_current=True,
                updated_at=time.time(),
            )
            mark_document_version_current(kb_id, document_id, version_id)
            indexed = True

            final_path = final_version_path(versions_root, document_id, version_no, final_name)
            promote_version(stage, final_path)
            user_db.set_version_file_path(version_id, str(final_path))
            user_db.activate_document_version(document_id, version_id, kb_id, final_name)
        except Exception:
            if indexed and not was_existing:
                try:
                    delete_doc(kb_id, final_name)
                except Exception:
                    logger.exception("batch import vector rollback failed")
            if version_id is not None:
                try:
                    user_db.delete_document_version(version_id)
                except Exception:
                    logger.exception("batch import version row rollback failed")
            if not was_existing:
                try:
                    user_db.delete_document_permission(document_id)
                except Exception:
                    logger.exception("batch import permission rollback failed")
            raise
        finally:
            stage.unlink(missing_ok=True)

        return {
            "status": "imported",
            "document_id": document_id,
            "version_id": version_id,
            "version_no": version_no,
            "filename": final_name,
            "chunks": len(chunks),
            "mode": mode,
        }

    def process_csv(
        self,
        file_path: str,
        mode: str = "qa_pair",
        kb_id: str = "rag_docs",
        *,
        user: dict,
        user_db,
        config: dict | None = None,
        doc_name: str | None = None,
        duplicate_action: str = "reject",
    ) -> dict:
        """处理 CSV 文件并完成版本化入库。"""
        return self.import_file(
            file_path=file_path,
            mode=mode,
            kb_id=kb_id,
            user=user,
            user_db=user_db,
            config=config,
            doc_name=doc_name,
            duplicate_action=duplicate_action,
        )

    def import_files(
        self,
        file_paths: list[str],
        mode: str = "qa_pair",
        kb_id: str = "rag_docs",
        *,
        user: dict,
        user_db,
        config: dict | None = None,
        duplicate_action: str = "reject",
    ) -> list[dict]:
        """批量导入多个文件并挂载到文档版本树。"""
        results = []
        for path in file_paths:
            res = self.import_file(
                file_path=path,
                mode=mode,
                kb_id=kb_id,
                user=user,
                user_db=user_db,
                config=config,
                duplicate_action=duplicate_action,
            )
            results.append(res)
        return results
