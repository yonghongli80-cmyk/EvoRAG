"""Incremental material lifecycle — public interfaces.

Extends the trusted knowledge loop: one logical document/ACL identity,
immutable ``document_versions``, and SQLite current-version pointers as the
single source of truth.
"""

from rag.materials.archives import (
    ArchiveImportResult,
    ArchiveLimits,
    ArchiveManifest,
    ArchiveManifestPreview,
    ArchiveService,
    confirm_archive_import,
    inspect_archive,
)
from rag.materials.formats import FormatCapabilityRegistry, build_default_format_registry
from rag.materials.models import (
    CitationLocation,
    FormatCapability,
    FormatResolution,
    IndexJobType,
    MaterialLimits,
    MaterialState,
)

__all__ = [
    "ArchiveImportResult",
    "ArchiveLimits",
    "ArchiveManifest",
    "ArchiveManifestPreview",
    "ArchiveService",
    "CitationLocation",
    "FormatCapability",
    "FormatCapabilityRegistry",
    "FormatResolution",
    "IndexJobType",
    "MaterialLimits",
    "MaterialState",
    "build_default_format_registry",
    "confirm_archive_import",
    "inspect_archive",
]
