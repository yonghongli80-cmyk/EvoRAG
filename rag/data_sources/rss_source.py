"""RSS / Atom data source powered by feedparser."""

from __future__ import annotations

import logging
import asyncio

import feedparser

from rag.data_sources.base import DataSource, validate_external_url

logger = logging.getLogger(__name__)


class RSSSource(DataSource):
    """Fetch articles from an RSS 2.0 or Atom feed."""

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("url is required for RSSSource")
        validate_external_url(url)
        self.url = url

    # -- DataSource interface -------------------------------------------------

    def test_connection(self) -> bool:
        """Return True if the feed can be fetched without errors."""
        try:
            validate_external_url(self.url)
            feed = feedparser.parse(self.url)
            return not feed.bozo
        except Exception as exc:
            logger.warning("RSS connection test failed for %s: %s", self.url, exc)
            return False

    async def fetch(self) -> list[dict]:
        """Parse the feed and return a list of articles."""
        validate_external_url(self.url)
        feed = await asyncio.to_thread(feedparser.parse, self.url)
        if feed.bozo:
            logger.warning("Feed parse warning for %s: %s", self.url, feed.bozo_exception)
        results: list[dict] = []
        for entry in feed.entries:
            title = entry.title or ""
            content = entry.summary or ""
            url = entry.link or ""
            published_at = entry.get("published", "") or entry.get("updated", "") or ""
            results.append(
                {
                    "title": title,
                    "content": content,
                    "url": url,
                    "published_at": published_at,
                }
            )
        return results
