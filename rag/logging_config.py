"""Centralized logging configuration for RAGv3."""

import logging
import sys
from contextvars import ContextVar

# Request ID context variable
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    """Get the current request ID from context."""
    return _request_id.get()


def set_request_id(request_id: str) -> None:
    """Set the current request ID in context."""
    _request_id.set(request_id)


class RequestIdFilter(logging.Filter):
    """Inject request_id into log records."""

    def filter(self, record):
        record.request_id = get_request_id()
        return True


def setup_logging(level: str = None, json_format: bool = False) -> None:
    """Configure root logger.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Default from RAG_LOG_LEVEL, fallback INFO.
        json_format: If True, output JSON lines. Default from RAG_LOG_JSON.
    """
    from config import settings

    level = level or settings.log_level or "INFO"
    json_format = json_format or settings.log_json

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())

    if json_format:
        import json as _json

        class JsonFormatter(logging.Formatter):
            def format(self, record):
                return _json.dumps(
                    {
                        "time": self.formatTime(record),
                        "level": record.levelname,
                        "logger": record.name,
                        "request_id": getattr(record, "request_id", "-"),
                        "message": record.getMessage(),
                    },
                    ensure_ascii=False,
                )

        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(request_id)s | %(name)s | %(message)s")
        )

    root.addHandler(handler)
