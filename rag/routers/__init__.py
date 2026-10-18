"""FastAPI modular routers for RAGv3."""

from rag.routers.analysis import router as analysis_router
from rag.routers.auth import router as auth_router
from rag.routers.documents import router as documents_router
from rag.routers.knowledge import router as knowledge_router
from rag.routers.query import router as query_router
from rag.routers.sources import router as sources_router
from rag.routers.spreadsheets import router as spreadsheets_router
from rag.routers.system import router as system_router

__all__ = [
    "analysis_router",
    "auth_router",
    "documents_router",
    "knowledge_router",
    "query_router",
    "sources_router",
    "spreadsheets_router",
    "system_router",
]
