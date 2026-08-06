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
from rag.materials.embedding_cache import EmbeddingCache
from rag.materials.formats import FormatCapabilityRegistry, build_default_format_registry
from rag.materials.incremental import (
    IncrementalIndexPlan,
    UnitFingerprint,
    plan_incremental_index,
    unit_fingerprint,
)
from rag.materials.models import (
    CitationLocation,
    FormatCapability,
    FormatResolution,
    IndexJobType,
    MaterialLimits,
    MaterialState,
)
from rag.materials.worker import (
    ActivationFailed,
    JobResult,
    WorkerServices,
    process_index_job,
)

__all__ = [
    "ActivationFailed",
    "ArchiveImportResult",
    "ArchiveLimits",
    "ArchiveManifest",
    "ArchiveManifestPreview",
    "ArchiveService",
    "CitationLocation",
    "EmbeddingCache",
    "FormatCapability",
    "FormatCapabilityRegistry",
    "FormatResolution",
    "IncrementalIndexPlan",
    "IndexJobType",
    "JobResult",
    "MaterialLimits",
    "MaterialState",
    "UnitFingerprint",
    "WorkerServices",
    "build_default_format_registry",
    "confirm_archive_import",
    "inspect_archive",
    "plan_incremental_index",
    "process_index_job",
    "unit_fingerprint",
]
