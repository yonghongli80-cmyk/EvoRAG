from unittest.mock import patch

import pytest

from rag.data_sources.api_source import APISource
from rag.data_sources.db_source import DBSource, validate_read_only_sql
from rag.data_sources.rss_source import RSSSource


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/internal", "https://169.254.169.254/latest/meta-data"],
)
def test_http_data_sources_reject_internal_ip_urls(url):
    with pytest.raises(ValueError, match="内网"):
        APISource(url)
    with pytest.raises(ValueError, match="内网"):
        RSSSource(url)


def test_http_data_sources_reject_hostnames_resolving_to_private_ips():
    with patch(
        "rag.data_sources.base.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("127.0.0.1", 0))],
    ), pytest.raises(ValueError, match="内网"):
        APISource("https://internal.example.test/data")


def test_database_source_rejects_private_remote_host():
    with pytest.raises(ValueError, match="内网"):
        DBSource("postgresql://user:password@127.0.0.1:5432/documents", "SELECT 1")


def test_database_source_allows_select_and_rejects_writes():
    assert validate_read_only_sql("SELECT title FROM documents") is None
    with pytest.raises(ValueError, match="只读"):
        validate_read_only_sql("DELETE FROM documents")


def test_http_data_sources_reject_redirects_to_internal_ip():
    from unittest.mock import MagicMock

    import requests

    from rag.data_sources.base import safe_requests_get

    redirect_resp = MagicMock(spec=requests.Response)
    redirect_resp.is_redirect = True
    redirect_resp.headers = {"Location": "http://127.0.0.1:8000/internal"}
    redirect_resp.status_code = 302

    with (
        patch("rag.data_sources.base.requests.get", return_value=redirect_resp),
        patch("rag.data_sources.base.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))]),
        pytest.raises(ValueError, match="内网"),
    ):
        safe_requests_get("http://public.example.com/api")


def test_database_source_rejects_sensitive_sqlite_paths():
    with pytest.raises(ValueError, match="禁止访问系统数据库"):
        DBSource("sqlite:///data/users.db", "SELECT * FROM users")
    with pytest.raises(ValueError, match="禁止访问系统数据库"):
        DBSource("sqlite:///data/jwt_secret.txt", "SELECT 1")
    with pytest.raises(ValueError, match="禁止访问系统数据库"):
        DBSource("sqlite:///../secret.db", "SELECT 1")

