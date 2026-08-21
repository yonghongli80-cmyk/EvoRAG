"""TDD tests for rag.data_sources – abstract base, RSS, DB, and API sources."""

import asyncio
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    """Run an async coroutine in a new event loop (for sync test functions)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Base class tests ────────────────────────────────────────────────────────


class TestDataSourceBase:
    """The abstract DataSource base class."""

    def test_cannot_instantiate_directly(self):
        from rag.data_sources.base import DataSource

        with pytest.raises(TypeError):
            DataSource()

    def test_subclass_must_implement_fetch(self):
        """A subclass that omits fetch() should raise TypeError on instantiation."""
        from rag.data_sources.base import DataSource

        class BadSource(DataSource):
            def test_connection(self):
                return True

        with pytest.raises(TypeError):
            BadSource()

    def test_subclass_must_implement_test_connection(self):
        """A subclass that omits test_connection() should raise TypeError on instantiation."""
        from rag.data_sources.base import DataSource

        class BadSource(DataSource):
            async def fetch(self):
                return []

        with pytest.raises(TypeError):
            BadSource()

    def test_concrete_subclass_can_be_instantiated(self):
        from rag.data_sources.base import DataSource

        class GoodSource(DataSource):
            async def fetch(self):
                return []

            def test_connection(self):
                return True

        src = GoodSource()
        assert src is not None

    def test_fetch_returns_list_of_dicts(self):
        from rag.data_sources.base import DataSource

        class FakeSource(DataSource):
            async def fetch(self):
                return [
                    {"title": "T", "content": "C", "url": "http://x", "published_at": "2025-01-01"}
                ]

            def test_connection(self):
                return True

        result = _run(FakeSource().fetch())
        assert isinstance(result, list)
        assert len(result) == 1
        assert "title" in result[0]
        assert "content" in result[0]


# ── RSS source tests ────────────────────────────────────────────────────────


class TestRSSSource:
    """RSS/Atom data source backed by feedparser."""

    def test_import(self):
        from rag.data_sources.rss_source import RSSSource

        assert RSSSource is not None

    def test_requires_url(self):
        from rag.data_sources.rss_source import RSSSource

        with pytest.raises(ValueError, match="url"):
            RSSSource(url="")

    def test_connection_success(self):
        from rag.data_sources.rss_source import RSSSource

        src = RSSSource(url="https://example.com/feed.xml")
        with patch("rag.data_sources.rss_source.feedparser.parse") as mock_parse:
            mock_feed = MagicMock()
            mock_feed.bozo = False
            mock_feed.entries = []
            mock_parse.return_value = mock_feed
            assert src.test_connection() is True

    def test_connection_failure(self):
        from rag.data_sources.rss_source import RSSSource

        src = RSSSource(url="https://example.com/feed.xml")
        with patch("rag.data_sources.rss_source.feedparser.parse") as mock_parse:
            mock_feed = MagicMock()
            mock_feed.bozo = True
            mock_feed.bozo_exception = Exception("network error")
            mock_parse.return_value = mock_feed
            assert src.test_connection() is False

    def test_fetch_rss_entries(self):
        from rag.data_sources.rss_source import RSSSource

        src = RSSSource(url="https://example.com/feed.xml")
        with patch("rag.data_sources.rss_source.feedparser.parse") as mock_parse:
            entry1 = MagicMock()
            entry1.title = "Article 1"
            entry1.summary = "Summary of article 1"
            entry1.link = "https://example.com/1"
            entry1.get.return_value = "2025-06-01T00:00:00Z"

            entry2 = MagicMock()
            entry2.title = "Article 2"
            entry2.summary = "Summary of article 2"
            entry2.link = "https://example.com/2"
            entry2.get.return_value = "2025-06-02T00:00:00Z"

            mock_feed = MagicMock()
            mock_feed.bozo = False
            mock_feed.entries = [entry1, entry2]
            mock_parse.return_value = mock_feed

            results = _run(src.fetch())
            assert len(results) == 2
            assert results[0]["title"] == "Article 1"
            assert results[0]["content"] == "Summary of article 1"
            assert results[0]["url"] == "https://example.com/1"
            assert "published_at" in results[0]

    def test_fetch_atom_entries(self):
        """Atom feeds should also work."""
        from rag.data_sources.rss_source import RSSSource

        src = RSSSource(url="https://example.com/atom.xml")
        with patch("rag.data_sources.rss_source.feedparser.parse") as mock_parse:
            entry = MagicMock()
            entry.title = "Atom Entry"
            entry.summary = "Atom content"
            entry.link = "https://example.com/a1"
            entry.get.return_value = "2025-06-03T00:00:00Z"

            mock_feed = MagicMock()
            mock_feed.bozo = False
            mock_feed.entries = [entry]
            mock_parse.return_value = mock_feed

            results = _run(src.fetch())
            assert len(results) == 1
            assert results[0]["title"] == "Atom Entry"


# ── DB source tests ─────────────────────────────────────────────────────────


class TestDBSource:
    """Database data source supporting SQLite, MySQL, PostgreSQL."""

    def test_import(self):
        from rag.data_sources.db_source import DBSource

        assert DBSource is not None

    def test_requires_connection_string(self):
        from rag.data_sources.db_source import DBSource

        with pytest.raises(ValueError, match="connection_string"):
            DBSource(connection_string="", query="SELECT 1")

    def test_requires_query(self):
        from rag.data_sources.db_source import DBSource

        with pytest.raises(ValueError, match="query"):
            DBSource(connection_string="sqlite:///test.db", query="")

    def test_sqlite_connection_success(self):
        """Test connection to a real SQLite in-memory database."""
        import os
        import tempfile

        from rag.data_sources.db_source import DBSource

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_name = tmp.name
        try:
            conn = sqlite3.connect(tmp_name)
            conn.execute("CREATE TABLE test (id INTEGER, name TEXT)")
            conn.commit()
            conn.close()

            src = DBSource(connection_string=f"sqlite:///{tmp_name}", query="SELECT * FROM test")
            assert src.test_connection() is True
        finally:
            os.unlink(tmp_name)

    def test_sqlite_connection_failure(self):
        from rag.data_sources.db_source import DBSource

        src = DBSource(connection_string="sqlite:///nonexistent_xyz.db", query="SELECT 1")
        assert src.test_connection() is False

    def test_fetch_sqlite(self):
        import os
        import tempfile

        from rag.data_sources.db_source import DBSource

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_name = tmp.name
        try:
            conn = sqlite3.connect(tmp_name)
            conn.execute("CREATE TABLE articles (title TEXT, content TEXT, url TEXT, published_at TEXT)")
            conn.execute("INSERT INTO articles VALUES ('Hello', 'World', 'http://example.com', '2025-01-01')")
            conn.execute("INSERT INTO articles VALUES ('Hi', 'Earth', 'http://example.org', '2025-01-02')")
            conn.commit()
            conn.close()

            src = DBSource(
                connection_string=f"sqlite:///{tmp_name}",
                query="SELECT title, content, url, published_at FROM articles",
            )
            results = _run(src.fetch())
            assert len(results) == 2
            assert results[0]["title"] == "Hello"
            assert results[0]["content"] == "World"
            assert results[1]["title"] == "Hi"
        finally:
            os.unlink(tmp_name)

    def test_fetch_empty_result(self):
        import os
        import tempfile

        from rag.data_sources.db_source import DBSource

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_name = tmp.name
        try:
            conn = sqlite3.connect(tmp_name)
            conn.execute("CREATE TABLE empty_table (id INTEGER)")
            conn.commit()
            conn.close()

            src = DBSource(
                connection_string=f"sqlite:///{tmp_name}",
                query="SELECT * FROM empty_table",
            )
            results = _run(src.fetch())
            assert results == []
        finally:
            os.unlink(tmp_name)


# ── API source tests ────────────────────────────────────────────────────────


class TestAPISource:
    """Generic REST API data source."""

    def test_import(self):
        from rag.data_sources.api_source import APISource

        assert APISource is not None

    def test_requires_url(self):
        from rag.data_sources.api_source import APISource

        with pytest.raises(ValueError, match="url"):
            APISource(url="")

    def test_connection_success(self):
        from rag.data_sources.api_source import APISource

        src = APISource(url="https://example.com/data")
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch("rag.data_sources.api_source.requests.get", return_value=mock_response):
            assert src.test_connection() is True

    def test_connection_failure(self):
        from rag.data_sources.api_source import APISource

        src = APISource(url="https://example.com/data")
        with patch("rag.data_sources.api_source.requests.get", side_effect=Exception("Network error")):
            assert src.test_connection() is False

    def test_fetch_json_array(self):
        from rag.data_sources.api_source import APISource

        src = APISource(
            url="https://example.com/articles",
            headers={"Authorization": "Bearer token123"},
            title_field="name",
            content_field="body",
            url_field="link",
            published_at_field="date",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"name": "Post 1", "body": "Content 1", "link": "http://a.com/1", "date": "2025-01-01"},
            {"name": "Post 2", "body": "Content 2", "link": "http://a.com/2", "date": "2025-01-02"},
        ]
        with patch("rag.data_sources.api_source.requests.get", return_value=mock_response):
            results = _run(src.fetch())
            assert len(results) == 2
            assert results[0]["title"] == "Post 1"
            assert results[0]["content"] == "Content 1"
            assert results[0]["url"] == "http://a.com/1"
            assert results[0]["published_at"] == "2025-01-01"

    def test_fetch_json_with_nested_path(self):
        """Support dot-notation for nested JSON fields like 'data.items'."""
        from rag.data_sources.api_source import APISource

        src = APISource(
            url="https://example.com/articles",
            items_path="data.items",
            title_field="title",
            content_field="desc",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "items": [
                    {"title": "A", "desc": "B"},
                ]
            }
        }
        with patch("rag.data_sources.api_source.requests.get", return_value=mock_response):
            results = _run(src.fetch())
            assert len(results) == 1
            assert results[0]["title"] == "A"

    def test_fetch_empty_response(self):
        from rag.data_sources.api_source import APISource

        src = APISource(url="https://example.com/empty")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        with patch("rag.data_sources.api_source.requests.get", return_value=mock_response):
            results = _run(src.fetch())
            assert results == []

    def test_fetch_with_auth_header(self):
        """Verify custom headers are passed through."""
        from rag.data_sources.api_source import APISource

        src = APISource(
            url="https://example.com/data",
            headers={"X-Custom": "value"},
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        with patch("rag.data_sources.api_source.requests.get") as mock_get:
            mock_get.return_value = mock_response
            _run(src.fetch())
            mock_get.assert_called_once_with(
                "https://example.com/data",
                headers={"X-Custom": "value"},
                timeout=5,
            )
