from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    text: str
    doc_name: str
    chunk_index: int
    doc_permission_id: int | None = None
    document_id: int | None = None
    document_version_id: int | None = None
    version_no: int | None = None
    is_current: bool = True
    validity_status: str = "valid"
    section_title: str | None = None
    updated_at: float | None = None
    historical_weight: float = 1.0
