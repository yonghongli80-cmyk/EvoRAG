"""Deterministic unit fingerprints and incremental index planning."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rag.materials.models import ExtractedMaterial


def normalize_unit_text(text: str) -> str:
    """Collapse whitespace so equivalent units fingerprint identically."""
    return " ".join(text.split())


def unit_fingerprint(text: str, parser_schema_version: str) -> str:
    """Stable fingerprint binding parser schema version to normalized text."""
    normalized = normalize_unit_text(text)
    return hashlib.sha256(
        f"{parser_schema_version}\0{normalized}".encode()
    ).hexdigest()


@dataclass(frozen=True)
class UnitFingerprint:
    unit_key: str
    digest: str
    unchanged_from_version_id: int | None = None


def fingerprint_material(
    material: ExtractedMaterial, parser_schema_version: str
) -> tuple[UnitFingerprint, ...]:
    return tuple(
        UnitFingerprint(unit_key=unit.unit_key, digest=unit_fingerprint(unit.text, parser_schema_version))
        for unit in material.units
    )


@dataclass(frozen=True)
class IncrementalIndexPlan:
    previous_version_id: int | None
    unchanged: tuple[UnitFingerprint, ...]
    changed: tuple[UnitFingerprint, ...]

    @property
    def reused_units(self) -> int:
        return len(self.unchanged)

    @property
    def embedded_units(self) -> int:
        return len(self.changed)


def plan_incremental_index(
    previous: ExtractedMaterial | None,
    current: ExtractedMaterial,
    parser_schema_version: str,
) -> IncrementalIndexPlan:
    """Compare unit digests between two extractions of the same document."""
    previous_fps = (
        fingerprint_material(previous, parser_schema_version) if previous is not None else ()
    )
    current_fps = fingerprint_material(current, parser_schema_version)
    previous_digests = {fp.unit_key: fp.digest for fp in previous_fps}
    unchanged = tuple(
        fp for fp in current_fps if previous_digests.get(fp.unit_key) == fp.digest
    )
    changed = tuple(
        fp for fp in current_fps if previous_digests.get(fp.unit_key) != fp.digest
    )
    previous_version_id = previous.version_id if previous is not None else None
    return IncrementalIndexPlan(
        previous_version_id=previous_version_id,
        unchanged=unchanged,
        changed=changed,
    )
