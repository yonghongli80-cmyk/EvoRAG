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
from rag.materials.locator import MaterialLocator, material_result_response
from rag.materials.models import (
    CitationLocation,
    FormatCapability,
    FormatResolution,
    IndexJobType,
    MaterialLimits,
    MaterialState,
)
from rag.materials.organizer import (
    OrganizationBatchResult,
    OrganizationInstruction,
    OrganizationPreview,
    OrganizationService,
)
from rag.materials.recycle import (
    PurgeResult,
    RecycleError,
    RecycleRecord,
    RestoreResult,
    delete_to_recycle,
    purge_document,
    restore_from_recycle,
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
    "MaterialLocator",
    "MaterialState",
    "OrganizationBatchResult",
    "OrganizationInstruction",
    "OrganizationPreview",
    "OrganizationService",
    "PurgeResult",
    "RecycleError",
    "RecycleRecord",
    "RestoreResult",
    "UnitFingerprint",
    "WorkerServices",
    "build_default_format_registry",
    "confirm_archive_import",
    "delete_to_recycle",
    "inspect_archive",
    "material_result_response",
    "plan_incremental_index",
    "process_index_job",
    "purge_document",
    "restore_from_recycle",
    "unit_fingerprint",
]
