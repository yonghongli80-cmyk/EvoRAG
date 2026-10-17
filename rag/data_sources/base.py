"""Abstract base class for all data sources."""

import ipaddress
import socket
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlparse

import requests


def validate_external_host(hostname: str) -> None:
    """Reject hosts which resolve to a local or non-public address."""
    if not hostname:
        raise ValueError("URL 必须包含主机名")

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError as exc:
        if hostname.lower() == "localhost":
            raise ValueError("不允许访问内网或保留地址") from exc
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


def safe_requests_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int | float = 10,
    max_redirects: int = 3,
    **kwargs,
) -> requests.Response:
    """Perform an HTTP GET request with SSRF redirect protection.

    Validates initial URL and all subsequent redirect locations against internal/private IP ranges.
    """
    current_url = url
    redirect_count = 0

    while True:
        validate_external_url(current_url)
        resp = requests.get(
            current_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            **kwargs,
        )
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            redirect_count += 1
            if redirect_count > max_redirects:
                raise ValueError(f"重定向次数过多（超过 {max_redirects} 次）")
            location = resp.headers.get("Location")
            if not location:
                return resp
            next_url = urljoin(current_url, location)
            validate_external_url(next_url)
            current_url = next_url
            continue
        return resp


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
