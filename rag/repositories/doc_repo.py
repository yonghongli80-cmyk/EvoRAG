"""Document repository handling KB metadata, documents, versions, permissions, and comparisons."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from rag.kb_lifecycle import assert_kb_available_in_tx
from rag.repositories.base import BaseRepository, VersionConflict

if TYPE_CHECKING:
    from rag.db.connection import ConnectionManager
    from rag.reliability.comparison import ComparisonResult
    from rag.user_db import UserDB


class DocRepository(BaseRepository):
    """Repository handling document permissions, versions, KB metadata, and comparisons."""

    _DOCUMENT_PERMISSION_COLUMNS = (
        "id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, "
        "downloadable, current_version_id, source_type, source_key, updated_at, created_at, "
        "logical_path, document_type, tags_json, subject_user_id, metadata_version, "
        "deleted_at, deleted_by, restore_until, container_document_id, container_relative_path, "
        "purge_state"
    )

    def __init__(self, connection_manager: ConnectionManager, db: UserDB | None = None) -> None:
        super().__init__(connection_manager, db)

    # ------------------------------------------------------------------
    # Knowledge Base Metadata
    # ------------------------------------------------------------------

    def create_kb_metadata(
        self, kb_id: str, name: str, owner_id: int = 0, scope: str = "private"
    ) -> None:
        """创建 KB 元数据（仅新 KB 调用，不覆盖已有数据）。"""
        with self.cm.transaction(immediate=True) as conn:
            assert_kb_available_in_tx(conn, kb_id)
            conn.execute(
                "INSERT OR IGNORE INTO kb_metadata "
                "(kb_id, name, owner_id, scope) VALUES (?, ?, ?, ?)",
                (kb_id, name, owner_id, scope),
            )

    def get_kb_metadata(self, kb_id: str) -> dict[str, Any] | None:
        """获取 KB 元数据。不存在返回 None。"""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT kb_id, name, owner_id, scope, created_at FROM kb_metadata WHERE kb_id = ?",
                (kb_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_kb_metadata_by_names(self, kb_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """批量查询 KB 元数据。返回 {kb_id: meta_dict_or_None}。"""
        if not kb_ids:
            return {}
        with self.cm.read_connection() as conn:
            placeholders = ",".join("?" for _ in kb_ids)
            rows = conn.execute(
                f"SELECT kb_id, name, owner_id, scope, created_at FROM kb_metadata WHERE kb_id IN ({placeholders})",
                kb_ids,
            ).fetchall()
        meta_map = {r["kb_id"]: dict(r) for r in rows}
        return {kid: meta_map.get(kid) for kid in kb_ids}

    def update_kb_scope(self, kb_id: str, scope: str) -> None:
        """更新 KB 的 scope 字段。scope 必须是 'private' 或 'public'。

        切换到 private 或 public 时，清除该 KB 的所有 shares（shared 模式才需要 shares）。
        """
        if scope not in ("private", "public"):
            raise ValueError(f"无效的 scope: {scope}，必须是 'private' 或 'public'")
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE kb_metadata SET scope = ? WHERE kb_id = ?",
                (scope, kb_id),
            )
            # 切换到非 shared 模式时清除 shares
            conn.execute(
                "DELETE FROM kb_shares WHERE kb_id = ?",
                (kb_id,),
            )
            conn.commit()

    def update_kb_overview(self, kb_id: str, overview: str) -> bool:
        """Update an existing knowledge base overview through the shared connection."""
        with self.cm.transaction(immediate=True) as conn:
            assert_kb_available_in_tx(conn, kb_id)
            cur = conn.execute(
                "UPDATE kb_metadata SET overview = ? WHERE kb_id = ?",
                (overview, kb_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def share_kb(self, kb_id: str, user_id: int, granted_by: int, permission: str = "view") -> int:
        """共享 KB 给指定用户，并自动将 scope 设为 'shared'。"""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO kb_shares (kb_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (kb_id, user_id, permission, granted_by),
            )
            # 自动将 scope 设为 shared
            conn.execute(
                "UPDATE kb_metadata SET scope = 'shared' WHERE kb_id = ? AND scope != 'shared'",
                (kb_id,),
            )
            conn.commit()
            return int(cur.lastrowid)

    def unshare_kb(self, kb_id: str, user_id: int) -> None:
        """取消 KB 共享，如果没有剩余共享用户则自动将 scope 设回 'private'。"""
        with self.cm.write_connection() as conn:
            conn.execute(
                "DELETE FROM kb_shares WHERE kb_id = ? AND user_id = ?",
                (kb_id, user_id),
            )
            # 检查是否还有其他共享用户
            remaining = conn.execute(
                "SELECT COUNT(*) as cnt FROM kb_shares WHERE kb_id = ?",
                (kb_id,),
            ).fetchone()
            if remaining["cnt"] == 0:
                conn.execute(
                    "UPDATE kb_metadata SET scope = 'private' WHERE kb_id = ? AND scope = 'shared'",
                    (kb_id,),
                )
            conn.commit()

    def is_kb_shared(self, kb_id: str, user_id: int, permission: str | None = None) -> bool:
        """检查知识库是否共享给指定用户。permission=None 表示任意权限，'view' 或 'edit' 表示特定权限。"""
        with self.cm.read_connection() as conn:
            if permission:
                row = conn.execute(
                    "SELECT 1 FROM kb_shares WHERE kb_id = ? AND user_id = ? AND permission = ?",
                    (kb_id, user_id, permission),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM kb_shares WHERE kb_id = ? AND user_id = ?",
                    (kb_id, user_id),
                ).fetchone()
        return row is not None

    def list_kb_shared_users(self, kb_id: str) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT u.id, u.username, ks.permission, ks.granted_by, ks.created_at AS granted_at "
                "FROM kb_shares ks JOIN users u ON ks.user_id = u.id "
                "WHERE ks.kb_id = ? ORDER BY ks.created_at",
                (kb_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # KB Documents (durable source-file lifecycle)
    # ------------------------------------------------------------------

    def create_kb_document_pending(self, kb_id: str, filename: str) -> int:
        with self.cm.transaction(immediate=True) as conn:
            assert_kb_available_in_tx(conn, kb_id)
            cur = conn.execute(
                "INSERT INTO kb_documents (kb_id, filename, status) VALUES (?, ?, 'pending')",
                (kb_id, filename),
            )
            return int(cur.lastrowid)

    def finish_kb_document(
        self, document_id: int, file_path: str, toc: str, summary: str, chunk_count: int
    ) -> None:
        with self.cm.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT kb_id FROM kb_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise VersionConflict("kb document no longer exists")
            assert_kb_available_in_tx(conn, row["kb_id"])
            updated = conn.execute(
                "UPDATE kb_documents SET file_path = ?, toc = ?, summary = ?, "
                "chunk_count = ?, status = 'indexed', last_error = '' WHERE id = ?",
                (file_path, toc, summary, chunk_count, document_id),
            )
            if updated.rowcount != 1:
                raise VersionConflict("kb document no longer exists")

    def set_kb_document_state(
        self, document_id: int, status: str, last_error: str = ""
    ) -> None:
        if status not in {"pending", "indexed", "deleting", "delete_failed"}:
            raise ValueError("invalid kb document status")
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE kb_documents SET status = ?, last_error = ? WHERE id = ?",
                (status, last_error[:500], document_id),
            )
            conn.commit()

    def get_kb_document(self, kb_id: str, filename: str) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, kb_id, filename, file_path, toc, summary, chunk_count, status "
                "FROM kb_documents WHERE kb_id = ? AND filename = ?",
                (kb_id, filename),
            ).fetchone()
        return dict(row) if row else None

    def list_kb_documents(self, kb_id: str) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, kb_id, filename, file_path, toc, summary, chunk_count, status "
                "FROM kb_documents WHERE kb_id = ? ORDER BY id",
                (kb_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_kb_document(self, document_id: int) -> None:
        with self.cm.write_connection() as conn:
            conn.execute("DELETE FROM kb_documents WHERE id = ?", (document_id,))
            conn.commit()

    def update_kb_document_toc(self, kb_id: str, filename: str, toc: str) -> bool:
        with self.cm.transaction(immediate=True) as conn:
            assert_kb_available_in_tx(conn, kb_id)
            cur = conn.execute(
                "UPDATE kb_documents SET toc = ? WHERE kb_id = ? AND filename = ?",
                (toc, kb_id, filename),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_kb_document_summary(self, kb_id: str, filename: str, summary: str) -> bool:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "UPDATE kb_documents SET summary = ? WHERE kb_id = ? AND filename = ?",
                (summary, kb_id, filename),
            )
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Document Permissions & Shares
    # ------------------------------------------------------------------

    def create_document_permission(
        self,
        doc_name: str,
        kb_id: str,
        owner_id: int,
        permission_level: int = 1,
        is_public: bool = False,
        protected: bool = False,
        scope: str | None = None,
    ) -> int:
        """创建文档权限记录。scope 不传时根据 is_public 自动推断。"""
        if scope is None:
            scope = "public" if is_public else "private"
        is_public = scope == "public"
        with self.cm.transaction(immediate=True) as conn:
            assert_kb_available_in_tx(conn, kb_id)
            cur = conn.execute(
                "INSERT INTO document_permissions (doc_name, kb_id, owner_id, permission_level, is_public, protected, scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_name, kb_id, owner_id, permission_level, int(is_public), int(protected), scope),
            )
            return int(cur.lastrowid)

    def get_document_permission(self, doc_name: str, kb_id: str) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                f"SELECT {self._DOCUMENT_PERMISSION_COLUMNS} "
                "FROM document_permissions WHERE doc_name = ? AND kb_id = ?",
                (doc_name, kb_id),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["is_public"] = d.get("scope") == "public"
        d["protected"] = bool(d.get("protected", 0))
        d["downloadable"] = bool(d.get("downloadable", 1))
        return d

    def get_document_permission_by_id(self, doc_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                f"SELECT {self._DOCUMENT_PERMISSION_COLUMNS} "
                "FROM document_permissions WHERE id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["is_public"] = d.get("scope") == "public"
        d["protected"] = bool(d.get("protected", 0))
        d["downloadable"] = bool(d.get("downloadable", 1))
        return d

    def toggle_document_visibility(self, doc_id: int, is_public: bool | None = None) -> bool:
        """切换文档公开/私有状态，返回新的 is_public 值。"""
        with self.cm.write_connection() as conn:
            row = conn.execute(
                "SELECT scope FROM document_permissions WHERE id = ?", (doc_id,)
            ).fetchone()
            if not row:
                return False
            new_val = row["scope"] != "public" if is_public is None else bool(is_public)
            scope = "public" if new_val else "private"
            conn.execute(
                "UPDATE document_permissions SET is_public = ?, scope = ? WHERE id = ?",
                (int(new_val), scope, doc_id),
            )
            conn.commit()
            return new_val

    def delete_document_permission(self, doc_id: int) -> None:
        with self.cm.transaction() as conn:
            conn.execute("DELETE FROM document_permissions WHERE id = ?", (doc_id,))

    def get_accessible_doc_names(self, kb_id: str, user_id: int, user_level: int = 1) -> list[str]:
        """返回用户在指定知识库中有权查看的文档名列表。

        规则：owner / scope='public' / 被共享 的文档可见。
        """
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT dp.doc_name FROM document_permissions dp "
                "WHERE dp.kb_id = ? AND (dp.owner_id = ? OR dp.scope = 'public' "
                "OR (dp.scope = 'shared' AND dp.id IN "
                "(SELECT doc_id FROM document_shares WHERE user_id = ?)))",
                (kb_id, user_id, user_id),
            ).fetchall()
        return [r["doc_name"] for r in rows]

    def get_document_permissions_by_names(
        self, doc_names: list[str], kb_id: str
    ) -> dict[str, dict[str, Any] | None]:
        """批量查询多个文档的权限记录。返回 {doc_name: perm_dict_or_None}。"""
        if not doc_names:
            return {}
        with self.cm.read_connection() as conn:
            placeholders = ",".join("?" for _ in doc_names)
            rows = conn.execute(
                f"SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, created_at "
                f"FROM document_permissions WHERE doc_name IN ({placeholders}) AND kb_id = ?",
                (*doc_names, kb_id),
            ).fetchall()
        perm_map = {}
        for r in rows:
            d = dict(r)
            d["is_public"] = d.get("scope") == "public"
            d["protected"] = bool(d.get("protected", 0))
            perm_map[d["doc_name"]] = d
        return {name: perm_map.get(name) for name in doc_names}

    def get_document_permissions_by_ids(self, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
        """批量查询多个权限记录（按 ID）。返回 {doc_id: perm_dict}。"""
        if not doc_ids:
            return {}
        with self.cm.read_connection() as conn:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = conn.execute(
                f"SELECT id, doc_name, kb_id, owner_id, permission_level, is_public, protected, scope, created_at "
                f"FROM document_permissions WHERE id IN ({placeholders})",
                doc_ids,
            ).fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            d["is_public"] = d.get("scope") == "public"
            d["protected"] = bool(d.get("protected", 0))
            result[d["id"]] = d
        return result

    def share_document(
        self, doc_id: int, user_id: int, granted_by: int, permission: str = "view"
    ) -> int:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO document_shares (doc_id, user_id, granted_by, permission) VALUES (?, ?, ?, ?)",
                (doc_id, user_id, granted_by, permission),
            )
            conn.execute(
                "UPDATE document_permissions SET scope = 'shared', is_public = 0 "
                "WHERE id = ? AND scope = 'private'",
                (doc_id,),
            )
            conn.commit()
            return int(cur.lastrowid)

    def unshare_document(self, doc_id: int, user_id: int) -> None:
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "DELETE FROM document_shares WHERE doc_id = ? AND user_id = ?",
                (doc_id, user_id),
            )
            if cur.rowcount:
                has_remaining_shares = conn.execute(
                    "SELECT 1 FROM document_shares WHERE doc_id = ? LIMIT 1", (doc_id,)
                ).fetchone()
                if not has_remaining_shares:
                    conn.execute(
                        "UPDATE document_permissions SET scope = 'private', is_public = 0 "
                        "WHERE id = ?",
                        (doc_id,),
                    )
            conn.commit()

    def is_document_shared(self, doc_id: int, user_id: int, permission: str | None = None) -> bool:
        """检查文档是否共享给指定用户。permission=None 表示任意权限，'view' 或 'edit' 表示特定权限。"""
        with self.cm.read_connection() as conn:
            if permission:
                row = conn.execute(
                    "SELECT 1 FROM document_shares WHERE doc_id = ? AND user_id = ? AND permission = ?",
                    (doc_id, user_id, permission),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM document_shares WHERE doc_id = ? AND user_id = ?",
                    (doc_id, user_id),
                ).fetchone()
        return row is not None

    def list_shared_users(self, doc_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT u.id, u.username, ds.permission, ds.granted_by, ds.created_at AS granted_at "
                "FROM document_shares ds JOIN users u ON ds.user_id = u.id "
                "WHERE ds.doc_id = ? ORDER BY ds.created_at",
                (doc_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_shared_doc_ids_for_user(self, user_id: int, doc_ids: list[int]) -> set[int]:
        """批量查询用户被共享的 doc_id 集合。返回指定 doc_ids 中被共享的子集。"""
        if not doc_ids:
            return set()
        with self.cm.read_connection() as conn:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = conn.execute(
                f"SELECT doc_id FROM document_shares WHERE user_id = ? AND doc_id IN ({placeholders})",
                (user_id, *doc_ids),
            ).fetchall()
        return {r["doc_id"] for r in rows}

    def list_document_permissions_all(self) -> list[dict[str, Any]]:
        """Return every logical document for startup migration and metrics."""
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, doc_name, kb_id, owner_id, current_version_id "
                "FROM document_permissions ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def foreign_key_check(self) -> list[tuple]:
        with self.cm.read_connection() as conn:
            rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        return [tuple(r) for r in rows]

    # ------------------------------------------------------------------
    # Document Versions & Lifecycle
    # ------------------------------------------------------------------

    def create_document_version(
        self,
        document_id: int,
        original_filename: str,
        file_path: str,
        content_hash: str,
        uploaded_by: int,
    ) -> int:
        """Reserve a staging version row and return its id.

        The row is ``staging`` until ``activate_document_version`` promotes
        it. ``version_no`` is derived per document as max+1.
        """
        with self.cm.transaction(immediate=True) as conn:
            permission = conn.execute(
                "SELECT kb_id FROM document_permissions WHERE id = ?",
                (document_id,),
            ).fetchone()
            if permission is None:
                raise VersionConflict("document permission no longer exists")
            assert_kb_available_in_tx(conn, permission["kb_id"])
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
                "FROM document_versions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            version_no = int(row["next_no"])
            cur = conn.execute(
                "INSERT INTO document_versions "
                "(document_id, version_no, original_filename, file_path, content_hash, uploaded_by, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'staging')",
                (document_id, version_no, original_filename, file_path, content_hash, uploaded_by),
            )
            return int(cur.lastrowid)

    def _activate_document_version_in_tx(
        self, document_id: int, version_id: int, kb_id: str, filename: str
    ) -> None:
        """Promote a staging version inside an already-open transaction.

        This row-mutation primitive never begins, commits, or rolls back a
        transaction; the caller owns exactly one.
        """
        now = time.time()
        conn = self.cm.get_write_connection()
        permission = conn.execute(
            "SELECT kb_id FROM document_permissions WHERE id = ?",
            (document_id,),
        ).fetchone()
        if permission is None:
            raise VersionConflict("document permission no longer exists")
        assert_kb_available_in_tx(conn, permission["kb_id"])
        if permission["kb_id"] != kb_id:
            raise VersionConflict("document knowledge base changed")
        conn.execute(
            "UPDATE document_versions SET status='historical', superseded_at=? "
            "WHERE document_id=? AND status='current'",
            (now, document_id),
        )
        activated = conn.execute(
            "UPDATE document_versions SET status='current', validity_status='valid' "
            "WHERE id=? AND document_id=? AND status='staging'",
            (version_id, document_id),
        )
        if activated.rowcount != 1:
            raise VersionConflict("version is no longer staging")
        conn.execute(
            "UPDATE document_permissions SET current_version_id=?, updated_at=? WHERE id=?",
            (version_id, now, document_id),
        )
        conn.execute(
            "UPDATE kb_documents SET document_id=?, document_version_id=? "
            "WHERE kb_id=? AND filename=?",
            (document_id, version_id, kb_id, filename),
        )

    def activate_document_version(
        self, document_id: int, version_id: int, kb_id: str, filename: str
    ) -> None:
        """Atomically promote a staging version to current."""
        with self.cm.transaction(immediate=True):
            self._activate_document_version_in_tx(document_id, version_id, kb_id, filename)

    def get_current_document_version(self, document_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at, "
                "restored_from_version_id "
                "FROM document_versions WHERE document_id = ? AND status = 'current'",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_document_versions(self, document_id: int) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at, "
                "restored_from_version_id "
                "FROM document_versions WHERE document_id = ? ORDER BY version_no DESC",
                (document_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_version_validity(self, version_id: int, validity_status: str) -> None:
        if validity_status not in {"valid", "reported_outdated", "confirmed_outdated"}:
            raise ValueError(f"invalid validity status: {validity_status}")
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE document_versions SET validity_status = ? WHERE id = ?",
                (validity_status, version_id),
            )
            conn.commit()

    def get_document_version_by_id(self, version_id: int) -> dict[str, Any] | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
                "uploaded_by, status, validity_status, created_at, superseded_at, "
                "restored_from_version_id "
                "FROM document_versions WHERE id = ?",
                (version_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_version_file_path(self, version_id: int, file_path: str) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE document_versions SET file_path = ? WHERE id = ?",
                (file_path, version_id),
            )
            conn.commit()

    def delete_document_version(self, version_id: int) -> None:
        with self.cm.transaction() as conn:
            conn.execute(
                "DELETE FROM document_versions WHERE id = ?", (version_id,)
            )

    def set_version_status(self, version_id: int, status: str) -> None:
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE document_versions SET status = ? WHERE id = ?",
                (status, version_id),
            )
            conn.commit()

    def current_version_id(self, document_id: int) -> int | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT current_version_id FROM document_permissions WHERE id = ?",
                (document_id,),
            ).fetchone()
        return row["current_version_id"] if row else None

    def get_latest_version_id(self, document_id: int) -> int | None:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id FROM document_versions "
                "WHERE document_id = ? ORDER BY version_no DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        return row["id"] if row else None

    def is_document_deleted(self, document_id: int) -> bool:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT deleted_at FROM document_permissions WHERE id = ?",
                (document_id,),
            ).fetchone()
        return bool(row and row["deleted_at"] is not None)

    def list_recycled_documents(self) -> list[dict[str, Any]]:
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, doc_name, kb_id, owner_id, deleted_at, deleted_by, restore_until "
                "FROM document_permissions WHERE deleted_at IS NOT NULL "
                "ORDER BY deleted_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_citation_state(self, document_id: int) -> str:
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT state FROM answer_citations WHERE source_kind = 'document' "
                "AND document_id = ? "
                "ORDER BY CASE state WHEN 'purged_by_retention_policy' THEN 0 ELSE 1 END "
                "LIMIT 1",
                (document_id,),
            ).fetchone()
        return row["state"] if row else "available"

    # ------------------------------------------------------------------
    # Document Comparisons
    # ------------------------------------------------------------------

    def persist_document_comparison(
        self, owner_id: int, request: Any, execution: Any
    ) -> ComparisonResult:
        """Persist comparison output and its independent citations atomically."""
        from rag.reliability.comparison import ComparisonCell, ComparisonResult, ComparisonRow

        snapshot = sorted(
            {
                (citation.document_id, citation.document_version_id)
                for row in execution.rows
                for cell in row.values.values()
                for citation in cell.citations
            }
        )
        with self.cm.transaction(immediate=True) as conn:
            comparison_cur = conn.execute(
                "INSERT INTO document_comparisons "
                "(owner_id, question, dimensions_json, document_snapshot_json, partial, failures_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    owner_id,
                    request.question,
                    json.dumps(request.dimensions),
                    json.dumps(snapshot),
                    int(execution.partial),
                    json.dumps(execution.failures),
                ),
            )
            comparison_id = int(comparison_cur.lastrowid)
            rows = []
            for row in execution.rows:
                values = {}
                for document_id, cell in row.values.items():
                    if cell.status == "supported" and not cell.citations:
                        raise ValueError("supported comparison cell requires citation")
                    if cell.status == "insufficient_evidence" and (cell.text or cell.citations):
                        raise ValueError("insufficient evidence comparison cell must be empty")
                    cell_cur = conn.execute(
                        "INSERT INTO comparison_cells "
                        "(comparison_id, dimension, document_id, status, text) VALUES (?, ?, ?, ?, ?)",
                        (comparison_id, row.dimension, document_id, cell.status, cell.text),
                    )
                    cell_id = int(cell_cur.lastrowid)
                    citation_ids = []
                    for citation in cell.citations:
                        if citation.document_id != document_id:
                            raise ValueError("comparison citation document mismatch")
                        citation_cur = conn.execute(
                            "INSERT INTO comparison_citations "
                            "(cell_id, document_id, document_version_id, chunk_index, section_title, "
                            "text_preview, rank) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                cell_id,
                                citation.document_id,
                                citation.document_version_id,
                                citation.chunk_index,
                                citation.section_title,
                                citation.text_preview,
                                citation.rank,
                            ),
                        )
                        citation_ids.append(int(citation_cur.lastrowid))
                    values[int(document_id)] = ComparisonCell(
                        status=cell.status,
                        text=cell.text,
                        citation_ids=tuple(citation_ids),
                    )
                rows.append(ComparisonRow(dimension=row.dimension, values=values))
        return ComparisonResult(
            comparison_id=comparison_id,
            rows=tuple(rows),
            partial=execution.partial,
            failures=execution.failures,
        )

    def get_document_comparison(
        self, comparison_id: int, actor_id: int, is_admin: bool
    ) -> ComparisonResult | None:
        """Return one stored comparison only to its owner or an administrator."""
        from rag.reliability.comparison import ComparisonCell, ComparisonResult, ComparisonRow

        with self.cm.read_connection() as conn:
            comparison = conn.execute(
                "SELECT id, partial, failures_json FROM document_comparisons "
                "WHERE id = ? AND (? OR owner_id = ?)",
                (comparison_id, int(is_admin), actor_id),
            ).fetchone()
            if comparison is None:
                return None
            cell_rows = conn.execute(
                "SELECT id, dimension, document_id, status, text FROM comparison_cells "
                "WHERE comparison_id = ? ORDER BY id",
                (comparison_id,),
            ).fetchall()
            citations = conn.execute(
                "SELECT id, cell_id FROM comparison_citations WHERE cell_id IN "
                "(SELECT id FROM comparison_cells WHERE comparison_id = ?) ORDER BY id",
                (comparison_id,),
            ).fetchall()
        citation_ids_by_cell: dict[int, list[int]] = {}
        for citation in citations:
            citation_ids_by_cell.setdefault(int(citation["cell_id"]), []).append(int(citation["id"]))
        values_by_dimension: dict[str, dict[int, Any]] = {}
        for cell in cell_rows:
            values_by_dimension.setdefault(cell["dimension"], {})[int(cell["document_id"])] = ComparisonCell(
                status=cell["status"],
                text=cell["text"],
                citation_ids=tuple(citation_ids_by_cell.get(int(cell["id"]), [])),
            )
        return ComparisonResult(
            comparison_id=int(comparison["id"]),
            rows=tuple(
                ComparisonRow(dimension=dimension, values=values)
                for dimension, values in values_by_dimension.items()
            ),
            partial=bool(comparison["partial"]),
            failures=tuple(json.loads(comparison["failures_json"])),
        )

    def get_document_comparison_document_ids(
        self, comparison_id: int, actor_id: int, is_admin: bool
    ) -> tuple[int, ...] | None:
        """Return comparison source IDs only after the same ownership check."""
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.document_id FROM comparison_cells c "
                "JOIN document_comparisons d ON d.id = c.comparison_id "
                "WHERE c.comparison_id = ? AND (? OR d.owner_id = ?) ORDER BY c.document_id",
                (comparison_id, int(is_admin), actor_id),
            ).fetchall()
            exists = conn.execute(
                "SELECT 1 FROM document_comparisons WHERE id = ? AND (? OR owner_id = ?)",
                (comparison_id, int(is_admin), actor_id),
            ).fetchone()
        if exists is None:
            return None
        return tuple(int(row["document_id"]) for row in rows)
