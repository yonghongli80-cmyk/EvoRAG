"""Live, read-only access to names owned by the legacy pipeline facade."""

from collections.abc import Iterator, Mapping
from typing import Any


class PipelineBindings(Mapping[str, Any]):
    """Read facade names on demand so patches remain effective after construction."""

    def __init__(self, namespace: Mapping[str, Any]):
        self._namespace = namespace

    def __getitem__(self, name: str) -> Any:
        return self._namespace[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._namespace)

    def __len__(self) -> int:
        return len(self._namespace)
