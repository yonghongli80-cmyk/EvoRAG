"""Database data source supporting SQLite, MySQL, and PostgreSQL."""

from __future__ import annotations

import logging
import re
import asyncio
from urllib.parse import urlparse

from rag.data_sources.base import DataSource, validate_external_host

logger = logging.getLogger(__name__)

_STRIP_SQL_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRIP_SQL_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`")
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|GRANT|REVOKE|"
    r"ATTACH|DETACH|VACUUM|REINDEX|LOAD|MERGE|UPSERT|CALL|EXEC|"
    r"EXECUTE|PREPARE|DEALLOCATE)\b",
    re.IGNORECASE,
)


def validate_read_only_sql(query: str) -> None:
    """校验 SQL 仅包含只读语句（必须以 ``SELECT`` 开头，且不含写关键字）。

    不满足时抛出 ``ValueError``。用于防止数据源配置携带任意写 SQL 被执行
    （``DELETE``/``DROP``/``INSERT``/``UPDATE``/``ALTER`` 等一律拒绝）。
    先剥离 SQL 注释与引号内容再检查，降低误报，同时防多语句注入。
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    no_comments = _STRIP_SQL_COMMENTS.sub(" ", query)
    no_quotes = _STRIP_SQL_QUOTED.sub(" ", no_comments)
    stripped = no_quotes.lstrip()
    first = stripped.split(None, 1)[0] if stripped.split(None, 1) else ""
    if first.upper() != "SELECT":
        raise ValueError("数据源 SQL 仅允许只读查询（必须以 SELECT 开头）")
    if _WRITE_KEYWORDS.search(no_quotes):
        raise ValueError("数据源 SQL 仅允许只读查询，检测到写操作关键字")
    return None


class DBSource(DataSource):
    """Execute a SQL query against a database and return the results as documents.

    Supported connection string formats:
    - ``sqlite:///path/to/db.sqlite``
    - ``mysql://user:pass@host:port/dbname``
    - ``postgresql://user:pass@host:port/dbname``
    """

    def __init__(self, connection_string: str, query: str) -> None:
        if not connection_string:
            raise ValueError("connection_string is required for DBSource")
        if not query:
            raise ValueError("query is required for DBSource")
        self.connection_string = connection_string
        self.query = query
        self._validate_remote_host()

    # -- helpers --------------------------------------------------------------

    def _parse_scheme(self) -> str:
        parsed = urlparse(self.connection_string)
        return parsed.scheme.lower()

    def _get_sqlite_path(self) -> str:
        """Extract the file path from a ``sqlite:///`` connection string."""
        return self.connection_string[len("sqlite://") :]

    def _validate_remote_host(self) -> None:
        parsed = urlparse(self.connection_string)
        if parsed.scheme.lower() != "sqlite" and parsed.hostname:
            validate_external_host(parsed.hostname)

    # -- SQLite ---------------------------------------------------------------

    def _sqlite_test(self) -> bool:
        import sqlite3

        try:
            conn = sqlite3.connect(self._get_sqlite_path())
            conn.execute("SELECT 1")  # 只验证连通性，不执行用户 SQL
            conn.close()
            return True
        except Exception as exc:
            logger.warning("SQLite connection test failed: %s", exc)
            return False

    def _sqlite_fetch(self) -> list[dict]:
        import sqlite3

        validate_read_only_sql(self.query)
        conn = sqlite3.connect(self._get_sqlite_path())
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(self.query)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            conn.close()

    # -- MySQL ----------------------------------------------------------------

    def _mysql_test(self) -> bool:
        try:
            import pymysql  # type: ignore[import-untyped]
        except ImportError:
            logger.error("pymysql is required for MySQL sources")
            return False

        parsed = urlparse(self.connection_string)
        try:
            conn = pymysql.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 3306,
                user=parsed.username or "",
                password=parsed.password or "",
                database=parsed.path.lstrip("/") or None,
            )
            cur = conn.cursor()
            cur.execute("SELECT 1")  # 只验证连通性，不执行用户 SQL
            cur.close()
            conn.close()
            return True
        except Exception as exc:
            logger.warning("MySQL connection test failed: %s", exc)
            return False

    def _mysql_fetch(self) -> list[dict]:
        try:
            import pymysql
        except ImportError:
            raise RuntimeError("pymysql is required for MySQL sources")

        validate_read_only_sql(self.query)
        parsed = urlparse(self.connection_string)
        conn = pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=parsed.username or "",
            password=parsed.password or "",
            database=parsed.path.lstrip("/") or None,
        )
        try:
            cur = conn.cursor()
            cur.execute(self.query)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            conn.close()

    # -- PostgreSQL -----------------------------------------------------------

    def _pg_test(self) -> bool:
        try:
            import psycopg2  # type: ignore[import-untyped]
        except ImportError:
            logger.error("psycopg2 is required for PostgreSQL sources")
            return False

        try:
            conn = psycopg2.connect(self.connection_string)
            cur = conn.cursor()
            cur.execute("SELECT 1")  # 只验证连通性，不执行用户 SQL
            cur.close()
            conn.close()
            return True
        except Exception as exc:
            logger.warning("PostgreSQL connection test failed: %s", exc)
            return False

    def _pg_fetch(self) -> list[dict]:
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError("psycopg2 is required for PostgreSQL sources")

        validate_read_only_sql(self.query)
        conn = psycopg2.connect(self.connection_string)
        try:
            cur = conn.cursor()
            cur.execute(self.query)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            conn.close()

    # -- DataSource interface -------------------------------------------------

    def test_connection(self) -> bool:
        self._validate_remote_host()
        scheme = self._parse_scheme()
        if scheme == "sqlite":
            return self._sqlite_test()
        elif scheme in ("mysql", "mysql+pymysql"):
            return self._mysql_test()
        elif scheme in ("postgresql", "postgres", "psycopg2"):
            return self._pg_test()
        else:
            logger.error("Unsupported database scheme: %s", scheme)
            return False

    async def fetch(self) -> list[dict]:
        self._validate_remote_host()
        scheme = self._parse_scheme()
        if scheme == "sqlite":
            return await asyncio.to_thread(self._sqlite_fetch)
        elif scheme in ("mysql", "mysql+pymysql"):
            return await asyncio.to_thread(self._mysql_fetch)
        elif scheme in ("postgresql", "postgres", "psycopg2"):
            return await asyncio.to_thread(self._pg_fetch)
        else:
            raise RuntimeError(f"Unsupported database scheme: {scheme}")
