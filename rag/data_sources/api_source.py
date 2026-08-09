"""Generic REST API data source."""

from __future__ import annotations

import logging
import asyncio
from typing import Any

import requests

from rag.data_sources.base import DataSource, validate_external_url

logger = logging.getLogger(__name__)


class APISource(DataSource):
    """Fetch documents from a REST API endpoint.

    Parameters
    ----------
    url:
        The API endpoint URL.
    headers:
        Optional HTTP headers (e.g. for authentication).
    items_path:
        Dot-notation path to the items array inside the JSON response
        (e.g. ``"data.items"``).  When *None*, the top-level value is used
        (must be a list).
    title_field:
        JSON key to map to ``title``.
    content_field:
        JSON key to map to ``content``.
    url_field:
        JSON key to map to ``url``.
    published_at_field:
        JSON key to map to ``published_at``.
    timeout:
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        items_path: str | None = None,
        title_field: str = "title",
        content_field: str = "content",
        url_field: str = "url",
        published_at_field: str = "published_at",
        timeout: int = 30,
    ) -> None:
        if not url:
            raise ValueError("url is required for APISource")
        validate_external_url(url)
        self.url = url
        self.headers = headers or {}
        self.items_path = items_path
        self.title_field = title_field
        self.content_field = content_field
        self.url_field = url_field
        self.published_at_field = published_at_field
        self.timeout = timeout

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _resolve_path(data: Any, path: str) -> Any:
        """Walk a dot-separated path into nested dicts/lists."""
        parts = path.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def _extract_items(self, data: Any) -> list[dict]:
        """Given the parsed JSON, return the list of item dicts."""
        if self.items_path:
            items = self._resolve_path(data, self.items_path)
            if items is None:
                return []
            return list(items) if isinstance(items, list) else []
        if isinstance(data, list):
            return data
        return []

    # -- DataSource interface -------------------------------------------------

    def test_connection(self) -> bool:
        try:
            validate_external_url(self.url)
            resp = requests.get(self.url, headers=self.headers, timeout=self.timeout)
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("API connection test failed for %s: %s", self.url, exc)
            return False

    async def fetch(self) -> list[dict]:
        validate_external_url(self.url)
        resp = await asyncio.to_thread(
            requests.get, self.url, headers=self.headers, timeout=min(self.timeout, 5)
        )
        resp.raise_for_status()
        data = resp.json()
        items = self._extract_items(data)

        results: list[dict] = []
        for item in items:
            results.append(
                {
                    "title": item.get(self.title_field, ""),
                    "content": item.get(self.content_field, ""),
                    "url": item.get(self.url_field, ""),
                    "published_at": item.get(self.published_at_field, ""),
                }
            )
        return results
