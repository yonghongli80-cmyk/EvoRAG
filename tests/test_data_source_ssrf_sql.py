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
