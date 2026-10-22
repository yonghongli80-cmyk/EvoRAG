"""User and authentication repository."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from rag.auth import hash_password, verify_password
from rag.repositories.base import BaseRepository


def validate_password_strength(password: str) -> None:
    """验证密码强度。不符合要求时抛出 ValueError。"""
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    if not any(c.isupper() for c in password):
        raise ValueError("密码需含大写字母")
    if not any(c.islower() for c in password):
        raise ValueError("密码需含小写字母")
    if not any(c.isdigit() for c in password):
        raise ValueError("密码需含数字")


_validate_password_strength = validate_password_strength


class UserRepository(BaseRepository):
    """Handles user accounts, authentication, passwords, and admin privileges."""

    def search_users(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """按用户名搜索用户。返回 [{id, username}]，最多 limit 条。"""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, username FROM users WHERE username LIKE ? ESCAPE '\\' LIMIT ?",
                (f"%{escaped}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def change_password(self, user_id: int, old_password: str, new_password: str) -> None:
        """修改密码。验证旧密码、新密码强度、新旧密码不同。"""
        validate_password_strength(new_password)
        if old_password == new_password:
            raise ValueError("新密码不能与旧密码相同")

        with self.cm.write_connection() as conn:
            row = conn.execute(
                "SELECT id, password FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row:
                raise ValueError("用户不存在")
            if not verify_password(old_password, row["password"]):
                raise ValueError("旧密码错误")
            new_hashed = hash_password(new_password)
            conn.execute(
                "UPDATE users SET password = ?, password_changed_at = ? WHERE id = ?",
                (new_hashed, time.time(), user_id),
            )
            conn.commit()

    def reset_password(self, user_id: int, new_password: str) -> None:
        """重置密码（不验证旧密码）。仅 admin 调用。"""
        validate_password_strength(new_password)

        with self.cm.write_connection() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row:
                raise ValueError("用户不存在")
            new_hashed = hash_password(new_password)
            conn.execute(
                "UPDATE users SET password = ?, password_changed_at = ? WHERE id = ?",
                (new_hashed, time.time(), user_id),
            )
            conn.commit()

    def create_user(self, username: str, password: str) -> int:
        """Create a new user. Raises ``ValueError`` if the username is taken."""
        hashed = hash_password(password)
        with self.cm.write_connection() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users (username, salt, password) VALUES (?, ?, ?)",
                    (username, "", hashed),
                )
                conn.commit()
                return cur.lastrowid  # type: ignore[return-value]
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Username '{username}' already exists") from exc

    def ensure_development_user(self) -> dict[str, Any]:
        """Ensure the auth-disabled principal has a real FK-safe user row."""
        with self.cm.write_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, salt, password, is_admin) "
                "VALUES (0, 'anonymous-development', '', '!disabled!', 1)"
            )
            conn.commit()
        user = self.get_user_by_id(0)
        if user is None:
            raise RuntimeError("failed to create development principal")
        return user

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """Return user dict on success, ``None`` on failure."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, username, password FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        if not verify_password(password, row["password"]):
            return None
        return {"id": row["id"], "username": row["username"]}

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Return user dict or ``None``."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, username, is_admin, password_changed_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
            "password_changed_at": row["password_changed_at"],
        }

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """按用户名查询用户。"""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT id, username, is_admin FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
        }

    def set_user_admin(self, user_id: int, is_admin: bool) -> None:
        """Set or unset administrator flag for a user."""
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (int(is_admin), user_id),
            )
            conn.commit()

    def _is_admin(self, user_id: Any) -> bool:
        """判断 *user_id* 是否具备数据源 admin 权限（可读/删全部数据源）。

        没有主体（``None``）不等于管理员：旧实现在缺省参数下 fail-open，任何
        忘记传 user_id 的调用都会拿到全量数据源（B-03）。现在缺省主体一律拒绝，
        显式 ``"admin"`` 保留为仓库层兼容别名（端点只传真实登录用户的 id）。
        """
        if user_id is None:
            return False
        uid = str(user_id)
        if uid in ("admin", "0"):
            # "admin" is the historical default owner (migrated rows) and "0" is the
            # auth-disabled development principal.
            return True
        if uid.isdigit():
            try:
                with self.cm.read_connection() as conn:
                    row = conn.execute(
                        "SELECT is_admin FROM users WHERE id = ?", (int(uid),)
                    ).fetchone()
                    if row is not None:
                        return bool(row["is_admin"])
            except (TypeError, ValueError, sqlite3.Error):
                return False
        return False
