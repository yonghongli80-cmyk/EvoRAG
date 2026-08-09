from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chunk:
    text: str
    doc_name: str
    chunk_index: int
    doc_permission_id: int | None = None
    metadata: dict[str, str] = field(default_factory=dict, compare=False, hash=False)
