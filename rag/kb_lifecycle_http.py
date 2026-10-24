"""HTTP translation for KB lifecycle domain errors."""

from __future__ import annotations

from fastapi import HTTPException

from rag.kb_lifecycle import (
    KBBusy,
    KBDeleted,
    KBDeleteIncomplete,
    KBDeleting,
    KBLifecycleError,
    KBUnknown,
)


def lifecycle_http_error(error: KBLifecycleError) -> HTTPException:
    """Translate lifecycle errors to stable status, detail, and machine-code header."""
    if isinstance(error, (KBBusy, KBDeleting)):
        status_code = 409
    elif isinstance(error, (KBDeleted, KBUnknown)):
        status_code = 404
    elif isinstance(error, KBDeleteIncomplete):
        status_code = 503
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail=str(error),
        headers={"X-KB-Lifecycle-Error": error.code},
    )


__all__ = ["lifecycle_http_error"]
