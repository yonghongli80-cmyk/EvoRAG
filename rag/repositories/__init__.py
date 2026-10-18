"""Domain sub-repositories decomposing the UserDB monolithic persistence layer."""

from __future__ import annotations

from rag.repositories.analysis_repo import AnalysisRepository, mask_data_source_config
from rag.repositories.base import (
    BaseRepository,
    VersionConflict,
    canonical_json_text,
    citations_hash,
    serialize_reliability,
)
from rag.repositories.chat_repo import ChatRepository
from rag.repositories.doc_repo import DocRepository
from rag.repositories.material_repo import MaterialRepository
from rag.repositories.schema import (
    create_material_schema,
    create_tables,
    rebuild_document_versions_status,
)
from rag.repositories.spreadsheet_repo import SpreadsheetRepository
from rag.repositories.user_repo import UserRepository, _validate_password_strength

__all__ = [
    "AnalysisRepository",
    "BaseRepository",
    "ChatRepository",
    "DocRepository",
    "MaterialRepository",
    "SpreadsheetRepository",
    "UserRepository",
    "VersionConflict",
    "_validate_password_strength",
    "canonical_json_text",
    "citations_hash",
    "create_material_schema",
    "create_tables",
    "mask_data_source_config",
    "rebuild_document_versions_status",
    "serialize_reliability",
]
