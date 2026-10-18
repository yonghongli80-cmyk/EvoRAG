"""Analysis cards, questions, summaries, and data sources repository."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, cast
from urllib.parse import urlsplit as _urlsplit
from urllib.parse import urlunsplit as _urlunsplit

from rag.repositories.base import BaseRepository

# Shared redaction primitives. The *policy* consuming them (which keys count as
# secrets, and how a secret field is presented) is owned by ``rag.api_security``;
# the primitives stay here because ``rag.user_db`` re-exports them while
# ``rag.api_security`` imports ``rag.user_db``, so defining them in the security
# module would be circular.
_SENSITIVE_CONFIG_HINTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "authorization",
    "credential",
    "signature",
)

_QUERY_SECRET_PARAMS = (
    "password",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "secret",
    "signature",
    "key",
    "credential",
)


def _mask_url_credentials(value: str) -> tuple[str, bool]:
    """脱敏任意 URL 中的凭据：userinfo（``user:pass@host``）与敏感 query 参数。"""
    if not isinstance(value, str) or "://" not in value:
        return value, False
    redacted = False
    try:
        parts = _urlsplit(value)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            if ":" in userinfo:
                user = userinfo.split(":", 1)[0]
                netloc = f"{user}:***@{host}"
            else:
                netloc = f"***@{host}"
            redacted = True
        query = parts.query
        if query:
            pairs = []
            for pair in query.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if v and any(s in k.lower() for s in _QUERY_SECRET_PARAMS):
                        pairs.append(f"{k}=***")
                        redacted = True
                    else:
                        pairs.append(pair)
                else:
                    pairs.append(pair)
            query = "&".join(pairs)
        return (
            _urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)),
            redacted,
        )
    except Exception:
        return value, True


def mask_data_source_config(config: dict) -> tuple[dict, bool]:
    """返回 ``(脱敏后的 config, has_secret)``。

    脱敏语义只有一处权威实现：:func:`rag.api_security.mask_source_config`。密钥类字段
    （``connection_string``、``dsn`` 等 DSN 也在内）整字段替换为 ``[REDACTED]`` —— 用户名、
    主机与库名同样属于凭据面；非密钥字段里夹带的 URL 才使用 :func:`_mask_url_credentials`
    掩掉 userinfo 与敏感 query 参数。本函数只是"名字 + 返回形状"的适配层，``/sources``
    路由与 ``UserDB`` 都经由它取用同一套语义，不保留第二份会漂移的实现。

    这里延迟导入以避免循环：``rag.api_security`` → ``rag.context`` → ``rag.user_db``
    → ``rag.repositories.analysis_repo``（即本模块）。
    """
    from rag.api_security import mask_source_config

    masked, has_secret = mask_source_config(config)
    return cast(dict, masked), has_secret


class AnalysisRepository(BaseRepository):
    """Handles data sources, analysis cards, summaries, and questions."""

    def _is_admin(self, user_id: Any) -> bool:
        if self.db is not None:
            return self.db._is_admin(user_id)
        if user_id is None:
            return True
        uid = str(user_id)
        if uid in ("admin", "0"):
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

    def create_data_source(
        self,
        name: str,
        source_type: str,
        config: str,
        owner_id: Any = "admin",
    ) -> int:
        """Create a data source and return its id."""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO data_sources (name, type, config, owner_id) VALUES (?, ?, ?, ?)",
                (name, source_type, config, str(owner_id)),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_data_sources(self, user_id: Any = None) -> list[dict[str, Any]]:
        """Return data sources visible to *user_id*, newest first."""
        with self.cm.read_connection() as conn:
            if self._is_admin(user_id):
                rows = conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources WHERE owner_id = ? ORDER BY created_at DESC",
                    (str(user_id),),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            raw = item.pop("config")
            try:
                cfg = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                cfg = {}
            masked, has_secret = mask_data_source_config(cfg)
            item["config"] = masked
            item["has_secret"] = has_secret
            result.append(item)
        return result

    def get_data_source(self, source_id: int, user_id: Any = None) -> dict[str, Any] | None:
        """Return a single data source by id, or None."""
        with self.cm.read_connection() as conn:
            if self._is_admin(user_id):
                row = conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, name, type, config, status, last_synced_at, created_at, owner_id "
                    "FROM data_sources WHERE id = ? AND owner_id = ?",
                    (source_id, str(user_id)),
                ).fetchone()
        return dict(row) if row else None

    def delete_data_source(self, source_id: int, user_id: Any = None) -> bool:
        """Delete a data source. Returns True if deleted."""
        with self.cm.write_connection() as conn:
            if self._is_admin(user_id):
                cur = conn.execute(
                    "DELETE FROM data_sources WHERE id = ?",
                    (source_id,),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM data_sources WHERE id = ? AND owner_id = ?",
                    (source_id, str(user_id)),
                )
            conn.commit()
            return cur.rowcount > 0

    def update_data_source_synced(self, source_id: int) -> None:
        """Update the last_synced_at timestamp to now."""
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE data_sources SET last_synced_at = ? WHERE id = ?",
                (time.time(), source_id),
            )
            conn.commit()

    def update_data_source_status(self, source_id: int, status: str) -> None:
        """Update the status of a data source."""
        with self.cm.write_connection() as conn:
            conn.execute(
                "UPDATE data_sources SET status = ? WHERE id = ?",
                (status, source_id),
            )
            conn.commit()

    def create_card(self, user_id: int | str, name: str) -> int:
        """Create an analysis card and return its id."""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "INSERT INTO analysis_cards (name, user_id) VALUES (?, ?)",
                (name, int(user_id)),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_cards(self, user_id: int | str) -> list[dict[str, Any]]:
        """Return all analysis cards for *user_id*, newest first."""
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, name, user_id, created_at FROM analysis_cards "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (int(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_card(self, card_id: int, user_id: int | str) -> bool:
        """Delete an analysis card and its questions. Returns True if deleted."""
        uid = int(user_id)
        with self.cm.write_connection() as conn:
            conn.execute(
                "DELETE FROM analysis_questions WHERE card_id = ? "
                "AND card_id IN (SELECT id FROM analysis_cards WHERE id = ? AND user_id = ?)",
                (card_id, card_id, uid),
            )
            cur = conn.execute(
                "DELETE FROM analysis_cards WHERE id = ? AND user_id = ?",
                (card_id, uid),
            )
            conn.commit()
            return cur.rowcount > 0

    def rename_card(self, card_id: int, name: str, user_id: int | str) -> bool:
        """Rename an analysis card. Returns True if updated."""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "UPDATE analysis_cards SET name = ? WHERE id = ? AND user_id = ?",
                (name, card_id, int(user_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_card_summary(self, card_id: int) -> str:
        """Return the summary text for a card, empty string if none."""
        with self.cm.read_connection() as conn:
            row = conn.execute(
                "SELECT summary FROM analysis_cards WHERE id = ?",
                (card_id,),
            ).fetchone()
        return row["summary"] if row else ""

    def update_card_summary(self, card_id: int, summary: str, user_id: int | str) -> bool:
        """Update the summary for a card. Returns True if updated."""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "UPDATE analysis_cards SET summary = ? WHERE id = ? AND user_id = ?",
                (summary, card_id, int(user_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def add_question(
        self,
        card_id: int,
        question: str,
        answer: str = "",
        source_mode: str = "",
        source_message_id: int | None = None,
        user_id: int | str | None = None,
    ) -> int | None:
        """Add a question to a card and return its id."""
        with self.cm.write_connection() as conn:
            if user_id is not None:
                row = conn.execute(
                    "SELECT 1 FROM analysis_cards WHERE id = ? AND user_id = ?",
                    (card_id, int(user_id)),
                ).fetchone()
                if row is None:
                    return None
            cur = conn.execute(
                "INSERT INTO analysis_questions "
                "(card_id, question, answer, source_mode, source_message_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (card_id, question, answer, source_mode, source_message_id),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_questions(self, card_id: int) -> list[dict[str, Any]]:
        """Return all questions for a card, ordered by created_at."""
        with self.cm.read_connection() as conn:
            rows = conn.execute(
                "SELECT id, card_id, question, answer, source_mode, "
                "source_message_id, created_at "
                "FROM analysis_questions WHERE card_id = ? ORDER BY created_at",
                (card_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_question(self, question_id: int, user_id: int | str) -> bool:
        """Delete a question. Returns True if deleted."""
        with self.cm.write_connection() as conn:
            cur = conn.execute(
                "DELETE FROM analysis_questions WHERE id = ? "
                "AND card_id IN (SELECT id FROM analysis_cards WHERE user_id = ?)",
                (question_id, int(user_id)),
            )
            conn.commit()
            return cur.rowcount > 0
