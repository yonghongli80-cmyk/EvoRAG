"""Abstract base class for all data sources."""

from abc import ABC, abstractmethod
import ipaddress
import socket
from urllib.parse import urlparse


def validate_external_host(hostname: str) -> None:
    """Reject hosts which resolve to a local or non-public address."""
    if not hostname:
        raise ValueError("URL 必须包含主机名")

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        if hostname.lower() == "localhost":
            raise ValueError("不允许访问内网或保留地址")
        try:
            resolved = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("URL 主机名无法解析") from exc
        addresses = [ipaddress.ip_address(item[4][0]) for item in resolved]

    if any(not address.is_global for address in addresses):
        raise ValueError("不允许访问内网或保留地址")


def validate_external_url(url: str) -> None:
    """Validate an HTTP(S) URL before a datasource performs a network request."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("数据源 URL 仅允许 http 或 https 协议")
    if not parsed.hostname:
        raise ValueError("URL 必须包含主机名")
    validate_external_host(parsed.hostname)


class DataSource(ABC):
    """Base class that every data source must implement.

    Subclasses must provide:
    - ``async fetch()``  -- retrieve documents from the source.
    - ``test_connection()`` -- check whether the source is reachable.
    """

    @abstractmethod
    async def fetch(self) -> list[dict]:
        """Fetch data from the source.

        Returns a list of dicts, each containing at least:
        ``title``, ``content``, ``url``, ``published_at``.
        """
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        """Return *True* if the source is reachable and configuration is valid."""
        ...
