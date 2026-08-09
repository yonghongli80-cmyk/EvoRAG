from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chunk:
    text: str
    doc_name: str
    chunk_index: int
    doc_permission_id: int | None = None
    metadata: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
    document_id: int | None = None
    document_version_id: int | None = None
    version_no: int | None = None
    is_current: bool = True
    validity_status: str = "valid"
    section_title: str | None = None
    updated_at: float | None = None
    historical_weight: float = 1.0
    retrieval_score: float | None = field(default=None, compare=False)
    rerank_score: float | None = field(default=None, compare=False)
